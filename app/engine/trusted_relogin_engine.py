# -*- coding: utf-8 -*-
"""
app/engine/trusted_relogin_engine.py
-------------------------------------------------------------------------------
纯 HTTP 免密直登与续杯引擎适配层 (Zero-Chromium)

彻底废除服务端 Playwright / Chromium / Xvfb / 本地 _LAST_ATTEMPT 频控节流与
旧浏览器报错缓存字典，所有免密重登与续杯请求统一桥接至
`app.engine.http_auth_engine.renew_account_token` 执行纯 HTTP 协议认证。
-------------------------------------------------------------------------------
"""

from __future__ import annotations

import os
import sys
import time
import threading
from typing import Tuple, Dict, Any, Optional, List

from app.engine import http_auth_engine
from app.engine.http_auth_engine import extract_shumei_device_id, evaluate_trusted_device

_RENEWAL_LOCK = threading.Lock()
_RECENT_RENEWALS: Dict[str, Dict[str, Any]] = {}
_RENEWAL_TTL_SECONDS = float(os.environ.get("DS_HTTP_RENEWAL_TTL", "86400"))

_RELOGIN_LOCKS_GUARD = threading.Lock()
_RELOGIN_LOCKS: Dict[str, threading.RLock] = {}


def get_account_relogin_lock(account_key: str) -> threading.RLock:
    """获取单账号重登/续杯互斥锁，防止并发请求与后台保活对同一账号同时发起登录。"""
    key = str(account_key or "").strip().lower() or "__default__"
    with _RELOGIN_LOCKS_GUARD:
        lock = _RELOGIN_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _RELOGIN_LOCKS[key] = lock
        return lock


def is_ip_restricted() -> bool:
    """纯 HTTP 模式下不再启用全局过热锁，恒返回 False。"""
    return False


def ip_restrict_remaining() -> int:
    """纯 HTTP 模式下不再启用全局冷却，恒返回 0。"""
    return 0


def record_http_renewal(
    account_key: str,
    mode: Optional[str] = None,
    cost: Optional[float] = None,
) -> None:
    """记录账号最近一次纯 HTTP 无感续杯时间，供管理面板展示“已续杯”状态。"""
    key = (account_key or "").strip()
    if not key:
        return
    with _RENEWAL_LOCK:
        _RECENT_RENEWALS[key] = {
            "at": time.time(),
            "mode": mode or "http_renewal",
            "cost": cost,
        }


def clear_http_renewal(account_key: str) -> None:
    """清除账号的近期续杯标记（如账号退入冷库或失效隔离时）。"""
    key = (account_key or "").strip()
    if not key:
        return
    with _RENEWAL_LOCK:
        _RECENT_RENEWALS.pop(key, None)


def get_recent_http_renewal(
    account_key: str,
    max_age_s: float = _RENEWAL_TTL_SECONDS,
) -> Optional[Dict[str, Any]]:
    """查询账号最近是否完成过纯 HTTP 续杯。"""
    key = (account_key or "").strip()
    if not key:
        return None
    with _RENEWAL_LOCK:
        rec = _RECENT_RENEWALS.get(key)
        if not rec:
            return None
        age_s = time.time() - float(rec.get("at") or 0)
        if age_s > max_age_s:
            _RECENT_RENEWALS.pop(key, None)
            return None
        return {**rec, "age_s": int(max(0.0, age_s))}


def _egress_candidates(proxy: Optional[str]) -> List[Optional[str]]:
    """构造登录出口列表；经统一 proxy_manager 优先使用指定代理 -> 健康备用池 -> 受控直连兜底。"""
    try:
        from app.engine.proxy_manager import resolve_egress_candidates
        cands = resolve_egress_candidates(primary_proxy=proxy, for_relogin=True)
        return cands[:5] if cands else [None]
    except Exception:
        return [proxy] if proxy else [None]


def _lookup_config_account(email_or_label: str) -> Optional[Dict[str, Any]]:
    """从 config_manager 内存号池中按 label 或 email 检索账号完整字典。"""
    key = (email_or_label or "").strip()
    if not key:
        return None
    try:
        from app.config import config_manager
        accounts = config_manager.get_accounts()
        for acc in accounts:
            if (
                acc.get("account_label") == key
                or acc.get("email") == key
                or acc.get("_email") == key
            ):
                return dict(acc)
        key_low = key.lower()
        for acc in accounts:
            if (
                (acc.get("account_label") or "").strip().lower() == key_low
                or (acc.get("email") or "").strip().lower() == key_low
                or (acc.get("_email") or "").strip().lower() == key_low
            ):
                return dict(acc)
    except Exception:
        pass
    return None


