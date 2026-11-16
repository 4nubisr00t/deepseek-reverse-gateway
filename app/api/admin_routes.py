"""
Admin 后台运维路由与页面
- 静态页面挂载 (/admin/login, /admin/dashboard, /admin -> redirect)
- 鉴权登录与登出 (/api/admin/login, /api/admin/logout)
- 账号池状态管理与静默探活 (/api/accounts/health, /api/accounts/check, /api/accounts/add, /api/accounts/delete)
- 拟真养号流程 (/api/accounts/warmup)
- 批量自动化注册 (/api/register/config, /api/register/run, /api/register/logs)
- 调用监控与统计 (/api/usage)
- API Keys 管理 (/api/keys)
- 系统设置管理 (/api/settings)
- 服务健康检查 (/health)
"""

import hmac
import os
import re
import urllib.request
import time
import json
import uuid
import random
import poplib
from email.parser import BytesParser
from email.header import decode_header
import asyncio
import shutil
import platform
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from curl_cffi import requests as cffi_requests

from app.config import config_manager
from app.core.security import (
    require_admin,
    create_admin_session,
    destroy_admin_session,
    verify_admin_session,
    list_keys,
    generate_key,
    revoke_key,
    update_key,
    set_key_accounts,
    account_owner_map,
)
from app.engine.account_pool import account_pool, MAX_CONCURRENT_PER_ACCOUNT, get_effective_proxy
from app.engine.deepseek_client import DeepSeekClient, _parse_mute_until
from app.engine.metrics import metrics_engine
from app.engine.key_usage import key_usage_engine
from app.engine.warmup import warmup_account_task, get_warmup_status
from app.engine.quick_register import quick_register_engine, DEFAULT_FARM_SOLVE_URL

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ── 请求模型 ──

class LoginRequest(BaseModel):
    password: str


class AddKeyRequest(BaseModel):
    name: str = "default"
    owner: str = ""
    account_labels: Optional[List[str]] = None
    sensitive_filter: Optional[bool] = True
    key_type: Optional[str] = "normal"
    card_category: Optional[str] = None
    expires_at: Optional[int] = None
    days: Optional[int] = None


class UpdateKeyRequest(BaseModel):
    owner: Optional[str] = None
    enabled: Optional[bool] = None
    pool_fallback: Optional[bool] = None
    sensitive_filter: Optional[bool] = None
    # T25 配额：0 = 不限
    quota_daily_requests: Optional[int] = None
    quota_daily_tokens: Optional[int] = None


class UserPoolUpdateRequest(BaseModel):
    account_labels: List[str]
    auto_replenish: Optional[bool] = None


class ResetUsageRequest(BaseModel):
    """清零某个 key 的今日计数（配错/跑飞时的应急手段）"""
    key_id: str


class AssignAccountsRequest(BaseModel):
    """把账号分配给某个 key（覆盖式）。传空列表 = 该 key 回到公共池。"""
    account_labels: List[str] = []


class DeleteAccountRequest(BaseModel):
    account: str


class CheckAccountRequest(BaseModel):
    account: Optional[str] = None


class ReloginAccountRequest(BaseModel):
    account: Optional[str] = None


class AddAccountRequest(BaseModel):
    mode: str = "token"  # "email_pwd" | "token" | "curl"
    email: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    account_label: Optional[str] = None
    curl_text: Optional[str] = None


class WarmupRequest(BaseModel):
    account: str
    seed_count: int = 2


class QuickRegisterStartRequest(BaseModel):
    email: str
    password: Optional[str] = ""
    proxy: Optional[str] = ""
    farm_url: Optional[str] = DEFAULT_FARM_SOLVE_URL
    captcha_token: Optional[str] = ""
    device_id: Optional[str] = ""
    pool_tag: Optional[str] = "public"
    skip_preflight: bool = False


class QuickRegisterVerifyRequest(BaseModel):
    job_id: str
    register_otp: str
    login_captcha_token: Optional[str] = ""
    login_device_id: Optional[str] = ""


class QuickRegisterCompleteRequest(BaseModel):
    job_id: str
    mfa_otp: str


class QuickRegisterPreflightRequest(BaseModel):
    proxy: Optional[str] = ""


class AccountActionRequest(BaseModel):
    account: str


class BatchImportAccountsRequest(BaseModel):
    accounts_text: Optional[str] = ""
    accounts_list: Optional[List[Dict[str, Any]]] = None
    target_status: Optional[str] = "ACTIVE"
    pool_tag: Optional[str] = "public"
    notes: Optional[str] = "批量文件上传入库"
    check_liveness: Optional[bool] = True
    refresh_trusted_token: Optional[bool] = True
    deduplicate: Optional[bool] = True
    overwrite_existing: Optional[bool] = False


class SettingsUpdateRequest(BaseModel):
    proxy: Optional[str] = None
    passthrough: Optional[bool] = None
    # 登录专用代理池（与业务转发分离，专治 40029 IP 频控）
    relogin_proxies: Optional[List[str]] = None


class PasswordUpdateRequest(BaseModel):
    password: str


# ── 统一前端 SPA 页面路由（老版静态面板已光荣退役，全量统一至新版 Vue 3 架构） ──

FRONTEND_DIST_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def _serve_unified_spa() -> HTMLResponse:
    spa_index = FRONTEND_DIST_DIR / "index.html"
    if spa_index.exists():
        return HTMLResponse(
            content=spa_index.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return HTMLResponse(content="<form><h2>Unified Frontend SPA not built</h2></form>", status_code=200)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@router.get("/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_root(request: Request):
    return _serve_unified_spa()


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    return _serve_unified_spa()


@router.get("/admin/{subpath:path}", response_class=HTMLResponse, include_in_schema=False)
async def admin_subpath_spa(subpath: str):
    return _serve_unified_spa()


# ── 管理登录认证 ──

@router.post("/api/admin/login")
async def admin_login(body: LoginRequest, request: Request, response: Response):
    correct_pass = config_manager.config.admin_password
    if not hmac.compare_digest(body.password.encode(), correct_pass.encode()):
        raise HTTPException(status_code=401, detail="密码错误")

    client_ip = request.client.host if request.client else "unknown"
    sid = create_admin_session(client_ip)
    response.set_cookie(
        key="ds_admin_session",
        value=sid,
        httponly=True,
        samesite="lax",
        max_age=86400,
        path="/",
    )
    return {"ok": True, "message": "登录成功"}


@router.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response):
    sid = request.cookies.get("ds_admin_session")
    if sid:
        destroy_admin_session(sid)
    response.delete_cookie("ds_admin_session", path="/")
    return {"ok": True, "message": "已退出"}


@router.get("/health")
async def health_check():
    statuses = account_pool.get_all_status()
    # ★ `valid` 的口径改过（2026-09-12）：从"本地 is_valid 为真"改成"**真实可用**"。
    #
    # 旧口径会把"上游已禁言、但本地还写着 ACTIVE"的号算成有效 —— 实测中
    # `/health` 报 valid=20/muted=5，而真相是 18/7：有 2 个已被禁言的号
    # 因为被某个 key 独占（不进公共池 → 不被探活）而一直显示为有效。
    # 运维看到"20 个可用"却不断失败，正是这个偏差造成的。
    #
    # 现在按号池**实际使用的判据**（`is_usable_state`：valid + ACTIVE + 不在停用期）统计，
    # 于是这个数与"能真正承载请求的号"逐一对齐。
    valid_count = sum(1 for a in statuses if a.get("usable"))
    warming_count = sum(1 for a in statuses if a.get("status") == "WARMING")
    standby_count = sum(1 for a in statuses if a.get("status") == "STANDBY")
    cold_count = sum(1 for a in statuses if a.get("status") == "INACTIVE_COLD")
    quarantine_count = sum(1 for a in statuses if "QUARANTINE" in (a.get("status") or "") or a.get("status") == "MUTED")
    muted_count = sum(1 for a in statuses
                      if not a.get("usable") and a.get("status") not in ("WARMING", "INACTIVE_COLD"))
    now_ts = time.time()
    dated_count = sum(
        1 for a in statuses
        if a.get("ban_kind") == "dated" or (a.get("mute_until") and a.get("mute_until") > now_ts and a.get("ban_kind") != "permanent")
    )

    db_info = {"engine": "SQLite 3", "status": "HEALTHY"}
    try:
        from app.db.database import db
        if db.db_path.exists():
            db_info["size_kb"] = round(db.db_path.stat().st_size / 1024, 1)
    except Exception:
        pass

    return {
        "status": "ok",
        "database": db_info,
        "accounts": len(statuses),
        "valid": valid_count,
        "warming": warming_count,
        "standby": standby_count,
        "cold": cold_count,
        "quarantine": quarantine_count,
        "muted": muted_count,
        "dated": dated_count,
        "timestamp": int(time.time()),
    }


@router.get("/api/system/db_status", dependencies=[Depends(require_admin)])
async def get_db_status():
    """获取 SQLite 数据库引擎运行状态与各表资产统计"""
    from app.db.database import db
    from app.db.repositories import account_repo, api_key_repo, user_repo, usage_repo

    acc_count = account_repo.count()
    key_count = api_key_repo.count()
    user_count = user_repo.count()
    journal_mode = db.fetchone("PRAGMA journal_mode")
    mode_str = journal_mode[0] if journal_mode else "unknown"
    db_size = 0
    if db.db_path.exists():
        db_size = db.db_path.stat().st_size

    return {
        "engine": "SQLite 3",
        "mode": mode_str.upper(),
        "db_path": str(db.db_path),
        "size_bytes": db_size,
        "size_formatted": f"{db_size / 1024:.1f} KB",
        "accounts_count": acc_count,
        "keys_count": key_count,
        "users_count": user_count,
        "status": "HEALTHY",
    }


class MarkAlertReadRequest(BaseModel):
    id: Optional[str] = None
    all: bool = False


# ── 全局实时封号/禁言与风控收件箱 ──
SYSTEM_ALERTS: List[Dict[str, Any]] = []

def record_system_alert(
    account: str,
    alert_type: str,
    detail: str = "",
    mute_until: Optional[float] = None,
    caller_info: Optional[Dict[str, Any]] = None,
):
    now = time.time()
    wake_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mute_until)) if mute_until else None
    caller_key = caller_info.get("key_name") if caller_info else None
    caller_owner = caller_info.get("owner") if caller_info else None
    caller_ip = caller_info.get("ip") if caller_info else None

    try:
        from app.db.repositories import alert_repo
        saved = alert_repo.add_alert(
            account=account,
            alert_type=alert_type,
            message=detail,
            wake_at=wake_str,
            caller_key=caller_key,
            caller_owner=caller_owner,
            caller_ip=caller_ip,
            created_at=int(now),
        )
    except Exception as e:
        print(f"[Alert] 写入 SQLite system_alerts 异常: {e}", flush=True)
        saved = {
            "id": f"alt-{int(now * 1000)}",
            "time": now,
            "time_fmt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "account": account,
            "type": alert_type,
            "detail": detail,
            "wake_at": wake_str,
            "caller_key": caller_key,
            "caller_owner": caller_owner,
            "caller_ip": caller_ip,
            "is_read": False,
        }

    SYSTEM_ALERTS.insert(0, saved)
    if len(SYSTEM_ALERTS) > 50:
        SYSTEM_ALERTS.pop()


@router.get("/api/system/alerts", dependencies=[Depends(require_admin)])
async def get_system_alerts(limit: int = 50):
    """获取封号与风控消息收件箱列表（包含未读数统计）"""
    from app.db.repositories import alert_repo
    alerts = alert_repo.list_alerts(limit=limit)
    unread_count = alert_repo.get_unread_count()
    return {"ok": True, "alerts": alerts, "unread_count": unread_count}


@router.post("/api/system/alerts/read", dependencies=[Depends(require_admin)])
async def mark_alerts_read(body: MarkAlertReadRequest):
    """标记单条或一键全部已读"""
    from app.db.repositories import alert_repo
    if body.all or not body.id:
        count = alert_repo.mark_read(None)
    else:
        count = alert_repo.mark_read(body.id)
    return {"ok": True, "marked_count": count, "unread_count": alert_repo.get_unread_count()}


@router.delete("/api/system/alerts/clear", dependencies=[Depends(require_admin)])
async def clear_read_alerts():
    """清理所有已读通知"""
    from app.db.repositories import alert_repo
    count = alert_repo.clear_read()
    return {"ok": True, "cleared_count": count, "unread_count": alert_repo.get_unread_count()}


# ── 账号池管理 ──

@router.get("/api/system/relogin_status", dependencies=[Depends(require_admin)])
async def relogin_status():
    """免码重登健康态：区分【过热(IP频控)】/【需重新登记设备】/【封号】。

    - ip.restricted=True 表示官方 40029 IP 访问受限，此时所有账号登录都会回落验证码，
      但**账号本身没有失效**，冷却结束自动恢复；
    - accounts[email].kind 为每个账号最近一次重登的分类结果。
    """
    from app.engine.trusted_relogin_engine import get_relogin_health
    return get_relogin_health()


