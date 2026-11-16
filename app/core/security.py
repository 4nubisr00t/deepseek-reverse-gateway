"""
ds-gateway 安全与鉴权模块
- API Key 校验 (SHA-256 存储, 兼容 Bearer sk-* 和 x-api-key)
- Key 身份与账号归属 (T2)
- Admin Session Cookie 校验 (防暴力破解，HttpOnly)
- Basic Auth 兜底 (CLI 自动化脚本使用)

────────────────────────────────────────────────────────────────────
T2 改造说明（身份与账号归属）
────────────────────────────────────────────────────────────────────
1. **身份可传递**
   旧实现 verify_api_key 只回答"能不能进"（返回 bool），身份在鉴权后即丢失，
   路由层无法知道"这个请求是谁发的"，因此账号调度只能是全局共享池。
   现在 verify_api_key 命中后把 key 记录挂到 request.state，
   路由层用 get_identity 依赖取用 → 账号调度可据此做归属隔离。

2. **账号归属的唯一数据模型**
   一条 key 记录里的 `account_labels` 就是它**独占**的账号列表。
   没出现在任何 key 下的账号 = 公共池。
   "一人一号 + 剩余号留池等你分配" 不需要额外的表或目录，就是这一条规则。

3. **消除每请求磁盘 I/O**
   旧实现 verify_api_key 每次命中都会：
       load_keys()   → 读整个 12KB api_keys.json 并 json.loads
       save_keys()   → 再把整个文件重写一遍（只为更新 last_used_at）
   而这两步都在**全局 RLock** 里做同步磁盘操作 —— 并发越高越互相排队，
   50 路并发时全部请求在鉴权处串行化。
   这与 T12 中"每次释放账号都读配置导致金丝雀 429"是同一类缺陷：
   把维护性 I/O 放进了热路径。
   现在：按 (mtime, size) 缓存，文件没变就不重读；last_used_at 只改内存，
   落盘节流 30s，并由 flush_pending() 在关机时补一次。
"""

import atexit
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import PROJECT_ROOT, config_manager
from app.core.request_context import set_current_identity

# 与 config.json / bindings.json / metrics.json 统一：默认跟随仓库根，可用环境变量覆盖。
# 覆盖点不是"可移植性"需要（PROJECT_ROOT 本来就是相对推导的），而是**测试隔离**需要：
# tests/conftest.py 把各存储重定向到临时目录，避免测试写坏真实文件。这个文件尤其要紧 ——
# 2026-09-11 的 atexit 全量 flush 事故就是把它从 45 条记录清成 0 的。
KEYS_FILE = Path(os.environ.get("DSG_KEYS_FILE", str(PROJECT_ROOT / "api_keys.json")))

# 挂在 request.state 上的身份属性名（路由层不直接拼字符串，统一走 get_identity）
IDENTITY_ATTR = "ds_api_identity"

# last_used_at 落盘节流：这是纯观测字段，不值得每请求写盘
_FLUSH_INTERVAL = 30.0

# ── API Key 状态（内存缓存 + mtime 失效） ──
_key_lock = threading.RLock()
_keys: List[Dict[str, Any]] = []
_keys_sig: Optional[tuple] = None      # (st_mtime_ns, st_size)；None 表示文件不存在
_loaded: bool = False                  # 是否已把磁盘内容读进内存（写盘的前置条件）

# ── 待落盘的**增量**改动（而不是一个布尔脏标记） ──
# 为什么要记增量：服务运行期间，api_keys.json 可能被外部改动
# （运维脚本 tools/prune_keys.py、手工编辑、将来的自动化）。
# 如果落盘时直接拿内存快照覆盖，这些外部改动会被**静默丢弃**。
# 实测发生过：prune_keys.py 删掉的 46 条记录，在一次 last_used_at 落盘后
# 被整体还原（内存里还是旧的 48 条）。
# 反方向更危险：有人手工在文件里撤销一把泄露的 key，会被服务悄悄"复活"——
# 你以为已经切断了，实际上那把钥照样能用。
# 所以落盘 = 以磁盘当前内容为基线 + 合并我们的增量。
_pending_records: Dict[str, Optional[Dict[str, Any]]] = {}  # id -> 记录副本（None = 删除）
_pending_touch: Dict[str, int] = {}                         # id -> last_used_at（高频、可合并）
_last_flush: float = 0.0


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_sig() -> Optional[tuple]:
    try:
        st = KEYS_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """补全新增字段的默认值 —— 让 42 条历史记录无需迁移即可继续工作。

    - owner:          归属人（人类可读）。空 = 未指定，日志里回退到 key 名。
    - account_labels: 独占账号列表。**空 = 走公共池（旧行为）**。
                      这个默认值是刻意的：升级后不改变任何现有行为，
                      归属隔离只在你于管理页真正分配账号之后才对该 key 生效。
    - enabled:        可逆暂停（区别于 revoked 的不可逆作废）。
    - pool_fallback:  自有账号满载时，是否允许退到公共池。
                      默认 False —— 严格"一人一号"，自己满了就 429，
                      不静默占用别人还没被分配的号。
    - quota_daily_*:  每日配额（T25）。**0 = 不限**，也是历史记录的默认值，
                      所以升级后不会有人突然被限住。
    """
    if "user_id" not in rec:
        rec["user_id"] = None
    if "account_labels" not in rec or not isinstance(rec.get("account_labels"), list):
        rec["account_labels"] = list(rec.get("account_labels") or []) if isinstance(rec.get("account_labels"), (list, tuple)) else []
    if "owner" not in rec or rec.get("owner") is None:
        rec["owner"] = ""
    if "key_type" not in rec or not rec.get("key_type"):
        rec["key_type"] = "card" if "卡" in str(rec.get("name", "") + rec.get("owner", "")) else ("user" if rec.get("user_id") else "favor")
    if "expires_at" not in rec:
        rec["expires_at"] = None
    if "card_category" not in rec:
        rec["card_category"] = None
    if "enabled" not in rec:
        rec["enabled"] = True
    if "pool_fallback" not in rec:
        rec["pool_fallback"] = False
    if "sensitive_filter" not in rec:
        rec["sensitive_filter"] = True
    for qf in ("quota_daily_requests", "quota_daily_tokens"):
        try:
            rec[qf] = max(0, int(rec.get(qf) or 0))
        except (TypeError, ValueError):
            rec[qf] = 0
    return rec



