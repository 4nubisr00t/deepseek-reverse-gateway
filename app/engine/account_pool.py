"""
智能账号池调度器与断路器 (Account Pool & Circuit Breaker)
- 单账号并发限流 (asyncio.Semaphore，默认 2，按上游实测能力)
- 五态状态机 (ACTIVE / BUSY / COOLDOWN / WARMING / MUTED / INVALID)
- 养号期严格隔离：WARMING 状态账号绝对不参与业务路由
- 会话粘性优先调度 (Session Affinity)
- 账号归属隔离 (T2)：有主的账号只对它的 key 开放
- 被动熔断断路器：捕获 biz_code:5 自动标记 mute_until，到期自动静默唤醒
"""

import time
import random
import asyncio
import zlib
from typing import Dict, Any, Iterable, List, Optional, Set
from app.config import config_manager

# 单账号允许的并发生成流数。
# 实测依据（2026-09-10）：同一账号并发发起独立会话流，
#   1→1/1、2→2/2、3→2/3、4→2/4、6→2/6，
# 即上游上限为 2；超出部分返回 HTTP 200 + 空正文（静默失败，非报错）。
MAX_CONCURRENT_PER_ACCOUNT = 2

# 修复：被动熔断的解禁退避阶梯（秒）。
# 杜绝将偶发限流升级为 6 小时长达 21600s 的恶性惩罚。采用秒级保护性渐进退避：30s → 60s → 180s → 300s（上限 5 分钟）。
MUTE_BACKOFF_LADDER = (30, 60, 180, 300)   # 30s → 1min → 3min → 5min
# 永久失效的停用时长：token 过期/账号被封这类问题不会自己好，需要人工换号。
# 给一个很长的值而不是 None —— None 会让它永远不再参与任何检查与展示。
PERMANENT_INVALID_SECONDS = 30 * 86400



def _owner_map() -> Dict[str, Dict[str, str]]:
    """账号归属表。**延迟导入**以避免 security ↔ account_pool 的循环依赖。"""
    try:
        from app.core.security import account_owner_map
        return account_owner_map()
    except Exception as e:
        # 归属表读不出来时退化为"全公共池"（即 T2 之前的旧行为），
        # 而不是把所有账号锁死 —— 可用性优先，且这种情况下宁可多放行也不误伤所有人。
        print(f"[AccountPool] 读取账号归属表失败，退化为公共池模式: {e}", flush=True)
        return {}


def _stable_proxy_index(label: str, size: int) -> int:
    """跨进程确定性的账号→代理槽位映射，避免重启后 PYTHONHASHSEED 变化引起出口 IP 漂移。"""
    if size <= 0:
        return 0
    return zlib.crc32(str(label or "default").encode("utf-8")) % size


def get_effective_proxy(account: Any) -> Optional[str]:
    """安全获取账号生效代理，兼容运行时 AccountRuntime、字典、测试 Mock 对象与 None，并自动避开熔断冷却中的代理。"""
    from app.engine.proxy_manager import proxy_health_registry

    proxies = getattr(config_manager.config, "proxies", None) or []
    default_proxy = getattr(config_manager.config, "proxy", None)

    if account is None:
        raw = default_proxy
        label = "default"
    elif isinstance(account, dict):
        raw = account.get("proxy")
        label = str(account.get("account_label") or account.get("email") or "default")
    elif hasattr(account, "effective_proxy"):
        return account.effective_proxy
    else:
        raw = getattr(account, "proxy", None)
        label = str(getattr(account, "account_label", None) or "default")

    if raw:
        if proxy_health_registry.is_healthy(raw):
            return raw
        if proxies:
            alt = proxy_health_registry.select_healthy_proxy(label, proxies, default_proxy=default_proxy, avoid_proxy=raw)
            if alt and proxy_health_registry.is_healthy(alt):
                return alt
        return raw

    if proxies:
        primary = proxies[_stable_proxy_index(label, len(proxies))]
        if proxy_health_registry.is_healthy(primary):
            return primary
        return proxy_health_registry.select_healthy_proxy(label, proxies, default_proxy=default_proxy, avoid_proxy=primary)
    return default_proxy


def should_trigger_anti_cascade(prev_proxy: Optional[str], candidate_proxy: Optional[str]) -> bool:
    """
    遭遇官方禁言时，判断是否应触发同 IP 连坐熔断保护。
    仅在系统配置了多代理出口池（len(proxies) > 1）且 candidate 依然与被禁言账号同属相同代理出口时触发。
    若系统未配置多代理出口，则保持原生故障转移，不阻断单 IP 换号。
    """
    if not getattr(config_manager.config, "anti_cascade_same_ip", False):
        return False
    if not prev_proxy or not candidate_proxy:
        return False
    if prev_proxy != candidate_proxy:
        return False
    proxies = getattr(config_manager.config, "proxies", None) or []
    return len(proxies) > 1