@router.get("/api/accounts/health", dependencies=[Depends(require_admin)])
async def accounts_health():
    statuses = account_pool.get_all_status()
    results = []
    now_ts = time.time()
    for a in statuses:
        mute_until_fmt = None
        if a["mute_until"]:
            mute_until_fmt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["mute_until"]))

        st = a.get("status") or ("ACTIVE" if a["is_valid"] else "MUTED")
        warmup_info = get_warmup_status(a["account_label"])
        bk = a.get("ban_kind")
        is_dated_ban = bk == "dated" or (a.get("mute_until") and a.get("mute_until") > now_ts and bk != "permanent")

        err_desc = ""
        if bk == "permanent" or st in ("BANNED", "QUARANTINE_INVALID"):
            err_desc = "🛑 官方永久封禁 (无法恢复)"
            mute_until_fmt = None
            is_dated_ban = False
        elif is_dated_ban:
            err_desc = "⏳ 官方有期禁言 (到期自动康复)"
        elif a["mute_until"]:
            err_desc = "Account Muted (Cooldown)"
        elif st == "WARMING":
            err_desc = "养号中 (隔离)"
        elif st == "STANDBY":
            err_desc = "⚡ 待命温库储备 (就绪可秒投产)"
        elif st == "INACTIVE_COLD":
            err_desc = "🧊 零IP冷库储备 (未激活)"
        elif not a.get("usable"):
            err_desc = "已失效下线 / Token失效"

        results.append({
            "account": a["account_label"],
            "token": a.get("token", ""),
            "has_trusted_mfa": a.get("has_trusted_mfa", False),
            "is_valid": a["is_valid"],
            "status": st,
            "ban_kind": bk,
            "is_dated_ban": is_dated_ban,
            "muted": bool(a["mute_until"]),
            "mute_until": mute_until_fmt,
            # ★ 真实可用（号池判据算出来的），与 `is_valid` 分开看：
            # `is_valid=True + muted=True` 是**实测出现过**的自相矛盾组合，
            # 管理页/巡检要按 `usable` 判断，不要按 `is_valid`。
            "usable": a.get("usable"),
            "muted_now": a.get("muted_now"),
            "error": err_desc,
            "is_busy": a["is_busy"],
            "warmup": warmup_info,
            # T2: 归属信息
            "owner": a.get("owner", ""),
            "owner_key_id": a.get("owner_key_id", ""),
            "in_pool": a.get("in_pool", True) and st != "INACTIVE_COLD",
            "in_flight": a.get("in_flight", 0),
        })
    from app.db.repositories import alert_repo
    unread_cnt = alert_repo.get_unread_count()

    # ★ 纯 HTTP 续杯健康态（已续杯 / HTTP续杯就绪 / 一次性账号 三态区分；永不返回 IP 过热）
    try:
        from app.engine.trusted_relogin_engine import get_relogin_health, get_recent_http_renewal
        rh = get_relogin_health()
    except Exception:
        rh = {"ip": {"restricted": False, "remaining_s": 0}, "accounts": {}, "legend": {}}
        get_recent_http_renewal = lambda _k: None

    if isinstance(rh, dict):
        ip_info = rh.get("ip")
        if not isinstance(ip_info, dict):
            rh["ip"] = {"restricted": False, "remaining_s": 0}
        else:
            ip_info["restricted"] = False
            ip_info["remaining_s"] = 0

    try:
        from app.engine.http_auth_engine import evaluate_trusted_device
    except Exception:
        evaluate_trusted_device = None

    raw_accounts = config_manager.get_accounts() or []
    acc_lookup = {}
    for a in raw_accounts:
        for k in (a.get("account_label"), a.get("_email"), a.get("email")):
            if k:
                acc_lookup[k] = a

    for r in results:
        acc_key = r.get("account") or ""
        acc_obj = dict(acc_lookup.get(acc_key) or {})
        if not acc_obj:
            try:
                from app.db.repositories import account_repo
                db_acc = account_repo.get(acc_key)
                if db_acc:
                    acc_obj = dict(db_acc)
            except Exception:
                pass
        if not acc_obj and hasattr(account_pool, "_accounts"):
            rt = account_pool._accounts.get(acc_key)
            if rt is not None:
                acc_obj = {
                    "credential_id": getattr(rt, "credential_id", None),
                    "mfa_private_key": getattr(rt, "mfa_private_key", None),
                    "mfa_public_key": getattr(rt, "mfa_public_key", None),
                    "mfa_key_version": getattr(rt, "mfa_key_version", None),
                    "device_id": getattr(rt, "device_id", None),
                    "device_fingerprint": getattr(rt, "device_fingerprint", None),
                    "cookie": getattr(rt, "cookie", "") or "",
                    "expire_at": getattr(rt, "expire_at", None),
                    "updated_at": getattr(rt, "updated_at", 0),
                    "token_updated_at": getattr(rt, "token_updated_at", 0),
                    "pool_tag": getattr(rt, "pool_tag", "public"),
                    "disposable": getattr(rt, "disposable", False),
                    "_email": getattr(rt, "email", "") or "",
                    "email": getattr(rt, "email", "") or "",
                }
        email_key = acc_obj.get("_email") or acc_obj.get("email") or r.get("email") or ""

        recent = get_recent_http_renewal(acc_key) or (get_recent_http_renewal(email_key) if email_key else None)
        h = (rh.get("accounts") or {}).get(acc_key) or ((rh.get("accounts") or {}).get(email_key) if email_key else None) or {}

        if evaluate_trusted_device is not None:
            eval_res = evaluate_trusted_device(acc_obj, now_ts=now_ts)
        else:
            eval_res = {
                "is_disposable": bool(acc_obj.get("disposable") is True or acc_obj.get("is_disposable") is True or acc_obj.get("pool_tag") == "disposable"),
                "device_trusted": False,
                "is_renewable": False,
                "expire_at": None,
                "reason": "unavailable",
            }

        is_disposable_user = eval_res["is_disposable"]
        r["is_disposable"] = is_disposable_user
        r["pool_tag"] = acc_obj.get("pool_tag") or "public"

        # 严格受信任设备与凭据健康信息（必须 5件套 + 有效数美 B 指纹 + 未过期 expire_at）
        r["has_trusted_mfa"] = eval_res["device_trusted"]
        r["is_renewable"] = eval_res["is_renewable"]
        r["device_trusted"] = eval_res["device_trusted"]
        r["trusted_reason"] = eval_res["reason"]

        # 受信任设备过期时间
        exp_ts = eval_res["expire_at"]
        r["expire_at"] = exp_ts
        r["expire_at_fmt"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp_ts)) if exp_ts else None

        # 换签倒计时计算 (优先使用独立 token_updated_at，不被非换签状态更新清零；阈值 7 小时 = 25200 秒)
        token_ts = int(acc_obj.get("token_updated_at") or acc_obj.get("updated_at") or 0)
        from app.engine.keepalive import _RENEW_THRESHOLD_SEC
        if token_ts > 0 and r["is_renewable"]:
            elapsed = max(0, int(now_ts - token_ts))
            rem_s = max(0, int(_RENEW_THRESHOLD_SEC - elapsed))
            r["token_age_s"] = elapsed
            r["renew_countdown_s"] = rem_s
            r["renew_countdown_fmt"] = f"{rem_s // 3600}小时{(rem_s % 3600) // 60}分" if rem_s > 0 else "即将触发续杯"
        else:
            r["token_age_s"] = None
            r["renew_countdown_s"] = None
            r["renew_countdown_fmt"] = None

        if is_disposable_user:
            r["relogin_kind"] = "ONE_SHOT"
            r["relogin_label"] = "一次性账号"
            r["relogin_level"] = "idle"
            r["relogin_desc"] = "已标记为一次性账号，已关闭后台自动续杯与极速换签"
            r["relogin_age_s"] = None
            r["relogin_error"] = ""
        elif r["is_renewable"] and (recent or h.get("kind") == "RENEWED" or h.get("label") == "已续杯"):
            src = recent or h
            r["relogin_kind"] = src.get("kind") or "RENEWED"
            r["relogin_label"] = "已续杯"
            r["relogin_level"] = "info"
            r["relogin_desc"] = src.get("desc") or "纯HTTP无感刷新"
            r["relogin_age_s"] = src.get("age_s")
            r["relogin_error"] = ""
        else:
            if r["is_renewable"]:
                r["relogin_kind"] = "HTTP_READY"
                r["relogin_label"] = "HTTP续杯就绪"
                r["relogin_level"] = "success"
                r["relogin_desc"] = "已具备受信任 5 件套、数美 B 指纹与有效到期时间，支持纯 HTTP 无感刷新"
                r["relogin_age_s"] = None
                r["relogin_error"] = ""
            else:
                reason_desc_map = {
                    "missing_expire_at": "缺少受信任设备到期时间 (expire_at)，未完成服务端确权，仅限当前 Token 有效期内使用",
                    "expired_trusted_device": "受信任设备有效期 (expire_at) 已过期，仅限当前 Token 有效期内使用",
                    "invalid_fingerprint": "缺少合法数美 B 指纹，仅限当前 Token 有效期内使用",
                }
                r["relogin_kind"] = "ONE_SHOT"
                r["relogin_label"] = "一次性账号"
                r["relogin_level"] = "idle"
                r["relogin_desc"] = reason_desc_map.get(
                    eval_res["reason"],
                    "缺少受信任 5 件套、数美 B 指纹或有效到期时间，仅限当前 Token 有效期内使用",
                )
                r["relogin_age_s"] = None
                r["relogin_error"] = ""

        # 纯 HTTP 指纹协议彻底取消浏览器 IP 过热误报
        r["ip_restricted"] = False

    return {
        "results": results,
        "alerts": SYSTEM_ALERTS[:15],
        "unread_alerts_count": unread_cnt,
        "relogin_health": rh,
    }


