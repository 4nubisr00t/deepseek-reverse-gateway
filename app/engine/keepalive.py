# -*- coding: utf-8 -*-
"""
app/engine/keepalive.py
================================================================================
纯 HTTP 双轨会话守护与滑动续杯引擎 (v3.0.0-phase1)
================================================================================
核心架构:
  彻底摒弃 Chromium 浏览器救援（原 v1 模式），基于纯 HTTP 协议层实现「双轨节律」:

  【第一轨：会话心跳探活 (Heartbeat Probe)】
    · 周期: 每 1200 秒（20 分钟）
    · 动作: 轻量 GET /api/v0/users/current（携带 Bearer Token 与可选 Cookie）
    · 目的: 向官方证明会话处于高频活跃状态，锁定服务端免密受信任温热通道
    · 判定:
        code == 0   -> 记录 alive_ok
        code == 40003 -> 标记账号 INVALID (expired)，优雅退出，绝不拉起浏览器

  【第二轨：主动滑动续杯 (Proactive Sliding Renewal)】
    · 周期: 当账号会话健康且 Token 连续做功达到阈值 (默认 7 小时 / 25200 秒)
    · 动作: 调用 http_auth_engine.renew_account_token(account)
    · 效果: 在 40003 发生前主动发起纯 HTTP 登录，官方 0 拦截秒级下发新 Token
    · 热更新:
        1. 原子更新 SQLite accounts 表 (token, is_valid=1, updated_at=now)
        2. 通知 config_manager 热更新内存账号态
        3. 动态更新 account_pool.AccountRuntime 运行实例
    · 错峰保护: 账号之间严格保持错峰间隔 (默认 60 秒)，防止突发批量发包

安全与约束:
  · 严格零 Chromium 依赖，严禁导入 playwright;
  · 严禁死后重登，只对健康存活会话做主动滑动续签;
  · 单实例串行异步驱动，异常完全捕获并持久化审计日志。
================================================================================
"""

from __future__ import annotations

import os
import time
import json
import asyncio
import sqlite3
from typing import Dict, Any, Optional, List

from app.engine.http_auth_engine import (
    renew_account_token,
    extract_shumei_device_id,
    evaluate_trusted_device,
    extract_token_issued_at,
)

_DB_PATH = os.environ.get("DS_DB_PATH") or os.environ.get("DSG_DB_FILE") or "/opt/ds-gateway/data/gateway.db"
_LOG_DIR = os.environ.get("DS_KEEPALIVE_LOG_DIR", "/opt/ds-gateway/logs")

_INTERVAL = float(os.environ.get("DS_KEEPALIVE_INTERVAL", "1200"))       # 探活心跳周期 (默认 20 分钟)
_STAGGER = float(os.environ.get("DS_KEEPALIVE_STAGGER", "60"))           # 账号间错峰间隔 (默认 60 秒)
_RENEW_THRESHOLD_SEC = float(os.environ.get("DS_RENEW_THRESHOLD_SEC", "0"))  # 主动盲目换签开关 (默认 0 关闭；仅通过心跳+40003 被动精准自愈，避免无故 /login 触发 MFA)
_ENABLED = os.environ.get("DS_KEEPALIVE_ENABLED", "1") not in ("0", "false", "False", "")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TASK: Optional[asyncio.Task] = None
_STATS: Dict[str, Any] = {
    "last_run": 0,
    "last_ok": 0,
    "last_fail": 0,
    "last_alive_ok": 0,
    "last_renewed": 0,
    "last_detail": [],
}