class AccountRuntime:
    def __init__(self, data: Dict[str, Any]):
        self.account_label: str = data.get("account_label", "")
        self.token: str = data.get("token", "")
        self.cookie: str = data.get("cookie", "")
        self.email: str = data.get("_email") or data.get("email") or self.account_label
        self.password: str = data.get("_password") or data.get("password") or ""
        self.proxy: Optional[str] = data.get("proxy")
        self.session_id: Optional[str] = data.get("session_id")
        self.is_valid: bool = data.get("is_valid", True)
        self.status: str = data.get("status", "ACTIVE" if self.is_valid else "MUTED")
        self.mute_until: Optional[int] = data.get("mute_until")
        self.ban_kind: Optional[str] = data.get("ban_kind")
        self.pool_tag: str = data.get("pool_tag", "public")
        self.disposable: bool = bool(
            data.get("disposable") is True
            or data.get("is_disposable") is True
            or str(self.pool_tag or "").strip().lower() == "disposable"
        )
        self.last_used: float = data.get("last_used") or 0.0
        # 5 件套 MFA 受信任设备凭据资产
        self.credential_id: Optional[str] = data.get("credential_id")
        self.mfa_private_key: Optional[str] = data.get("mfa_private_key")
        self.mfa_public_key: Optional[str] = data.get("mfa_public_key")
        self.mfa_key_version: Optional[str] = data.get("mfa_key_version")
        self.device_id: Optional[str] = data.get("device_id")
        self.device_fingerprint: Optional[str] = data.get("device_fingerprint")
        self.expire_at: Optional[int] = data.get("expire_at")
        self.updated_at: int = int(data.get("updated_at") or 0)
        self.token_updated_at: int = int(data.get("token_updated_at") or data.get("updated_at") or 0)
        # 并发闸门（T3）：上游实测单账号可同时承载 2 条生成流
        #   （并发 1→1/1、2→2/2、3→2/3、4→2/4、6→2/6，超出部分返回 HTTP 200 + 空正文）。
        # 旧实现用 asyncio.Lock（二值），把每账号锁成 1 并发 → 一半容量闲置。
        # 改为 Semaphore(2) 后容量翻倍，且"每人多号"扩容时并发能力随号数线性放大。
        self.semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_PER_ACCOUNT)
        # 兼容保留：旧代码/测试引用了 .lock（见 tests/test_gateway.py 的并发锁测试）
        self.lock: asyncio.Semaphore = self.semaphore
        # 上一次请求完成时间，用于估算"建议退避时长"（供 429 的 Retry-After 使用）
        self.last_released_at: float = 0.0
        # 快速失败时写下的建议退避秒数
        self.wait_hint: float = 0.0
        # T12: 被熔断过几次（持久化到 config.json），用于指数退避与告警
        self.mute_count: int = int(data.get("mute_count") or 0)
        # 调度用的单调序号。
        self.dispatch_seq: int = 0
        # 防封号防御机制：临时保护性冷却截止时间戳（遇到 429/限流时触发）
        self.cooldown_until: float = 0.0
        # 自适应单次请求间隔随机抖动（秒）
        self.effective_cooldown: float = 0.0
        # 最近一次免密重登失败的详细结构化信息 (biz_code / kind / error)
        self.last_relogin_error: Dict[str, Any] = {}

    def apply_protective_cooldown(self, seconds: float = 20.0, reason: str = "rate_limit"):
        """给账号施加临时保护性冷却，防止短时间内反复请求导致官方封禁。"""
        self.cooldown_until = max(self.cooldown_until, time.time() + seconds)
        print(f"[AccountPool] 账号 {self.account_label} 进入保护性冷却 {seconds:.1f}s (原因: {reason})", flush=True)

    def apply_quota_cooldown(
        self,
        reset_at: Optional[int] = None,
        default_seconds: float = 600.0,
        reason: str = "quota_exceeded",
    ) -> int:
        """按官方配额重置时间 `reset_at`（或默认退避秒数）施加冷却，不触发永久熔断也不剥离绑定。"""
        now = time.time()
        if reset_at and reset_at > now:
            sec = max(30.0, min(float(reset_at) - now, 24.0 * 3600.0))
        else:
            sec = max(30.0, float(default_seconds))
        self.apply_protective_cooldown(sec, reason=reason)
        return int(sec)

    def next_mute_seconds(self, upstream_mute_until: Optional[int] = None) -> int:
        """算这次熔断该停多久。

        优先级：**上游给的确切时间 > 指数退避阶梯**。
        上游自己说了"禁言到 X 点"，就没有理由去猜。
        """
        now = int(time.time())
        if upstream_mute_until and upstream_mute_until > now:
            return max(60, upstream_mute_until - now)
        idx = min(max(0, self.mute_count - 1), len(MUTE_BACKOFF_LADDER) - 1)
        return MUTE_BACKOFF_LADDER[idx]

    def cooldown_remaining(self) -> float:
        """估算该账号距下次可用还需多久（秒）。"""
        now = time.time()
        if self.cooldown_until and self.cooldown_until > now:
            return float(self.cooldown_until - now)
        est = 6.0
        if self.last_released_at:
            elapsed = now - self.last_released_at
            # 已过的时间可视作"可能即将释放"，但保留至少 2 秒余量
            est = max(2.0, 6.0 - elapsed)
        return float(min(15.0, est))

    def is_muted_now(self) -> bool:
        """当前是否仍在停用期内（`mute_until` 是未来时间）。"""
        return bool(self.mute_until) and self.mute_until > time.time()

    def check_mute_expiry(self):
        """若解禁时间已过，自动静默唤醒账号。

        ⚠️ 必须 `self.mute_until` 非空才可能唤醒。旧实现路由传 None，
        于是被熔断的账号永远醒不过来（见 release_account 的说明）。
        现在所有熔断路径都会写一个真实的 mute_until。

        ⚠️ 判据从「必须先是 MUTED/INVALID」放宽为「**只要 mute_until 到期就清掉**」。
        原因是实测抓到过一个自相矛盾的记录（见 `is_usable_state`）：

            is_valid=True + status=ACTIVE + mute_until=未来

        它是"一键探活"写出来的（admin_routes 的 update_account 没传 status）。
        旧条件 `status == "MUTED" or (not is_valid and mute_until)` 对这种记录
        **恒不成立** → 那条 stale 的 `mute_until` 永远不会被清掉 →
        管理页会一直把它显示成"已被官方禁言"，即使解禁时间早就过了。
        """
        now = int(time.time())
        prev_kind = getattr(self, "ban_kind", None)
        if prev_kind == "permanent" or self.status in ("BANNED", "PERMANENT_BANNED"):
            return  # 永久封禁严禁自动康复回流
        if self.mute_until and now >= self.mute_until:
            prev = self.status
            if prev in ("QUARANTINE_INVALID", "INVALID", "QUARANTINE_MUTED") or prev_kind == "dated":
                if self.has_trusted_mfa:
                    # 5件套受信任账号：解封后进入公共冷库 (INACTIVE_COLD)，清空旧凭证，随时待命免密唤醒
                    self.is_valid = False
                    self.status = "INACTIVE_COLD"
                    self.mute_until = None
                    self.ban_kind = None
                    self.token = ""
                    self.cookie = ""
                    self.session_id = None
                    self.mute_count = 0
                    config_manager.update_account(
                        self.account_label,
                        is_valid=False,
                        status="INACTIVE_COLD",
                        mute_until=None,
                        ban_kind=None,
                        mute_count=0,
                        token="",
                        cookie="",
                        session_id="",
                    )
                    try:
                        from app.core.security import disown_account
                        disown_account(self.account_label)
                    except Exception:
                        pass
                    print(
                        f"[AccountPool] 5件套账号 {self.account_label} 隔离/有期封禁已到期（原状态 {prev}, 类别 {prev_kind}），"
                        f"已重置失效凭证并康复回流至零IP冷库 (INACTIVE_COLD)",
                        flush=True,
                    )
                else:
                    # 旧版无5件套余粮号：一旦清空Token将无法唤醒，若仍有Token且非INVALID则恢复ACTIVE，否则退役
                    if self.token and prev not in ("QUARANTINE_INVALID", "INVALID"):
                        self.is_valid = True
                        self.status = "ACTIVE"
                        self.mute_until = None
                        self.ban_kind = None
                        self.mute_count = 0
                        config_manager.update_account(
                            self.account_label, is_valid=True, status="ACTIVE", mute_until=None, ban_kind=None, mute_count=0
                        )
                        print(f"[AccountPool] 旧版余粮账号 {self.account_label} 有期禁言已到期，保留原 Token 恢复 ACTIVE", flush=True)
                    else:
                        self.is_valid = False
                        self.status = "INVALID"
                        self.mute_until = None
                        self.ban_kind = "permanent"
                        config_manager.update_account(
                            self.account_label, is_valid=False, status="INVALID", mute_until=None, ban_kind="permanent"
                        )
            else:
                self.is_valid = True
                self.status = "ACTIVE"
                self.mute_until = None
                self.ban_kind = None
                config_manager.update_account(
                    self.account_label, is_valid=True, status="ACTIVE", mute_until=None, ban_kind=None
                )
                try:
                    from app.core.security import rebind_rehabilitated_account
                    rebound_kid = rebind_rehabilitated_account(self.account_label)
                    if rebound_kid:
                        print(
                            f"[AccountPool] 账号 {self.account_label} 康复已自动归还原专属 Key「{rebound_kid}」",
                            flush=True,
                        )
                except Exception:
                    pass
                print(
                    f"[AccountPool] 账号 {self.account_label} 停用期已满（原状态 {prev}），"
                    f"自动恢复 ACTIVE 入池",
                    flush=True,
                )

    @property
    def has_trusted_mfa(self) -> bool:
        """是否拥有真实有效的受信任设备资产（5 件套 + 数美 B 指纹 + 未过期 expire_at）"""
        from app.engine.http_auth_engine import evaluate_trusted_device
        res = evaluate_trusted_device({
            "credential_id": self.credential_id,
            "mfa_private_key": self.mfa_private_key,
            "mfa_public_key": self.mfa_public_key,
            "mfa_key_version": self.mfa_key_version,
            "device_id": self.device_id,
            "device_fingerprint": self.device_fingerprint,
            "cookie": self.cookie,
            "expire_at": self.expire_at,
            "pool_tag": self.pool_tag,
            "disposable": self.disposable,
        })
        return bool(res["device_trusted"])

    @property
    def effective_proxy(self) -> Optional[str]:
        """返回该账号应使用的出口代理。优先使用账号专属 proxy，否则按 proxies 列表分散，自动绕开隔离期失效代理，兜底使用全局 proxy。"""
        from app.engine.proxy_manager import proxy_health_registry

        proxies = getattr(config_manager.config, "proxies", None) or []
        default_proxy = config_manager.config.proxy
        if self.proxy:
            if proxy_health_registry.is_healthy(self.proxy):
                return self.proxy
            if proxies:
                alt = proxy_health_registry.select_healthy_proxy(
                    self.account_label, proxies, default_proxy=default_proxy, avoid_proxy=self.proxy
                )
                if alt and proxy_health_registry.is_healthy(alt):
                    return alt
            if default_proxy and default_proxy != self.proxy and proxy_health_registry.is_healthy(default_proxy):
                return default_proxy
            return self.proxy
        if proxies:
            idx = _stable_proxy_index(self.account_label, len(proxies))
            primary = proxies[idx]
            if proxy_health_registry.is_healthy(primary):
                return primary
            return proxy_health_registry.select_healthy_proxy(
                self.account_label, proxies, default_proxy=default_proxy, avoid_proxy=primary
            )
        return default_proxy

    @property
    def relogin_proxy(self) -> Optional[str]:
        """登录（免码重登）专用出口代理。

        官方按 **IP** 做登录频控(40029 IP_ACCESS_RESTRICTED)，业务转发与登录若共用一个出口 IP
        会互相拖累。`config.relogin_proxies` 非空时按账号标签哈希分散选一个；
        留空则返回 None（沿用直连/旧行为）。
        """
        pool = getattr(config_manager.config, "relogin_proxies", None) or []
        if pool:
            return pool[_stable_proxy_index(self.account_label, len(pool))]
        return self.proxy or None

    async def try_relogin(self, proxy: Optional[str] = None) -> bool:
        """
        【受信任设备无感自愈核心】
        若账号拥有 5 件套 MFA 凭证，在 Token 失效（40003）时，通过 Playwright 1.65s 动态注入直登刷新 Token。
        若账号为旧版一次性账号（无 5 件套），Token 耗尽后无法二次接码，直接永久退役 (INVALID)，不阻塞上游。
        """
        if not self.has_trusted_mfa:
            print(f"[AccountPool] ⚠️ 旧版一次性账号 {self.account_label} 无 5 件套 MFA 凭证，Token 已耗尽，直接永久退役 (INVALID)", flush=True)
            self.is_valid = False
            self.status = "INVALID"
            self.ban_kind = "permanent"
            config_manager.update_account(
                self.account_label,
                is_valid=False,
                status="INVALID",
                ban_kind="permanent",
            )
            try:
                from app.core.security import disown_account
                disown_account(self.account_label)
            except Exception:
                pass
            return False

        if getattr(self, "disposable", False) or getattr(self, "pool_tag", "public") == "disposable":
            print(f"[AccountPool] 🛑 耗材账号 {self.account_label} Token 已自然失效，已设为不自动续杯，直接退役 (INVALID)", flush=True)
            self.is_valid = False
            self.status = "INVALID"
            self.ban_kind = "permanent"
            config_manager.update_account(
                self.account_label,
                is_valid=False,
                status="INVALID",
                ban_kind="permanent",
            )
            try:
                from app.core.security import disown_account
                disown_account(self.account_label)
            except Exception:
                pass
            return False

        if not self.password or not self.email:
            return False
        try:
            from app.engine.browser_login import quick_trusted_relogin
            from app.engine.trusted_relogin_engine import get_account_relogin_lock
            print(f"[AccountPool] 正在通过纯 HTTP 指纹免密通道续杯账号 {self.account_label}...", flush=True)
            account_dict = {
                "account_label": self.account_label,
                "email": self.email,
                "_email": self.email,
                "password": self.password,
                "_password": self.password,
                "cookie": getattr(self, "cookie", "") or "",
                "credential_id": self.credential_id,
                "mfa_private_key": self.mfa_private_key,
                "mfa_public_key": self.mfa_public_key,
                "mfa_key_version": self.mfa_key_version,
                "device_id": self.device_id,
                "device_fingerprint": getattr(self, "device_fingerprint", None),
                "expire_at": getattr(self, "expire_at", None),
            }
            stale_token = self.token or ""
            stale_tok_upd = int(getattr(self, "token_updated_at", 0) or 0)
            wait_started_ts = int(time.time())
            lock_key = self.email or self.account_label
            eff_proxy = proxy or self.relogin_proxy
            self.last_relogin_error = {}

            def _guarded_single_flight():
                with get_account_relogin_lock(lock_key):
                    latest = None
                    try:
                        for a in config_manager.get_accounts():
                            if a.get("account_label") == self.account_label or a.get("email") == self.email:
                                latest = a
                                break
                    except Exception:
                        latest = None
                    if latest is None:
                        try:
                            from app.db.repositories import account_repo
                            latest = account_repo.get(self.account_label)
                        except Exception:
                            latest = None
                    if isinstance(latest, dict):
                        lat_tok = latest.get("token") or ""
                        lat_valid = bool(latest.get("is_valid", True))
                        lat_status = latest.get("status") or ("ACTIVE" if lat_valid else "INVALID")
                        lat_tok_upd = int(latest.get("token_updated_at") or 0)
                        if (
                            lat_status == "ACTIVE"
                            and lat_valid
                            and lat_tok
                            and (
                                lat_tok != stale_token
                                or (lat_tok_upd >= wait_started_ts and lat_tok_upd > stale_tok_upd)
                            )
                        ):
                            return True, {
                                "token": lat_tok,
                                "is_muted": False,
                                "mute_until": None,
                                "reused_single_flight": True,
                                "token_updated_at": lat_tok_upd,
                            }
                        if lat_status in ("MFA_REQUIRED", "INVALID", "BANNED", "PERMANENT_BANNED") and lat_tok == stale_token:
                            lat_ban = latest.get("ban_kind")
                            if lat_ban in ("mfa_required", "permanent"):
                                return False, {
                                    "error": f"并发重登跳过：账号已处于终态 ({lat_status}/{lat_ban})",
                                    "biz_code": 14 if lat_ban == "mfa_required" else 10,
                                    "kind": lat_ban,
                                }
                    return quick_trusted_relogin(
                        self.email,
                        self.password,
                        account_dict=account_dict,
                        proxy=eff_proxy,
                    )

            ok, res = await asyncio.to_thread(_guarded_single_flight)
            token_val = res.get("token") if (ok and isinstance(res, dict)) else ""
            if ok and token_val:
                is_muted = bool(res.get("is_muted", False)) if isinstance(res, dict) else False
                raw_mute_until = res.get("mute_until") if isinstance(res, dict) else None
                parsed_mute_until: Optional[int] = None
                if is_muted and raw_mute_until is not None:
                    if isinstance(raw_mute_until, (int, float)):
                        ts = int(raw_mute_until)
                        parsed_mute_until = ts // 1000 if ts > 10_000_000_000 else ts
                    elif isinstance(raw_mute_until, str) and raw_mute_until.strip():
                        s_val = raw_mute_until.strip()
                        try:
                            ts = int(float(s_val))
                            parsed_mute_until = ts // 1000 if ts > 10_000_000_000 else ts
                        except ValueError:
                            from datetime import datetime
                            try:
                                parsed_mute_until = int(datetime.fromisoformat(s_val.replace("Z", "+00:00")).timestamp())
                            except ValueError:
                                parsed_mute_until = None

                self.token = token_val
                if is_muted:
                    self.is_valid = False
                    self.status = "MUTED"
                    self.mute_until = parsed_mute_until
                    self.ban_kind = "dated"
                    config_manager.update_account(
                        self.account_label,
                        token=token_val,
                        is_valid=False,
                        status="MUTED",
                        mute_until=parsed_mute_until,
                        ban_kind="dated",
                    )
                    print(f"[AccountPool] ⚠️ 账号 {self.account_label} 纯 HTTP 刷新成功但处于官方有期禁言 (MUTED)", flush=True)
                    return False

                self.is_valid = True
                self.status = "ACTIVE"
                self.mute_until = None
                self.ban_kind = None
                self.mute_count = 0
                self.token_updated_at = int(res.get("token_updated_at") or time.time()) if isinstance(res, dict) else int(time.time())
                if isinstance(res, dict) and res.get("reused_single_flight"):
                    print(f"[AccountPool] 🔒 账号 {self.account_label} 命中单飞并发复用，直接采用并发刷新后的最新 Token！", flush=True)
                    return True
                config_manager.update_account(
                    self.account_label,
                    token=token_val,
                    is_valid=True,
                    status="ACTIVE",
                    mute_until=None,
                    ban_kind=None,
                    mute_count=0,
                    token_updated_at=self.token_updated_at,
                )
                print(f"[AccountPool] 🎉 账号 {self.account_label} 纯 HTTP 免密续杯成功，Token 已刷新并热生效！", flush=True)
                return True
            else:
                if isinstance(res, dict):
                    self.last_relogin_error = dict(res)
                    err_msg = res.get("error") or str(res)
                else:
                    self.last_relogin_error = {"error": str(res), "biz_code": None, "kind": "unknown"}
                    err_msg = str(res)
                print(f"[AccountPool] ❌ 账号 {self.account_label} 纯 HTTP 续杯失败: {err_msg}", flush=True)
                return False
        except Exception as e:
            self.last_relogin_error = {"error": f"{type(e).__name__}: {e}", "biz_code": None, "kind": "exception"}
            print(f"[AccountPool] ❌ 账号 {self.account_label} 自动续杯异常: {type(e).__name__}: {e}", flush=True)
            return False

    @property
    def is_usable_state(self) -> bool:
        """账号**状态**上是否可用（**不看**并发额度）。

        这是"能不能发出去"的**唯一判据入口**：
        `is_valid` + `status == "ACTIVE"` + **不在停用期内** + **不在临时保护性冷却期内**。
        """
        return (
            self.is_valid
            and self.status == "ACTIVE"
            and not self.is_muted_now()
            and (not self.cooldown_until or time.time() >= self.cooldown_until)
        )

    @property
    def is_available_for_api(self) -> bool:
        """状态可用且仍有并发额度，且不在自适应抖动冷却期内，才允许接收业务请求。"""
        if not self.is_usable_state or self.semaphore.locked():
            return False
        eff = getattr(self, "effective_cooldown", 0.0)
        if eff > 0 and self.last_released_at > 0:
            if time.time() - self.last_released_at < eff:
                return False
        return True

    @property
    def in_flight(self) -> int:
        """当前在途生成流数量（供可观测性使用，严格限幅 [0, MAX_CONCURRENT_PER_ACCOUNT]）"""
        return max(0, min(MAX_CONCURRENT_PER_ACCOUNT, MAX_CONCURRENT_PER_ACCOUNT - self.semaphore._value))