def _lookup_repo_account(email_or_label: str) -> Optional[Dict[str, Any]]:
    """从 SQLite 仓储中补充查询账号资产（支持 label 或 email 匹配）。"""
    key = (email_or_label or "").strip()
    if not key:
        return None
    try:
        from app.db.repositories import account_repo
        row = account_repo.get(key)
        if row:
            return dict(row)
        from app.db.database import db
        db_row = db.fetchone(
            "SELECT * FROM accounts WHERE email = ? OR account_label = ? LIMIT 1",
            (key, key),
        )
        if db_row:
            return dict(db_row)
    except Exception:
        pass
    return None


def resolve_account_payload(
    email: str,
    password: str = "",
    account_dict: Optional[Dict[str, Any]] = None,
    mfa_assets: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    合并调用方传参、config_manager 内存资产与 SQLite 凭据库，返回可用于
    `http_auth_engine.renew_account_token` 的完整账号字典及匹配到的 `account_label`。
    """
    merged: Dict[str, Any] = {}
    matched_label: Optional[str] = None

    cfg_acc = _lookup_config_account(email)
    if cfg_acc:
        merged.update(cfg_acc)
        matched_label = cfg_acc.get("account_label")

    # 仅在缺失关键资产时回查 SQLite，避免多余磁盘查询
    if not (merged.get("credential_id") and merged.get("mfa_private_key") and merged.get("device_fingerprint")):
        repo_acc = _lookup_repo_account(matched_label or email)
        if repo_acc:
            for k, v in repo_acc.items():
                if v not in (None, "") and merged.get(k) in (None, ""):
                    merged[k] = v
            if not matched_label:
                matched_label = repo_acc.get("account_label")

    if isinstance(account_dict, dict):
        for k, v in account_dict.items():
            if v not in (None, ""):
                merged[k] = v
        if not matched_label and account_dict.get("account_label"):
            matched_label = account_dict.get("account_label")

    if isinstance(mfa_assets, dict):
        alias_map = {
            "credential_id": "credential_id",
            "mfa_private_key": "mfa_private_key",
            "priv_b64": "mfa_private_key",
            "mfa_public_key": "mfa_public_key",
            "pub_b64": "mfa_public_key",
            "mfa_key_version": "mfa_key_version",
            "key_version": "mfa_key_version",
            "device_id": "device_id",
            "device_fingerprint": "device_fingerprint",
            "db_password": "password",
            "password": "password",
        }
        for src_k, dst_k in alias_map.items():
            val = mfa_assets.get(src_k)
            if val not in (None, "") and merged.get(dst_k) in (None, ""):
                merged[dst_k] = val

    real_email = merged.get("_email") or merged.get("email") or email
    real_password = password or merged.get("password") or merged.get("_password") or ""
    if real_email:
        merged["email"] = real_email
    if real_password:
        merged["password"] = real_password

    return merged, matched_label


def _persist_relogin_result(
    target_label: Optional[str],
    email: str,
    res: Dict[str, Any],
) -> Tuple[bool, Optional[int], str]:
    """将纯 HTTP 续杯所得 Token 与禁言态持久化到 config_manager 与 SQLite。"""
    tok = res.get("token") or ""
    is_muted = bool(res.get("is_muted", False))
    raw_mute_until = res.get("mute_until")
    mute_until: Optional[int] = None
    if is_muted and raw_mute_until is not None:
        try:
            from app.engine.deepseek_client import _parse_mute_until
            mute_until = _parse_mute_until(raw_mute_until)
        except Exception:
            mute_until = None

    new_status = "MUTED" if is_muted else "ACTIVE"
    is_valid = not is_muted
    now_ts = int(time.time())

    renewal_key = target_label or email
    record_http_renewal(renewal_key, mode=res.get("mode"), cost=res.get("cost"))
    if email and email != renewal_key:
        record_http_renewal(email, mode=res.get("mode"), cost=res.get("cost"))
    try:
        from app.engine.drop_probe import register_account_token, record_event
        register_account_token(renewal_key, tok, now_ts)
        record_event(
            "relogin_ok",
            account=renewal_key,
            token=tok,
            token_updated_at=now_ts,
            msg=f"mode={res.get('mode')} status={new_status} cost={res.get('cost')}",
        )
    except Exception:
        pass

    if target_label:
        updated_cfg = False
        try:
            from app.config import config_manager
            if any(a.get("account_label") == target_label for a in config_manager.get_accounts()):
                config_manager.update_account(
                    target_label,
                    token=tok,
                    is_valid=is_valid,
                    status=new_status,
                    mute_until=mute_until,
                    mute_count=0,
                    ban_kind="dated" if is_muted else None,
                    updated_at=now_ts,
                    token_updated_at=now_ts,
                )
                updated_cfg = True
        except Exception as e_cfg:
            print(f"[PureHttpRelogin] 更新 config_manager 异常: {e_cfg}", flush=True)
        if not updated_cfg:
            try:
                from app.db.repositories import account_repo
                account_repo.update_runtime_state(
                    target_label,
                    token=tok,
                    is_valid=is_valid,
                    status=new_status,
                    mute_until=mute_until,
                    mute_count=0,
                    ban_kind="dated" if is_muted else None,
                    token_updated_at=now_ts,
                )
            except Exception as e_db:
                print(f"[PureHttpRelogin] 更新 SQLite 异常: {e_db}", flush=True)

    return is_muted, mute_until, new_status


def quick_trusted_login(
    email: str,
    password: str = "",
    timeout: float = 20.0,
    proxy: Optional[str] = None,
    account_dict: Optional[Dict[str, Any]] = None,
    mfa_assets: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """纯 HTTP 免密续杯/直登入口（委托至 quick_trusted_relogin，零浏览器依赖）。"""
    from app.engine.browser_login import quick_trusted_relogin

    merged_dict = account_dict
    if mfa_assets:
        merged_dict, _ = resolve_account_payload(
            email=email,
            password=password,
            account_dict=account_dict,
            mfa_assets=mfa_assets,
        )
    return quick_trusted_relogin(
        email=email,
        password=password,
        timeout=timeout,
        account_dict=merged_dict,
        proxy=proxy,
    )


def get_relogin_health() -> Dict[str, Any]:
    """
    返回纯 HTTP 鉴权引擎健康态。
    `ip.restricted` 恒为 False，不再输出旧浏览器故障字典与过热告警。
    """
    accounts_health: Dict[str, Any] = {}
    try:
        from app.config import config_manager
        for acc in config_manager.get_accounts():
            lbl = acc.get("account_label") or acc.get("email") or ""
            if not lbl:
                continue
            renewal = (
                get_recent_http_renewal(lbl)
                or get_recent_http_renewal(acc.get("_email") or "")
                or get_recent_http_renewal(acc.get("email") or "")
            )
            if renewal:
                accounts_health[lbl] = {
                    "kind": "RENEWED",
                    "label": "已续杯",
                    "level": "info",
                    "desc": "纯HTTP无感刷新",
                    "at": int(renewal.get("at") or 0),
                    "age_s": int(renewal.get("age_s") or 0),
                    "biz_code": 0,
                    "error": "",
                }
                continue

            eval_res = evaluate_trusted_device(acc)
            if eval_res["is_renewable"]:
                accounts_health[lbl] = {
                    "kind": "HTTP_READY",
                    "label": "HTTP续杯就绪",
                    "level": "success",
                    "desc": "具备5件套、真实数美指纹与有效到期时间，支持纯HTTP无感续杯",
                    "at": 0,
                    "age_s": None,
                    "biz_code": None,
                    "error": "",
                }
            else:
                accounts_health[lbl] = {
                    "kind": "LEGACY_ONETIME",
                    "label": "一次性账号",
                    "level": "idle",
                    "desc": "一次性或未确权账号（缺少受信任设备到期时间/数美指纹或已锁定）",
                    "at": 0,
                    "age_s": None,
                    "biz_code": None,
                    "error": "",
                }
    except Exception:
        pass

    return {
        "ip": {
            "restricted": False,
            "remaining_s": 0,
            "cooldown_s": 0,
            "min_interval_s": 0,
            "label": "正常",
        },
        "egress": {},
        "accounts": accounts_health,
        "legend": {
            "RENEWED": {"label": "已续杯", "level": "info", "desc": "纯HTTP无感刷新"},
            "HTTP_READY": {"label": "HTTP续杯就绪", "level": "success", "desc": "具备5件套与真实数美指纹"},
            "LEGACY_ONETIME": {"label": "一次性账号", "level": "idle", "desc": "旧版一次性账号"},
        },
    }


if __name__ == "__main__":
    test_email = sys.argv[1] if len(sys.argv) > 1 else ""
    if not test_email:
        print("usage: python trusted_relogin_engine.py <email_or_label> [password]")
        sys.exit(1)
    test_pwd = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"[*] 正在测试账号 {test_email} 纯 HTTP 免码续杯...")
    ok, res = quick_trusted_login(test_email, test_pwd)
    print("结果:", ok, res)