def probe_single_account(account_item: Dict[str, Any], proxy: Optional[str]) -> Dict[str, Any]:
    """通过官方 /api/v0/users/current 进行零风控无痕静默探活"""
    token = account_item.get("token", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = cffi_requests.get(
            "https://chat.deepseek.com/api/v0/users/current",
            headers=headers,
            proxies=proxies,
            impersonate="chrome124",
            timeout=15,
        )
        data = r.json()
        if r.status_code == 200 and data.get("code") == 0:
            biz_data = (data.get("data") or {}).get("biz_data") or {}
            chat_info = biz_data.get("chat") or {}
            is_muted = chat_info.get("is_muted", 0) == 1
            # ⚠️ 必须过 `_parse_mute_until` 规整成 **unix 秒**。
            #
            # 上游这个字段可能是秒、毫秒、或"还要等多少秒"（相对值）——
            # `_parse_mute_until` 的 docstring 写明"无法事前确定"，所以才按量级判断。
            # 而这里原来直接把**原始值**写进 config.json，而全项目其它地方
            # （`AccountRuntime.mute_until` / `check_mute_expiry` / `is_muted_now`）
            # 一律按 unix 秒解释，于是同一个字段出现两种单位：
            #   · 上游给毫秒 → 记录成 1.7e12 → 被判定"停用期永不结束"，号被永久冷藏
            #   · 上游给相对值（如 64011）→ 记录成 64011 → 远小于当前时间
            #     → 被判定"已解禁" → **把禁言中的号发出去**（正是本次要修的故障）
            # 规整不了时它返回 None，由退避阶梯兜底 —— 绝不猜一个时间点。
            mute_until = _parse_mute_until(chat_info.get("mute_until"))
            if is_muted:
                record_system_alert(
                    account_item.get("account_label") or biz_data.get("email") or "unknown",
                    "MUTED",
                    f"账号已被官方禁言封禁，预计解禁时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mute_until)) if mute_until else '未知'}",
                    mute_until
                )
            return {
                "ok": True,
                "is_valid": True,
                "is_muted": is_muted,
                "mute_until": mute_until,
                "raw_email": biz_data.get("email"),
                "msg": "正常可用" if not is_muted else "已被官方禁言",
            }

        # 官方服务器级故障 / 瞬态过载：绝不惩罚账号
        if r.status_code in (500, 502, 503, 504, 520, 521, 522, 524):
            return {
                "ok": False,
                "is_valid": True,
                "is_muted": False,
                "is_server_busy": True,
                "msg": f"DeepSeek 官方服务器繁忙/瞬态故障 (HTTP {r.status_code})，暂不变更账号状态",
            }

        if r.status_code == 403:
            body_low = (r.text or "")[:400].lower()
            if any(w in body_low for w in ("cloudflare", "waf", "<html", "<!doctype", "just a moment")):
                return {
                    "ok": False,
                    "is_valid": True,
                    "is_muted": False,
                    "msg": "出口网络遭遇 Cloudflare/WAF 拦截，非账号失效",
                }

        is_token_invalid = (
            r.status_code in (401, 403)
            or data.get("code") in (40001, 40002, 40003)
            or "invalid token" in str(data.get("msg") or "").lower()
            or "authorization failed" in str(data.get("msg") or "").lower()
        )
        if is_token_invalid:
            lbl = account_item.get("account_label") or account_item.get("email")
            pwd = account_item.get("password") or account_item.get("_password")
            email = account_item.get("email") or account_item.get("_email") or lbl
            from app.engine.http_auth_engine import evaluate_trusted_device
            eval_info = evaluate_trusted_device(account_item)
            has_mfa = bool(account_item.get("credential_id") and account_item.get("mfa_private_key"))
            if eval_info["is_renewable"] and pwd and email:
                try:
                    from app.engine.browser_login import quick_trusted_relogin
                    ok, res = quick_trusted_relogin(email, pwd, account_dict=account_item)
                    if ok and isinstance(res, dict) and res.get("token"):
                        new_tok = res["token"]
                        account_item["token"] = new_tok
                        config_manager.update_account(
                            lbl,
                            token=new_tok,
                            is_valid=True,
                            status="ACTIVE",
                            mute_until=None,
                            ban_kind=None,
                            mute_count=0,
                        )
                        account_pool.reload()
                        return {
                            "ok": True,
                            "is_valid": True,
                            "status": "ACTIVE",
                            "is_muted": False,
                            "mute_until": None,
                            "raw_email": email,
                            "new_token": new_tok,
                            "msg": "🎉 Token 已通过纯 HTTP 指纹引擎极速续杯成功！",
                        }
                    if isinstance(res, dict) and (
                        res.get("biz_code") == 14
                        or "MFA_VERIFICATION_CODE_REQUIRED" in str(res.get("error") or "")
                    ):
                        if lbl:
                            config_manager.update_account(
                                lbl,
                                is_valid=False,
                                status="MFA_REQUIRED",
                                ban_kind="mfa_required",
                            )
                            from app.core.security import disown_account
                            disown_account(lbl)
                            account_pool.reload()
                        return {
                            "ok": True,
                            "is_valid": False,
                            "status": "MFA_REQUIRED",
                            "ban_kind": "mfa_required",
                            "is_muted": False,
                            "mute_until": None,
                            "msg": "⚠️ 设备信任已失效，触发官方邮件 MFA 校验 (biz_code=14)",
                        }
                except Exception as ex:
                    print(f"[AdminCheck] 账号受信任免密重登刷新异常: {ex}", flush=True)
            elif not eval_info["is_renewable"] and lbl:
                retire_status = "INVALID"
                retire_ban = "permanent" if not has_mfa else "expired"
                config_manager.update_account(lbl, is_valid=False, status=retire_status, ban_kind=retire_ban)
                account_pool.reload()
                return {
                    "ok": True,
                    "is_valid": False,
                    "status": retire_status,
                    "ban_kind": retire_ban,
                    "is_muted": False,
                    "mute_until": None,
                    "msg": f"⚠️ 账号不可纯 HTTP 续杯（{eval_info['reason']}），已标记退役",
                }
            return {
                "ok": True,
                "is_valid": False,
                "status": "INVALID",
                "ban_kind": "expired",
                "is_muted": False,
                "mute_until": None,
                "msg": "Token 已过期失效",
            }
        else:
            return {"ok": False, "is_valid": False, "msg": data.get("msg") or f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "is_valid": False, "msg": f"请求异常: {e}"}


@router.post("/api/accounts/check", dependencies=[Depends(require_admin)])
async def check_accounts(body: CheckAccountRequest):
    """一键探活：对指定账号或全部活跃账号执行无痕静默检测，并在 Token 失效时自动重登刷新；自动保护冷库账号"""
    accounts = config_manager.get_accounts()
    proxy = config_manager.config.proxy
    results = []

    target_accounts = (
        [a for a in accounts if a.get("account_label") == body.account]
        if body.account
        else accounts
    )

    loop = asyncio.get_event_loop()
    for acc in target_accounts:
        label = acc.get("account_label")
        # 保护冷库储备：未激活账号绝对不得发送空 Token 探活，更不得篡改其冷库状态！
        if acc.get("status") == "INACTIVE_COLD" or (not acc.get("token") and (acc.get("password") or acc.get("_password"))):
            results.append({
                "account": label,
                "ok": True,
                "is_valid": False,
                "status": "INACTIVE_COLD",
                "is_muted": False,
                "mute_until": None,
                "msg": "🧊 零IP冷库储备号（安全跳过探活，保持冷冻）",
            })
            continue

        probe = await loop.run_in_executor(None, probe_single_account, acc, proxy)
        if probe["ok"]:
            is_muted = bool(probe.get("is_muted"))
            is_valid_now = bool(probe["is_valid"]) and not is_muted
            mu = probe.get("mute_until")
            if is_muted:
                bk = "dated" if (mu and mu > time.time()) else "backoff"
                new_status = "MUTED"
                record_system_alert(
                    label, "MUTED",
                    f"一键探活检测到该账号已被官方禁言封禁（类别: {bk}），预计解禁时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mu)) if mu else '未知'}",
                    mu
                )
            elif is_valid_now:
                bk = None
                new_status = "ACTIVE"
            else:
                new_status = probe.get("status") or (
                    acc.get("status")
                    if acc.get("status") in ("INVALID", "QUARANTINE", "MFA_REQUIRED", "BANNED")
                    else "INVALID"
                )
                bk = probe.get("ban_kind") or (acc.get("ban_kind") if new_status in ("INVALID", "QUARANTINE", "MFA_REQUIRED", "BANNED") else "expired")
            update_kw = {
                "is_valid": is_valid_now,
                "status": new_status,
                "mute_until": mu,
                "ban_kind": bk,
            }
            if probe.get("new_token"):
                update_kw["token"] = probe["new_token"]
            config_manager.update_account(label, **update_kw)
        results.append({"account": label, **probe})

    account_pool.reload()
    return {"results": results}


@router.post("/api/accounts/relogin", dependencies=[Depends(require_admin)])
async def relogin_accounts(body: ReloginAccountRequest):
    """
    一键重新登录：针对指定账号或全部失效/未登录账号，
    严格使用已绑定的真实数美指纹 + 5件套调用 DeepSeek 官方登录接口刷新 Token。
    """
    accounts = config_manager.get_accounts()

    from app.engine.http_auth_engine import evaluate_trusted_device

    target_accounts = (
        [a for a in accounts if a.get("account_label") == body.account]
        if body.account
        else [
            a for a in accounts
            if evaluate_trusted_device(a)["is_renewable"]
            and (not a.get("is_valid") or a.get("status") in ("QUARANTINE", "ACTIVE", "INACTIVE_COLD"))
        ]
    )

    results = []
    loop = asyncio.get_event_loop()

    for acc in target_accounts:
        label = acc.get("account_label")
        pwd = acc.get("password") or acc.get("_password")
        email = acc.get("email") or acc.get("_email") or label

        eval_info = evaluate_trusted_device(acc)
        if not eval_info["is_renewable"]:
            results.append({
                "account": label,
                "ok": False,
                "msg": f"不可纯 HTTP 续杯（{eval_info['reason']}），拒绝兜底登录",
            })
            continue

        if not pwd:
            results.append({"account": label, "ok": False, "msg": "未保存密码，无法自动重新登录"})
            continue

        def _do_login(_email=email, _pwd=pwd, _acc=acc):
            # 统一走纯 HTTP 5 件套 + 一对一绑定真实指纹续杯引擎
            from app.engine.browser_login import quick_trusted_relogin
            return quick_trusted_relogin(_email, _pwd, account_dict=_acc)

        ok, res = await loop.run_in_executor(None, _do_login)
        token_val = res.get("token") if isinstance(res, dict) else (res if ok else "")
        if ok and token_val:
            new_token = token_val
            is_muted = res.get("is_muted", False) if isinstance(res, dict) else False
            mute_until = res.get("mute_until") if isinstance(res, dict) else None
            new_status = "MUTED" if is_muted else "ACTIVE"
            config_manager.update_account(
                label,
                token=new_token,
                is_valid=not is_muted,
                status=new_status,
                mute_until=mute_until if is_muted else None,
                ban_kind="dated" if is_muted else None,
            )
            # 同步写 DB
            try:
                from app.db.repositories import account_repo
                account_repo.update_runtime_state(
                    label,
                    token=new_token,
                    status=new_status,
                    is_valid=not is_muted,
                    mute_until=mute_until if is_muted else None,
                    ban_kind="dated" if is_muted else None,
                )
            except Exception:
                pass
            msg = "重登成功，但官方有期禁言中" if is_muted else "重登成功，Token已刷新"
            results.append({"account": label, "ok": True, "msg": msg, "token": new_token[:15] + "..." if new_token else "", "status": new_status})
        else:
            err_msg = str(res.get("error") if isinstance(res, dict) else res)
            biz_code = res.get("biz_code") if isinstance(res, dict) else None
            if "USER_IS_BANNED" in err_msg or biz_code == 10:
                config_manager.update_account(label, is_valid=False, status="INVALID", ban_kind="permanent")
                try:
                    from app.db.repositories import account_repo
                    from app.core.security import disown_account
                    account_repo.update_runtime_state(label, status="INVALID", is_valid=False, ban_kind="permanent")
                    disown_account(label)
                except Exception:
                    pass
                user_friendly_msg = "🛑 账号已被官方永久封禁销号 (USER_IS_BANNED)"
            elif "MFA_VERIFICATION_CODE_REQUIRED" in err_msg or biz_code == 14:
                config_manager.update_account(label, is_valid=False, status="MFA_REQUIRED", ban_kind="mfa_required")
                try:
                    from app.db.repositories import account_repo
                    from app.core.security import disown_account
                    account_repo.update_runtime_state(label, status="MFA_REQUIRED", is_valid=False, ban_kind="mfa_required")
                    disown_account(label)
                except Exception:
                    pass
                user_friendly_msg = "⚠️ 设备信任已失效，触发官方邮件 MFA 校验 (biz_code=14)"
            elif "RISK_DEVICE_DETECTED" in err_msg or biz_code == 11:
                user_friendly_msg = "⚠️ 触发官方数美风控/同IP频控 (RISK_DEVICE_DETECTED)，请检查代理与设备指纹一致性"
            elif "PASSWORD_OR_USER_NAME_IS_WRONG" in err_msg or biz_code == 2:
                user_friendly_msg = "❌ 账号或密码不匹配 (PASSWORD_OR_USER_NAME_IS_WRONG)"
            else:
                user_friendly_msg = f"登录失败: {err_msg}"
            results.append({"account": label, "ok": False, "msg": user_friendly_msg, "raw_error": err_msg})

    account_pool.reload()
    return {"results": results}


@router.post("/api/accounts/add", dependencies=[Depends(require_admin)])
async def add_account(body: AddAccountRequest):
    """添加账号：支持邮箱密码、Token直接录入、或cURL解析"""
    proxy = config_manager.config.proxy
    token = None
    account_label = None
    cookie = ""

    if body.mode == "email_pwd":
        if not body.email or not body.password:
            raise HTTPException(status_code=400, detail="请输入邮箱和密码")
        loop = asyncio.get_event_loop()
        from app.engine.browser_login import quick_trusted_relogin
        ok, res = await loop.run_in_executor(
            None, quick_trusted_relogin, body.email, body.password
        )
        if not ok or not isinstance(res, dict) or not res.get("token"):
            err_msg = res.get("error") if isinstance(res, dict) else str(res)
            raise HTTPException(status_code=400, detail=f"登录失败: {err_msg}")
        token = res["token"]
        account_label = body.email

    elif body.mode == "token":
        if not body.token:
            raise HTTPException(status_code=400, detail="Token 不能为空")
        raw_token = body.token.strip()
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token[7:].strip()
        token = raw_token
        account_label = body.account_label or f"user_{token[:8]}"

    elif body.mode == "curl":
        if not body.curl_text:
            raise HTTPException(status_code=400, detail="cURL 文本不能为空")
        # 正则提取 Authorization: Bearer
        m_token = re.search(r"authorization:\s*bearer\s+([A-Za-z0-9_\-\.]+)", body.curl_text, re.IGNORECASE)
        if m_token:
            token = m_token.group(1)
        else:
            raise HTTPException(status_code=400, detail="未能从 cURL 中解析出 Authorization Bearer Token")
        m_cookie = re.search(r"cookie:\s*([^\r\n\'\"]+)", body.curl_text, re.IGNORECASE)
        if m_cookie:
            cookie = m_cookie.group(1)
        account_label = body.account_label or f"curl_{token[:8]}"

    # 无痕检测该 Token 是否可用
    loop = asyncio.get_event_loop()
    probe = await loop.run_in_executor(
        None, probe_single_account, {"token": token, "account_label": account_label}, proxy
    )
    if not probe["ok"] or not probe["is_valid"]:
        raise HTTPException(status_code=400, detail=f"Token 验证失败: {probe.get('msg')}")

    # 创建初始会话
    sid = await loop.run_in_executor(None, DeepSeekClient.create_chat_session, token, cookie, proxy)
    now_ts = int(time.time())
    from app.engine.http_auth_engine import extract_token_issued_at
    tok_upd_ts = (now_ts if body.mode == "email_pwd" else (extract_token_issued_at(token, now_ts=now_ts) or now_ts))

    new_acc = {
        "account_label": account_label,
        "email": body.email or account_label,
        "password": body.password or "",
        "token": token,
        "cookie": cookie,
        "session_id": sid,
        "is_valid": True,
        "status": "ACTIVE",
        "mute_until": probe.get("mute_until"),
        "created_at": now_ts,
        "updated_at": now_ts,
        "token_updated_at": tok_upd_ts,
    }
    config_manager.add_account(new_acc)
    account_pool.reload()

    return {"ok": True, "message": f"账号 {account_label} 添加成功并完成验证！", "account": account_label}


@router.post("/api/accounts/delete", dependencies=[Depends(require_admin)])
async def delete_account(body: DeleteAccountRequest):
    config_manager.remove_account(body.account)
    account_pool.reload()
    return {"ok": True, "message": "账号已删除"}


@router.post("/api/accounts/warmup", dependencies=[Depends(require_admin)])
async def trigger_warmup(body: WarmupRequest):
    asyncio.create_task(warmup_account_task(body.account, seed_count=body.seed_count, auto_activate=True))
    return {"ok": True, "message": f"已启动账号 {body.account} 的养号任务，账号已临时隔离出业务池"}


def _is_cold_account_usable(acc: dict, now_ts: Optional[int] = None) -> tuple[bool, str]:
    """判定冷库账号是否干净可用（排除一次性号、缺凭证、永久/MFA封禁与仍在禁言期账号）。"""
    from app.engine.http_auth_engine import evaluate_trusted_device

    ts = int(now_ts if now_ts is not None else time.time())
    if acc.get("is_disposable") or acc.get("disposable") or str(acc.get("pool_tag") or "").lower() == "disposable":
        return False, "一次性耗材号不可在冷库待命"
    if not (acc.get("_password") or acc.get("password")):
        return False, "缺少明文密码"
    ev = evaluate_trusted_device(acc, now_ts=ts)
    if not ev.get("is_renewable"):
        return False, f"不具备受信任续杯凭证 ({ev.get('reason') or 'invalid'})"
    bk = str(acc.get("ban_kind") or "").strip().lower()
    if bk in ("permanent", "mfa_required", "dated"):
        return False, f"账号带故障标记 ({bk})"
    mu = _parse_mute_until(acc.get("mute_until"))
    if mu and mu > ts:
        return False, f"账号仍在禁言期 (至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mu))})"
    return True, "ok"


def do_activate_cold_account(account_key: str) -> tuple[bool, str]:
    from app.db.repositories import account_repo
    from app.engine import http_auth_engine
    from app.engine.trusted_relogin_engine import (
        resolve_account_payload,
        record_http_renewal,
        get_account_relogin_lock,
    )
    from app.engine.browser_login import _resolve_relogin_proxy

    raw_accounts = config_manager.get_accounts()
    acc = next(
        (
            a for a in raw_accounts
            if a.get("account_label") == account_key or a.get("_email") == account_key or a.get("email") == account_key
        ),
        None,
    )
    if not acc:
        acc = account_repo.get(account_key)
    if not acc:
        return False, f"未找到账号 {account_key}"

    real_email = acc.get("_email") or acc.get("email") or acc.get("account_label") or account_key
    password = acc.get("_password") or acc.get("password") or ""
    lock_key = real_email or acc.get("account_label") or account_key

    with get_account_relogin_lock(lock_key):
        # 锁内复核：防止并发唤醒对同一冷库账号发起两次登录
        latest_raw = config_manager.get_accounts()
        latest_acc = next(
            (
                a for a in latest_raw
                if a.get("account_label") == account_key or a.get("_email") == account_key or a.get("email") == account_key
            ),
            None,
        ) or account_repo.get(account_key)
        if latest_acc:
            acc = latest_acc
            if acc.get("status") == "ACTIVE" and acc.get("is_valid") and acc.get("token"):
                account_pool.reload()
                return True, f"账号 {real_email} 已被并发激活并加入活跃号池！"

        acc_payload, resolved_label = resolve_account_payload(real_email, password, account_dict=acc)
        target_label = resolved_label or acc.get("account_label") or account_key
        effective_pwd = acc_payload.get("_password") or acc_payload.get("password") or ""
        if not effective_pwd:
            return False, f"账号 {account_key} 没有记录明文密码，无法执行纯 HTTP 自动登录激活"

        proxy = _resolve_relogin_proxy(real_email) or config_manager.config.proxy
        ok, res = http_auth_engine.renew_account_token(acc_payload, platform="ios", proxy_url=proxy)

        if not ok:
            err = res.get("error") if isinstance(res, dict) else str(res)
            biz_code = res.get("biz_code") if isinstance(res, dict) else None
            if biz_code == 14:
                config_manager.update_account(
                    target_label,
                    is_valid=False,
                    status="MFA_REQUIRED",
                    ban_kind="mfa_required",
                )
                try:
                    account_repo.update_runtime_state(
                        target_label,
                        status="MFA_REQUIRED",
                        is_valid=False,
                        ban_kind="mfa_required",
                    )
                except Exception:
                    pass
                account_pool.reload()
                return False, (
                    "该账号已被要求邮箱验证码(biz_code=14)：免码通道对它已失效，"
                    "已将其移出冷库并标记为 MFA_REQUIRED"
                )
            if biz_code == 40029:
                return False, f"上游返回 40029 频控提示 ({err})，请更换代理或稍后再试"
            return False, f"纯 HTTP 激活拒绝 ({err}, biz_code: {biz_code})"

        token = res.get("token") if isinstance(res, dict) else None
        if not token:
            return False, "纯 HTTP 登录成功但未提取到 Token"

        is_muted = bool(res.get("is_muted", False)) if isinstance(res, dict) else False
        raw_mute_until = res.get("mute_until") if isinstance(res, dict) else None
        mute_until = _parse_mute_until(raw_mute_until) if is_muted else None
        new_status = "MUTED" if is_muted else "ACTIVE"
        now_ts = int(time.time())
        try:
            from app.engine.drop_probe import register_account_token, update_context as _dp_update
            register_account_token(target_label, token=token, issued_at=now_ts)
            _dp_update(account=target_label, token=token)
        except Exception:
            pass

        # 2. 换取初始 Session ID (可选)
        sid = ""
        try:
            session_proxy = get_effective_proxy({"account_label": target_label, "proxy": acc.get("proxy")}) or proxy
            sid = DeepSeekClient.create_chat_session(token=token, proxy=session_proxy) or ""
        except Exception:
            pass

        config_manager.update_account(
            target_label,
            token=token,
            cookie="",
            session_id=sid,
            is_valid=not is_muted,
            status=new_status,
            mute_until=mute_until,
            ban_kind="dated" if is_muted else None,
            token_updated_at=now_ts,
        )
        # 同步写 DB (包含 token 与 session_id)
        try:
            account_repo.update_runtime_state(
                target_label,
                token=token,
                session_id=sid,
                status=new_status,
                is_valid=not is_muted,
                mute_until=mute_until,
                ban_kind="dated" if is_muted else None,
                token_updated_at=now_ts,
            )
        except Exception as db_err:
            print(f"[冷库激活] 账号 {real_email} 同步持久化 SQLite 警告: {db_err}")

        try:
            mode = res.get("mode", "") if isinstance(res, dict) else ""
            cost = res.get("cost") if isinstance(res, dict) else None
            record_http_renewal(target_label, mode=mode, cost=cost)
            if real_email and real_email != target_label:
                record_http_renewal(real_email, mode=mode, cost=cost)
        except Exception:
            pass

        account_pool.reload()
        mode_tag = f"纯HTTP指纹协议:{res.get('mode', 'http')}" if isinstance(res, dict) and res.get("mode") else "纯HTTP指纹协议"
        if is_muted:
            mute_desc = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mute_until)) if mute_until else "未知"
            return True, f"账号 {real_email} 经由【{mode_tag}】激活完成，但当前处于官方有期禁言中 (解禁时间: {mute_desc})，已标记为 MUTED"
        return True, f"账号 {real_email} 经由【{mode_tag}】极速激活成功，已加入活跃号池！"