class AccountPool:
    """管理多账号运行时状态与并发排队"""

    def __init__(self):
        self._accounts: Dict[str, AccountRuntime] = {}
        # 快速失败时给出的建议退避秒数（供上层填 Retry-After）
        self.last_wait_hint: float = 0.0
        # 上次快速失败的原因，供上层区分"自有号满"与"根本没号可用"
        self.last_deny_reason: str = ""
        # 上次快速失败的人类可读细节（谁满了、池里还有没有空闲）
        self.last_deny_detail: str = ""
        # 弹性补位检查节流（见 release_account 中的说明）
        self._REPLENISH_CHECK_INTERVAL: float = 30.0
        self._last_replenish_check: float = 0.0
        # T12: 释放序号计数器（LRU tie-break）
        self._seq_counter: int = 0
        # 热替换并发互斥与冷库预占保护（防止并发熔断抢号与冷库雪崩连坐）
        self._hotswap_lock: asyncio.Lock = asyncio.Lock()
        self._reserving_labels: Set[str] = set()
        self._last_cold_wake_ts: float = 0.0
        self.reload()

    def reload(self):
        try:
            from app.core.security import sanitize_key_bindings
            sanitize_key_bindings()
        except Exception:
            pass
        raw_list = config_manager.get_accounts()
        new_dict = {}
        for item in raw_list:
            label = item.get("account_label")
            if not label:
                continue
            if label in self._accounts:
                existing = self._accounts[label]
                existing.token = item.get("token", existing.token)
                existing.cookie = item.get("cookie", existing.cookie)
                existing.session_id = item.get("session_id", existing.session_id)
                existing.email = item.get("_email") or item.get("email") or existing.email
                existing.password = item.get("_password") or item.get("password") or existing.password
                existing.credential_id = item.get("credential_id") or existing.credential_id
                existing.mfa_private_key = item.get("mfa_private_key") or existing.mfa_private_key
                existing.mfa_public_key = item.get("mfa_public_key") or existing.mfa_public_key
                existing.mfa_key_version = item.get("mfa_key_version") or existing.mfa_key_version
                existing.device_id = item.get("device_id") or existing.device_id
                existing.device_fingerprint = item.get("device_fingerprint") or existing.device_fingerprint
                existing.is_valid = item.get("is_valid", existing.is_valid)
                existing.status = item.get("status", "ACTIVE" if existing.is_valid else "MUTED")
                existing.mute_until = item.get("mute_until", existing.mute_until)
                existing.mute_count = int(item.get("mute_count") or existing.mute_count or 0)
                existing.ban_kind = item.get("ban_kind", existing.ban_kind)
                existing.pool_tag = item.get("pool_tag", getattr(existing, "pool_tag", "public"))
                existing.disposable = bool(
                    item.get("disposable") is True
                    or item.get("is_disposable") is True
                    or str(existing.pool_tag or "").strip().lower() == "disposable"
                )
                if item.get("expire_at") is not None:
                    existing.expire_at = item.get("expire_at")
                new_dict[label] = existing
            else:
                new_dict[label] = AccountRuntime(item)
        self._accounts = new_dict

    def get_all_status(self) -> List[Dict[str, Any]]:
        self.reload()
        owners = _owner_map()
        out = []
        for acc in self._accounts.values():
            acc.check_mute_expiry()
            own = owners.get(acc.account_label)
            out.append({
                "account_label": acc.account_label,
                "is_valid": acc.is_valid,
                "status": acc.status,
                "ban_kind": acc.ban_kind,
                "is_dated_ban": acc.ban_kind == "dated",
                "is_busy": acc.lock.locked(),
                "mute_until": acc.mute_until,
                "last_used": acc.last_used,
                "in_flight": acc.in_flight,
                "owner_key_id": (own or {}).get("key_id", ""),
                "owner": (own or {}).get("owner", ""),
                "in_pool": own is None,
                "has_trusted_mfa": acc.has_trusted_mfa,
                "token": acc.token or "",
                # ★ "真实可用"必须和"本地认为有效"分开报。
                #
                # `is_valid` 是**持久化的本地字段**，它可能过期（被独占的号
                # 永远不进公共池 → 永远不被探活 → 上游早已禁言而本地仍写 ACTIVE）。
                # 实测就抓到过 `is_valid=True + status=ACTIVE + mute_until=未来` 这种记录。
                # 因此另给两个**由判据入口算出来**的字段，不要在调用方各写一遍逻辑。
                "usable": acc.is_usable_state,
                "muted_now": acc.is_muted_now(),
                # T12: 熔断次数与解禁时间必须可见 ——
                # 否则"号池悄悄缩水"就只能靠人去数
                "mute_count": acc.mute_count,
                "mute_until": acc.mute_until,
                "wake_at": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(acc.mute_until))
                            if acc.mute_until else None),
            })
        return out

    # ── T2: 归属隔离 ──

    def allowed_labels(
        self,
        owned_labels: Optional[Iterable[str]] = None,
        pool_fallback: bool = False,
        is_vip: bool = False,
        strict_owned: bool = False,
        prefer_disposable: bool = False,
    ) -> Optional[Set[str]]:
        """算出这次请求**允许使用**的账号集合。返回 None 表示不设限。

        分流与特权调度规则：
          · 个人池 / 专属号独占：已被分配给某用户或某 Key 的号，其他用户/Key 严禁越权。
          · strict_owned=True 且 pool_fallback=False：即使归属已空也返回空集（503 隔离），绝不偷公共池。
          · VIP 付费池保供与人情/散客隔离、储备冷库（reserve）隔离不变。
        """
        for acc in self._accounts.values():
            acc.check_mute_expiry()

        declared = [str(x).strip() for x in (owned_labels or []) if str(x or "").strip()]
        owners = _owner_map()

        # 基础自由号集合（排除已分配独占号、以及专供补位的 reserve 储备号）
        free_base = {
            lbl for lbl, acc in self._accounts.items()
            if lbl not in owners and acc.status == "ACTIVE" and acc.is_valid and not acc.is_muted_now()
            and getattr(acc, "pool_tag", "public") != "reserve"
        }

        if is_vip:
            free = free_base
        else:
            # 人情/普通 Key：严格排除 VIP 专属号与 Reserve 储备号
            free = {
                lbl for lbl in free_base
                if getattr(self._accounts[lbl], "pool_tag", "public") not in ("vip", "reserve")
            }

        if declared:
            mine = {
                lbl for lbl in declared
                if lbl in self._accounts and self._accounts[lbl].status == "ACTIVE" and self._accounts[lbl].is_valid and not self._accounts[lbl].is_muted_now()
            }
            return mine | free if pool_fallback else mine
        if strict_owned and not pool_fallback:
            return set()
        return free

    async def acquire_account(
        self,
        preferred_label: Optional[str] = None,
        timeout: float = 60.0,
        immediate: bool = False,
        owned_labels: Optional[Iterable[str]] = None,
        pool_fallback: bool = False,
        avoid_label: Optional[str] = None,
        avoid_proxy: Optional[str] = None,
        sticky_micro_wait: Optional[float] = None,
        allow_sticky_drift: Optional[bool] = None,
        is_vip: bool = False,
        strict_owned: bool = False,
        prefer_disposable: bool = False,
    ) -> Optional[AccountRuntime]:
        """
        获取一个可用的账号并发额度

        - 若指定了 preferred_label (粘性会话)，优先尝试获取该账号（除非被 avoid_label 规避）
        - 只有 status == ACTIVE 且仍有并发额度的账号才能入池
        - 否则选择空闲、有效且最久未使用的账号
        - **归属隔离 (T2)**：owned_labels 非空时只能用这些号；别人名下的号永远不可用
        - **轮换与容灾 (avoid_label / avoid_proxy)**：支持避开刚退役或发生异常的账号及出口代理，优先选择名下其他空闲号与不同出口 IP

        :param immediate: **快速失败模式**。True 表示"没有额度就立刻返回 None"，
            绝不阻塞等待。

        为什么要 immediate（实测依据 2026-09-10）：
          旧行为是全忙时**静默阻塞**，默认 timeout=60s、调用点 30s。
          而客户端看门狗约 50s —— 一旦排队时间接近或超过它，
          客户端会在网关还在排队时就掐断连接，表现为
          TRANSPORT / "卡住几十秒然后报错"。
          更关键的是：**DSH 本身就是 Agent 调度器**，它自己才知道该让哪个
          子代理等待、哪个可以延后。网关只需诚实告知"现在没容量"，
          由客户端决定退避与调度；网关盲目排队反而更糟。
          因此改为：容量不足 → 立即返回 None → 上层返回 429 + Retry-After。
        """
        self.reload()
        for acc in self._accounts.values():
            acc.check_mute_expiry()
        deadline = time.time() + timeout

        allowed = self.allowed_labels(
            owned_labels,
            pool_fallback,
            is_vip=is_vip,
            strict_owned=strict_owned,
            prefer_disposable=prefer_disposable,
        )
        if allowed is not None and not allowed:
            declared = [str(x).strip() for x in (owned_labels or []) if str(x or "").strip()]
            declared_existing = [lbl for lbl in declared if lbl in self._accounts]
            if declared and declared_existing:
                now_ts = time.time()
                # 仅将真实有效的活跃号或处于短时退避冷却(<=3600s 且非永久/dated/MFA封禁)的账号计为可恢复冷却
                cooling_accs = [
                    self._accounts[lbl] for lbl in declared_existing
                    if (
                        self._accounts[lbl].status == "ACTIVE"
                        and self._accounts[lbl].is_valid
                        and not self._accounts[lbl].is_muted_now()
                    ) or (
                        self._accounts[lbl].status == "MUTED"
                        and getattr(self._accounts[lbl], "ban_kind", None) not in ("permanent", "mfa_required", "dated")
                        and self._accounts[lbl].mute_until
                        and 0 < (self._accounts[lbl].mute_until - now_ts) <= 3600
                    )
                ]
                if cooling_accs:
                    min_wait = 15.0
                    for acc in cooling_accs:
                        if acc.mute_until and acc.mute_until > now_ts:
                            min_wait = min(min_wait, max(2.0, acc.mute_until - now_ts))
                    self.last_wait_hint = min_wait
                    self.last_deny_reason = "own_account_at_capacity"
                    self.last_deny_detail = f"名下专属账号均在临时冷却中（预计 {int(min_wait)}s 后恢复）"
                    print(
                        f"[AccountPool] 该 key 名下账号 {declared_existing} 均在临时冷却中，拒绝即时分配并提示重试 (预计 {int(min_wait)}s)",
                        flush=True,
                    )
                    return None
                else:
                    # 名下分配账号已全部失效/封禁进垃圾库！绝非 15s 临时冷却！
                    try:
                        from app.core.security import sanitize_key_bindings
                        sanitize_key_bindings()
                    except Exception:
                        pass
                    # 仅当明确开启 pool_fallback=True 时才允许应急借用公共/耗材兜底池；严禁越权穿池盗用公共/其他池账号
                    fallback_allowed = (
                        self.allowed_labels(None, True, is_vip=is_vip, prefer_disposable=prefer_disposable)
                        if pool_fallback else set()
                    )
                    if fallback_allowed:
                        print(
                            f"[AccountPool] ⚠️ 该 key 原绑定账号 {declared_existing} 已全部失效，已开启 pool_fallback，触发{'耗材' if prefer_disposable else '公共'}池应急兜底（可用 {len(fallback_allowed)} 个）",
                            flush=True,
                        )
                        allowed = fallback_allowed
                    else:
                        self.last_wait_hint = 0.0
                        self.last_deny_reason = "all_assigned_accounts_invalid"
                        self.last_deny_detail = "名下专属账号已全部失效/隔离（未开启公共池兜底，拒绝越权分配公共池）"
                        print(
                            f"[AccountPool] 🛑 该 key 名下账号 {declared_existing} 已全部失效且 pool_fallback=False，严格拒绝越权借用公共池",
                            flush=True,
                        )
                        return None
            else:
                self.last_wait_hint = 0.0 if (strict_owned and not pool_fallback) else 30.0
                self.last_deny_reason = "no_account_assigned"
                print(
                    f"[AccountPool] 该 key 名下无可用账号（归属声明 "
                    f"{list(owned_labels or [])}, strict_owned={strict_owned}），拒绝分配",
                    flush=True,
                )
                return None

        while True:
            for acc in self._accounts.values():
                acc.check_mute_expiry()

            # 1. 优先获取 preferred 账号（长会话粘性：必须等待同一账号可用，严禁会话途中漂移到其他账号）
            #    ⚠️ 若该号被 avoid_label 显式规避（如上下文压缩主动换号），则跳过粘性直接走候选轮换
            #    ⚠️ 归属校验必须在粘性之前：会话记录里存的 account_label 可能属于
            #    另一个 key（换 key 重放 / 号被改派），此时粘性会变成越权通道。
            if (
                preferred_label
                and (not avoid_label or preferred_label != avoid_label)
                and preferred_label in self._accounts
                and (allowed is None or preferred_label in allowed)
            ):
                target = self._accounts[preferred_label]
                # 粘性路径同样只认 `is_usable_state`（含停用期检查）：
                # 一个仍被禁言的粘性号，等多久都拿不到答案，只能告知上层限流。
                if target.is_usable_state:
                    # ⚠️ T12 修掉的关键缺陷：immediate 模式下**必须立刻**判定，不能等。
                    # 旧实现这里写的是 `wait_for = remaining if immediate else min(3.0, remaining)`，
                    # 也就是 immediate=True 时用的竟是**剩余总预算**（默认 60 秒）——
                    # 于是"快速失败"在粘性路径上实际会阻塞整整一分钟才返回 429，
                    # 而客户端看门狗约 50 秒：**客户端永远先超时**，
                    # 表现为"卡住几十秒然后报错"（TRANSPORT）。
                    # 这正是 T22 想消灭的症状，却在这条分支上一直没被修掉。
                    # 单测 test_sticky_account_full_does_not_drift 实测到它耗时 60.01s。
                    eff_micro_wait = (
                        sticky_micro_wait
                        if sticky_micro_wait is not None
                        else float(getattr(config_manager.config, "sticky_micro_wait_seconds", 0.0) or 0.0)
                    )
                    eff_allow_drift = (
                        allow_sticky_drift
                        if allow_sticky_drift is not None
                        else bool(getattr(config_manager.config, "allow_sticky_drift_without_files", False))
                    )
                    if immediate and target.semaphore.locked():
                        # 第一轨优雅降级：若配置了微等待窗口 (sticky_micro_wait > 0)，先尝试微等待吸收流交接毛刺
                        if eff_micro_wait > 0:
                            try:
                                await asyncio.wait_for(target.semaphore.acquire(), timeout=eff_micro_wait)
                                print(f"[AccountPool] 微等待 {eff_micro_wait:.1f}s 后成功获取粘性账号: {target.account_label}", flush=True)
                                return target
                            except asyncio.TimeoutError:
                                pass

                        idle_list = [
                            a for a in self._accounts.values()
                            if a.is_available_for_api and a.account_label != target.account_label
                            and (allowed is None or a.account_label in allowed)
                        ]
                        idle = len(idle_list)

                        # 第二轨优雅降级：若允许无图粘性漂移 (allow_sticky_drift=True) 且池内有空闲号，平滑切换至空闲号重建上下文
                        if eff_allow_drift and idle > 0:
                            print(
                                f"[AccountPool] 🔄 粘性账号 {target.account_label} 满载，启用无图会话平滑漂移（可用空闲号 {idle} 个）",
                                flush=True,
                            )
                            preferred_label = None
                        else:
                            target.wait_hint = max(1.0, target.cooldown_remaining())
                            self.last_wait_hint = target.wait_hint
                            self.last_deny_reason = "own_account_at_capacity"
                            self.last_deny_detail = (
                                f"本次会话绑定在账号 {target.account_label}，该号已满载；"
                                + (f"池里另有 {idle} 个账号空闲，但会话不会中途换号"
                                   f"（换号会丢掉上游会话续写）" if idle else "池里也没有其它空闲账号")
                            )
                            print(
                                f"[AccountPool] 粘性账号 {target.account_label} 无并发额度，"
                                f"立即快速失败（不等待、不漂移到其它账号；池内空闲 {idle} 个）",
                                flush=True,
                            )
                            return None
                    if preferred_label is not None:
                        remaining = max(0.1, deadline - time.time())
                        wait_for = remaining if immediate else min(3.0, remaining)
                        try:
                            await asyncio.wait_for(target.semaphore.acquire(), timeout=wait_for)
                            print(f"[AccountPool] 成功获取长会话粘性账号: {target.account_label}", flush=True)
                            return target
                        except asyncio.TimeoutError:
                            if immediate:
                                target.wait_hint = max(1.0, target.cooldown_remaining())
                                self.last_wait_hint = target.wait_hint
                                self.last_deny_reason = "own_account_at_capacity"
                                print(
                                    f"[AccountPool] 粘性账号 {target.account_label} 无并发额度，"
                                    f"快速失败（不漂移到其它账号）",
                                    flush=True,
                                )
                                return None
                            print(f"[AccountPool] 粘性账号 {target.account_label} 当前被占用 (等待释放中)...", flush=True)
                            await asyncio.sleep(0.5)
                            continue

            # 2. 从业务可用候选账号中选出未锁定且 last_used 最小的账号
            candidates = [
                acc for acc in self._accounts.values()
                if acc.is_available_for_api
                and (allowed is None or acc.account_label in allowed)
            ]

            if not candidates:
                # 软降级：若账号健康且并发未满，仅因随机抖动冷却未过，优先放行以避免单号用户被误报 429
                jitter_candidates = [
                    acc for acc in self._accounts.values()
                    if acc.is_usable_state and not acc.semaphore.locked()
                    and (allowed is None or acc.account_label in allowed)
                ]
                if jitter_candidates:
                    jitter_candidates.sort(key=lambda a: (a.last_used, a.dispatch_seq))
                    candidates = jitter_candidates

            if candidates:
                # 轮换/容灾：若指定了 avoid_label 且有其他候选账号，优先使用非 avoid_label 账号
                if avoid_label and len(candidates) > 1:
                    non_avoid = [a for a in candidates if a.account_label != avoid_label]
                    if non_avoid:
                        candidates = non_avoid

                # 出口代理隔离容灾：若指定了 avoid_proxy（如上一个账号被限流/禁言时的出口代理），
                # 优先选择不同出口代理的账号，切断上游同 IP 连坐风控
                if avoid_proxy and len(candidates) > 1:
                    diff_proxy = [a for a in candidates if get_effective_proxy(a) != avoid_proxy]
                    if diff_proxy:
                        candidates = diff_proxy

                # 严格 LRU：先比最后使用时刻，再比释放序号。
                # 只比 last_used 会在同一时钟刻度内退化（见 dispatch_seq 的说明）。
                candidates.sort(key=lambda a: (a.last_used, a.dispatch_seq))
                chosen = candidates[0]
                remaining = max(0.1, deadline - time.time())
                wait_for = remaining if immediate else min(3.0, remaining)
                try:
                    await asyncio.wait_for(chosen.semaphore.acquire(), timeout=wait_for)
                    print(f"[AccountPool] 成功获取空闲账号: {chosen.account_label}", flush=True)
                    return chosen
                except asyncio.TimeoutError:
                    if immediate:
                        chosen.wait_hint = max(1.0, chosen.cooldown_remaining())
                        self.last_wait_hint = chosen.wait_hint
                        self.last_deny_reason = "own_account_at_capacity"
                        print(
                            f"[AccountPool] 候选账号 {chosen.account_label} 无并发额度，快速失败",
                            flush=True,
                        )
                        return None
                    print(f"[AccountPool] 候选账号 {chosen.account_label} 获取额度超时，继续轮询...", flush=True)
                    await asyncio.sleep(0.5)
                    continue
                continue

            # 无可用候选：分别给出可诊断的等待提示
            if immediate:
                # 只看**本次允许的**账号，否则会把"别人名下的号在忙"算成自己的容量
                scope = [
                    a for a in self._accounts.values()
                    if (allowed is None or a.account_label in allowed)
                ]
                infos = [a for a in scope if a.is_usable_state and a.semaphore.locked()]
                if infos:
                    # 有账号但并发额度用尽 → 提示等最短的那个
                    self.last_wait_hint = min(a.cooldown_remaining() for a in infos)
                    self.last_deny_reason = "own_account_at_capacity"
                else:
                    # 根本没有可用账号（全被禁言/养号）→ 给较长退避，避免打空转
                    self.last_wait_hint = 30.0
                    self.last_deny_reason = "no_active_account"
                print(
                    f"[AccountPool] 无可用账号额度（本次可用账号 "
                    f"{sum(1 for a in scope if a.is_usable_state)} 个，"
                    f"池内总有效 "
                    f"{sum(1 for a in self._accounts.values() if a.is_usable_state)} 个），"
                    f"快速失败，建议退避 {self.last_wait_hint:.0f}s",
                    flush=True,
                )
                return None

            # 非 immediate：维持旧的阻塞重试行为
            if time.time() >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def acquire_for_identity(
        self,
        identity: Optional[Dict[str, Any]],
        preferred_label: Optional[str] = None,
        timeout: float = 60.0,
        immediate: bool = False,
        avoid_label: Optional[str] = None,
        avoid_proxy: Optional[str] = None,
        refresh: bool = False,
        **extra,
    ) -> Optional[AccountRuntime]:
        """按身份视图（或 Failover 时动态刷新后的最新快照）统一调用 acquire_account。"""
        ident = identity or {}
        if refresh:
            from app.core.security import get_identity_snapshot
            ident = get_identity_snapshot(ident)
        acc = await self.acquire_account(
            preferred_label=preferred_label,
            timeout=timeout,
            immediate=immediate,
            owned_labels=ident.get("account_labels") or [],
            pool_fallback=bool(ident.get("pool_fallback")),
            avoid_label=avoid_label,
            avoid_proxy=avoid_proxy,
            is_vip=bool(ident.get("is_vip")),
            strict_owned=bool(ident.get("strict_owned")),
            prefer_disposable=bool(ident.get("prefer_disposable")),
            **extra,
        )
        if acc is not None:
            try:
                from app.engine.drop_probe import register_account_token, update_context
                register_account_token(
                    acc.account_label,
                    acc.token,
                    getattr(acc, "token_updated_at", None),
                )
                update_context(
                    account=acc.account_label,
                    token=acc.token,
                    token_updated_at=float(getattr(acc, "token_updated_at", 0) or 0.0),
                    in_flight=getattr(acc, "in_flight", None),
                )
            except Exception:
                pass
        return acc

    def release_account(
        self,
        account: AccountRuntime,
        is_muted: bool = False,
        mute_until: Optional[int] = None,
        permanent: bool = False,
        caller_info: Optional[Dict[str, Any]] = None,
        ban_kind_override: Optional[str] = None,
    ):
        """释放账号额度并按需熔断。

        :param is_muted: 上游明确说这个号被禁言了（biz_code:5 / muted）。
        :param mute_until: 上游给出的解禁时间（unix 秒）。给了就用它。
        :param permanent: 永久性失效（token 过期 / 账号被封），不会自动恢复。
        :param caller_info: 调用方审计元数据 (owner / key_name / ip)，用于封禁根因溯源。

        ⚠️ T12 修复的核心：**熔断必须有一个真实的解禁时间**。
        旧实现路由只传 `is_muted=True`、`mute_until` 走默认 None，
        而 `check_mute_expiry` 要求 mute_until 非空才会唤醒 ——
        于是账号一旦被禁言就**永久退出池子**，而 docstring 却承诺"到期自动静默唤醒"。
        多用户长期运行下这表现为"号池悄悄缩水"，直到某天没人可用，而且全程无告警。
        """
        account.last_used = time.time()
        # 记录释放时刻，用于估算"距下次可用还需多久"（429 的 Retry-After 依据）
        account.last_released_at = time.time()
        # 防封号防御机制 3：自适应请求间隔随机抖动（消除机械化调用时间戳指纹）
        base_cooldown = getattr(config_manager.config, "cooldown_seconds", 3.0)
        account.effective_cooldown = max(1.0, base_cooldown + random.uniform(-0.5, 0.8))
        # T12: 单调序号，给 LRU 排序做 tie-break（同刻度内时间戳会相同）
        self._seq_counter += 1
        account.dispatch_seq = self._seq_counter
        if is_muted or permanent:
            account.mute_count += 1
            if permanent:
                account.ban_kind = "permanent"
                # 永久失效给一个很长的停用期（而不是 None）：
                until = int(time.time()) + PERMANENT_INVALID_SECONDS
                status = "INVALID"
            else:
                # 细分封禁类型：若调用方明确传入 backoff（如高危短冷却）或无未来时间则记为 backoff；否则有明确未来时间记为 dated
                if ban_kind_override:
                    account.ban_kind = ban_kind_override
                elif mute_until and mute_until > time.time():
                    account.ban_kind = "dated"
                else:
                    account.ban_kind = "backoff"
                seconds = account.next_mute_seconds(mute_until)
                until = int(time.time()) + seconds
                status = "MUTED"
            account.is_valid = False
            account.status = status
            try:
                from app.engine.drop_probe import record_event
                record_event(
                    "mark_invalid" if permanent else "release_muted",
                    account=account.account_label,
                    token=getattr(account, "token", "") or "",
                    token_updated_at=getattr(account, "token_updated_at", None),
                    error_kind="invalid" if permanent else str(account.ban_kind or "muted"),
                    msg=f"mute_count={account.mute_count} until={until}",
                    extra={"caller": caller_info or {}, "ban_kind": account.ban_kind},
                )
            except Exception:
                pass
            account.mute_until = until
            config_manager.update_account(
                account.account_label,
                is_valid=False,
                status=status,
                mute_until=until,
                mute_count=account.mute_count,
                ban_kind=account.ban_kind,
            )
            if not permanent and not (mute_until and mute_until > time.time()):
                until = int(time.time()) + seconds
                account.mute_until = until
            # 剥离归属判定：
            # 1. 若 permanent=True（账号彻底销号/永久失效），必须剥离；
            # 2. 若上游为官方有期禁言（dated，通常长达 7~10 天）或退避时长超过 3600 秒（长效停用），也必须从 API Key 名下解绑！
            # 3. 若账号状态为 QUARANTINE / INVALID / BANNED，必须立即剥离，严禁僵尸号霸占用户配额导致 429 瘫痪；
            # 4. 仅在超短阶梯退避（如 300s/900s 临时抖动冷却）时才允许保留归属。
            should_disown = (
                permanent
                or (account.ban_kind in ("permanent", "dated"))
                or (status in ("INVALID", "QUARANTINE", "QUARANTINE_MUTED", "QUARANTINE_INVALID", "BANNED"))
                or (until and (until - int(time.time())) > 3600)
            )
            if should_disown:
                try:
                    from app.core.security import disown_account
                    disown_account(account.account_label)
                except Exception as e:
                    print(f"[AccountPool] 剥离账号归属失败（已忽略）: {e}")

                # 触发用户专属账号热替换 (Hot Swap) 补位
                if caller_info and (caller_info.get("key_id") or caller_info.get("owner")):
                    self._trigger_key_hotswap(caller_info)

            # 触发实时系统风控告警，直通管理页面与告警流（附带调用方根因溯源审计）
            try:
                from app.api.admin_routes import record_system_alert
                alert_type = "MUTED" if is_muted else "INVALID"
                caller_str = ""
                if caller_info:
                    c_owner = caller_info.get("owner") or "公共"
                    c_key = caller_info.get("key_name") or "unknown"
                    c_ip = caller_info.get("ip") or "unknown"
                    caller_str = f" [溯源: 用户={c_owner}, Key={c_key}, IP={c_ip}]"
                alert_detail = (
                    f"上游调用遭遇官方封禁/禁言（第 {account.mute_count} 次，类别: {account.ban_kind}），预计解禁时间: "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until)) if until else '未知'}，已自动隔离{caller_str}"
                    if is_muted else
                    f"账号凭证已失效/封号（类别: {account.ban_kind}），已踢入隔离垃圾库{caller_str}"
                )
                record_system_alert(account.account_label, alert_type, alert_detail, until, caller_info=caller_info)
            except Exception as e:
                print(f"[AccountPool] 记录封号系统告警失败（已忽略）: {e}")

            wait_desc = (
                f"{PERMANENT_INVALID_SECONDS // 86400} 天（永久失效，需人工换号）"
                if permanent else
                f"{until - int(time.time())}s → {time.strftime('%H:%M:%S', time.localtime(until))}"
            )
            print(
                f"[CircuitBreaker] 账号 {account.account_label} 触发封号并被剥离归属，进入隔离垃圾库 "
                f"(第 {account.mute_count} 次, {status}, 类别 {account.ban_kind}, 解禁 {wait_desc})",
                flush=True,
            )
            # T5-A：file_id 是**账号级**的，账号被熔断后它的图片缓存条目永远不会再被命中，
            # 纯粹占内存；而且万一将来复用会直接 invalid ref file id。这里顺手清掉。
            try:
                from app.engine.vision_pipeline import VisionPipeline
                n = VisionPipeline.invalidate_account_cache(account.token)
                if n:
                    print(f"[VisionPipeline] 已清理熔断账号 {account.account_label} 的 {n} 条图片缓存")
            except Exception as e:
                print(f"[VisionPipeline] 清理账号图片缓存失败（已忽略）: {e}")
        elif account.mute_count or getattr(account, "ban_kind", None):
            # 一次成功的使用就把退避阶梯和封禁类型清零 —— 否则偶发禁言会让阶梯只升不降，
            # 几个月后所有账号都停在 6 小时退避上
            account.mute_count = 0
            account.ban_kind = None
            try:
                config_manager.update_account(account.account_label, mute_count=0, ban_kind=None)
            except Exception:
                pass


        # 无条件释放一个并发额度。
        # 旧写法 `if lock.locked(): lock.release()` 是错的：Semaphore 下
        # locked() 表示"额度已用尽"，而不是"本请求持有额度"；
        # 若因此漏掉一次 release，信号量会永久少一个额度（容量悄悄缩水，且极难发现）。
        # 多释放则由 except 兜底，不掩盖真实问题。
        # 安全释放并发额度：严格防止 Semaphore 超量充气导致并发闸门被击穿 (in_flight 变负)
        try:
            if account.semaphore._value < MAX_CONCURRENT_PER_ACCOUNT:
                account.semaphore.release()
                print(
                    f"[AccountPool] 账号 {account.account_label} 并发额度已释放 "
                    f"(in_flight={account.in_flight}/{MAX_CONCURRENT_PER_ACCOUNT})",
                    flush=True,
                )
            else:
                print(
                    f"[AccountPool] ⚠️ 账号 {account.account_label} 并发额度已达上限 {MAX_CONCURRENT_PER_ACCOUNT}，"
                    f"安全忽略多余的重复释放",
                    flush=True,
                )
        except Exception as e_rel:
            print(f"[AccountPool] 账号 {account.account_label} 额度释放异常: {e_rel}", flush=True)

    def mark_mfa_required(
        self,
        account: AccountRuntime,
        caller_info: Optional[Dict[str, Any]] = None,
        stage: str = "Failover",
    ):
        """统一处理受信任重登返回 MFA_REQUIRED (biz_code=14)：打标、剥离、告警与释放额度。"""
        account.status = "MFA_REQUIRED"
        account.is_valid = False
        account.ban_kind = "mfa_required"
        try:
            config_manager.update_account(
                account.account_label,
                is_valid=False,
                status="MFA_REQUIRED",
                ban_kind="mfa_required",
            )
        except Exception:
            pass
        try:
            from app.core.security import disown_account
            disown_account(account.account_label)
        except Exception:
            pass
        try:
            from app.api.admin_routes import record_system_alert
            caller_str = ""
            if caller_info:
                caller_str = (
                    f" [溯源: 用户={caller_info.get('owner') or '公共'}, "
                    f"Key={caller_info.get('key_name') or 'unknown'}, "
                    f"IP={caller_info.get('ip') or 'unknown'}]"
                )
            record_system_alert(
                account.account_label,
                "MFA_REQUIRED",
                f"[{stage}] 账号触发官方邮件 MFA 校验 (biz_code=14)，已自动下线隔离{caller_str}",
                None,
                caller_info=caller_info,
            )
        except Exception:
            pass
        try:
            from app.engine.drop_probe import record_event
            record_event(
                "mark_mfa_required",
                account=account.account_label,
                token=getattr(account, "token", "") or "",
                token_updated_at=getattr(account, "token_updated_at", None),
                biz_code=14,
                error_kind="mfa_required",
                msg=f"stage={stage}",
                extra={"caller": caller_info or {}, "relogin_err": getattr(account, "last_relogin_error", None)},
            )
        except Exception:
            pass
        self.release_account(account, is_muted=False, permanent=False, caller_info=caller_info)

    async def recover_or_release_on_auth_error(
        self,
        account: AccountRuntime,
        *,
        caller_info: Optional[Dict[str, Any]] = None,
        stage: str = "请求",
    ) -> bool:
        """
        遇到 40003/40002 等鉴权失败时，先尝试受信任设备纯 HTTP 免密自愈（try_relogin）；
        自愈失败后再按具体原因（MFA_REQUIRED / 40029网络冷却 / 永久退役）精细释放账号。
        """
        relogin_ok = False
        if getattr(account, "password", None):
            try:
                relogin_ok = bool(await account.try_relogin())
            except Exception as e_rel:
                print(f"[AccountPool] [{stage}] 账号 {account.account_label} try_relogin 异常: {e_rel}", flush=True)
                relogin_ok = False
        if relogin_ok:
            return True

        from app.engine import trusted_relogin_engine as _tre
        err_info = getattr(account, "last_relogin_error", None) or {}
        err_biz = err_info.get("biz_code")
        err_kind = err_info.get("kind")
        err_str = str(err_info.get("error") or "")
        if err_biz == 14 or "MFA_VERIFICATION_CODE_REQUIRED" in err_str:
            self.mark_mfa_required(account, caller_info=caller_info, stage=stage)
        elif _tre.is_ip_restricted() or err_biz in (11, 40029) or err_kind == "network_error":
            self.release_account(
                account,
                is_muted=True,
                mute_until=int(time.time()) + 900,
                permanent=False,
                caller_info=caller_info,
            )
        else:
            self.release_account(account, is_muted=True, permanent=True, caller_info=caller_info)
        return False

    def release_with_safety_cooldown(
        self,
        account: AccountRuntime,
        reason: str,
        caller_info: Optional[Dict[str, Any]] = None,
    ):
        """触发 <ds_safety> 刷屏熔断或高频风控阻断时，对当前号施加短时保护性退避（backoff），避免连续打穿至 40003/MFA。"""
        sec = int(getattr(config_manager.config, "upstream_error_cooldown_seconds", 120) or 120)
        sec = max(30, min(900, sec))
        until = int(time.time()) + sec
        print(
            f"[Safety-CircuitBreaker] 🛡️ 账号 {account.account_label} 触发保护性短冷却 {sec}s ({reason})",
            flush=True,
        )
        try:
            from app.api.admin_routes import record_system_alert
            record_system_alert(
                account.account_label,
                "SAFETY_COOLDOWN",
                f"触发高危安全输出熔断 ({reason})，已保护性冷却 {sec}s 防连击",
                until,
                caller_info=caller_info,
            )
        except Exception:
            pass
        self.release_account(
            account,
            is_muted=True,
            mute_until=until,
            permanent=False,
            caller_info=caller_info,
            ban_kind_override="backoff",
        )

        # 触发弹性补位检查（包 try：此处位于 finally 上下文，异常会污染上层清理逻辑）
        # ⚠️ 必须节流：release_account 在每次请求结束时都会执行，
        # 而 _check_replenish 要读配置、遍历账号、必要时触发注册流程——
        # 每次释放都跑会成为 I/O 热点并拖慢释放路径
        # （该问题由金丝雀首次运行暴露：连续短请求下非流式路径拿到 429）。
        # 补位属于维护性动作，每 30 秒检查一次足够。
        now = time.time()
        if now - self._last_replenish_check >= self._REPLENISH_CHECK_INTERVAL:
            self._last_replenish_check = now
            try:
                self._check_replenish()
            except Exception as ex:
                print(f"[ElasticPool] 补位检查异常（已忽略）: {ex}", flush=True)

    def _check_replenish(self):
        """若开启弹性扩容且有效账号低于水位线，自动触发后台洗礼补位。

        修复说明（T12）：本函数原先引用两个**不存在的属性**
        —— `self._lock` 与 `self.accounts`（本类的字段实际叫 `self._accounts`）。
        因此一旦被调用必然抛 AttributeError，`auto_replenish` 配置为开、功能为坏。
        而且调用点未包 try —— 而 release_account 常在 finally 中执行，
        异常会直接冒泡并可能干扰清理。

        另：旧实现在**只有账号被熔断时才检查**（is_muted=True 分支），
        正常释放路径完全不检查，所以即使属性名正确，弹性补位也几乎不会被触发。
        现改为每次释放都检查，使该功能真正生效。
        """
        cfg = config_manager.config
        if not getattr(cfg, "auto_replenish", False):
            return

        min_acc = getattr(cfg, "min_active_accounts", 3)
        # 直接读取实际字段名 self._accounts（原先误写为 self.accounts）
        # 用 is_usable_state（含停用期）：否则"禁言中但被记成 ACTIVE"的号
        # 会被算进有效数，于是补号阈值永远触发不了 —— 池子明明在缩水却看着够用。
        valid_count = sum(
            1 for a in self._accounts.values()
            if a.is_usable_state
        )

        if valid_count < min_acc:
            # 严禁后台私自用旧版静态 DEVICE_IDS 批量注册机产半成品号（必须由独立产号端铸造完整受信任 5 件套 + 真实 B 指纹后推送入库）
            print(
                f"[ElasticPool] ⚠️ 活跃可用账号 ({valid_count}) 低于阈值 ({min_acc})，"
                f"请通过独立受信任产号流水线补充新号（已禁止自动触发旧静态指纹批量注册）",
                flush=True,
            )

    def _trigger_key_hotswap(self, caller_info: Dict[str, Any]):
        """当专享账号遭遇长效封禁被剥离时，异步为该 Key 寻找替补并自动绑定（三阶段无锁 I/O 并发安全热替换）"""
        key_id = caller_info.get("key_id")
        owner = caller_info.get("owner")
        if not key_id and not owner:
            return

        async def _do_swap():
            async with self._hotswap_lock:
                try:
                    from app.core.security import _key_lock, _get_keys, _mark_record, _maybe_flush, account_owner_map
                    # ── 阶段 1：持锁做纯内存快速检视与公共池即时绑定（绝不在持 _key_lock 期间执行 await） ──
                    target_key_id = None
                    target_label_str = ""
                    cold_candidates = []

                    with _key_lock:
                        target_key = None
                        for k in _get_keys():
                            if (key_id and k.get("id") == key_id) or (owner and k.get("owner") == owner):
                                if not k.get("revoked"):
                                    target_key = k
                                    break
                        if not target_key:
                            return

                        target_key_id = target_key.get("id")
                        target_label_str = target_key.get("owner") or target_key.get("name") or str(target_key_id)
                        u_id = caller_info.get("user_id") or target_key.get("user_id")

                        # 严格尊重用户个人池的 auto_replenish 开关，绝不向已关闭自动补位的用户强行热替换公共号
                        if caller_info.get("auto_replenish") is False:
                            print(f"[ElasticPool] 🛑 Key「{target_label_str}」已关闭 auto_replenish=0，禁止自动热替换补位", flush=True)
                            return
                        user_row = None
                        is_vip = bool(caller_info.get("is_vip"))
                        if u_id:
                            try:
                                from app.db.repositories import user_repo
                                user_row = user_repo.get_by_id(u_id)
                                if user_row is not None and not bool(user_row.get("auto_replenish", 1)):
                                    print(f"[ElasticPool] 🛑 用户「{target_label_str}」(auto_replenish=0) 已禁用自动补位，跳过热替换", flush=True)
                                    return
                                if user_row and user_row.get("tier") in ("standard", "pro", "ultra"):
                                    is_vip = True
                            except Exception:
                                pass

                        curr_labels = list(target_key.get("account_labels") or [])
                        active_owned = [
                            lbl for lbl in curr_labels
                            if lbl in self._accounts and self._accounts[lbl].is_usable_state
                        ]
                        if active_owned:
                            return

                        # 1. 从空闲未归属健康账号挑选，并严格遵守 pool_tag 分级隔离（public / vip，跳过 reserve）
                        owners = account_owner_map()
                        candidates = [
                            lbl for lbl, acc in self._accounts.items()
                            if lbl not in owners
                            and lbl not in self._reserving_labels
                            and acc.is_usable_state
                            and str(getattr(acc, "pool_tag", "public") or "public").lower() not in ("disposable", "reserve")
                            and (is_vip or str(getattr(acc, "pool_tag", "public") or "public").lower() != "vip")
                        ]
                        if candidates:
                            chosen = candidates[0]
                            if chosen not in curr_labels:
                                curr_labels.append(chosen)
                                target_key["account_labels"] = curr_labels
                                _mark_record(target_key)
                                _maybe_flush(force=True)
                                if u_id:
                                    try:
                                        from app.db.repositories import user_repo, alert_repo
                                        user_repo.add_to_personal_pool(u_id, chosen)
                                        alert_repo.add_alert(
                                            account=chosen,
                                            alert_type="VIP_HOTSWAP" if is_vip else "POOL_HOTSWAP",
                                            message=f"⚡ 成功为用户「{target_label_str}」自动热替换补位账号: {chosen}"
                                        )
                                    except Exception:
                                        pass
                                print(f"[ElasticPool] ⚡ 成功为 Key「{target_label_str}」从公共池自动热替换补位账号: {chosen}", flush=True)
                            return

                        # 2. 公共池无可用号，从冷库 (INACTIVE_COLD) 中仅挑选经 evaluate_trusted_device 验证的受信任账号唤醒
                        from app.engine.http_auth_engine import evaluate_trusted_device
                        cold_candidates = []
                        raw_accs = config_manager.get_accounts()
                        now_ts = int(time.time())
                        for a in raw_accs:
                            c_lbl = a.get("account_label")
                            c_tag = str(a.get("pool_tag") or "public").lower()
                            c_bk = str(a.get("ban_kind") or "").strip().lower()
                            c_mu = a.get("mute_until")
                            c_muted = isinstance(c_mu, (int, float)) and c_mu > now_ts
                            if (
                                c_lbl
                                and a.get("status") == "INACTIVE_COLD"
                                and c_bk not in ("permanent", "mfa_required", "dated")
                                and not c_muted
                                and c_tag not in ("disposable", "reserve")
                                and (is_vip or c_tag != "vip")
                                and evaluate_trusted_device(a)["is_renewable"]
                                and (a.get("password") or a.get("_password"))
                                and c_lbl not in owners
                                and c_lbl not in self._reserving_labels
                            ):
                                self._reserving_labels.add(c_lbl)
                                cold_candidates.append(dict(a))

                    if not cold_candidates:
                        return

                    chosen_cold = None
                    try:
                        from app.engine.browser_login import quick_trusted_relogin
                        for c in cold_candidates:
                            c_lbl = c["account_label"]
                            c_email = c.get("email") or c.get("_email") or c_lbl
                            c_pwd = c.get("password") or c.get("_password")
                            ok, res = await asyncio.to_thread(quick_trusted_relogin, c_email, c_pwd, account_dict=c)
                            if ok and isinstance(res, dict) and res.get("token") and not res.get("is_muted"):
                                config_manager.update_account(
                                    c_lbl,
                                    token=res["token"],
                                    is_valid=True,
                                    status="ACTIVE",
                                    mute_until=None,
                                    ban_kind=None,
                                    mute_count=0,
                                )
                                self.reload()
                                chosen_cold = c_lbl
                                break
                    finally:
                        for c in cold_candidates:
                            self._reserving_labels.discard(c["account_label"])

                    if chosen_cold:
                        with _key_lock:
                            for k in _get_keys():
                                if (target_key_id and k.get("id") == target_key_id) or (owner and k.get("owner") == owner):
                                    c_list = list(k.get("account_labels") or [])
                                    if chosen_cold not in c_list:
                                        c_list.append(chosen_cold)
                                        k["account_labels"] = c_list
                                        _mark_record(k)
                                        _maybe_flush(force=True)
                                        print(f"[ElasticPool] ⚡ 成功为 Key「{target_label_str}」从受信任冷库唤醒并热替换补位账号: {chosen_cold}", flush=True)
                                    break
                        if u_id:
                            try:
                                from app.db.repositories import user_repo
                                user_repo.add_to_personal_pool(u_id, chosen_cold)
                            except Exception:
                                pass
                except Exception as ex:
                    print(f"[ElasticPool] 热替换补位异常（已忽略）: {ex}", flush=True)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_swap())
        except RuntimeError:
            pass