def _read_keys_locked():
    """从磁盘或 SQLite 重读（调用方必须已持锁）。**不清空待落盘增量。**"""
    global _keys, _keys_sig, _loaded
    sig = _file_sig()
    if KEYS_FILE.exists():
        try:
            raw = json.loads(KEYS_FILE.read_text("utf-8"))
            _keys = [_normalize(r) for r in raw if isinstance(r, dict)]
            try:
                from app.db.repositories import api_key_repo
                api_key_repo.save_batch(_keys)
            except Exception:
                pass
        except Exception as e:
            print(f"[Security] 读取 api_keys.json 失败: {e}")
            _keys = []
    else:
        # 单轨模式：KEYS_FILE 不存在时，纯读 SQLite 数据库
        try:
            from app.db.repositories import api_key_repo
            raw_db = api_key_repo.list_all()
            _keys = [_normalize(r) for r in raw_db if isinstance(r, dict)]
        except Exception as e:
            print(f"[Security] 读取 SQLite api_keys 异常: {e}")
            _keys = []

    _keys_sig = sig
    _loaded = True


def _mark_record(rec: Optional[Dict[str, Any]], key_id: str = ""):
    """登记一条结构性改动（调用方必须已持锁）。rec=None 表示删除。

    存**副本**而不是引用：落盘时要先重读磁盘基线，那会把内存对象换掉，
    引用旧对象就丢了改动。
    """
    kid = key_id or (rec or {}).get("id") or ""
    if kid:
        _pending_records[kid] = dict(rec) if rec is not None else None


def _mark_touched(key_id: str, ts: int):
    if key_id:
        _pending_touch[key_id] = ts


def has_pending() -> bool:
    with _key_lock:
        return bool(_pending_records or _pending_touch)


def _write_keys_locked():
    """把待落盘增量**合并**写入 SQLite 单轨数据库与可选镜像文件（调用方必须已持锁）"""
    global _keys, _keys_sig, _last_flush, _loaded, _pending_records, _pending_touch

    if not _loaded:
        _read_keys_locked()

    # 以磁盘当前内容为基线
    if _file_sig() != _keys_sig and KEYS_FILE.exists():
        _read_keys_locked()

    by_id: Dict[str, Dict[str, Any]] = {
        r.get("id"): r for r in _keys if isinstance(r, dict) and r.get("id")
    }
    for kid, rec in _pending_records.items():
        if rec is None:
            by_id.pop(kid, None)
        else:
            by_id[kid] = rec
    for kid, ts in _pending_touch.items():
        tgt = by_id.get(kid)
        if tgt is not None:
            tgt["last_used_at"] = ts

    _keys = list(by_id.values())

    # 1. 核心单轨持久化：强写入 SQLite
    try:
        from app.db.repositories import api_key_repo
        for kid, rec in _pending_records.items():
            if rec is None:
                api_key_repo.delete(kid)
            else:
                api_key_repo.save(rec)
        for kid, ts in _pending_touch.items():
            api_key_repo.touch_last_used(kid, ts)
    except Exception as e:
        print(f"[Security] 写入 SQLite api_keys 异常: {e}")

    # 2. 软同步/镜像：若配置路径有效则同步一份快照（便于运维排查，失败不影响主服务）
    try:
        tmp = KEYS_FILE.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_keys, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(KEYS_FILE)
    except Exception:
        pass

    # 同步刷新签名：否则下一次 _get_keys 会把自己刚写的文件当成"外部改动"再读一遍
    _keys_sig = _file_sig()
    _last_flush = time.time()
    _pending_records = {}
    _pending_touch = {}
    _loaded = True


def _get_keys() -> List[Dict[str, Any]]:
    """取内存缓存；仅在文件签名变化（外部改动）时重读。**不主动写盘。**"""
    global _loaded
    with _key_lock:
        if _file_sig() != _keys_sig:
            # 文件被外部改过（手工编辑 / 运维脚本）。若内存还有未落盘的增量，
            # 必须先合并落盘再重读，否则这次外部改动会被吃掉。
            if _pending_records or _pending_touch:
                _write_keys_locked()
            else:
                _read_keys_locked()
        else:
            # 签名一致 = 内存与磁盘已经对齐。
            # 这条分支也覆盖"文件从头到尾就不存在"（sig 与 _keys_sig 同为 None）——
            # 此时内存里的空列表**就是**磁盘的真实内容，不是"还没读"。
            _loaded = True
        return _keys