WAKE_STATUS = {
    "is_waking": False,
    "running": False,
    "total": 0,
    "current": 0,
    "success_count": 0,
    "fail_count": 0,
    "last_account": "",
    "last_msg": "",
}


async def run_wake_cold_batch_task(count: int, delay: float):
    global WAKE_STATUS
    WAKE_STATUS["is_waking"] = True
    WAKE_STATUS["running"] = True
    WAKE_STATUS["current"] = 0
    WAKE_STATUS["success_count"] = 0
    WAKE_STATUS["fail_count"] = 0

    try:
        raw_accounts = config_manager.get_accounts()
        now_ts = int(time.time())
        cold_accs = [
            a for a in raw_accounts
            if a.get("status") == "INACTIVE_COLD"
            and _is_cold_account_usable(a, now_ts=now_ts)[0]
        ]
        target_list = cold_accs[:count]
        WAKE_STATUS["total"] = len(target_list)
        print(f"[冷库唤醒] 启动平缓唤醒任务，目标: {len(target_list)} 个 (冷库共 {len(cold_accs)} 个)，安全间隔: {delay:.0f}s/账号")

        for idx, acc in enumerate(target_list, 1):
            email = acc.get("account_label") or acc.get("email") or acc.get("_email")
            WAKE_STATUS["current"] = idx
            WAKE_STATUS["last_account"] = email
            WAKE_STATUS["last_msg"] = f"正在激活 ({idx}/{len(target_list)}): {email} ..."
            print(f"[冷库唤醒 {idx}/{len(target_list)}] 正在激活: {email} ...")

            ok, msg = await asyncio.to_thread(do_activate_cold_account, email)
            if ok:
                WAKE_STATUS["success_count"] += 1
                WAKE_STATUS["last_msg"] = f"✓ {email} 激活成功并入池！"
                print(f"[冷库唤醒 {idx}/{len(target_list)}] ✅ {email} 激活成功，已加入活跃池！")
            else:
                WAKE_STATUS["fail_count"] += 1
                WAKE_STATUS["last_msg"] = f"[-] {email} 激活失败: {msg}"
                print(f"[冷库唤醒 {idx}/{len(target_list)}] ❌ {email} 激活失败: {msg}")

            if idx < len(target_list):
                WAKE_STATUS["last_msg"] += f" (安全等待 {delay:.0f}s)"
                print(f"[冷库唤醒] 安全等待冷却 {delay:.0f} 秒后处理下一个账号...")
                await asyncio.sleep(delay)

        WAKE_STATUS["last_msg"] = f"🏁 唤醒任务完成！成功: {WAKE_STATUS['success_count']}，失败: {WAKE_STATUS['fail_count']}"
        print(f"\n🎉 [冷库唤醒] 批处理完成！成功: {WAKE_STATUS['success_count']}，失败: {WAKE_STATUS['fail_count']}")
    except Exception as e:
        WAKE_STATUS["last_msg"] = f"唤醒批处理异常: {e}"
        print(f"[冷库唤醒] 异常中断: {e}")
    finally:
        WAKE_STATUS["is_waking"] = False
        WAKE_STATUS["running"] = False


@router.post("/api/accounts/activate", dependencies=[Depends(require_admin)])
async def activate_account_route(body: AccountActionRequest):
    ok, msg = await asyncio.to_thread(do_activate_cold_account, body.account)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.post("/api/accounts/demote_cold", dependencies=[Depends(require_admin)])
@router.post("/api/accounts/demote_to_standby", dependencies=[Depends(require_admin)])
async def demote_cold_route(body: AccountActionRequest):
    from app.core.security import disown_account
    from app.db.repositories import account_repo
    raw_acc = next((a for a in config_manager.get_accounts() if a.get('account_label') == body.account or a.get('email') == body.account), None)
    if not raw_acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    has_mfa = bool(raw_acc.get("credential_id") and raw_acc.get("mfa_private_key"))
    if not has_mfa:
        raise HTTPException(
            status_code=400,
            detail="该账号为旧版一次性账号，缺少 5 件套 MFA 受信任凭证，清空 Token 进入冷库后无法自动唤醒！建议直接在活跃池使用至自然失效。"
        )

    disown_account(body.account)
    config_manager.update_account(
        body.account,
        token="",
        cookie="",
        session_id="",
        status="INACTIVE_COLD",
        is_valid=False,
        mute_until=None,
        ban_kind=None,
        mute_count=0,
    )
    account_repo.update_runtime_state(
        body.account,
        token="",
        session_id="",
        status="INACTIVE_COLD",
        is_valid=False,
        mute_until=None,
        ban_kind=None,
        mute_count=0,
    )
    account_pool.reload()
    try:
        from app.engine.trusted_relogin_engine import clear_http_renewal
        clear_http_renewal(body.account)
        em = raw_acc.get("_email") or raw_acc.get("email")
        if em and em != body.account:
            clear_http_renewal(em)
    except Exception:
        pass
    return {"ok": True, "message": f"账号 {body.account} 已退入零IP冷库 (INACTIVE_COLD)，Token 已注销，静默冷储备"}


@router.post("/api/accounts/quarantine_rehab", dependencies=[Depends(require_admin)])
async def quarantine_rehab_route(body: AccountActionRequest):
    from app.core.security import disown_account
    from app.db.repositories import account_repo
    raw_acc = next((a for a in config_manager.get_accounts() if a.get('account_label') == body.account or a.get('email') == body.account), None)
    if not raw_acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    disown_account(body.account)
    has_mfa = bool(raw_acc.get("credential_id") and raw_acc.get("mfa_private_key"))
    if has_mfa:
        target_status = "INACTIVE_COLD"
        target_valid = False
        target_token = ""
        msg = f"账号 {body.account} 已解除隔离并归入零IP冷库 (INACTIVE_COLD) 备用"
    else:
        if raw_acc.get("token"):
            target_status = "ACTIVE"
            target_valid = True
            target_token = raw_acc.get("token")
            msg = f"账号 {body.account} 已解除隔离并重回活跃池"
        else:
            target_status = "INVALID"
            target_valid = False
            target_token = ""
            msg = f"账号 {body.account} 无 5 件套且无 Token，解除隔离后标记为 INVALID"

    config_manager.update_account(
        body.account,
        token=target_token,
        session_id="",
        status=target_status,
        is_valid=target_valid,
        mute_until=None,
        ban_kind=None,
        mute_count=0,
    )
    account_repo.update_runtime_state(
        body.account,
        token=target_token,
        session_id="",
        status=target_status,
        is_valid=target_valid,
        mute_until=None,
        ban_kind=None,
        mute_count=0,
    )
    account_pool.reload()
    return {"ok": True, "message": msg}


@router.post("/api/accounts/quarantine_clear", dependencies=[Depends(require_admin)])
async def quarantine_clear_route():
    from app.core.security import disown_account
    now_ts = time.time()
    raw_accounts = config_manager.get_accounts()
    to_remove = []
    for a in raw_accounts:
        st = a.get("status") or ""
        bk = a.get("ban_kind")
        mu = a.get("mute_until")
        # 保护冷库储备与有效有期禁言：
        if st == "INACTIVE_COLD":
            continue
        if bk == "dated" and mu and mu > now_ts:
            continue
        # 其余永久封禁、已确认失效或已进入垃圾隔离库的账号，一律彻底清理
        if "QUARANTINE" in st or st in ("MUTED", "INVALID", "QUARANTINE_INVALID", "QUARANTINE_MUTED") or bk == "permanent" or not a.get("is_valid"):
            to_remove.append(a["account_label"])
    for lbl in to_remove:
        disown_account(lbl)
        config_manager.remove_account(lbl)
    account_pool.reload()
    return {"ok": True, "count": len(to_remove), "message": f"已清空 {len(to_remove)} 个永久隔离垃圾账号（有期禁言账号已自动保留受保护）"}


# ── 调用统计 ──

@router.get("/api/usage", dependencies=[Depends(require_admin)])
async def get_usage():
    return metrics_engine.get_summary()


# ── API Keys ──

@router.get("/api/keys", dependencies=[Depends(require_admin)])
async def get_keys(category: Optional[str] = None):
    from app.core.security import sanitize_key_bindings, recycle_expired_keys
    sanitize_key_bindings()
    recycle_expired_keys()
    all_keys = list_keys()
    for k in all_keys:
        k_type = k.get("key_type") or ("card" if "卡" in str(k.get("name", "") + k.get("owner", "")) else ("user" if k.get("user_id") else "favor"))
        k["key_type"] = k_type
        k["is_card"] = (k_type == "card")
        k["is_favor"] = (k_type == "favor")
        k["is_user"] = (k_type == "user" or bool(k.get("user_id")))
        k["category"] = "card" if k["is_card"] else ("favor" if k["is_favor"] else "user")

    card_keys = [k for k in all_keys if k["is_card"]]
    favor_keys = [k for k in all_keys if k["is_favor"]]
    user_keys = [k for k in all_keys if k["is_user"]]

    filtered = all_keys
    if category == "card":
        filtered = card_keys
    elif category == "favor":
        filtered = favor_keys
    elif category == "user":
        filtered = user_keys

    return {
        "keys": filtered,
        "counts": {
            "total": len(all_keys),
            "card": len(card_keys),
            "favor": len(favor_keys),
            "user": len(user_keys),
        }
    }


