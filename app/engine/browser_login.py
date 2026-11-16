# -*- coding: utf-8 -*-
"""
app/engine/browser_login.py
纯 HTTP 重登桥接模块 (已彻底废除 Playwright / Chromium 浏览器拉起逻辑)

所有存量 `quick_trusted_relogin` 调用均直接桥接至
`app.engine.http_auth_engine.renew_account_token(account_dict, platform="web")`。
"""

from __future__ import annotations

import asyncio
from typing import Dict, Any, Tuple, Optional

from app.engine import http_auth_engine
from app.engine.trusted_relogin_engine import (
    resolve_account_payload,
    _persist_relogin_result,
    get_account_relogin_lock,
)


def _resolve_relogin_proxy(email: str) -> Optional[str]:
    """解析登录专用出口代理：config.relogin_proxies 非空时按邮箱哈希选择，留空走直连。"""
    try:
        from app.config import config_manager
        pool = list(getattr(config_manager.config, "relogin_proxies", None) or [])
        if not pool:
            return None
        key = (email or "").strip().lower()
        return pool[abs(hash(key)) % len(pool)]
    except Exception:
        return None


def quick_trusted_relogin(
    email: str,
    password: str = "",
    timeout: float = 25.0,
    account_dict: Optional[Dict[str, Any]] = None,
    proxy: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    核心重登入口（纯 HTTP iOS 极速续杯）：
    直接调用 `app.engine.http_auth_engine.renew_account_token(account_dict, platform="ios")`，
    返回兼容结构 `(ok, {"token": ..., "error": ..., "biz_code": ...})`。
    """
    if proxy is None:
        proxy = _resolve_relogin_proxy(email)

    payload, target_label = resolve_account_payload(
        email=email,
        password=password,
        account_dict=account_dict,
    )
    if not (payload.get("password") or payload.get("_password")):
        return False, {"error": f"账号 {email} 未配置密码，无法执行纯 HTTP 续杯", "biz_code": -1}

    lock_key = payload.get("_email") or payload.get("email") or email or target_label
    with get_account_relogin_lock(lock_key):
        ok, res = http_auth_engine.renew_account_token(
            payload,
            platform="ios",
            proxy_url=proxy,
            timeout=int(timeout) if timeout else 20,
        )
        if ok and isinstance(res, dict) and res.get("token"):
            is_muted, mute_until, new_status = _persist_relogin_result(target_label, email, res)
            try:
                from app.engine.trusted_relogin_engine import record_http_renewal
                record_http_renewal(target_label, mode=res.get("mode"), cost=res.get("cost"))
                if target_label != email:
                    record_http_renewal(email, mode=res.get("mode"), cost=res.get("cost"))
            except Exception:
                pass
            return True, {
                "token": res["token"],
                "email": payload.get("email") or email,
                "status": new_status,
                "is_muted": is_muted,
                "mute_until": mute_until,
                "biz_code": res.get("biz_code", 0),
                "mode": res.get("mode", "http"),
                "cost": res.get("cost"),
                "platform": res.get("platform", "web"),
            }

    err_dict = dict(res) if isinstance(res, dict) else {"error": str(res)}
    if "biz_code" not in err_dict:
        err_dict["biz_code"] = err_dict.get("code", -1)
    if "error" not in err_dict:
        err_dict["error"] = "纯 HTTP 续杯失败"
    return False, err_dict


async def login_with_browser_async(
    email: str,
    password: str,
    proxy: Optional[str] = None,
    headless: bool = True,
    timeout: float = 35.0,
) -> Tuple[bool, Dict[str, Any]]:
    """兼容旧接口：已废除浏览器拉起，直接走纯 HTTP 极速续杯。"""
    return await asyncio.to_thread(
        quick_trusted_relogin,
        email,
        password=password,
        timeout=timeout,
        proxy=proxy,
    )


def login_with_browser(
    email: str,
    password: str,
    proxy: Optional[str] = None,
    headless: bool = True,
    timeout: float = 35.0,
) -> Tuple[bool, Dict[str, Any]]:
    """兼容旧接口：已废除浏览器拉起，直接走纯 HTTP 极速续杯。"""
    return quick_trusted_relogin(email, password=password, timeout=timeout, proxy=proxy)


async def auto_mfa_exchange_token_async(
    email: str,
    password: str,
    anymessage_token: str = "",
    order_id: str = "",
    proxy: Optional[str] = None,
    headless: bool = True,
    timeout: float = 45.0,
) -> Tuple[bool, Dict[str, Any]]:
    """兼容旧接口：服务端严禁拉起无头浏览器，已停用浏览器交互式 MFA 接码换 Token。"""
    return False, {
        "error": "服务端已严禁拉起无头浏览器，请导入含 5 件套与真实数美指纹的账号走纯 HTTP 极速续杯，或直接手工录入 Token",
        "biz_code": -1,
    }


def auto_mfa_exchange_token(
    email: str,
    password: str,
    anymessage_token: str = "",
    order_id: str = "",
    proxy: Optional[str] = None,
    headless: bool = True,
    timeout: float = 45.0,
) -> Tuple[bool, Dict[str, Any]]:
    """兼容旧接口：服务端严禁拉起无头浏览器。"""
    return False, {
        "error": "服务端已严禁拉起无头浏览器，请导入含 5 件套与真实数美指纹的账号走纯 HTTP 极速续杯，或直接手工录入 Token",
        "biz_code": -1,
    }