def _maybe_flush(force: bool = False):
    """节流落盘。默认 30s 一次；force=True 立刻写（增删改 key 时必须立即持久化）。

    ⚠️ 这里**没有**"无条件写"这条路：没有登记的增量就绝不写盘，force 也只跳过节流。
    早期版本把 force 实现成"跳过脏标记判断直接写"，于是只 import 过、
    从没读过文件的进程在退出时（atexit → flush_pending）会用内存里的空列表
    把整个 api_keys.json 清成 0 条 —— 45 条记录连同正在服役的 key 一起消失，
    而 key 明文不存盘、只有哈希，丢了就找不回来。
    """
    global _last_flush
    with _key_lock:
        if not _pending_records and not _pending_touch:
            return
        now = time.time()
        if not force and (now - _last_flush) < _FLUSH_INTERVAL:
            return
        _write_keys_locked()


def flush_pending():
    """关机/重启前补一次落盘（由 main.py lifespan 调用）

    没有任何登记的改动时直接返回 —— 这种进程内存里没有值得写回的东西，
    写盘只可能造成破坏（见 _write_keys_locked 里记录的真实事故）。
    """
    try:
        if not has_pending():
            return
        _maybe_flush(force=True)
    except Exception as e:
        print(f"[Security] 落盘 last_used_at 失败（已忽略）: {e}")


atexit.register(flush_pending)


# 兼容旧接口：显式从磁盘重读 / 显式落盘
def load_keys():
    with _key_lock:
        _read_keys_locked()


def save_keys():
    """立刻落盘（没有登记的改动时为空操作）"""
    _maybe_flush(force=True)


def ensure_default_key():
    """无有效 key 时自动生成 default key 并打印在终端"""
    with _key_lock:
        if any(not k.get("revoked") for k in _get_keys()):
            return
    rec = generate_key("default")
    print("\n" + "=" * 60)
    print("[Gateway] 未配置任何 API Key，已自动生成 default key:")
    print(f"  {rec['key']}")
    print("  请保存此 key 用于客户端鉴权（Authorization: Bearer <key>）。")
    print("=" * 60 + "\n")


def generate_key(
    name: str = "default",
    owner: str = "",
    account_labels: Optional[List[str]] = None,
    sensitive_filter: bool = True,
    user_id: Optional[str] = None,
    quota_daily_requests: int = 0,
    quota_daily_tokens: int = 0,
    key_type: str = "normal",
    expires_at: Optional[int] = None,
    card_category: Optional[str] = None,
) -> Dict[str, Any]:
    with _key_lock:
        raw = "sk-" + secrets.token_hex(24)
        rec = _normalize({
            "id": secrets.token_hex(6),
            "user_id": user_id,
            "name": (name or "default").strip()[:40],
            "owner": (owner or "").strip()[:40],
            "account_labels": list(account_labels or []),
            "key_type": key_type,
            "expires_at": int(expires_at) if expires_at is not None else None,
            "card_category": card_category,
            "sensitive_filter": bool(sensitive_filter),
            "quota_daily_requests": max(0, int(quota_daily_requests or 0)),
            "quota_daily_tokens": max(0, int(quota_daily_tokens or 0)),
            "key_hash": _hash_key(raw),
            "prefix": raw[:15] + "…",
            "created_at": int(time.time()),
            "revoked": False,
            "revoked_at": None,
            "last_used_at": None,
        })
        _get_keys().append(rec)
        _mark_record(rec)
        _maybe_flush(force=True)
        return {**rec, "key": raw}


def list_keys_by_user(user_id: str) -> List[Dict[str, Any]]:
    with _key_lock:
        return [_public(k) for k in _get_keys() if k.get("user_id") == user_id]