def _log(rec: Dict[str, Any]) -> None:
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        path = os.path.join(_LOG_DIR, "keepalive_%s.jsonl" % time.strftime("%Y%m%d"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(f"[KeepAlive] {json.dumps(rec, ensure_ascii=False)[:400]}", flush=True)


def list_targets() -> List[Dict[str, Any]]:
    """
    读取所有 ACTIVE 状态的就绪账号全量凭据信息，用于双轨守护
    """
    try:
        c = sqlite3.connect(_DB_PATH, timeout=5)
        c.row_factory = sqlite3.Row
        cursor = c.cursor()
        rows = cursor.execute(
            """
            SELECT account_label, email, password, token, cookie, proxy,
                   pool_tag, expire_at, is_valid,
                   credential_id, mfa_private_key, mfa_public_key,
                   device_id, device_fingerprint, profile_path, updated_at, token_updated_at
            FROM accounts
            WHERE status = 'ACTIVE'
            """
        ).fetchall()
        targets = [dict(r) for r in rows]
        c.close()
        return targets
    except Exception as e:
        _log({"kind": "db_error", "error": str(e)})
        return []


def _probe_http(email: str, token: str, cookie: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    """第一轨：GET /api/v0/users/current 探活，只读，使用与协议端匹配的 TLS 指纹。"""
    if not token or len(token) < 20:
        return {"alive": False, "code": None, "err": "empty_token"}

    from app.engine.deepseek_client import DeepSeekClient
    plat = DeepSeekClient.get_client_platform()
    raw_headers = DeepSeekClient.build_headers(
        token, cookie, platform=plat, include_content_type=False
    )
    url = "https://chat.deepseek.com/api/v0/users/current"

    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        cffi_requests = None

    if cffi_requests is not None:
        from app.engine.proxy_manager import execute_with_proxy_failover
        try:
            r = execute_with_proxy_failover(
                lambda p_url: cffi_requests.get(
                    url,
                    headers=raw_headers,
                    impersonate=DeepSeekClient.get_impersonate(plat),
                    proxies={"http": p_url, "https": p_url} if p_url else None,
                    timeout=15,
                ),
                primary_proxy=proxy,
                account_label=email,
                context="KeepAlive探活",
            )
            try:
                data = r.json() if r.content else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            code = data.get("code")
            if r.status_code == 200:
                return {"alive": code == 0, "code": code, "err": None}
            return {"alive": False, "code": code, "err": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"alive": False, "code": None, "err": f"{type(e).__name__}: {e}"}

    import urllib.request
    import urllib.error
    headers = {k.title(): v for k, v in raw_headers.items()}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
        data = json.loads(body) if body else {}
        code = data.get("code")
        return {"alive": code == 0, "code": code, "err": None}
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b"{}")
        except Exception:
            data = {}
        return {"alive": False, "code": data.get("code"), "err": f"HTTP {e.code}"}
    except Exception as e:
        return {"alive": False, "code": None, "err": f"{type(e).__name__}: {e}"}


async def _probe_with_retry(
    email: str,
    token: str,
    cookie: str,
    retries: int = 1,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """探活轻量重试，降低偶发网络抖动误判"""
    def _call():
        return _probe_http(email, token, cookie, proxy=proxy) if proxy else _probe_http(email, token, cookie)

    probe = await asyncio.to_thread(_call)
    if probe.get("alive"):
        return probe
    for i in range(retries):
        await asyncio.sleep(3)
        probe = await asyncio.to_thread(_call)
        if probe.get("alive"):
            probe["retried"] = i + 1
            return probe
    return probe


def _persist_token(email: str, new_token: str) -> None:
    """热更新新 Token 至 DB、配置中心与内存运行池"""
    now_ts = int(time.time())
    try:
        c = sqlite3.connect(_DB_PATH, timeout=5)
        c.execute(
            "UPDATE accounts SET token=?, is_valid=1, updated_at=?, token_updated_at=? WHERE email=? OR account_label=?",
            (new_token, now_ts, now_ts, email, email)
        )
        c.commit()
        c.close()
    except Exception as e:
        _log({"kind": "persist_error", "email": email, "error": str(e)})

    # 1. 同步配置管理器内存
    try:
        from app.config import config_manager
        config_manager.update_account(email, token=new_token, is_valid=True, updated_at=now_ts, token_updated_at=now_ts)
    except Exception:
        pass

    # 2. 同步运行实例内存池
    try:
        from app.engine.account_pool import account_pool
        store = getattr(account_pool, "accounts", None) or getattr(account_pool, "_accounts", None)
        for rt in (store.values() if isinstance(store, dict) else (store or [])):
            if getattr(rt, "account_label", "") == email or getattr(rt, "email", "") == email:
                rt.token = new_token
                rt.is_valid = True
                rt.token_updated_at = now_ts
                break
    except Exception:
        pass


def _mark_account_status_db(
    email: str,
    status: str,
    ban_kind: str,
    now: float,
    mute_until: Optional[int] = None,
) -> None:
    """持久化更新账号失效/MFA/禁言状态到 SQLite、config_manager 与运行池。"""
    try:
        c = sqlite3.connect(_DB_PATH, timeout=5)
        if mute_until is not None:
            c.execute(
                "UPDATE accounts SET is_valid = 0, status = ?, ban_kind = ?, mute_until = ?, updated_at = ? "
                "WHERE email = ? OR account_label = ?",
                (status, ban_kind, int(mute_until), int(now), email, email),
            )
        else:
            c.execute(
                "UPDATE accounts SET is_valid = 0, status = ?, ban_kind = ?, updated_at = ? "
                "WHERE email = ? OR account_label = ?",
                (status, ban_kind, int(now), email, email),
            )
        c.commit()
        c.close()
    except Exception as e_db:
        _log({"kind": "persist_error", "email": email, "error": str(e_db)})

    try:
        from app.config import config_manager
        kw: Dict[str, Any] = {"is_valid": False, "status": status, "ban_kind": ban_kind}
        if mute_until is not None:
            kw["mute_until"] = int(mute_until)
        config_manager.update_account(email, **kw)
    except Exception:
        pass

    try:
        from app.engine.account_pool import account_pool
        store = getattr(account_pool, "accounts", None) or getattr(account_pool, "_accounts", None)
        for rt in (store.values() if isinstance(store, dict) else (store or [])):
            if getattr(rt, "account_label", "") == email or getattr(rt, "email", "") == email:
                rt.is_valid = False
                rt.status = status
                rt.ban_kind = ban_kind
                if mute_until is not None:
                    rt.mute_until = int(mute_until)
                break
    except Exception:
        pass


def _apply_pool_quota_cooldown(email: str, reset_at: Optional[int], reason: str) -> None:
    """若 chat/status 报告额度已耗尽，通知运行池对账号施加配额冷却，直至 reset_at。"""
    try:
        from app.engine.account_pool import account_pool
        store = getattr(account_pool, "accounts", None) or getattr(account_pool, "_accounts", None)
        for rt in (store.values() if isinstance(store, dict) else (store or [])):
            if getattr(rt, "account_label", "") == email or getattr(rt, "email", "") == email:
                rt.apply_quota_cooldown(reset_at=reset_at, reason=reason)
                break
    except Exception:
        pass


async def _run_guarded_renew(acc: Dict[str, Any], email: str, stale_token: str = "", force: bool = False):
    """单账号互斥锁保护下的纯 HTTP 免密续杯（兼顾主动滑动续杯与 40003 被动救援）。"""
    relogin_proxy = acc.get("proxy") or None
    if not relogin_proxy:
        try:
            from app.engine.browser_login import _resolve_relogin_proxy
            relogin_proxy = _resolve_relogin_proxy(email)
        except Exception:
            relogin_proxy = None

    from app.engine.deepseek_client import DeepSeekClient
    plat = DeepSeekClient.get_client_platform()

    def _guarded():
        from app.engine.trusted_relogin_engine import get_account_relogin_lock
        lock_key = email or acc.get("account_label") or ""
        with get_account_relogin_lock(lock_key):
            try:
                c = sqlite3.connect(_DB_PATH, timeout=5)
                c.row_factory = sqlite3.Row
                row = c.execute(
                    "SELECT status, is_valid, token, token_updated_at FROM accounts "
                    "WHERE email = ? OR account_label = ? LIMIT 1",
                    (email, acc.get("account_label") or email),
                ).fetchone()
                c.close()
                if row is not None:
                    if row["status"] != "ACTIVE" or not row["is_valid"]:
                        return "skipped_inactive", None
                    latest_tok = str(row["token"] or "")
                    latest_tok_ts = float(row["token_updated_at"] or 0)
                    if force:
                        if latest_tok and stale_token and latest_tok != stale_token:
                            return "skipped_already_fresh", latest_tok_ts
                    elif latest_tok_ts > 0 and (time.time() - latest_tok_ts) < _RENEW_THRESHOLD_SEC:
                        return "skipped_already_fresh", latest_tok_ts
            except Exception:
                pass
            from app.engine.proxy_manager import resolve_egress_candidates, proxy_health_registry
            cands = resolve_egress_candidates(
                primary_proxy=relogin_proxy,
                account_label=lock_key,
                for_relogin=True,
            )
            last_res: Any = None
            for idx, cand_proxy in enumerate(cands):
                ok, res = renew_account_token(acc, platform=plat, proxy_url=cand_proxy)
                last_res = (ok, res)
                if ok:
                    if cand_proxy:
                        proxy_health_registry.mark_proxy_healthy(cand_proxy)
                    return ok, res
                r_kind = res.get("kind") if isinstance(res, dict) else None
                if r_kind != "network_error" or idx >= len(cands) - 1:
                    return ok, res
                if cand_proxy:
                    proxy_health_registry.mark_proxy_suspect(
                        cand_proxy,
                        cooldown_sec=120.0,
                        reason="keepalive_renew_network_error",
                    )
            return last_res if last_res is not None else (False, {"error": "no_egress_candidate", "kind": "network_error"})

    return await asyncio.to_thread(_guarded)


async def run_once() -> Dict[str, Any]:
    targets = list_targets()
    if not targets:
        _log({"kind": "run", "targets": 0, "note": "没有需要保活的 ACTIVE 账号"})
        return _STATS

    ok_cnt = fail_cnt = alive_ok_cnt = renewed_cnt = 0
    detail = []
    now = time.time()

    for idx, acc in enumerate(targets):
        email = acc.get("email") or acc.get("account_label") or ""
        token = acc.get("token") or ""
        cookie = acc.get("cookie") or ""
        token_updated_at = float(acc.get("token_updated_at") or 0)
        updated_at = float(acc.get("updated_at") or 0)
        if token_updated_at > 0:
            effective_tok_ts = token_updated_at
        else:
            jwt_iat = extract_token_issued_at(token, now_ts=int(now))
            effective_tok_ts = float(jwt_iat) if jwt_iat else updated_at

        if idx > 0 and _STAGGER > 0:
            await asyncio.sleep(_STAGGER)

        eval_res = evaluate_trusted_device(acc, now_ts=now)
        has_credentials = bool(acc.get("password") and eval_res["is_renewable"])
        try:
            from app.engine.account_pool import get_effective_proxy
            acc_proxy = get_effective_proxy(acc)
        except Exception:
            acc_proxy = acc.get("proxy") or None

        # -------------------------------------------------------------
        # 轨 ①：纯 HTTP 心跳探活
        # -------------------------------------------------------------
        probe = await (
            _probe_with_retry(email, token, cookie, proxy=acc_proxy)
            if acc_proxy
            else _probe_with_retry(email, token, cookie)
        )
        if not probe.get("alive"):
            probe_code = probe.get("code")
            # 受信任非耗材账号遇 40001/40002/40003 时，先走纯 HTTP 免密续签救援，绝不直接判死
            if probe_code in (40001, 40002, 40003) and has_credentials:
                renew_ok, renew_res = await _run_guarded_renew(acc, email, stale_token=token, force=True)
                if renew_ok == "skipped_already_fresh":
                    ok_cnt += 1
                    detail.append({"email": email, "ok": True, "action": "40003_already_refreshed"})
                    continue
                if renew_ok == "skipped_inactive":
                    detail.append({"email": email, "ok": True, "action": "skipped_inactive"})
                    continue
                if renew_ok and isinstance(renew_res, dict) and renew_res.get("token"):
                    new_tok = renew_res["token"]
                    renewed_cnt += 1
                    ok_cnt += 1
                    _persist_token(email, new_tok)
                    try:
                        from app.engine.trusted_relogin_engine import record_http_renewal
                        target_key = acc.get("account_label") or email
                        record_http_renewal(target_key, mode=renew_res.get("mode", ""), cost=float(renew_res.get("cost") or 0.0))
                        if target_key != email:
                            record_http_renewal(email, mode=renew_res.get("mode", ""), cost=float(renew_res.get("cost") or 0.0))
                    except Exception:
                        pass
                    _log({
                        "kind": "token_40003_recovered",
                        "email": email,
                        "mode": renew_res.get("mode"),
                        "cost": renew_res.get("cost"),
                    })
                    detail.append({"email": email, "ok": True, "action": "40003_recovered", "cost": renew_res.get("cost")})
                    continue

                r_biz = renew_res.get("biz_code") if isinstance(renew_res, dict) else None
                r_kind = renew_res.get("kind") if isinstance(renew_res, dict) else None
                if r_biz == 14:
                    fail_cnt += 1
                    _mark_account_status_db(email, "MFA_REQUIRED", "mfa_required", now)
                    _log({"kind": "probe_failed_mfa_required", "email": email, "code": probe_code, "biz_code": 14})
                    try:
                        from app.engine.drop_probe import record_event
                        record_event(
                            "keepalive_mfa_required",
                            account=email,
                            token=token,
                            token_updated_at=acc.get("token_updated_at"),
                            proxy_primary=acc_proxy,
                            code=probe_code,
                            biz_code=14,
                            error_kind="mfa_required",
                            msg=str(renew_res),
                        )
                    except Exception:
                        pass
                    detail.append({"email": email, "ok": False, "via": "http_probe_renew", "err": "MFA_REQUIRED"})
                    continue
                if r_biz in (11, 40029) or r_kind == "network_error":
                    fail_cnt += 1
                    _log({
                        "kind": "probe_40003_renew_deferred",
                        "email": email,
                        "code": probe_code,
                        "biz_code": r_biz,
                        "r_kind": r_kind,
                    })
                    detail.append({"email": email, "ok": False, "via": "http_probe_renew_deferred", "err": str(r_biz or r_kind)})
                    continue

            # 纯网络抖动/超时（未返回任何业务鉴权码）时不误判账号死亡
            if probe_code is None:
                fail_cnt += 1
                _log({
                    "kind": "probe_network_error",
                    "email": email,
                    "err": probe.get("err"),
                })
                detail.append({"email": email, "ok": False, "via": "http_probe_network", "err": probe.get("err")})
                continue

            # 确认非可自愈/已明确拒绝：标记退役，绝不起浏览器
            fail_cnt += 1
            _log({
                "kind": "probe_failed", "email": email,
                "code": probe_code, "err": probe.get("err"),
                "action": "deactivate_no_browser"
            })
            _mark_account_status_db(email, "INVALID", "expired", now)
            detail.append({"email": email, "ok": False, "via": "http_probe", "err": probe.get("err")})
            continue

        # 探活成功
        alive_ok_cnt += 1
        ok_cnt += 1
        _log({"kind": "alive_ok", "email": email, "via": "http", "retried": probe.get("retried", 0)})

        # 检查新版 /api/v0/chat/status 的 mute 与 quotas 耗尽情况
        try:
            from app.engine.deepseek_client import DeepSeekClient
            st = await asyncio.to_thread(DeepSeekClient.fetch_chat_status, token, cookie, acc_proxy)
            if isinstance(st, dict) and st.get("ok"):
                if st.get("is_muted"):
                    mu = st.get("mute_until")
                    _mark_account_status_db(email, "MUTED", "dated" if mu else "backoff", now, mute_until=mu)
                    _log({"kind": "chat_status_muted", "email": email, "mute_until": mu})
                    detail.append({"email": email, "ok": True, "action": "chat_status_muted", "mute_until": mu})
                    continue
                if st.get("quota_exhausted"):
                    ra = st.get("reset_at")
                    _apply_pool_quota_cooldown(email, ra, reason="keepalive_chat_status_quota")
                    _log({"kind": "chat_status_quota_exhausted", "email": email, "reset_at": ra})
        except Exception:
            pass

        # -------------------------------------------------------------
        # 轨 ②：主动滑动换签判定 (Proactive Renewal)
        # -------------------------------------------------------------
        # 耗材账号与未确权账号守护：仅对真实受信任设备 (5件套 + 数美B指纹 + 未过期expire_at) 且非一次性账号续杯
        if eval_res["is_disposable"]:
            continue

        if effective_tok_ts > 0:
            token_age = max(0.0, now - effective_tok_ts)
        elif has_credentials:
            # 历史库缺失 token_updated_at / updated_at 时避免恒为 0 永不续杯，直接触发一次核对换签
            token_age = _RENEW_THRESHOLD_SEC
        else:
            token_age = 0.0

        if has_credentials and _RENEW_THRESHOLD_SEC > 0 and token_age >= _RENEW_THRESHOLD_SEC:
            _log({
                "kind": "trigger_slid_renewal",
                "email": email,
                "token_age_hours": round(token_age / 3600, 2),
                "threshold_hours": round(_RENEW_THRESHOLD_SEC / 3600, 2)
            })

            renew_ok, renew_res = await _run_guarded_renew(acc, email, stale_token=token, force=False)
            if renew_ok == "skipped_inactive":
                detail.append({"email": email, "ok": True, "action": "skipped_inactive"})
                continue
            if renew_ok == "skipped_already_fresh":
                _log({
                    "kind": "slid_renewal_skipped_fresh",
                    "email": email,
                    "token_age_hours": round(max(0.0, time.time() - float(renew_res or 0)) / 3600, 2),
                })
                detail.append({"email": email, "ok": True, "action": "skipped_already_fresh"})
                continue
            if renew_ok and renew_res.get("token"):
                new_tok = renew_res["token"]
                renewed_cnt += 1
                _persist_token(email, new_tok)
                try:
                    from app.engine.trusted_relogin_engine import record_http_renewal
                    target_key = acc.get("account_label") or email
                    record_http_renewal(
                        target_key,
                        mode=renew_res.get("mode", ""),
                        cost=float(renew_res.get("cost") or 0.0),
                    )
                    if target_key != email:
                        record_http_renewal(
                            email,
                            mode=renew_res.get("mode", ""),
                            cost=float(renew_res.get("cost") or 0.0),
                        )
                except Exception:
                    pass
                _log({
                    "kind": "token_slid_renewed",
                    "email": email,
                    "mode": renew_res.get("mode"),
                    "cost": renew_res.get("cost"),
                    "token_age_hours": round(token_age / 3600, 2)
                })
                detail.append({"email": email, "ok": True, "action": "renewed", "cost": renew_res.get("cost")})
                continue
            else:
                r_biz = renew_res.get("biz_code") if isinstance(renew_res, dict) else None
                if r_biz == 14:
                    _mark_account_status_db(email, "MFA_REQUIRED", "mfa_required", now)
                elif r_biz == 10:
                    _mark_account_status_db(email, "INVALID", "permanent", now)
                _log({
                    "kind": "slid_renewal_failed",
                    "email": email,
                    "error": renew_res.get("error") if isinstance(renew_res, dict) else str(renew_res),
                    "biz_code": r_biz
                })
                detail.append({"email": email, "ok": True, "action": "renewal_failed", "biz_code": r_biz, "err": renew_res.get("error") if isinstance(renew_res, dict) else str(renew_res)})
                continue

        detail.append({"email": email, "ok": True, "action": "heartbeat_ok", "token_age_hours": round(token_age / 3600, 2)})

    _STATS.update({
        "last_run": int(now),
        "last_ok": ok_cnt,
        "last_fail": fail_cnt,
        "last_alive_ok": alive_ok_cnt,
        "last_renewed": renewed_cnt,
        "last_detail": detail,
    })
    _log({
        "kind": "run_done", "total": len(targets),
        "ok": ok_cnt, "fail": fail_cnt,
        "alive_ok": alive_ok_cnt, "renewed": renewed_cnt
    })
    return _STATS


async def _loop() -> None:
    await asyncio.sleep(60)  # 启动后避开瞬时洪峰
    while True:
        try:
            await run_once()
        except Exception as e:
            _log({"kind": "loop_error", "error": f"{type(e).__name__}: {e}"})
        await asyncio.sleep(max(120.0, _INTERVAL))


def start() -> None:
    global _TASK
    if not _ENABLED:
        print("[KeepAlive] 已禁用（DS_KEEPALIVE_ENABLED=0）", flush=True)
        return
    if _TASK and not _TASK.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _TASK = loop.create_task(_loop())
        print(
            f"[KeepAlive] ✅ 双轨会话守护引擎（纯 HTTP，心跳+滑动续杯）已启动：心跳每 {int(_INTERVAL)}s，"
            f"换签门槛 {round(_RENEW_THRESHOLD_SEC / 3600, 1)}h，账号错峰 {int(_STAGGER)}s",
            flush=True
        )
    except Exception as e:
        print(f"[KeepAlive] 启动失败（已忽略）: {e}", flush=True)


def status() -> Dict[str, Any]:
    return {
        "version": 3,
        "mode": "pure_http_dual_track",
        "enabled": _ENABLED,
        "interval_s": int(_INTERVAL),
        "stagger_s": int(_STAGGER),
        "renew_threshold_s": int(_RENEW_THRESHOLD_SEC),
        "running": bool(_TASK and not _TASK.done()),
        **_STATS
    }