@router.post("/api/admin/keys/{key_id}/move-to-pool", dependencies=[Depends(require_admin)])
async def move_key_to_pool_route(key_id: str):
    """将某把人情 Key 绑定的独占账号彻底移入公共卡池，Key 转为使用公共共享池"""
    from app.core.security import move_key_to_pool
    try:
        return move_key_to_pool(key_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Key 不存在")


@router.post("/api/keys", dependencies=[Depends(require_admin)])
async def create_key(body: AddKeyRequest):
    expires_at = body.expires_at
    if body.days and body.days > 0:
        expires_at = int(time.time()) + int(body.days) * 86400

    key_type = body.key_type or "normal"
    if body.card_category or body.days or (expires_at and expires_at > 0):
        key_type = "card"

    rec = generate_key(
        body.name,
        owner=body.owner or "",
        account_labels=body.account_labels or [],
        sensitive_filter=body.sensitive_filter if body.sensitive_filter is not None else True,
        key_type=key_type,
        expires_at=expires_at,
        card_category=body.card_category,
    )
    return rec


@router.put("/api/keys/{key_id}", dependencies=[Depends(require_admin)])
async def edit_key(key_id: str, body: UpdateKeyRequest):
    """改归属人 / 启用停用 / 公共池回退 / 敏感词过滤 / 每日配额"""
    try:
        rec = update_key(
            key_id,
            owner=body.owner,
            enabled=body.enabled,
            pool_fallback=body.pool_fallback,
            sensitive_filter=body.sensitive_filter,
            quota_daily_requests=body.quota_daily_requests,
            quota_daily_tokens=body.quota_daily_tokens,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Key 不存在")
    return {"ok": True, "key": rec}


@router.get("/api/admin/user-pools", dependencies=[Depends(require_admin)])
async def get_user_pools():
    """获取所有注册用户的个人池状态及名下共享 Key 汇总"""
    from app.db.repositories import user_repo, api_key_repo
    from app.engine.account_pool import account_pool
    
    users = user_repo.list_all()
    all_keys = api_key_repo.list_all()
    keys_by_user = {}
    for k in all_keys:
        uid = k.get("user_id")
        if uid:
            keys_by_user.setdefault(uid, []).append({
                "id": k.get("id"),
                "name": k.get("name"),
                "prefix": k.get("prefix"),
                "enabled": bool(k.get("enabled", 1)),
                "revoked": bool(k.get("revoked", 0)),
                "last_used_at": k.get("last_used_at"),
            })
            
    out = []
    for u in users:
        uid = u["id"]
        pool_labels = user_repo.get_personal_pool(uid)
        healthy = 0
        for lbl in pool_labels:
            acc = account_pool._accounts.get(lbl)
            if acc and acc.status == "ACTIVE" and acc.is_valid and not acc.is_muted_now():
                healthy += 1
        
        active_sub = None
        out.append({
            "id": uid,
            "email": u.get("email"),
            "username": u.get("username"),
            "tier": u.get("tier", "free"),
            "is_vip": bool(u.get("tier") in ("standard", "pro") or active_sub is not None),
            "subscription": active_sub,
            "personal_pool": pool_labels,
            "auto_replenish": bool(u.get("auto_replenish", 1)),
            "keys": keys_by_user.get(uid, []),
            "pool_health": {
                "total": len(pool_labels),
                "healthy": healthy,
                "needs_replenish": len(pool_labels) > healthy
            }
        })
    return {"user_pools": out}


@router.put("/api/admin/user-pools/{user_id}", dependencies=[Depends(require_admin)])
async def update_user_pool(user_id: str, body: UserPoolUpdateRequest):
    """更新用户个人号池：名下所有 Key 立即共享生效（拒绝跨用户抢号冲突）"""
    from app.db.repositories import user_repo
    from app.core.security import account_owner_map
    from app.engine.account_pool import account_pool
    user = user_repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    owners = account_owner_map()
    conflicts = []
    for raw_lbl in (body.account_labels or []):
        lbl = str(raw_lbl or "").strip()
        if not lbl:
            continue
        owner_info = owners.get(lbl)
        if owner_info and owner_info.get("user_id") != user_id:
            conflicts.append(f"{lbl} (当前归属: {owner_info.get('owner') or owner_info.get('key_name')})")
    if conflicts:
        raise HTTPException(status_code=409, detail=f"账号归属冲突，禁止跨户分配: {', '.join(conflicts)}")
    user_repo.set_personal_pool(user_id, body.account_labels, auto_replenish=body.auto_replenish)
    account_pool.reload()
    return {"ok": True, "personal_pool": user_repo.get_personal_pool(user_id)}


@router.post("/api/admin/user-pools/{user_id}/replenish", dependencies=[Depends(require_admin)])
async def trigger_user_replenish(user_id: str):
    """超管一键为该付费用户个人池补号（严格遵守 pool_tag 与 VIP 隔离）"""
    from app.db.repositories import user_repo, alert_repo
    from app.engine.account_pool import account_pool, _owner_map
    user = user_repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    owners = _owner_map()
    is_vip_user = bool(user.get("is_vip")) or str(user.get("tier") or "").lower() in ("vip", "pro", "ultra")
    # 优先从储备/空闲号中选择（严禁划拨一次性号，非 VIP 禁止划拨 VIP 专属号）
    candidates = [
        lbl for lbl, acc in account_pool._accounts.items()
        if lbl not in owners
        and acc.status == "ACTIVE"
        and acc.is_valid
        and not acc.is_muted_now()
        and getattr(acc, "pool_tag", "public") != "disposable"
        and not getattr(acc, "disposable", False)
        and (is_vip_user or getattr(acc, "pool_tag", "public") != "vip")
    ]
    if not candidates:
        raise HTTPException(status_code=400, detail="当前暂无可用合规储备号可供补位")
    candidates.sort(key=lambda lbl: (0 if getattr(account_pool._accounts[lbl], "pool_tag", "") == "reserve" else 1))

    chosen = candidates[0]
    user_repo.add_to_personal_pool(user_id, chosen)
    account_pool.reload()
    alert_repo.add_alert(
        account=chosen,
        alert_type="MANUAL_HOTSWAP",
        message=f"超管手动为用户「{user.get('email')}」补充账号: {chosen}"
    )
    return {"ok": True, "replenished_account": chosen, "personal_pool": user_repo.get_personal_pool(user_id)}


@router.put("/api/admin/accounts/{account_label}/pool-tag", dependencies=[Depends(require_admin)])
async def set_account_pool_tag(account_label: str, body: Dict[str, Any]):
    """设置账号池标签（vip / public / reserve / favor / disposable），支持一键切换一次性状态"""
    from app.db.repositories import account_repo
    from app.engine.account_pool import account_pool
    pool_tag = str(body.get("pool_tag", "public")).lower()
    if pool_tag not in ("vip", "public", "reserve", "favor", "disposable", "active"):
        raise HTTPException(status_code=400, detail="pool_tag 必须为 active, public, vip, reserve, favor 或 disposable")
    
    is_disp = pool_tag == "disposable" or bool(body.get("is_disposable"))
    ok = account_repo.update_pool_tag(account_label, pool_tag)
    if not ok:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    config_manager.update_account(
        account_label,
        pool_tag=pool_tag,
        is_disposable=is_disp,
        disposable=is_disp
    )
    
    acc = account_pool._accounts.get(account_label)
    if acc:
        acc.pool_tag = pool_tag
        acc.disposable = is_disp
    return {"ok": True, "account_label": account_label, "pool_tag": pool_tag, "is_disposable": is_disp}


# ── T25: 按 key 的用量 ──

@router.get("/api/usage/by-key", dependencies=[Depends(require_admin)])
async def get_usage_by_key():
    """每个 API Key 的今日/累计用量 + 配额使用率。

    这是"谁在跑、谁跑飞了、谁被挡住了"的唯一操作台数据源。
    """
    usage = key_usage_engine.all_snapshots()
    keys = [k for k in list_keys() if not k.get("revoked")]

    rows = []
    for k in keys:
        kid = k.get("id", "")
        u = usage.get(kid) or {}
        used_req = int(u.get("requests", 0))
        used_tok = int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))
        lim_req = int(k.get("quota_daily_requests") or 0)
        lim_tok = int(k.get("quota_daily_tokens") or 0)

        def _pct(used, limit):
            if limit <= 0:
                return None
            return round(min(100.0, used * 100.0 / limit), 1)

        rows.append({
            "id": kid,
            "name": k.get("name", ""),
            "owner": k.get("owner") or "",
            "enabled": k.get("enabled", True),
            "account_labels": k.get("account_labels") or [],
            "max_concurrent_streams": len(k.get("account_labels") or []) * MAX_CONCURRENT_PER_ACCOUNT,
            "pool_fallback": k.get("pool_fallback", False),
            "today": {
                "date": u.get("day", ""),
                "requests": used_req,
                "prompt_tokens": int(u.get("prompt_tokens", 0)),
                "completion_tokens": int(u.get("completion_tokens", 0)),
                "total_tokens": used_tok,
                "errors": int(u.get("errors", 0)),
                "rejected_by_quota": int(u.get("rejected", 0)),
                "avg_duration_ms": round(float(u.get("duration_ms", 0.0)) / used_req, 1) if used_req else 0.0,
            },
            "lifetime": {
                "requests": int(u.get("total_requests", 0)),
                "total_tokens": int(u.get("total_tokens", 0)),
                "last_used_at": u.get("last_used_at"),
                "last_error": u.get("last_error", ""),
            },
            "quota": {
                "daily_requests": lim_req,
                "daily_tokens": lim_tok,
                "used_percent_requests": _pct(used_req, lim_req),
                "used_percent_tokens": _pct(used_tok, lim_tok),
            },
        })

    # 历史流水（已作废的 key 记录保留下来，供审计查看）
    hist = {k["id"]: key_usage_engine.history(k["id"]) for k in keys}
    revoked_usage = key_usage_engine.revoked_snapshots()

    return {
        "keys": rows,
        "history": hist,
        "revoked": {
            "count": len(revoked_usage),
            "items": [
                {
                    "id": kid,
                    "label": u.get("label", ""),
                    "revoked_at": u.get("revoked_at"),
                    "requests": int(u.get("total_requests", 0)),
                    "total_tokens": int(u.get("total_tokens", 0)),
                    "last_used_at": u.get("last_used_at"),
                }
                for kid, u in revoked_usage.items()
            ],
        },
        "pool": {
            "in_pool_accounts": len([
                a for a in config_manager.get_accounts()
                if a.get("account_label") not in account_owner_map()
            ]),
            "max_concurrent_streams": MAX_CONCURRENT_PER_ACCOUNT,
        },
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.post("/api/usage/reset", dependencies=[Depends(require_admin)])
async def reset_key_usage(body: ResetUsageRequest):
    """清零某个 key 的今日计数（不动累计量，也不动其他 key）"""
    if not key_usage_engine.reset(body.key_id):
        raise HTTPException(status_code=404, detail="该 key 今日没有用量记录")
    return {"ok": True, "message": "今日计数已清零"}


@router.post("/api/usage/prune", dependencies=[Depends(require_admin)])
async def prune_revoked_usage(keep_days: int = 0):
    """清理已作废 key 的历史用量记录（默认清空全部已作废的）。

    撤销 key 时只标记、不删数据 —— 因为撤销往往正是"他把号跑飞了"的时刻，
    那恰恰是最需要历史证据的时候。清理留给管理员显式决定。
    """
    n = key_usage_engine.prune_revoked(keep_days=max(0, keep_days))
    return {"ok": True, "removed": n, "message": f"已清理 {n} 条已作废 key 的用量记录"}


@router.put("/api/keys/{key_id}/accounts", dependencies=[Depends(require_admin)])
async def assign_key_accounts(key_id: str, body: AssignAccountsRequest):
    """把账号分配给某个 key（覆盖式）。

    **不变式：一个账号同一时刻只能属于一个 key。**
    若目标账号已被别人占用，返回 409 并说明被谁占了 —— 而不是静默抢占。
    静默抢占会让另一个用户在自己毫不知情的情况下突然开始拿到 429，
    这种故障极难归因，所以宁可让分配失败得明明白白。
    """
    try:
        result = set_key_accounts(key_id, body.account_labels)
    except KeyError:
        raise HTTPException(status_code=404, detail="Key 不存在")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, **result}


@router.delete("/api/keys/purge-revoked", dependencies=[Depends(require_admin)])
@router.delete("/api/admin/keys/revoked", dependencies=[Depends(require_admin)])
async def purge_revoked_keys_endpoint():
    """彻底物理删除所有已作废或已禁用的 Key 记录"""
    from app.core.security import purge_revoked_keys
    count = purge_revoked_keys()
    return {"ok": True, "count": count, "message": f"已成功彻底删除 {count} 条作废 Key 记录"}