def update_key_by_user(
    user_id: str,
    key_id: str,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> bool:
    with _key_lock:
        for k in _get_keys():
            if k.get("id") == key_id and k.get("user_id") == user_id and not k.get("revoked"):
                if name is not None:
                    k["name"] = name.strip()[:40]
                if enabled is not None:
                    k["enabled"] = bool(enabled)
                _mark_record(k)
                _maybe_flush(force=True)
                return True
    return False


def set_key_quota_by_user(
    user_id: str,
    key_id: str,
    quota_daily_requests: int,
    quota_daily_tokens: int,
) -> bool:
    """给用户自己的 key 设置每日配额（订阅开通时同步用）。"""
    with _key_lock:
        for k in _get_keys():
            if k.get("id") == key_id and k.get("user_id") == user_id and not k.get("revoked"):
                k["quota_daily_requests"] = max(0, int(quota_daily_requests or 0))
                k["quota_daily_tokens"] = max(0, int(quota_daily_tokens or 0))
                _mark_record(k)
                _maybe_flush(force=True)
                return True
    return False


def revoke_key_by_user(user_id: str, key_id: str) -> bool:
    with _key_lock:
        for k in _get_keys():
            if k.get("id") == key_id and k.get("user_id") == user_id and not k.get("revoked"):
                return revoke_key(key_id)
    return False



def _public(rec: Dict[str, Any]) -> Dict[str, Any]:
    """对外视图：剔除 key_hash，聚合用户个人号池与等级特权（绝不外泄缓存内的活引用）"""
    out = {k: v for k, v in rec.items() if k != "key_hash"}
    user_id = rec.get("user_id")
    raw_labels = list(rec.get("account_labels") or [])

    if user_id:
        out["is_user_key"] = True
        out["user_id"] = user_id
        try:
            from app.db.repositories import user_repo
            user = user_repo.get_by_id(user_id)
            user_pool = user_repo.get_personal_pool(user_id)
            # 核心业务逻辑：名下所有 Key 共享用户个人池
            merged = list(dict.fromkeys(raw_labels + user_pool))
            out["account_labels"] = merged
            out["personal_pool"] = user_pool
            tier = user.get("tier", "free") if user else "free"
            out["user_tier"] = tier
            out["is_vip"] = bool(tier in ("standard", "pro", "ultra"))
            out["auto_replenish"] = bool(user.get("auto_replenish", 1)) if user else True
        except Exception:
            out["account_labels"] = raw_labels
            out["is_vip"] = False
            out["auto_replenish"] = True
    else:
        # 无 user_id：归纳为孤单人情/散客 Key
        out["is_user_key"] = False
        out["is_vip"] = False
        out["account_labels"] = raw_labels
        out["personal_pool"] = []
        out["auto_replenish"] = False

    out["owner"] = rec.get("owner") or ""
    out["key_type"] = rec.get("key_type") or ("card" if "卡" in str(rec.get("name", "") + rec.get("owner", "")) else ("user" if user_id else "favor"))
    out["expires_at"] = rec.get("expires_at")
    out["card_category"] = rec.get("card_category")
    out["enabled"] = bool(rec.get("enabled", True))
    out["pool_fallback"] = bool(rec.get("pool_fallback", False))
    out["sensitive_filter"] = bool(rec.get("sensitive_filter", True))
    # 全面切换至完整五要素受信任账号后，不再将免过滤 Key 强绑到 disposable 耗材池
    out["prefer_disposable"] = False
    out["strict_owned"] = (
        not out["pool_fallback"]
        and (
            bool(user_id)
            or bool(raw_labels)
            or bool(rec.get("isolated_empty"))
        )
    )
    out["log_label"] = out["owner"] or out["name"] or "anonymous"
    out["quota_daily_requests"] = int(rec.get("quota_daily_requests") or 0)
    out["quota_daily_tokens"] = int(rec.get("quota_daily_tokens") or 0)
    out["key_id"] = rec.get("id", "")
    return out


def get_identity_snapshot(identity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """获取最新的 Key 身份与归属快照（供 Failover 容灾重选时绕过请求入口陈旧快照）。"""
    if not isinstance(identity, dict):
        return {}
    key_id = identity.get("key_id")
    if not key_id:
        return identity
    with _key_lock:
        for rec in _get_keys():
            if rec.get("id") == key_id:
                snap = _public(rec)
                snap["key_name"] = snap.get("name") or identity.get("key_name") or snap.get("log_label")
                return snap
    return identity


def list_keys() -> List[Dict[str, Any]]:
    # 注意：这里不落盘也不重读 —— 内存里的 last_used_at 比磁盘新，
    # 管理页要看到的就是最新值。
    with _key_lock:
        return [_public(item) for item in _get_keys()]


def revoke_key(key_id: str) -> bool:
    with _key_lock:
        for k in _get_keys():
            if k.get("id") == key_id and not k.get("revoked"):
                k["revoked"] = True
                k["revoked_at"] = int(time.time())
                k["enabled"] = False
                # 作废即释放其名下账号，号自动回到公共池
                k["account_labels"] = []
                _mark_record(k)
                _maybe_flush(force=True)
                # 用量记录**保留**（只标记，不删）。
                # 反过来想过：撤销一个 key 往往正是因为"他把号跑飞了"，
                # 而那一刻恰恰是最需要历史证据的时候 —— 删掉等于在需要证据的
                # 瞬间销毁证据。用量表按 key_id 存，撤销的 key 无法通过鉴权，
                # 所以不会影响配额判断；管理页的表格只列未撤销的 key，
                # 也不会被这些历史记录弄乱。
                # 真要清理时走 POST /api/usage/prune。
                try:
                    from app.engine.key_usage import key_usage_engine
                    key_usage_engine.mark_revoked(key_id)
                except Exception as e:
                    print(f"[Security] 标记 {key_id} 的用量记录失败（已忽略）: {e}")
                return True
    return False


def delete_key(key_id: str) -> bool:
    """彻底物理删除单条 Key（包含内存、SQLite与快照文件）"""
    with _key_lock:
        global _keys
        initial_len = len(_get_keys())
        _keys = [k for k in _get_keys() if k.get("id") != key_id]
        if len(_keys) < initial_len:
            _mark_record(None, key_id=key_id)
            _maybe_flush(force=True)
            try:
                from app.db.repositories import api_key_repo
                api_key_repo.delete(key_id)
            except Exception:
                pass
            return True
        return False


def purge_revoked_keys() -> int:
    """彻底物理删除所有已作废或已禁用的 Key"""
    with _key_lock:
        global _keys
        current = _get_keys()
        to_del = [k.get("id") for k in current if k.get("revoked") or not k.get("enabled", True)]
        for kid in to_del:
            if kid:
                _mark_record(None, key_id=kid)
        _keys = [k for k in current if not (k.get("revoked") or not k.get("enabled", True))]
        _maybe_flush(force=True)
        try:
            from app.db.repositories import api_key_repo
            api_key_repo.purge_revoked()
        except Exception:
            pass
        return len(to_del)


# ══════════════════════════════════════════════════════════════════════
# T2: 账号归属
# ══════════════════════════════════════════════════════════════════════

def account_owner_map() -> Dict[str, Dict[str, str]]:
    """account_label -> 拥有它的 key 或所属用户个人池。

    这是账号池做物理隔离与特权调度的核心依据：
      · 绑定在 Key 上或用户个人池上 → 专属个人独占，只有该 Key 或该用户名下的所有 Key 能用
      · 未出现在这里 → 自由公共池 / 人情池
    """
    with _key_lock:
        out: Dict[str, Dict[str, str]] = {}
        for rec in _get_keys():
            if rec.get("revoked") or not rec.get("enabled", True):
                continue
            for lbl in rec.get("account_labels") or []:
                lbl = str(lbl or "").strip()
                if lbl and lbl not in out:
                    out[lbl] = {
                        "key_id": rec.get("id", ""),
                        "key_name": rec.get("name", ""),
                        "owner": rec.get("owner") or rec.get("name") or "",
                        "user_id": rec.get("user_id") or "",
                    }
        try:
            from app.db.repositories import user_repo
            users = user_repo.list_all()
            for u in users:
                u_pool = user_repo.get_personal_pool(u["id"])
                for lbl in u_pool:
                    lbl = str(lbl or "").strip()
                    if lbl and lbl not in out:
                        out[lbl] = {
                            "key_id": "",
                            "key_name": f"用户个人池 ({u.get('email')})",
                            "owner": u.get("email") or "",
                            "user_id": u["id"],
                        }
        except Exception:
            pass
        return out


def get_key(key_id: str) -> Optional[Dict[str, Any]]:
    with _key_lock:
        for rec in _get_keys():
            if rec.get("id") == key_id:
                return _public(rec)
    return None


def set_key_accounts(key_id: str, labels: List[str]) -> Dict[str, Any]:
    """把一组账号分配给某个 key（覆盖式）。

    不变量：**一个账号最多属于一个 key。** 若目标账号已被别的 key 占用，
    直接拒绝而不是静默抢占 —— 静默抢号会让另一个用户在自己毫不知情的情况下
    突然开始拿到 429，这种故障极难归因。

    传空列表 = 该 key 回到公共池模式。
    分配成功后返回 {"key": <key视图>, "released": [...], "assigned": [...]}
    """
    want = []
    for lbl in labels or []:
        lbl = str(lbl or "").strip()
        if lbl and lbl not in want:
            want.append(lbl)

    with _key_lock:
        records = _get_keys()
        target = None
        for rec in records:
            if rec.get("id") == key_id:
                target = rec
                break
        if target is None:
            raise KeyError(f"key 不存在: {key_id}")
        if target.get("revoked"):
            raise ValueError("该 key 已作废，无法分配账号")

        # 冲突检查：跳过自己，查其他 key
        conflicts = []
        for rec in records:
            if rec.get("id") == key_id or rec.get("revoked"):
                continue
            owned = set(str(x or "").strip() for x in (rec.get("account_labels") or []))
            for lbl in want:
                if lbl in owned:
                    conflicts.append(
                        f"{lbl} 已被 key「{rec.get('owner') or rec.get('name')}」占用"
                    )
        if conflicts:
            raise ValueError("账号归属冲突：" + "；".join(conflicts))

        before = list(str(x or "").strip() for x in (target.get("account_labels") or []))
        target["account_labels"] = want
        if want:
            target["isolated_empty"] = False
        _mark_record(target)
        _maybe_flush(force=True)
        return {
            "key": _public(target),
            "assigned": [x for x in want if x not in before],
            "released": [x for x in before if x not in want],
        }


# 记录因长效有期禁言 (dated) 被剥离的账号原归属 Key ID，用于解禁康复后优先归还原主（软挂起租约）
_suspended_owner_map: Dict[str, Dict[str, Any]] = {}


def get_suspended_owner(account_label: str) -> Optional[Dict[str, Any]]:
    """查询某个曾因长效封禁被剥离的账号的原归属 Key 信息"""
    with _key_lock:
        info = _suspended_owner_map.get(str(account_label or "").strip())
        return dict(info) if info else None


def rebind_rehabilitated_account(account_label: str) -> Optional[str]:
    """当有期禁言账号康复重激活后，若原所属 Key 仍有效且缺额，则自动归还原主；返回归还的 key_id"""
    lbl = str(account_label or "").strip()
    if not lbl:
        return None
    with _key_lock:
        info = _suspended_owner_map.pop(lbl, None)
        if not info:
            return None
        kid = info.get("key_id")
        if not kid:
            return None
        # 确保该号当前未被其他 key 占用
        if lbl in account_owner_map():
            return None
        for rec in _get_keys():
            if rec.get("id") == kid and not rec.get("revoked") and rec.get("enabled", True):
                curr = list(rec.get("account_labels") or [])
                if lbl not in curr:
                    curr.append(lbl)
                    rec["account_labels"] = curr
                    _mark_record(rec)
                    _maybe_flush(force=True)
                    print(f"[Security] 🔄 康复账号 {lbl} 已按软挂起契约归还原专属 Key「{rec.get('owner') or rec.get('name')}」", flush=True)
                    return kid
    return None


def disown_account(account_label: str) -> bool:
    """从所有 key 的 account_labels 及用户个人池中彻底剔除某个账号（封号/失效隔离时使用），并登记软挂起溯源快照。"""
    lbl = str(account_label or "").strip()
    if not lbl:
        return False
    changed = False
    with _key_lock:
        for rec in _get_keys():
            labels = rec.get("account_labels") or []
            if lbl in labels:
                _suspended_owner_map[lbl] = {
                    "key_id": rec.get("id", ""),
                    "key_name": rec.get("name", ""),
                    "owner": rec.get("owner") or rec.get("name") or "",
                    "disowned_at": int(time.time()),
                }
                rec["account_labels"] = [x for x in labels if str(x).strip() != lbl]
                if not rec["account_labels"] and not rec.get("pool_fallback", False):
                    rec["isolated_empty"] = True
                _mark_record(rec)
                changed = True
                print(f"[Security] 账号 {lbl} 已从 key「{rec.get('owner') or rec.get('name')}」解除归属绑定（已保留原主溯源快照）", flush=True)
        if changed:
            _maybe_flush(force=True)
    # 同步清理用户个人池 (users.account_labels)，防止 _public() 重新合回失效账号
    try:
        from app.db.repositories import user_repo
        for u in user_repo.list_all():
            u_pool = user_repo.get_personal_pool(u["id"])
            if lbl in u_pool:
                user_repo.remove_from_personal_pool(u["id"], lbl)
                changed = True
    except Exception:
        pass
    return changed


def sanitize_key_bindings() -> int:
    """全面体检并清洗所有 API Key 的账号绑定：
    若绑定的账号属于隔离垃圾库（QUARANTINE、QUARANTINE_MUTED、QUARANTINE_INVALID、INVALID、BANNED）、
    或者已被删除不存在于账号表中的僵尸号，立即自动剥离解绑并记录溯源快照，严禁僵尸号死锁用户配额！
    """
    try:
        from app.config import config_manager
        accs = config_manager.get_accounts()
        # ⚠️ 冷库(INACTIVE_COLD)/待命(STANDBY)/预热(WARMING) 是**正常储备态**，不是僵尸号：
        #    它们天然 is_valid=False 且 mute_until=None，早期用「not is_valid and not mute_until」
        #    一刀切，导致刚绑定到 key 的冷库号在下次读 /api/keys 时被 sanitize 悄悄剔除，
        #    表现为「后台绑定成功、刷新后绑定消失」。
        _RESERVE_STATUS = {"INACTIVE_COLD", "STANDBY", "WARMING"}
        dead_labels = set()
        for a in accs:
            lbl = a.get("account_label")
            if not lbl:
                continue
            st = a.get("status")
            if st in ("QUARANTINE", "QUARANTINE_MUTED", "QUARANTINE_INVALID", "INVALID", "BANNED", "MFA_REQUIRED"):
                dead_labels.add(lbl)
            elif (not a.get("is_valid")) and (not a.get("mute_until")) and st not in _RESERVE_STATUS:
                dead_labels.add(lbl)
        all_existing = {a.get("account_label") for a in accs if a.get("account_label")}
    except Exception:
        dead_labels = set()
        all_existing = set()

    cleaned_count = 0
    with _key_lock:
        for rec in _get_keys():
            labels = rec.get("account_labels") or []
            new_labels = []
            for lbl in labels:
                lbl_clean = str(lbl).strip()
                # 僵尸号判定：属于垃圾隔离库，或在 accounts 表中根本不存在
                if lbl_clean in dead_labels or (all_existing and lbl_clean not in all_existing):
                    _suspended_owner_map[lbl_clean] = {
                        "key_id": rec.get("id", ""),
                        "key_name": rec.get("name", ""),
                        "owner": rec.get("owner") or rec.get("name") or "",
                        "disowned_at": int(time.time()),
                        "reason": "zombie_quarantine_cleanup",
                    }
                    cleaned_count += 1
                    print(
                        f"[Security] 🧟 自动清理僵尸号绑定: 账号 {lbl_clean} (状态已进入垃圾池/失效) 已从 Key「{rec.get('name')}」解绑",
                        flush=True,
                    )
                else:
                    new_labels.append(lbl_clean)

            if len(new_labels) != len(labels):
                rec["account_labels"] = new_labels
                _mark_record(rec)
        if cleaned_count > 0:
            _maybe_flush(force=True)
    return cleaned_count


def update_key(
    key_id: str,
    owner: Optional[str] = None,
    enabled: Optional[bool] = None,
    pool_fallback: Optional[bool] = None,
    sensitive_filter: Optional[bool] = None,
    quota_daily_requests: Optional[int] = None,
    quota_daily_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    with _key_lock:
        for rec in _get_keys():
            if rec.get("id") == key_id:
                if owner is not None:
                    rec["owner"] = str(owner).strip()[:40]
                if enabled is not None:
                    rec["enabled"] = bool(enabled)
                if pool_fallback is not None:
                    rec["pool_fallback"] = bool(pool_fallback)
                if sensitive_filter is not None:
                    rec["sensitive_filter"] = bool(sensitive_filter)
                if quota_daily_requests is not None:
                    rec["quota_daily_requests"] = max(0, int(quota_daily_requests))
                if quota_daily_tokens is not None:
                    rec["quota_daily_tokens"] = max(0, int(quota_daily_tokens))
                _mark_record(rec)
                _maybe_flush(force=True)
                return _public(rec)
    raise KeyError(f"key 不存在: {key_id}")


# ══════════════════════════════════════════════════════════════════════
# T2: 身份解析
# ══════════════════════════════════════════════════════════════════════

def _anonymous_identity() -> Dict[str, Any]:
    """require_api_key=false 时的合成身份：没有归属、没有独占账号、走公共池。

    刻意不叫 "admin"：网关不鉴权时任何人都是匿名访客，
    给它管理员身份会导致后续按身份分配资源的逻辑出现提权。
    """
    return {
        "key_id": "",
        "name": "anonymous",
        "owner": "",
        "account_labels": [],
        "enabled": True,
        "pool_fallback": True,
        "sensitive_filter": True,
        "quota_daily_requests": 0,
        "quota_daily_tokens": 0,
        "log_label": "anonymous",
        "authenticated": False,
    }


def _extract_raw_token(request: Request) -> str:
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        return auth_hdr[7:].strip()
    if "x-api-key" in request.headers:
        return request.headers["x-api-key"].strip()
    return ""


def _match_identity(request: Request) -> Optional[Dict[str, Any]]:
    """命中返回身份视图（含 authenticated=True），未命中返回 None。

    顺带更新 last_used_at —— 这是高频的观测字段，登记成**可合并的 touch 增量**
    （只改内存 + 30s 节流落盘），而不是结构性改动，避免每请求写盘。
    """
    raw_token = _extract_raw_token(request)
    if not raw_token:
        return None

    incoming_hash = _hash_key(raw_token)
    with _key_lock:
        for k in _get_keys():
            if k.get("revoked") or not k.get("enabled", True):
                continue
            if hmac.compare_digest(k.get("key_hash", ""), incoming_hash):
                exp = k.get("expires_at")
                if exp and int(exp) > 0 and time.time() > float(exp):
                    k["revoked"] = True
                    k["revoked_at"] = int(time.time())
                    k["enabled"] = False
                    k["account_labels"] = []
                    _mark_record(k)
                    _maybe_flush(force=True)
                    print(f"[Gateway-Auth] 401: Key {k.get('id')} 已到期({exp})，已自动解绑回收并失效", flush=True)
                    return None
                ts = int(time.time())
                k["last_used_at"] = ts
                _mark_touched(k.get("id", ""), ts)
                _maybe_flush()
                ident = _public(k)
                ident["authenticated"] = True
                return ident
    return None


def _explain_key_miss(raw_token: str) -> str:
    """给 401 一个**能自我解释**的原因（只读，绝不参与判定）。

    为什么值得单写一个函数：线上排查"API key 无效"这件事，成本几乎全花在
    "到底是谁发了哪把 key、为什么被拒"上 —— access log 只有 IP，没有任何线索，
    只能靠时间线去猜（2026-09-12 就是这么查了一轮）。

    而两种失败的性质完全不同、处置也完全不同：

      · **表里根本没有这把 key** —— 客户端揣着旧明文（明文只在创建时给一次，
        服务端只存 sha256 与前缀，**丢了就找不回来**），只能新建一把
      · **有这把 key 但已作废/停用** —— 有人点了按钮，去管理页看就知道

    只打前缀（15 位，与管理页显示的口径一致），**绝不打完整 key**。
    """
    if not raw_token:
        return "未携带 key（Authorization: Bearer sk-... 或 x-api-key 都没有）"
    incoming_hash = _hash_key(raw_token)
    with _key_lock:
        for k in _get_keys():
            if hmac.compare_digest(k.get("key_hash", ""), incoming_hash):
                if k.get("revoked"):
                    return "该 key 已被作废(revoked)"
                if not k.get("enabled", True):
                    return "该 key 已被停用(enabled=false)"
                return "已登记且有效（不该走到这里，请报告）"
    return ("表里没有这把 key：旧明文 / 从未创建 / 创建后明文丢失"
            "（明文只在创建时给一次，丢了只能新建）")


async def verify_api_key(request: Request) -> bool:
    """FastAPI 依赖：只负责"能否放行"，并把身份挂到 request.state。

    路由层不要直接用它的返回值取身份 —— 用 get_identity 依赖，
    这样即使将来鉴权开关变化，取身份的路径也保持一致。

    ⚠️ **必须是 async def，不能改成同步 def。**
    FastAPI 对**同步**依赖会 `run_in_threadpool` 执行，而线程池是通过
    `contextvars.copy_context()` 拿到上下文副本的 —— 在副本里
    `set_current_identity()` 只会改副本，回不到请求自己的上下文，
    于是 `metrics_engine.record()` 永远读不到身份，按 key 的用量恒为 0。
    这个失败是**静默的**：图表上只是"数字偏小"，不会报任何错。

    注意这里和 get_identity **两个都要 async**：只改一个也能跑通，
    但那样归因就依赖"每条路由都恰好声明了 get_identity"这个脆弱前提，
    将来新增一条只挂 router 级依赖的路由，用量会静默漏统。
    """
    if not config_manager.config.require_api_key:
        ident = _anonymous_identity()
        setattr(request.state, IDENTITY_ATTR, ident)
        set_current_identity(ident)
        return True

    raw = _extract_raw_token(request)
    if not raw:
        _log_auth_reject(request, "", _explain_key_miss(""))
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing API key. Use 'Authorization: Bearer sk-...' or 'x-api-key'.", "type": "auth_error"}},
        )

    ident = _match_identity(request)
    if ident is None:
        _log_auth_reject(request, raw, _explain_key_miss(raw))
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid API key.", "type": "auth_error"}},
        )

    setattr(request.state, IDENTITY_ATTR, ident)
    # 同时写进请求上下文：metrics/key_usage 的调用点有 14 处，
    # 靠上下文归因可以做到"一处不漏"（见 app/core/request_context.py 的说明）
    set_current_identity(ident)
    return True


def _log_auth_reject(request: Request, raw_token: str, reason: str) -> None:
    """打印一行可自证的 401（只含前缀，不含完整 key）。"""
    try:
        prefix = (raw_token[:15] + "…") if raw_token else "(无)"
        path = getattr(getattr(request, "url", None), "path", "?")
        client = getattr(request, "client", None)
        host = getattr(client, "host", "?") if client is not None else "?"
        print(
            f"[Gateway-Auth] 401 拒绝: prefix={prefix} 原因={reason} "
            f"path={path} client={host}",
            flush=True,
        )
    except Exception:
        pass          # 观测失败绝不影响鉴权本身


async def get_identity(request: Request) -> Dict[str, Any]:
    """FastAPI 依赖：取当前请求的身份（永远返回一个可用的 dict）。

    优先级：request.state 上已解析的身份 → 现场解析 → 匿名。
    现场解析这一路是给没走 verify_api_key 的调用方（如测试直调）兜底。

    ⚠️ 同样必须是 async def，原因见 verify_api_key 的说明。
    """
    ident = getattr(request.state, IDENTITY_ATTR, None)
    if isinstance(ident, dict) and ident:
        set_current_identity(ident)
        return ident
    if not config_manager.config.require_api_key:
        ident = _anonymous_identity()
        set_current_identity(ident)
        return ident
    ident = _match_identity(request) or _anonymous_identity()
    set_current_identity(ident)
    return ident


# ── Admin 会话认证 ──
SESSION_TTL = 86400 * 7  # 7 天有效

basic_security = HTTPBasic(auto_error=False)


def create_admin_session(ip: str) -> str:
    """生成带 HMAC 签名的防篡改会话 Token，服务重启不失效"""
    ts = int(time.time())
    msg = f"admin:{ts}"
    secret = config_manager.config.admin_password.encode("utf-8")
    sig = hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def destroy_admin_session(sid: str):
    pass


def verify_admin_session(sid: str) -> bool:
    if not sid or "." not in sid:
        return False
    parts = sid.split(".", 1)
    if len(parts) != 2:
        return False
    ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if time.time() - ts > SESSION_TTL or ts > time.time() + 60:
        return False
    msg = f"admin:{ts}"
    secret = config_manager.config.admin_password.encode("utf-8")
    expected_sig = hmac.new(secret, msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)


def require_admin(request: Request, basic: Optional[HTTPBasicCredentials] = Depends(basic_security)) -> bool:
    """支持 Session Cookie、JWT Token 或 Basic Auth 认证"""
    # 1. 尝试 Cookie session
    sid = request.cookies.get("ds_admin_session")
    if sid and verify_admin_session(sid):
        return True

    # 2. 尝试 JWT Token (Header 或 Cookie)
    token = None
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        token = auth_hdr[7:].strip()
    elif "ds_user_token" in request.cookies:
        token = request.cookies.get("ds_user_token", "").strip()

    if token:
        try:
            from app.core.auth import decode_access_token
            payload = decode_access_token(token)
            if payload and payload.get("role") == "admin":
                return True
        except Exception:
            pass

    # 3. 尝试 Basic Auth (用于 CLI)
    if basic is not None:
        correct_password = config_manager.config.admin_password
        if hmac.compare_digest(basic.username.encode(), b"admin") and \
           hmac.compare_digest(basic.password.encode(), correct_password.encode()):
            return True

    raise HTTPException(status_code=401, detail="未登录或认证过期，请重新登录")



def recycle_expired_keys() -> int:
    """扫描并自动解绑作废已过期的卡密，释放占用的账号回号池"""
    with _key_lock:
        now = time.time()
        expired_count = 0
        for k in _get_keys():
            exp = k.get("expires_at")
            if exp and int(exp) > 0 and now > float(exp) and not k.get("revoked"):
                k["revoked"] = True
                k["revoked_at"] = int(now)
                k["enabled"] = False
                k["account_labels"] = []
                _mark_record(k)
                expired_count += 1
        if expired_count > 0:
            _maybe_flush(force=True)
            print(f"[Security] ♻️ 自动巡检已回收并注销 {expired_count} 张到期卡密", flush=True)
        return expired_count


def move_key_to_pool(key_id: str) -> Dict[str, Any]:
    """将某把 Key（如人情 Key）绑定的账号彻底移入公共卡池，Key 转为使用公共共享池调度"""
    with _key_lock:
        for k in _get_keys():
            if k.get("id") == key_id:
                old_labels = list(k.get("account_labels") or [])
                k["account_labels"] = []
                k["pool_fallback"] = True
                _mark_record(k)
                _maybe_flush(force=True)
                print(f"[Security] ♻️ Key「{k.get('owner') or k.get('name')}」({key_id}) 已解绑 {len(old_labels)} 个独占账号移入卡池库", flush=True)
                return {
                    "ok": True,
                    "key_id": key_id,
                    "released_accounts": old_labels,
                    "key": _public(k)
                }
    raise KeyError(f"Key 不存在: {key_id}")

