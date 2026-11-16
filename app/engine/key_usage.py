"""
按 API Key 的用量统计与日配额 (T25)

**为什么单独一个存储，而不是并进 metrics.json**
全局 metrics.json 只有一组总量计数，没有"日"的概念，也没有 per-key 的维度。
配额需要「今天用了多少」这种会跨零点归零的计数器，
把日滚动逻辑塞进全局统计会把两者都搞乱；分开更清楚，也更容易备份/清空。

数据结构（key_usage.json）：
{
  "<key_id>": {
    "label": "张三",
    "day": "2026-09-10",
    "requests": 12, "prompt_tokens": 34567, "completion_tokens": 1234,
    "errors": 1, "rejected": 0, "duration_ms": 45678.0,
    "total_requests": 900, "total_tokens": 1234567,
    "last_used_at": 1789052894,
    "last_error": "",
    "history": { "2026-09-09": {requests, prompt_tokens, completion_tokens, errors, duration_ms} }
  }
}

**落盘策略**：与 keystore 同样节流（30s）+ 关机补刷。
用量是观测数据，不值得每请求写盘；而 T12 的教训是
"把维护性 I/O 放进热路径"会直接制造 429。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import PROJECT_ROOT

# 与 api_keys.json / config.json / metrics.json 统一：默认跟随仓库根，可用环境变量覆盖。
# 覆盖点的用途是**测试隔离**（tests/conftest.py 把各存储重定向到临时目录），
# 避免测试运行时把真实用量数据写脏或清空。
USAGE_FILE = Path(os.environ.get("DSG_USAGE_FILE", str(PROJECT_ROOT / "key_usage.json")))

# 保留多少天的历史（够看趋势与定位"昨天谁跑飞了"）
HISTORY_DAYS = 14
_FLUSH_INTERVAL = 30.0
_DAY_FIELDS = ("requests", "prompt_tokens", "completion_tokens", "cached_tokens", "errors", "duration_ms")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _seconds_until_tomorrow() -> int:
    """距本地零点还有多少秒（用作 429 的 Retry-After）。"""
    now = time.localtime()
    passed = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    return max(60, 86400 - passed)


class KeyUsageEngine:
    def __init__(self, storage_file: Path = USAGE_FILE):
        self._lock = threading.RLock()
        self.storage_file = storage_file
        self.is_custom_storage = (self.storage_file != USAGE_FILE)
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._loaded = False
        self._last_flush = 0.0
        self._load()

    # ── 持久化 ──

    def _load(self):
        self._loaded = True
        if self.storage_file.exists():
            try:
                data = json.loads(self.storage_file.read_text("utf-8"))
                if isinstance(data, dict):
                    self._keys = {k: v for k, v in data.items() if isinstance(v, dict)}
                return
            except Exception as e:
                print(f"[KeyUsage] 读取 {self.storage_file} 失败: {e}")
                self._keys = {}

        if not self.is_custom_storage:
            db_keys = {}
            try:
                from app.db.repositories import usage_repo
                today = _today()
                all_records = usage_repo.list_all(limit=1000)
                if all_records:
                    for r in all_records:
                        kid = r.get("key_id")
                        r_day = r.get("day")
                        if not kid or not r_day:
                            continue
                        if kid not in db_keys:
                            db_keys[kid] = {"day": r_day, "history": {}}
                        if r_day == today:
                            db_keys[kid].update({
                                "day": r_day,
                                "requests": int(r.get("requests") or 0),
                                "prompt_tokens": int(r.get("prompt_tokens") or 0),
                                "completion_tokens": int(r.get("completion_tokens") or 0),
                                "cached_tokens": int(r.get("cached_tokens") or 0),
                                "errors": int(r.get("errors") or 0),
                                "rejected": int(r.get("rejected") or 0),
                                "duration_ms": float(r.get("duration_ms") or 0.0),
                                "last_used_at": r.get("last_used_at"),
                                "last_error": str(r.get("last_error") or ""),
                            })
                        else:
                            db_keys[kid].setdefault("history", {})[r_day] = {
                                "requests": int(r.get("requests") or 0),
                                "prompt_tokens": int(r.get("prompt_tokens") or 0),
                                "completion_tokens": int(r.get("completion_tokens") or 0),
                                "cached_tokens": int(r.get("cached_tokens") or 0),
                                "errors": int(r.get("errors") or 0),
                                "duration_ms": float(r.get("duration_ms") or 0.0),
                            }
                    if db_keys:
                        self._keys = db_keys
            except Exception as e:
                print(f"[KeyUsage] 从 SQLite 恢复用量记录异常: {e}")

    def _flush_locked(self):
        if not self._loaded:
            self._load()

        # 1. 核心单轨持久化：主服务模式下强写入 SQLite
        if not self.is_custom_storage:
            try:
                from app.db.repositories import usage_repo
                for kid, v in self._keys.items():
                    day = v.get("day")
                    if kid and day:
                        usage_repo.save_day_snapshot(
                            key_id=kid,
                            day=day,
                            requests=int(v.get("requests") or 0),
                            prompt_tokens=int(v.get("prompt_tokens") or 0),
                            completion_tokens=int(v.get("completion_tokens") or 0),
                            cached_tokens=int(v.get("cached_tokens") or 0),
                            errors=int(v.get("errors") or 0),
                            rejected=int(v.get("rejected") or 0),
                            duration_ms=float(v.get("duration_ms") or 0.0),
                            last_used_at=v.get("last_used_at"),
                            last_error=str(v.get("last_error") or ""),
                        )
            except Exception as e:
                print(f"[KeyUsage] 写入 SQLite 失败: {e}")

        self._last_flush = time.time()

        # 2. 软同步/镜像：若配置路径有效则同步一份快照（失败不影响主服务）
        try:
            tmp = self.storage_file.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(self._keys, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.storage_file)
        except Exception:
            pass

    def flush(self, force: bool = False):
        with self._lock:
            if not force and not self._dirty:
                return
            if not force and (time.time() - self._last_flush) < _FLUSH_INTERVAL:
                return
            self._flush_locked()
            self._dirty = False

    # ── 日滚动 ──

    def _record_locked(self, key_id: str) -> Dict[str, Any]:
        rec = self._keys.get(key_id)
        if rec is None:
            rec = {"day": _today(), "history": {}}
            self._keys[key_id] = rec
        today = _today()
        if rec.get("day") != today:
            old = rec.get("day")
            if old:
                hist = rec.setdefault("history", {})
                hist[old] = {f: rec.get(f, 0) for f in _DAY_FIELDS}
                for stale in sorted(hist)[:-HISTORY_DAYS]:
                    hist.pop(stale, None)
            rec["day"] = today
            for f in _DAY_FIELDS:
                rec[f] = 0
            self._dirty = True
        for f in _DAY_FIELDS:
            rec.setdefault(f, 0)
        rec.setdefault("total_requests", 0)
        rec.setdefault("total_tokens", 0)
        rec.setdefault("total_cached_tokens", 0)
        return rec

    # ── 记录 ──

    def record(
        self,
        key_id: str,
        label: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        duration_ms: float = 0.0,
        ok: bool = True,
        error: str = "",
        rejected: bool = False,
    ):
        """记一次请求。rejected=True 表示这次是被配额挡下的（没真正干活）。"""
        if not key_id:
            return
        with self._lock:
            rec = self._record_locked(key_id)
            if label:
                rec["label"] = label
            if rejected:
                rec["rejected"] = rec.get("rejected", 0) + 1
            else:
                rec["requests"] = rec.get("requests", 0) + 1
                rec["total_requests"] = rec.get("total_requests", 0) + 1
                rec["prompt_tokens"] = rec.get("prompt_tokens", 0) + int(prompt_tokens or 0)
                rec["completion_tokens"] = rec.get("completion_tokens", 0) + int(completion_tokens or 0)
                rec["cached_tokens"] = rec.get("cached_tokens", 0) + int(cached_tokens or 0)
                rec["total_tokens"] = rec.get("total_tokens", 0) + int(prompt_tokens or 0) + int(completion_tokens or 0)
                rec["total_cached_tokens"] = rec.get("total_cached_tokens", 0) + int(cached_tokens or 0)
                rec["duration_ms"] = rec.get("duration_ms", 0.0) + float(duration_ms or 0.0)
                if not ok:
                    rec["errors"] = rec.get("errors", 0) + 1
                    if error:
                        rec["last_error"] = str(error)[:200]
            rec["last_used_at"] = int(time.time())
            self._dirty = True
            self.flush()

    # ── 配额 ──

    def check_quota(self, identity: Dict[str, Any]) -> Optional[Tuple[str, int]]:
        """返回 (拒绝原因, Retry-After 秒)，未超限返回 None。

        配额字段放在 api_keys.json 的 key 记录上（`quota_daily_requests` /
        `quota_daily_tokens`），0 或缺失 = 不限。
        """
        if not identity or not identity.get("key_id"):
            return None
        limit_req = int(identity.get("quota_daily_requests") or 0)
        limit_tok = int(identity.get("quota_daily_tokens") or 0)
        if limit_req <= 0 and limit_tok <= 0:
            return None

        with self._lock:
            rec = self._record_locked(identity["key_id"])
            used_req = int(rec.get("requests", 0))
            used_tok = int(rec.get("prompt_tokens", 0)) + int(rec.get("completion_tokens", 0))

        label = identity.get("log_label") or identity.get("name") or "该 Key"
        retry_after = _seconds_until_tomorrow()

        if limit_req > 0 and used_req >= limit_req:
            return (
                f"已达每日请求配额：{label} 今日 {used_req}/{limit_req} 次。"
                f"配额将在本地次日 00:00 重置（约 {retry_after // 3600} 小时后）。"
                f"如需调整请联系管理员。",
                retry_after,
            )
        if limit_tok > 0 and used_tok >= limit_tok:
            return (
                f"已达每日 Token 配额：{label} 今日 {used_tok}/{limit_tok} tokens。"
                f"配额将在本地次日 00:00 重置（约 {retry_after // 3600} 小时后）。",
                retry_after,
            )
        return None

    # ── 查询 ──

    def snapshot(self, key_id: str) -> Dict[str, Any]:
        """单个 key 的用量快照（供 /v1/usage 自助查询）"""
        with self._lock:
            rec = self._record_locked(key_id) if key_id in self._keys else dict(self._keys.get(key_id) or {})
            out = dict(rec or {})
        out.pop("history", None)
        pt = out.get("prompt_tokens", 0)
        ct = out.get("cached_tokens", 0)
        out["cache_hit_rate"] = round((ct / pt * 100), 1) if pt > 0 else 0.0
        return out

    def all_snapshots(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            out = {}
            for kid in list(self._keys.keys()):
                rec = self._record_locked(kid)
                snap = dict(rec)
                snap.pop("history", None)
                pt = snap.get("prompt_tokens", 0)
                ct = snap.get("cached_tokens", 0)
                snap["cache_hit_rate"] = round((ct / pt * 100), 1) if pt > 0 else 0.0
                out[kid] = snap
            return out

    def history(self, key_id: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._keys.get(key_id) or {}
            return dict(rec.get("history") or {})

    def reset(self, key_id: str) -> bool:
        """清零某个 key 的今日计数（管理员应急用：配错了/跑飞了）"""
        with self._lock:
            if key_id not in self._keys:
                return False
            rec = self._keys[key_id]
            for f in _DAY_FIELDS:
                rec[f] = 0
            self._dirty = True
            self.flush(force=True)
            return True

    def forget(self, key_id: str) -> bool:
        """删除某个 key 的用量记录（仅用于显式清理）"""
        with self._lock:
            if key_id in self._keys:
                self._keys.pop(key_id)
                self._dirty = True
                self.flush(force=True)
                return True
            return False

    def mark_revoked(self, key_id: str):
        """把某个 key 标记为已作废 —— **不删数据**。

        撤销 key 往往正是因为"他把号跑飞了"，那一刻恰恰最需要历史证据；
        删掉等于在需要证据的瞬间销毁证据。所以只打标记，
        由 `prune_revoked()` 或管理端显式清理。
        """
        with self._lock:
            rec = self._keys.get(key_id)
            if rec is not None:
                rec["revoked"] = True
                rec["revoked_at"] = int(time.time())
                self._dirty = True
                self.flush()

    def prune_revoked(self, keep_days: int = 0) -> int:
        """清理已作废且超过 keep_days 天的用量记录，返回清理条数。

        keep_days=0 表示清掉全部已作废的（默认只在管理员显式调用时执行）。
        """
        cutoff = int(time.time()) - max(0, keep_days) * 86400
        with self._lock:
            doomed = [
                kid for kid, rec in self._keys.items()
                if rec.get("revoked") and int(rec.get("revoked_at") or 0) <= cutoff
            ]
            for kid in doomed:
                self._keys.pop(kid, None)
            if doomed:
                self._dirty = True
                self.flush(force=True)
            return len(doomed)

    def revoked_snapshots(self) -> Dict[str, Dict[str, Any]]:
        """已作废但仍留有历史的用量记录（供管理端查看，不计入配额）"""
        with self._lock:
            out = {}
            for kid, rec in self._keys.items():
                if not rec.get("revoked"):
                    continue
                snap = dict(rec)
                snap.pop("history", None)
                out[kid] = snap
            return out


key_usage_engine = KeyUsageEngine()