@router.delete("/api/keys/{key_id}", dependencies=[Depends(require_admin)])
async def remove_key(key_id: str, purge: bool = False):
    from app.core.security import delete_key
    if purge:
        ok = delete_key(key_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Key 不存在")
        return {"ok": True, "message": "Key 已物理删除"}
    ok = delete_key(key_id) or revoke_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key 不存在")
    return {"ok": True, "message": "Key 已删除"}


class AdminSetUserTierRequest(BaseModel):
    tier: str  # 'free', 'pro', 'ultra', 'legacy'
    role: Optional[str] = None  # 'admin', 'user'



class AdminManageSubscriptionRequest(BaseModel):
    tier: str = Field(..., description="付费档位: free / standard / pro / ultra / legacy")
    period_end: Optional[int] = Field(None, description="自定义确切到期时间戳")
    add_days: Optional[int] = Field(None, description="从当前或到期时间顺延的天数")
    auto_allocate_account: bool = Field(True, description="是否自动分配/补充专属保供账号")
    account_count: int = Field(1, description="期望绑定的专属账号数量")
    bonus_tokens: Optional[int] = Field(None, description="调整额外赠送额度")


@router.get("/api/accounts/ownership", dependencies=[Depends(require_admin)])
async def get_accounts_ownership():
    """账号池 × 归属 的完整视图：谁名下有哪几个号、哪些号还在公共池。

    这是"一人一号 + 剩余留池"的操作台数据源。
    """
    owners = account_owner_map()
    accounts = config_manager.get_accounts()

    items = []
    assigned = 0
    for a in accounts:
        lbl = a.get("account_label", "")
        own = owners.get(lbl)
        if own:
            assigned += 1
        items.append({
            "account_label": lbl,
            "is_valid": a.get("is_valid", True),
            "status": a.get("status") or ("ACTIVE" if a.get("is_valid", True) else "MUTED"),
            "owner_key_id": (own or {}).get("key_id", ""),
            "owner": (own or {}).get("owner", ""),
            "in_pool": own is None,
        })

    keys = [k for k in list_keys() if not k.get("revoked")]
    return {
        "accounts": items,
        "keys": [
            {
                "id": k.get("id"),
                "name": k.get("name"),
                "owner": k.get("owner") or "",
                "enabled": k.get("enabled", True),
                "pool_fallback": k.get("pool_fallback", False),
                "account_labels": k.get("account_labels") or [],
                "last_used_at": k.get("last_used_at"),
            }
            for k in keys
        ],
        "summary": {
            "total": len(items),
            "assigned": assigned,
            "in_pool": len(items) - assigned,
            "keys": len(keys),
            "concurrency_capacity": len(items) * MAX_CONCURRENT_PER_ACCOUNT,
        },
    }


# ── 批量注册与冷库管理 ──

@router.post("/api/register/preflight", dependencies=[Depends(require_admin)])
async def quick_register_preflight(body: QuickRegisterPreflightRequest):
    ok, message = await asyncio.to_thread(quick_register_engine.preflight_probe, (body.proxy or "").strip() or None)
    return {"ok": ok, "message": message}


@router.post("/api/register/quick/start", dependencies=[Depends(require_admin)])
async def quick_register_start(body: QuickRegisterStartRequest):
    try:
        job = await asyncio.to_thread(
            quick_register_engine.start,
            email=body.email,
            password=body.password or "",
            proxy=body.proxy or "",
            farm_url=body.farm_url or DEFAULT_FARM_SOLVE_URL,
            captcha_token=body.captcha_token or "",
            device_id=body.device_id or "",
            pool_tag=body.pool_tag or "public",
            skip_preflight=bool(body.skip_preflight),
        )
        return {"ok": True, "job": job}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/register/quick/verify_register", dependencies=[Depends(require_admin)])
async def quick_register_verify(body: QuickRegisterVerifyRequest):
    try:
        job = await asyncio.to_thread(
            quick_register_engine.verify_register,
            job_id=body.job_id,
            register_otp=body.register_otp,
            login_captcha_token=body.login_captcha_token or "",
            login_device_id=body.login_device_id or "",
        )
        return {"ok": True, "job": job}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/register/quick/complete", dependencies=[Depends(require_admin)])
async def quick_register_complete(body: QuickRegisterCompleteRequest):
    try:
        job = await asyncio.to_thread(
            quick_register_engine.complete,
            job_id=body.job_id,
            mfa_otp=body.mfa_otp,
        )
        return {"ok": True, "job": job}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/register/quick/jobs", dependencies=[Depends(require_admin)])
async def quick_register_jobs():
    return {"ok": True, "jobs": quick_register_engine.list_jobs()}


@router.get("/api/register/quick/jobs/{job_id}", dependencies=[Depends(require_admin)])
async def quick_register_job_detail(job_id: str):
    job = quick_register_engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="注册任务不存在")
    return {"ok": True, "job": job}


def _clean_token(raw: str) -> str:
    """自动兼容原生 Token、Bearer 前缀以及内部存储的 {"value":"...","__version":"0"} 格式"""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("{") and "value" in raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("value"):
                raw = str(data["value"]).strip()
        except Exception:
            pass
    if raw.startswith("Bearer "):
        raw = raw[7:].strip()
    return raw.strip('"\'')