def allocate_pool_for_user(user_id: str, count: int = 1, force_wake_cold: bool = True) -> List[str]:
    """
    智能优选账号分配给用户个人池（选号算法）：
    1. 优选：在 ACTIVE 状态、is_valid、未禁言、且不在 _owner_map() 里的号中选取（优先 vip/public 标签）；
    2. 次选：若活跃空闲号不足，且 force_wake_cold 为真，从 INACTIVE_COLD 冷库中自动执行极速拟人激活，唤醒崭新零IP账号；
    3. 兜底：若冷库无可唤醒号，从当前可用活跃号中按负载/归属最少原则智能挑选进行保供共享；
    4. 将选取到的账号写入 user_repo 个人池，并在 account_pool 中即时生效。
    """
    from app.db.repositories import user_repo, alert_repo
    from app.config import config_manager

    account_pool.reload()
    owners = _owner_map()
    user_row = user_repo.get_by_id(user_id)
    user_is_vip = bool(user_row and user_row.get("tier") in ("standard", "pro", "ultra"))
    existing_auto = int(user_row.get("auto_replenish", 1)) if user_row else 1
    existing_pool = user_repo.get_personal_pool(user_id)
    needed = max(1, count) - len(existing_pool)
    if needed <= 0:
        return existing_pool

    allocated = list(existing_pool)

    # 1. 仅在未被任何其他用户/Key独占的健康长期号中选（严格排除 disposable / reserve，非 VIP 排除 vip）
    candidates = [
        lbl for lbl, acc in account_pool._accounts.items()
        if lbl not in owners and lbl not in allocated
        and acc.status == "ACTIVE" and acc.is_valid and not acc.is_muted_now()
        and str(getattr(acc, "pool_tag", "public") or "public").lower() not in ("reserve", "disposable")
        and (user_is_vip or str(getattr(acc, "pool_tag", "public") or "public").lower() != "vip")
    ]
    candidates.sort(key=lambda x: 0 if getattr(account_pool._accounts[x], "pool_tag", "public") == "vip" else 1)

    while candidates and len(allocated) < count:
        chosen = candidates.pop(0)
        allocated.append(chosen)

    # 2. 如果还不够，且允许唤醒冷库（仅挑选具备 5 件套 + B 指纹 + 有效 expire_at 的未归属冷库账号）
    if len(allocated) < count and force_wake_cold:
        try:
            from app.engine.browser_login import quick_trusted_relogin
            from app.engine.http_auth_engine import evaluate_trusted_device
            from app.db.repositories import account_repo
            raw_accs = config_manager.get_accounts()
            cold_candidates = [
                a for a in raw_accs
                if a.get("status") == "INACTIVE_COLD"
                and a.get("account_label") not in allocated
                and a.get("account_label") not in owners
                and str(a.get("pool_tag") or "public").lower() not in ("disposable", "reserve")
                and (user_is_vip or str(a.get("pool_tag") or "public").lower() != "vip")
                and evaluate_trusted_device(a)["is_renewable"]
                and (a.get("password") or a.get("_password"))
            ]
            for c in cold_candidates:
                if len(allocated) >= count:
                    break
                c_lbl = c["account_label"]
                c_email = c.get("email") or c.get("_email") or c_lbl
                c_pwd = c.get("password") or c.get("_password")
                ok, res = quick_trusted_relogin(c_email, c_pwd, account_dict=c)
                if ok and isinstance(res, dict) and res.get("token") and not res.get("is_muted"):
                    new_token = res["token"]
                    config_manager.update_account(
                        c_lbl,
                        token=new_token,
                        is_valid=True,
                        status="ACTIVE",
                        mute_until=None,
                        ban_kind=None,
                        mute_count=0,
                    )
                    account_repo.update_runtime_state(
                        c_lbl,
                        token=new_token,
                        status="ACTIVE",
                        is_valid=True,
                        mute_until=None,
                        ban_kind=None,
                        mute_count=0,
                    )
                    allocated.append(c_lbl)
                    try:
                        alert_repo.add_alert(
                            account=c_lbl,
                            alert_type="WAKE_COLD_ALLOCATE",
                            message=f"⚡ 成功为用户「{user_id}」个人池自动从冷库唤醒 5 件套受信任账号: {c_lbl}"
                        )
                    except Exception:
                        pass
                    print(f"[allocate_pool_for_user] ⚡ 成功为用户「{user_id}」从冷库唤醒 5 件套账号: {c_lbl}", flush=True)
        except Exception as ex:
            print(f"[allocate_pool_for_user] 唤醒冷库异常: {ex}", flush=True)

    # 不再允许跨用户偷抢其他用户已独占的活跃号；保留用户既有 auto_replenish 配置
    user_repo.set_personal_pool(user_id, allocated, auto_replenish=existing_auto)
    account_pool.reload()
    return allocated


account_pool = AccountPool()