def passive_verify_single_token(token: str, proxy: Optional[str] = None, timeout: int = 10) -> Dict[str, Any]:
    """
    零风控官方被动静默探活 (GET /api/v0/users/current)
    严禁调用账密重登或主动发消息，仅验证 Token 存活性与是否被禁言
    """
    clean_tok = _clean_token(token)
    if not clean_tok or len(clean_tok) < 20:
        return {"is_valid": False, "is_muted": False, "msg": "Token 格式无效/缺失"}

    headers = {
        "Authorization": f"Bearer {clean_tok}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = cffi_requests.get(
            "https://chat.deepseek.com/api/v0/users/current",
            headers=headers,
            proxies=proxies,
            impersonate="chrome124",
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                biz_data = (data.get("data") or {}).get("biz_data") or {}
                chat_info = biz_data.get("chat") or {}
                is_muted = chat_info.get("is_muted", 0) == 1
                mute_until = _parse_mute_until(chat_info.get("mute_until"))
                if is_muted:
                    return {
                        "is_valid": False,
                        "is_muted": True,
                        "mute_until": mute_until,
                        "raw_email": biz_data.get("email"),
                        "msg": "Token有效但账号已被官方禁言(MUTED)",
                    }
                return {
                    "is_valid": True,
                    "is_muted": False,
                    "raw_email": biz_data.get("email"),
                    "msg": "正常可用",
                }
            else:
                return {
                    "is_valid": False,
                    "is_muted": False,
                    "msg": data.get("msg") or f"官方业务返回异常: {data.get('code')}",
                }

        if r.status_code in (401, 403):
            data = {}
            try:
                data = r.json()
            except Exception:
                pass
            msg = data.get("msg") or f"Token已失效 (HTTP {r.status_code})"
            return {"is_valid": False, "is_muted": False, "msg": msg}

        if r.status_code in (500, 502, 503, 504, 520, 521, 522, 524):
            return {
                "is_valid": False,
                "is_muted": False,
                "msg": f"官方服务瞬态故障 (HTTP {r.status_code})",
            }

        return {
            "is_valid": False,
            "is_muted": False,
            "msg": f"探活异常 (HTTP {r.status_code})",
        }
    except Exception as e:
        return {"is_valid": False, "is_muted": False, "msg": f"网络异常: {e}"}

@router.post("/api/accounts/batch_import", dependencies=[Depends(require_admin)])
async def batch_import_accounts(body: BatchImportAccountsRequest):
    """批量解析并导入账号 (集成智能去重、官方零风控被动静默探活与双轨入库)"""
    raw_items = []

    # 1. 优先解析 accounts_list
    if body.accounts_list and isinstance(body.accounts_list, list):
        raw_items = body.accounts_list
    # 2. 解析 accounts_text
    elif body.accounts_text and body.accounts_text.strip():
        text = body.accounts_text.strip()
        # 尝试整体 JSON 数组
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    raw_items = parsed
            except Exception:
                pass
        # 若非数组，按 JSONL 逐行解析
        if not raw_items:
            for line in text.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        raw_items.append(obj)
                except Exception:
                    continue

    if not raw_items:
        raise HTTPException(status_code=400, detail="未能解析出任何有效的账号 JSON / JSONL 数据")

    raw_target = (body.target_status or "ACTIVE").strip().upper()
    if raw_target in ("INACTIVE_COLD", "COLD", "STANDBY"):
        target_status = "INACTIVE_COLD"
    else:
        target_status = "ACTIVE"
    pool_tag = (body.pool_tag or "public").strip()
    notes = (body.notes or "批量文件上传入库").strip()
    now_ts = int(time.time())

    # 收集当前库中已有的邮箱、Token 与真实数美指纹 (支持一对一绑定校验与去重拦截)
    from app.engine.http_auth_engine import extract_shumei_device_id, is_valid_shumei_device_id
    existing_emails = set()
    existing_tokens = set()
    existing_fps: Dict[str, str] = {}
    existing_bad_reasons: Dict[str, str] = {}

    def _collect_existing_bad(rec: dict, em_key: str) -> None:
        if not em_key:
            return
        bk = str(rec.get("ban_kind") or "").strip().lower()
        st = str(rec.get("status") or "").strip().upper()
        mu = _parse_mute_until(rec.get("mute_until"))
        if bk in ("permanent", "mfa_required") or st in ("BANNED", "PERMANENT_BANNED", "MFA_REQUIRED"):
            existing_bad_reasons[em_key] = f"库内原账号已失效 ({bk or st})"
        elif bk == "dated" or (mu and mu > now_ts) or st in ("MUTED", "QUARANTINE_MUTED"):
            existing_bad_reasons[em_key] = f"库内原账号处于禁言/隔离期 ({bk or st})"

    try:
        from app.db.repositories import account_repo
        for r in account_repo.list_all():
            em = (r.get("email") or r.get("account_label") or "").strip().lower()
            if em:
                existing_emails.add(em)
                _collect_existing_bad(r, em)
            tok = (r.get("token") or "").strip()
            if tok:
                existing_tokens.add(tok)
            fp = extract_shumei_device_id(r)
            if is_valid_shumei_device_id(fp) and fp not in existing_fps:
                existing_fps[fp] = em
    except Exception:
        pass

    for a in config_manager.accounts:
        em = (a.get("email") or a.get("account_label") or "").strip().lower()
        if em:
            existing_emails.add(em)
            _collect_existing_bad(a, em)
        tok = (a.get("token") or "").strip()
        if tok:
            existing_tokens.add(tok)
        fp = extract_shumei_device_id(a)
        if is_valid_shumei_device_id(fp) and fp not in existing_fps:
            existing_fps[fp] = em

    # 阶段 1：格式校验、5件套提取与严格去重（非一次性账号强制唯一 B... 数美指纹）
    candidates = []
    skipped_list = []
    seen_batch_emails = set()
    seen_batch_tokens = set()
    seen_batch_fps = set()

    for item in raw_items:
        email = (item.get("email") or item.get("account_label") or item.get("_email") or "").strip()
        pwd = (item.get("password") or item.get("_password") or "").strip()
        raw_tok = item.get("token") or ""
        token = _clean_token(raw_tok)
        sid = (item.get("session_id") or "").strip()

        cred_id = item.get("credential_id")
        priv_key = item.get("mfa_private_key")
        pub_key = item.get("mfa_public_key")
        key_ver = item.get("mfa_key_version")
        dev_id = item.get("device_id")
        from app.engine.http_auth_engine import parse_expire_at, evaluate_trusted_device, extract_token_issued_at
        expire_at = parse_expire_at(item.get("expire_at") or item.get("mfa_expire_at"))
        raw_tok_upd = parse_expire_at(item.get("token_updated_at") or item.get("token_issued_at"))
        if not raw_tok_upd and token:
            raw_tok_upd = extract_token_issued_at(token, now_ts=now_ts)
        if not raw_tok_upd and token:
            cand_created = parse_expire_at(item.get("created_at") or item.get("updated_at"))
            if cand_created and cand_created <= now_ts + 300:
                raw_tok_upd = cand_created
        tok_updated_at = raw_tok_upd if (raw_tok_upd and raw_tok_upd <= now_ts + 300) else None
        item_pool_tag = str(item.get("pool_tag") or "").strip().lower()
        cand_pool_tag = item_pool_tag if item_pool_tag in ("vip", "public", "reserve", "favor", "disposable", "active") else pool_tag
        if cand_pool_tag == "active":
            cand_pool_tag = "public"
        is_disp = bool(
            item.get("is_disposable") is True
            or item.get("disposable") is True
            or cand_pool_tag == "disposable"
        )
        if is_disp:
            cand_pool_tag = "disposable"
        eval_cand = evaluate_trusted_device({
            "credential_id": cred_id,
            "mfa_private_key": priv_key,
            "mfa_public_key": pub_key,
            "mfa_key_version": key_ver,
            "device_id": dev_id,
            "device_fingerprint": item.get("device_fingerprint"),
            "cookie": item.get("cookie") or "",
            "expire_at": expire_at,
            "pool_tag": cand_pool_tag,
            "disposable": is_disp,
        }, now_ts=now_ts)
        has_5piece = bool(eval_cand["device_trusted"])
        bound_fp = str(eval_cand.get("shumei_device_id") or "").strip()

        if not email or "@" not in email:
            skipped_list.append({
                "email": email or "unknown",
                "reason": "邮箱格式无效",
                "category": "INVALID_FORMAT",
            })
            continue

        email_lower = email.lower()

        if not is_disp:
            if not eval_cand.get("has_valid_shumei_fp"):
                skipped_list.append({
                    "email": email,
                    "reason": "非一次性账号必须绑定真实有效的 B... 数美指纹（禁止空指纹/UUID/示例兜底指纹）",
                    "category": "INVALID_FINGERPRINT",
                })
                continue
            if body.deduplicate and bound_fp in seen_batch_fps:
                skipped_list.append({
                    "email": email,
                    "reason": "批次内存在重复数美指纹（严格一对一绑定，禁止跨号复用）",
                    "category": "DUPLICATE_FINGERPRINT",
                })
                continue
            if body.deduplicate and bound_fp in existing_fps and existing_fps[bound_fp] != email_lower:
                skipped_list.append({
                    "email": email,
                    "reason": f"该数美指纹已被库内账号 {existing_fps[bound_fp]} 占用（严格一对一绑定）",
                    "category": "DUPLICATE_FINGERPRINT",
                })
                continue

        if target_status == "INACTIVE_COLD":
            item_bk = str(item.get("ban_kind") or "").strip().lower()
            item_mu = _parse_mute_until(item.get("mute_until"))
            if item_bk in ("permanent", "mfa_required", "dated") or (item_mu and item_mu > now_ts):
                skipped_list.append({
                    "email": email,
                    "reason": f"源记录带有封禁/禁言标记 ({item_bk or 'muted'})，禁止存入冷库",
                    "category": "ALREADY_BAD",
                })
                continue
            if email_lower in existing_bad_reasons:
                skipped_list.append({
                    "email": email,
                    "reason": f"{existing_bad_reasons[email_lower]}，禁止覆盖回零IP冷库",
                    "category": "ALREADY_BAD",
                })
                continue
            if not has_5piece or not pwd:
                skipped_list.append({
                    "email": email,
                    "reason": f"存入零IP冷库必须具备完整受信任设备 ({eval_cand.get('reason')}) 且保存明文密码",
                    "category": "NO_PASSWORD" if (has_5piece and not pwd) else "NOT_TRUSTED_COLD",
                })
                continue
            token = ""
        elif not token or len(token) < 20:
            if target_status == "ACTIVE" and has_5piece and pwd:
                token = ""  # 稍后现场上号铸造
            else:
                skipped_list.append({
                    "email": email,
                    "reason": "缺少有效 Token 且无完整 5 件套 MFA 凭证",
                    "category": "NO_TOKEN",
                })
                continue

        # 批次内去重
        if body.deduplicate:
            if email_lower in seen_batch_emails:
                skipped_list.append({
                    "email": email,
                    "reason": "批次内重复邮箱 (自动跳过)",
                    "category": "DUPLICATE_IN_BATCH",
                })
                continue
            if target_status != "INACTIVE_COLD" and token and token in seen_batch_tokens:
                skipped_list.append({
                    "email": email,
                    "reason": "批次内重复 Token (自动跳过)",
                    "category": "DUPLICATE_IN_BATCH",
                })
                continue

        seen_batch_emails.add(email_lower)
        if target_status != "INACTIVE_COLD" and token:
            seen_batch_tokens.add(token)
        if not is_disp and bound_fp:
            seen_batch_fps.add(bound_fp)

        # 与现有数据库/配置文件去重
        if body.deduplicate and not body.overwrite_existing:
            if email_lower in existing_emails:
                skipped_list.append({
                    "email": email,
                    "reason": "网关库内已存在该账号 (去重拦截)",
                    "category": "ALREADY_EXISTS",
                })
                continue
            if target_status != "INACTIVE_COLD" and token and token in existing_tokens:
                skipped_list.append({
                    "email": email,
                    "reason": "网关库内已存在相同 Token (去重拦截)",
                    "category": "ALREADY_EXISTS",
                })
                continue

        norm_fp_payload = item.get("device_fingerprint")
        if not norm_fp_payload and bound_fp:
            norm_fp_payload = json.dumps({"device_id": bound_fp}, ensure_ascii=False)

        candidates.append({
            "email": email,
            "email_lower": email_lower,
            "password": pwd,
            "token": token,
            "cookie": item.get("cookie") or "",
            "session_id": sid,
            "credential_id": cred_id,
            "mfa_private_key": priv_key,
            "mfa_public_key": pub_key,
            "mfa_key_version": key_ver,
            "device_id": bound_fp or dev_id,
            "device_fingerprint": norm_fp_payload,
            "profile_path": item.get("profile_path"),
            "expire_at": expire_at,
            "token_updated_at": tok_updated_at,
            "pool_tag": cand_pool_tag,
            "is_disposable": is_disp,
            "has_5piece": has_5piece,
            "is_renewable": bool(eval_cand["is_renewable"]),
        })

    # 阶段 2：探活与 5 件套上号处理
    surviving_candidates = []
    effective_proxy = config_manager.config.proxy
    loop = asyncio.get_event_loop()

    from app.engine.browser_login import quick_trusted_relogin

    if target_status == "INACTIVE_COLD":
        # 冷库储备模式：清空 Token，零 IP 静默入库
        for cand in candidates:
            cand["token"] = ""
            cand["session_id"] = ""
            cand["token_updated_at"] = 0
            surviving_candidates.append((cand, {"is_valid": False, "msg": "🧊 零IP冷库储备 (唤醒时上号)"}))
    else:
        # ACTIVE 模式：探活已有 Token，并在具备 5 件套时按策略刷出全新 Token
        for cand in candidates:
            tok = cand.get("token")
            if tok and body.check_liveness:
                probe = passive_verify_single_token(tok, proxy=effective_proxy)
                if probe.get("is_valid") and not probe.get("is_muted"):
                    if body.refresh_trusted_token and cand.get("has_5piece") and cand.get("password"):
                        ok, res = await loop.run_in_executor(
                            None, quick_trusted_relogin, cand["email"], cand["password"], 20.0, cand
                        )
                        if ok and isinstance(res, dict) and res.get("token"):
                            if res.get("is_muted"):
                                skipped_list.append({
                                    "email": cand["email"],
                                    "reason": "5件套上号刷新后检测到账号处于禁言期",
                                    "category": "MUTED",
                                })
                                continue
                            cand["token"] = res["token"]
                            cand["token_updated_at"] = int(time.time())
                            surviving_candidates.append((cand, {"is_valid": True, "msg": "探活通过，已通过 5 件套刷新全新 Token 入池"}))
                            continue
                        r_biz = res.get("biz_code") if isinstance(res, dict) else None
                        if r_biz in (10, 14):
                            err_m = res.get("error") if isinstance(res, dict) else str(res)
                            skipped_list.append({
                                "email": cand["email"],
                                "reason": f"5件套上号校验失败 (biz_code={r_biz}): {err_m}",
                                "category": "LOGIN_FAILED",
                            })
                            continue
                    surviving_candidates.append((cand, probe))
                    continue
                if probe.get("is_muted"):
                    skipped_list.append({
                        "email": cand["email"],
                        "token_preview": tok[:8] + "..." + tok[-6:] if len(tok) > 14 else tok,
                        "reason": f"探活未通过: {probe.get('msg') or '账号处于禁言期'}",
                        "category": "MUTED",
                    })
                    continue
                # Token 失效，尝试用 5 件套现场重登
                if cand.get("has_5piece") and cand.get("password"):
                    ok, res = await loop.run_in_executor(
                        None, quick_trusted_relogin, cand["email"], cand["password"], 20.0, cand
                    )
                    if ok and isinstance(res, dict) and res.get("token"):
                        cand["token"] = res["token"]
                        cand["token_updated_at"] = int(time.time())
                        surviving_candidates.append((cand, {"is_valid": True, "msg": "旧Token失效，已通过 5 件套现场重登刷新"}))
                    else:
                        err_m = res.get("error") if isinstance(res, dict) else str(res)
                        skipped_list.append({"email": cand["email"], "reason": f"5件套重登失败: {err_m}", "category": "LOGIN_FAILED"})
                else:
                    reason = probe.get("msg") or "Token 探活失败/已失效"
                    skipped_list.append({
                        "email": cand["email"],
                        "token_preview": tok[:8] + "..." + tok[-6:] if len(tok) > 14 else tok,
                        "reason": f"探活未通过: {reason}",
                        "category": "MUTED" if probe.get("is_muted") else "INVALID_TOKEN",
                    })
            elif not tok and cand.get("has_5piece") and cand.get("password"):
                # 无 Token，直接用 5 件套现场直登铸造新 Token
                ok, res = await loop.run_in_executor(
                    None, quick_trusted_relogin, cand["email"], cand["password"], 20.0, cand
                )
                if ok and isinstance(res, dict) and res.get("token"):
                    cand["token"] = res["token"]
                    cand["token_updated_at"] = int(time.time())
                    surviving_candidates.append((cand, {"is_valid": True, "msg": "已通过 5 件套免密直登铸造全新 Token 入池"}))
                else:
                    err_m = res.get("error") if isinstance(res, dict) else str(res)
                    skipped_list.append({"email": cand["email"], "reason": f"5件套免密直登失败: {err_m}", "category": "LOGIN_FAILED"})
            else:
                surviving_candidates.append((cand, {"is_valid": True, "msg": "未执行探活"}))

    # 阶段 3：存活账号正式入库与热生效 (持久化 5 件套字段)
    imported_list = []
    for cand, probe in surviving_candidates:
        email = cand["email"]
        token = cand["token"]
        is_active = (target_status == "ACTIVE")
        final_tok_upd = int(cand.get("token_updated_at") or (now_ts if token else 0))
        acc_dict = {
            "account_label": email,
            "email": email,
            "password": cand["password"],
            "token": token,
            "cookie": cand["cookie"],
            "session_id": cand["session_id"],
            "credential_id": cand.get("credential_id"),
            "mfa_private_key": cand.get("mfa_private_key"),
            "mfa_public_key": cand.get("mfa_public_key"),
            "mfa_key_version": cand.get("mfa_key_version"),
            "device_id": cand.get("device_id"),
            "device_fingerprint": cand.get("device_fingerprint"),
            "profile_path": cand.get("profile_path"),
            "expire_at": cand.get("expire_at"),
            "is_valid": is_active,
            "status": target_status,
            "pool_tag": cand.get("pool_tag") or pool_tag,
            "is_disposable": bool(cand.get("is_disposable")),
            "disposable": bool(cand.get("is_disposable")),
            "notes": notes,
            "created_at": now_ts,
            "updated_at": now_ts,
            "token_updated_at": final_tok_upd,
        }
        config_manager.add_account(acc_dict)
        existing_emails.add(cand["email_lower"])
        if token:
            existing_tokens.add(token)
        imported_list.append({
            "email": email,
            "token_preview": token[:8] + "..." + token[-6:] if len(token) > 14 else (token or "(零Token冷储备)"),
            "status": target_status,
            "has_trusted_mfa": cand.get("has_5piece", False),
            "action": "imported",
            "probe_msg": probe.get("msg", "入库成功")
        })

    # 热重载底层号池
    if imported_list:
        account_pool.reload()

    # 查当前活跃与冷库总数
    active_total = len([a for a in config_manager.accounts if a.get("status") == "ACTIVE" and a.get("is_valid")])
    cold_total = len([a for a in config_manager.accounts if a.get("status") == "INACTIVE_COLD"])

    dup_cnt = sum(1 for s in skipped_list if s.get("category") == "DUPLICATE_IN_BATCH")
    exist_cnt = sum(1 for s in skipped_list if s.get("category") == "ALREADY_EXISTS")
    dead_cnt = sum(1 for s in skipped_list if s.get("category") in ("INVALID_TOKEN", "LOGIN_FAILED"))
    mute_cnt = sum(1 for s in skipped_list if s.get("category") == "MUTED")
    no_tok_cnt = sum(1 for s in skipped_list if s.get("category") in ("NO_TOKEN", "INVALID_FORMAT"))

    status_desc = "活跃池 (ACTIVE)" if target_status == "ACTIVE" else "零IP冷库 (INACTIVE_COLD)"
    msg = (
        f"🎉 批量清洗完成：成功入库 {len(imported_list)} 个纯净可用账号至【{status_desc}】！"
        f"共拦截过滤 {len(skipped_list)} 个条目 (已有/重复: {exist_cnt + dup_cnt}，失效/失败: {dead_cnt + mute_cnt}，缺少凭证: {no_tok_cnt})。"
    )

    return {
        "ok": True,
        "message": msg,
        "total_parsed": len(raw_items),
        "imported_count": len(imported_list),
        "skipped_count": len(skipped_list),
        "active_total": active_total,
        "cold_total": cold_total,
        "summary": {
            "imported": len(imported_list),
            "duplicate_in_batch": dup_cnt,
            "already_exists": exist_cnt,
            "invalid_token": dead_cnt,
            "muted": mute_cnt,
            "no_token": no_tok_cnt,
        },
        "imported": imported_list,
        "skipped": skipped_list,
    }


@router.post("/api/accounts/upload_profile", dependencies=[Depends(require_admin)])
async def upload_profile_snapshot(file: UploadFile = File(...)):
    """接收产号器导出的**设备 profile 快照(zip)**，存入 data/profiles/。

    这是「一账号一份小黄金底座」的传输通道：产号器打完号后把精简 profile
    （约 100~150KB）通过公网 HTTPS 传上来，重登时用 launch_persistent_context 加载。
    """
    raw_name = os.path.basename(file.filename or "profile.zip")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)
    if not safe.lower().endswith(".zip"):
        safe += ".zip"

    content = await file.read()
    if len(content) < 1024:
        raise HTTPException(status_code=400, detail=f"快照文件过小({len(content)}B)，疑似异常")
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"快照文件过大({len(content)}B)，上限 8MB")
    if not content.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=400, detail="不是合法的 zip 文件")

    dest_dir = Path("/opt/ds-gateway/data/profiles")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        with open(dest, "wb") as f:
            f.write(content)
        try:
            os.chmod(dest, 0o600)
        except Exception:
            pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"快照落盘失败: {e}")

    return {"ok": True, "path": f"data/profiles/{safe}", "bytes": len(content), "name": safe}


@router.post("/api/register/manual/upload_file", dependencies=[Depends(require_admin)])
@router.post("/api/accounts/upload_file", dependencies=[Depends(require_admin)])
async def upload_accounts_file(
    file: UploadFile = File(...),
    target_status: str = Form("ACTIVE"),
    pool_tag: str = Form("public"),
    notes: str = Form("批量文件上传入库"),
    check_liveness: bool = Form(True),
    deduplicate: bool = Form(True),
):
    """支持客户端直接通过 multipart/form-data 上传 accounts_manual.jsonl 或 .json 文件入库"""
    try:
        content = await file.read()
        text = content.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件读取失败: {e}")
    req = BatchImportAccountsRequest(
        accounts_text=text,
        target_status=target_status,
        pool_tag=pool_tag,
        notes=notes,
        check_liveness=check_liveness,
        deduplicate=deduplicate,
    )
    return await batch_import_accounts(req)


# ── 系统设置 ──

@router.get("/api/settings", dependencies=[Depends(require_admin)])
async def get_settings():
    return {
        "proxy": config_manager.config.proxy or "",
        "passthrough": config_manager.config.passthrough,
        "port": config_manager.config.port,
        "host": config_manager.config.host,
        "cooldown_seconds": config_manager.config.cooldown_seconds,
        "proxies": list(getattr(config_manager.config, "proxies", None) or []),
        "relogin_proxies": list(getattr(config_manager.config, "relogin_proxies", None) or []),
    }


@router.put("/api/settings", dependencies=[Depends(require_admin)])
async def update_settings(body: SettingsUpdateRequest):
    if body.proxy is not None:
        config_manager.config.proxy = body.proxy.strip() or None
    if body.relogin_proxies is not None:
        cleaned = [str(x).strip() for x in body.relogin_proxies if str(x).strip()]
        config_manager.config.relogin_proxies = cleaned
    if body.passthrough is not None:
        config_manager.config.passthrough = body.passthrough
    config_manager.save()
    return {"ok": True, "message": "系统设置已更新"}


@router.put("/api/settings/password", dependencies=[Depends(require_admin)])
async def update_password(body: PasswordUpdateRequest):
    if len(body.password) < 4:
        raise HTTPException(status_code=400, detail="密码长度不能小于 4 位")
    config_manager.config.admin_password = body.password
    config_manager.save()
    return {"ok": True, "message": "管理员密码修改成功"}


# ── 运维监控与日志 ──

async def _fetch_system_log_lines(
    service: str = "ds-gateway",
    lines: Optional[int] = 300,
    date: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    filter_kw: Optional[str] = None,
    level: Optional[str] = None,
    max_lines: int = 50000,
) -> Tuple[List[str], str, Dict[str, Any]]:
    """底层日志抽取通用函数，支持按服务、日期、时间范围、日志级别与关键字高效检索"""
    service_map = {
        "ds-gateway": "ds-gateway.service",
        "clash": "clash.service",
        "cloudflared": "cloudflared.service",
    }
    service_name = service_map.get(service, f"{service}.service" if not service.endswith(".service") else service)

    since_str: Optional[str] = None
    until_str: Optional[str] = None
    if date and re.match(r"^\d{4}-\d{2}-\d{2}$", date.strip()):
        d = date.strip()
        since_str = f"{d} 00:00:00"
        until_str = f"{d} 23:59:59"
    else:
        if since:
            since_str = since.strip()
        if until:
            until_str = until.strip()

    raw_lines: List[str] = []
    meta = {
        "service": service_name,
        "date": date,
        "since": since_str,
        "until": until_str,
        "level": level,
        "filter": filter_kw,
    }

    if shutil.which("journalctl"):
        cmd = ["journalctl", "-u", service_name, "--no-pager"]
        if since_str:
            cmd.extend(["--since", since_str])
        if until_str:
            cmd.extend(["--until", until_str])
        if level in ("error", "err"):
            cmd.extend(["-p", "err"])
        elif level in ("warn", "warning"):
            cmd.extend(["-p", "warning"])
        elif level in ("info", "notice"):
            cmd.extend(["-p", "info"])

        # 控制提取行数避免全盘/单日海量日志打满内存与造成超时 (无论实时或历史查询)
        if lines:
            fetch_n = min(lines * 5, 10000) if filter_kw else lines
            cmd.extend(["-n", str(fetch_n)])
        else:
            cmd.extend(["-n", str(min(max_lines, 50000))])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            raw_lines = stdout.decode("utf-8", errors="replace").splitlines()
        except Exception as e:
            raw_lines = [f"[System-Error] Failed to execute journalctl: {e}"]
    else:
        # 本地开发/测试兜底（如 Windows 无 journalctl）
        log_dir = Path("logs")
        if log_dir.exists():
            log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if log_files:
                try:
                    text = log_files[0].read_text(encoding="utf-8", errors="replace")
                    raw_lines = text.splitlines()[- (lines or 500):]
                except Exception:
                    raw_lines = ["[System-Notice] Running in non-journalctl environment."]
            else:
                raw_lines = ["[System-Notice] Running in non-journalctl environment."]
        else:
            raw_lines = [
                f"[Local-Dev] OS: {platform.system()}. Systemd journal is active on Linux production server.",
                f"[Local-Dev] Service queried: {service_name}, date: {date or 'latest'}, level: {level or 'all'}.",
            ]

    # 关键字过滤（支持逗号分隔多关键字任一匹配）
    if filter_kw:
        keywords = [k.strip().lower() for k in filter_kw.split(",") if k.strip()]
        if keywords:
            raw_lines = [
                l for l in raw_lines
                if any(k in l.lower() for k in keywords)
            ]

    # 行数限制与截取
    if lines and len(raw_lines) > lines:
        raw_lines = raw_lines[-lines:]
    elif len(raw_lines) > max_lines:
        raw_lines = raw_lines[-max_lines:]

    return raw_lines, service_name, meta


@router.get("/api/system/logs", dependencies=[Depends(require_admin)])
async def get_system_logs(
    service: str = "ds-gateway",
    lines: int = 300,
    date: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    filter: Optional[str] = None,
    level: Optional[str] = None,
):
    """获取系统服务实时或历史日志（支持指定日期如 2026-10-10、时间范围、关键字与日志级别）"""
    lines = max(10, min(lines, 5000))
    raw_lines, service_name, meta = await _fetch_system_log_lines(
        service=service,
        lines=lines,
        date=date,
        since=since,
        until=until,
        filter_kw=filter,
        level=level,
    )
    return {
        "ok": True,
        "service": service,
        "service_name": service_name,
        "date": date,
        "since": meta.get("since"),
        "until": meta.get("until"),
        "level": level,
        "filter": filter,
        "total_lines": len(raw_lines),
        "lines": raw_lines,
        "timestamp": int(time.time()),
    }


@router.get("/api/system/logs/export", dependencies=[Depends(require_admin)])
async def export_system_logs(
    service: str = "ds-gateway",
    date: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    filter: Optional[str] = None,
    level: Optional[str] = None,
    max_lines: int = 50000,
):
    """一键导出并下载指定日期/时间段/服务的完整系统日志文件 (.log)"""
    max_lines = max(100, min(max_lines, 100000))
    raw_lines, service_name, meta = await _fetch_system_log_lines(
        service=service,
        lines=None,
        date=date,
        since=since,
        until=until,
        filter_kw=filter,
        level=level,
        max_lines=max_lines,
    )

    content = "\n".join(raw_lines)
    date_part = date.strip() if date and re.match(r"^\d{4}-\d{2}-\d{2}$", date.strip()) else time.strftime("%Y%m%d_%H%M%S")
    filename = f"{service}-{date_part}.log"

    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
            "X-Total-Lines": str(len(raw_lines)),
        },
    )


@router.get("/api/system/forensics", dependencies=[Depends(require_admin)])
async def get_system_forensics():
    """获取账号封禁/受限溯源诊断与近期关键风控容灾事件"""
    statuses = account_pool.get_all_status()
    muted_accounts = []
    now = time.time()

    cold_count = sum(1 for a in statuses if a.get("status") == "INACTIVE_COLD")
    warming_count = sum(1 for a in statuses if a.get("status") == "WARMING")
    active_count = sum(1 for a in statuses if a.get("usable"))

    for a in statuses:
        st = a.get("status")
        # 保护零IP冷库储备账号与养号中账号：冷库储备未曾激活使用，绝非封禁/隔离账号
        if st in ("INACTIVE_COLD", "WARMING"):
            continue

        is_muted = (
            a.get("muted_now")
            or bool(a.get("mute_until"))
            or st in ("MUTED", "QUARANTINE_MUTED", "QUARANTINE_INVALID", "QUARANTINE")
        )
        if is_muted or not a.get("usable"):
            is_perm = not bool(a.get("mute_until"))
            rem = max(0, int(a["mute_until"] - now)) if a.get("mute_until") else 0
            muted_accounts.append({
                "account": a["account_label"],
                "status": st or "MUTED",
                "mute_until": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["mute_until"])) if a.get("mute_until") else "永久/需人工检查",
                "remaining_seconds": rem,
                "is_permanent": is_perm,
                "mute_count": a.get("mute_count", 0),
                "owner": a.get("owner") or "公共池",
            })

    recent_events = []
    if shutil.which("journalctl"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", "ds-gateway.service", "-n", "300", "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                if any(tag in line for tag in ("biz:5", "Failover", "CircuitBreaker", "同 IP 熔断", "MUTED", "auth_expired", "429", "OverCapacity")):
                    recent_events.append(line.strip())
                    if len(recent_events) >= 30:
                        break
        except Exception:
            pass

    return {
        "ok": True,
        "muted_count": len(muted_accounts),
        "active_count": active_count,
        "cold_count": cold_count,
        "warming_count": warming_count,
        "total_accounts": len(statuses),
        "muted_accounts": muted_accounts,
        "recent_events": recent_events,
        "timestamp": int(now),
    }


@router.get("/api/system/drop_probe", dependencies=[Depends(require_admin)])
async def get_drop_probe_report(account: Optional[str] = None, limit: int = 40):
    """获取全链路账号请求与掉号特征抓包报告（proxy漂移 / token年龄 / 错误序列快照）"""
    from app.engine.drop_probe import get_probe_report
    return {"ok": True, **get_probe_report(account=account, limit=limit)}


@router.get("/api/system/proxies/status", dependencies=[Depends(require_admin)])
async def get_proxies_status():
    """并发检测多代理出口连通性、真实公网出口 IP 与延迟"""
    cfg_proxies = list(getattr(config_manager.config, "proxies", []) or [])
    default_proxy = getattr(config_manager.config, "proxy", None)
    if default_proxy and default_proxy not in cfg_proxies:
        cfg_proxies.insert(0, default_proxy)

    async def check_proxy(proxy_url: str):
        t0 = time.time()
        ok = False
        ip = "未知"
        code = None
        latency_ms = None
        err = ""
        try:
            def _probe():
                r = cffi_requests.get(
                    "https://chat.deepseek.com/",
                    impersonate="chrome120",
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=5,
                )
                return r.status_code
            code = await asyncio.to_thread(_probe)
            latency_ms = int((time.time() - t0) * 1000)
            ok = code in (200, 202, 301, 302, 403)
        except Exception as e:
            err = str(e)
            latency_ms = int((time.time() - t0) * 1000)

        if ok:
            try:
                def _get_ip():
                    r = cffi_requests.get(
                        "http://api.ipify.org",
                        proxies={"http": proxy_url, "https": proxy_url},
                        timeout=4,
                    )
                    return r.text.strip()
                ip = await asyncio.to_thread(_get_ip)
            except Exception:
                ip = "获取超时"

        return {
            "proxy": proxy_url,
            "online": ok,
            "status_code": code,
            "latency_ms": latency_ms,
            "egress_ip": ip,
            "error": err[:60] if err else "",
        }

    results = []
    if cfg_proxies:
        results = await asyncio.gather(*[check_proxy(p) for p in cfg_proxies])

    return {
        "ok": True,
        "proxies": results,
        "anti_cascade_same_ip": getattr(config_manager.config, "anti_cascade_same_ip", True),
        "timestamp": int(time.time()),
    }


# ── 敏感词风控拦截管理 ──

class SensitiveWordsRequest(BaseModel):
    words: List[str]


@router.get("/api/system/sensitive_words", dependencies=[Depends(require_admin)])
async def get_sensitive_words():
    from app.engine.sensitive_filter import sensitive_filter
    return {
        "custom_words": sensitive_filter._custom_words,
        "built_in_rules": {
            "cyber_exploit": [desc for _, desc in sensitive_filter._compiled_exploit],
            "jailbreak": [desc for _, desc in sensitive_filter._compiled_jailbreak],
            "compliance": [desc for _, desc in sensitive_filter._compiled_compliance],
        }
    }


@router.post("/api/system/sensitive_words", dependencies=[Depends(require_admin)])
async def update_sensitive_words(body: SensitiveWordsRequest):
    from app.engine.sensitive_filter import sensitive_filter
    sensitive_filter.save_custom_words(body.words)
    return {"ok": True, "count": len(sensitive_filter._custom_words)}


# ── 卡密批发中心 (Card-Key Bulk Generation & Lifecycle) ──

class BatchCreateCardsRequest(BaseModel):
    category: str = "daily"    # daily (1天) | weekly (7天) | monthly (30天) | custom (自定义)
    days: Optional[int] = 1
    count: int = 5
    batch_remark: str = "零售批发"
    mode: str = "shared"       # shared (走高速公共卡池) | dedicated (每卡独占1个账号)


@router.post("/api/admin/cards/batch-create", dependencies=[Depends(require_admin)])
async def batch_create_cards(body: BatchCreateCardsRequest):
    """
    一键批量批发卡密 (免账号即插即用)：
    - 支持天卡 (1天)、周卡 (7天)、月卡 (30天)、自定义天数
    - 到期自动释放绑定的账号并失效
    - 一键导出纯文本格式卡密，方便直接发货或对接自动化发卡
    """
    import secrets
    from app.engine.account_pool import account_pool
    from app.core.security import generate_key

    cat = (body.category or "daily").lower()
    if cat == "daily":
        duration_days = 1
        cat_name = "天卡"
    elif cat == "weekly":
        duration_days = 7
        cat_name = "周卡"
    elif cat == "monthly":
        duration_days = 30
        cat_name = "月卡"
    else:
        duration_days = max(1, min(365, int(body.days or 1)))
        cat_name = f"{duration_days}天卡"

    count = max(1, min(50, body.count))
    now = int(time.time())
    expires_at = now + duration_days * 86400
    expire_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))

    cards = []
    bundle_lines = []

    dedicated_candidates = []
    if body.mode == "dedicated":
        from app.core.security import account_owner_map
        owned = account_owner_map()
        for lbl, acc in account_pool._accounts.items():
            if acc.is_valid and acc.status == "ACTIVE" and lbl not in owned:
                dedicated_candidates.append(lbl)
                if len(dedicated_candidates) >= count:
                    break

    for i in range(count):
        accs = []
        if body.mode == "dedicated" and i < len(dedicated_candidates):
            accs = [dedicated_candidates[i]]

        key_name = f"{cat_name} {body.batch_remark} #{i+1}"
        owner_name = f"{cat_name}-{body.batch_remark}"
        key_rec = generate_key(
            name=key_name,
            owner=owner_name,
            account_labels=accs,
            key_type="card",
            expires_at=expires_at,
            card_category=cat,
            quota_daily_requests=0,
            quota_daily_tokens=0,
        )
        raw_key = key_rec.get("key") or key_rec.get("raw_key") or ""
        mode_str = f"专属独占号: {','.join(accs)}" if accs else "高速公共卡池"
        card_line = f"卡密: {raw_key} | 类型: {cat_name} | 有效期: {duration_days}天 | 到期时间: {expire_str} | 调度模式: {mode_str}"
        cards.append({
            "id": key_rec["id"],
            "key": raw_key,
            "prefix": key_rec.get("prefix"),
            "category": cat,
            "category_name": cat_name,
            "duration_days": duration_days,
            "expires_at": expires_at,
            "expire_str": expire_str,
            "mode": body.mode,
            "account_labels": accs,
            "card_text": card_line,
        })
        bundle_lines.append(card_line)

    return {
        "ok": True,
        "count": len(cards),
        "cards": cards,
        "bundle_text": "\n".join(bundle_lines),
        "message": f"成功批量生成 {len(cards)} 张{cat_name}卡密！",
    }


@router.post("/api/admin/cards/clean-expired", dependencies=[Depends(require_admin)])
async def clean_expired_cards():
    """一键清理并释放所有已过期的卡密，彻底释放号源回公共池"""
    from app.core.security import recycle_expired_keys, purge_revoked_keys
    recycled = recycle_expired_keys()
    purged = purge_revoked_keys()
    return {
        "ok": True,
        "recycled_count": recycled,
        "purged_count": purged,
        "message": f"成功回收 {recycled} 张过期卡密，物理清退 {purged} 把失效密钥！",
    }