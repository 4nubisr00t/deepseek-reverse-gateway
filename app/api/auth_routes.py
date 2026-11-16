"""
C 端与管理员统一认证接口路由
/api/auth/send-code - 发送 6 位邮箱注册验证码（含 IP 滑动窗口频控、邮箱冷却、每日上限、HMAC 加密存储）
/api/auth/register - 用户自注册（支持强制/可选 6 位邮箱验证码校验与防爆破）
/api/auth/login - 登录 (支持普通用户与超级管理员)
/api/auth/me - 获取当前登录用户信息
/api/auth/change-password - 修改个人密码
/api/auth/logout - 退出登录
"""

import asyncio
import re
import secrets
import smtplib
import threading
import time
from collections import defaultdict
from email.message import EmailMessage
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Response, Request
from curl_cffi import requests as cffi_requests

from app.config import config_manager
from app.db.repositories import user_repo, email_verification_repo
from app.core.auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_jwt_secret,
    require_user,
    DEFAULT_TOKEN_EXPIRE_DAYS,
)
from app.core.security import generate_key

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_REGEX = re.compile(r"^[\w\.-]+@[\w\.-]+\.\w+$")

# 一次性临时邮箱黑名单，防止自动化脚本批量刷取免费初始 Key
DISPOSABLE_EMAIL_DOMAINS = frozenset({
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "yopmail.com", "sharklasers.com", "trashmail.com", "getnada.com",
})


def normalize_and_validate_email(raw_email: str, check_disposable: bool = True) -> str:
    """规范化邮箱地址并拦截一次性临时邮箱"""
    email_clean = (raw_email or "").strip().lower()
    if not EMAIL_REGEX.match(email_clean):
        raise HTTPException(status_code=400, detail="请输入有效的邮箱地址")
    local, domain = email_clean.rsplit("@", 1)
    if check_disposable and domain in DISPOSABLE_EMAIL_DOMAINS:
        raise HTTPException(status_code=400, detail="不支持使用一次性临时邮箱注册，请使用常用邮箱")
    return email_clean


def extract_client_ip(request: Optional[Request]) -> str:
    """提取真实客户端 IP（优先解析 Cloudflare / Nginx 转发头）"""
    if request is None:
        return "unknown"
    for hdr in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        val = request.headers.get(hdr, "").strip()
        if val:
            return val.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class IpSlidingWindowLimiter:
    """单 IP 滑动窗口频控器（防邮件炸弹与注册接口滥用）"""

    def __init__(self, limit_per_minute: int = 3, limit_per_hour: int = 15):
        self.limit_per_minute = limit_per_minute
        self.limit_per_hour = limit_per_hour
        self._lock = threading.Lock()
        self._events: Dict[str, List[float]] = defaultdict(list)

    def check_and_record(self, ip: str) -> Optional[str]:
        if not ip or ip == "unknown":
            return None
        now = time.time()
        with self._lock:
            history = [t for t in self._events[ip] if now - t < 3600.0]
            recent_1m = sum(1 for t in history if now - t < 60.0)
            if recent_1m >= self.limit_per_minute:
                return "当前 IP 请求过于频繁，请 1 分钟后再试"
            if len(history) >= self.limit_per_hour:
                return "当前 IP 1 小时内请求次数已达上限，请稍后再试"
            history.append(now)
            self._events[ip] = history
            return None

    def reset(self):
        with self._lock:
            self._events.clear()


ip_send_limiter = IpSlidingWindowLimiter()


async def send_verification_email_async(to_email: str, code: str) -> None:
    """双通道异步验证码邮件发送服务：优先 Resend HTTP API，次选 SMTP_SSL"""
    cfg = config_manager.config
    subject = f"【Anubis DeepSeek Gateway】您的注册验证码：{code}"
    html_body = (
        f"<div style='font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px;"
        f"border:1px solid #e5e7eb;border-radius:12px;'>"
        f"<h2 style='margin-top:0;color:#111827;'>邮箱安全验证</h2>"
        f"<p style='color:#374151;font-size:15px;'>您正在注册 Anubis DeepSeek Gateway，您的 6 位数字验证码为：</p>"
        f"<div style='font-size:32px;font-weight:700;letter-spacing:6px;color:#2563eb;"
        f"padding:16px 0;'>{code}</div>"
        f"<p style='color:#6b7280;font-size:13px;'>该验证码将在 <strong>5 分钟</strong> 后失效，请勿向他人泄露。"
        f"如非本人操作，请忽略本邮件。</p></div>"
    )

    resend_api_key = getattr(cfg, "resend_api_key", "")
    from_addr = getattr(cfg, "mail_from", f"noreply@{getattr(cfg, 'cf_mail_domain', 'example.com')}")

    def _send_sync():
        if resend_api_key:
            resp = cffi_requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_addr,
                    "to": [to_email],
                    "subject": subject,
                    "html": html_body,
                },
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Resend API HTTP {resp.status_code}: {getattr(resp, 'text', '')[:150]}")
            return

        smtp_host = getattr(cfg, "smtp_host", "")
        smtp_port = int(getattr(cfg, "smtp_port", 465) or 465)
        smtp_user = getattr(cfg, "smtp_user", "")
        smtp_pass = getattr(cfg, "smtp_password", "")
        if not smtp_host or not smtp_user:
            raise RuntimeError("邮件投递服务尚未配置（请配置 resend_api_key 或 smtp_host/smtp_user）")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email
        msg.set_content(f"您的注册验证码为：{code}（5分钟内有效）。")
        msg.add_alternative(html_body, subtype="html")

        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

    await asyncio.to_thread(_send_sync)


class SendCodeRequest(BaseModel):
    email: str = Field(..., description="目标注册邮箱")
    turnstile_token: Optional[str] = Field(None, description="可选人机验证令牌")


class RegisterRequest(BaseModel):
    email: str = Field(..., description="用户邮箱")
    username: Optional[str] = Field(None, description="自定义用户名 (选填)")
    password: str = Field(..., min_length=6, description="密码 (不少于6位)")
    verification_code: Optional[str] = Field(None, description="6位邮箱数字验证码")
    invite_code: Optional[str] = Field(None, description="6位专属邀请码 (选填)")


class LoginRequest(BaseModel):
    email: Optional[str] = Field(None, description="登录邮箱")
    username: Optional[str] = Field(None, description="登录用户名")
    account: Optional[str] = Field(None, description="用户名或邮箱")
    password: str = Field(..., description="登录密码")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="原密码")
    new_password: str = Field(..., min_length=6, description="新密码 (不少于6位)")


def _set_auth_cookie(response: Response, token: str):
    """设置客户端安全认证 Cookie"""
    max_age = 86400 * DEFAULT_TOKEN_EXPIRE_DAYS
    response.set_cookie(
        key="ds_user_token",
        value=token,
        max_age=max_age,
        expires=max_age,
        httponly=True,
        samesite="lax",
        secure=False,  # 允许 HTTP 与 HTTPS 自适应
        path="/",
    )


@router.get("/config")
async def get_auth_config():
    """获取前端自注册公开配置（是否强制邮箱验证码、发信服务是否就绪）"""
    cfg = config_manager.config
    has_resend = bool(getattr(cfg, "resend_api_key", ""))
    has_smtp = bool(getattr(cfg, "smtp_host", "") and getattr(cfg, "smtp_user", ""))
    return {
        "require_email_verification": bool(getattr(cfg, "require_email_verification", False)),
        "mail_service_ready": has_resend or has_smtp,
    }


@router.post("/send-code")
async def send_verification_code(req: SendCodeRequest, request: Request):
    """发送 6 位邮箱注册验证码（含单 IP 频控、单邮箱 60s 冷却、每日 5 次上限与 HMAC 落盘）"""
    email_clean = normalize_and_validate_email(req.email, check_disposable=True)
    if user_repo.get_by_email(email_clean):
        raise HTTPException(status_code=400, detail="该邮箱已被注册，请直接登录")

    client_ip = extract_client_ip(request)
    ip_err = ip_send_limiter.check_and_record(client_ip)
    if ip_err:
        raise HTTPException(status_code=429, detail=ip_err, headers={"Retry-After": "60"})

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    rate_err = email_verification_repo.check_and_prepare_send(
        email=email_clean,
        code=code,
        client_ip=client_ip,
        secret=get_jwt_secret(),
    )
    if rate_err:
        raise HTTPException(status_code=429, detail=rate_err, headers={"Retry-After": "60"})

    try:
        await send_verification_email_async(email_clean, code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"验证码邮件发送失败: {e}")

    return {
        "ok": True,
        "expires_in": email_verification_repo.CODE_TTL_SECONDS,
        "cooldown": email_verification_repo.RESEND_COOLDOWN_SECONDS,
        "message": "验证码已发送至您的邮箱，5 分钟内有效",
    }


@router.post("/register")
async def register(req: RegisterRequest, response: Response):
    """C 端用户自注册，支持 6 位邮箱验证码防爆破核验，成功后自动签发初始 API Key 与 JWT Token"""
    email_clean = normalize_and_validate_email(req.email, check_disposable=True)

    existing = user_repo.get_by_email(email_clean)
    if existing:
        raise HTTPException(status_code=400, detail="该邮箱已被注册，请直接登录")

    # 邮箱验证码核验：当开启强制验证 (require_email_verification=True) 或客户端传入了 verification_code 时强制核验
    require_code = bool(getattr(config_manager.config, "require_email_verification", False))
    if require_code or req.verification_code is not None:
        raw_code = (req.verification_code or "").strip()
        if not re.match(r"^\d{6}$", raw_code):
            raise HTTPException(status_code=400, detail="请输入 6 位数字邮箱验证码")
        ok, verify_msg = email_verification_repo.verify_and_consume(
            email=email_clean,
            code=raw_code,
            secret=get_jwt_secret(),
        )
        if not ok:
            raise HTTPException(status_code=400, detail=verify_msg)

    # 若数据库中尚无任何用户，第一个注册者自动赋予 admin 角色
    is_first_user = (user_repo.count() == 0)
    role = "admin" if is_first_user else "user"
    tier = "ultra" if is_first_user else "free"

    uid = f"u_{secrets.token_hex(6)}"
    pwd_hash = hash_password(req.password)
    my_invite_code = secrets.token_hex(3).lower()
    invited_by = None
    if req.invite_code:
        inviter = user_repo.get_by_invite_code(req.invite_code)
        if inviter:
            invited_by = inviter["id"]

    user = user_repo.create(
        user_id=uid,
        email=email_clean,
        password_hash=pwd_hash,
        role=role,
        tier=tier,
        username=getattr(req, "username", None),
        invite_code=my_invite_code,
        invited_by=invited_by,
    )

    # 为新用户自动生成一把初始默认 API Key (默认额度: 试用版 1.28 亿 Tokens)
    default_quota_req = 0
    default_quota_tokens = 0 if role == "admin" else 128000000
    initial_key_rec = generate_key(
        name="默认密钥",
        owner=email_clean,
        user_id=uid,
        quota_daily_requests=default_quota_req,
        quota_daily_tokens=default_quota_tokens,
    )

    token = create_access_token(
        user_id=uid,
        email=email_clean,
        role=role,
        tier=tier,
    )
    _set_auth_cookie(response, token)

    return {
        "token": token,
        "user": {
            "id": uid,
            "email": email_clean,
            "username": user.get("username"),
            "role": role,
            "tier": tier,
            "bonus_tokens": int(user.get("bonus_tokens") or 0),
            "invite_code": user.get("invite_code") or uid,
            "created_at": user["created_at"],
        },
        "initial_key": initial_key_rec.get("key"),  # 仅注册成功返回一次明文
        "message": "注册成功！已为您自动生成默认 API Key。",
    }


@router.post("/login")
async def login(req: LoginRequest, response: Response):
    """统一登录接口：支持 C 端用户以及超级管理员，支持用户名或邮箱统一登录"""
    login_id = (req.account or req.username or req.email or "").strip().lower()
    if not login_id:
        raise HTTPException(status_code=400, detail="请输入用户名或登录邮箱")
    password = req.password

    # 1. 尝试匹配普通用户或管理员用户 (支持邮箱或用户名检索)
    user = user_repo.get_by_identifier(login_id)

    # 2. 特殊兼容处理：如果输入 'admin' 作为账号，指向默认管理员
    if not user and (login_id == "admin" or login_id == "admin@localhost"):
        user = user_repo.get_by_email("admin@localhost")

    # 3. 兜底兼容：直接使用系统全局管理员密码对比
    admin_pwd = config_manager.config.admin_password
    is_direct_admin_match = (
        (login_id == "admin" or login_id == "admin@localhost")
        and password == admin_pwd
    )

    if not user:
        if is_direct_admin_match:
            # 自动修复并建立 admin 用户
            uid = f"u_admin_{secrets.token_hex(4)}"
            user = user_repo.create(
                user_id=uid,
                email="admin@localhost",
                password_hash=hash_password(admin_pwd),
                role="admin",
                tier="ultra",
            )
        else:
            raise HTTPException(status_code=401, detail="账号或密码错误")

    # 4. 校验密码
    valid_password = False
    if is_direct_admin_match:
        valid_password = True
    elif user and user.get("password_hash"):
        valid_password = verify_password(password, user["password_hash"])

    if not valid_password:
        raise HTTPException(status_code=401, detail="账号或密码错误")

    if not user.get("is_active", 1):
        raise HTTPException(status_code=403, detail="该账号已被停用，请联系管理员")

    # 5. 透明升级旧的 sha256 哈希为安全 bcrypt
    if user.get("password_hash") and not (
        user["password_hash"].startswith("$2b$") or user["password_hash"].startswith("$2a$")
    ):
        try:
            new_hash = hash_password(password)
            user_repo.update_password(user["id"], new_hash)
        except Exception:
            pass

    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        role=user.get("role", "user"),
        tier=user.get("tier", "free"),
    )
    _set_auth_cookie(response, token)

    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "role": user.get("role", "user"),
            "tier": user.get("tier", "free"),
            "created_at": user.get("created_at"),
        },
        "message": "登录成功",
    }


class UpdateProfileRequest(BaseModel):
    username: Optional[str] = Field(None, description="修改用户名 (3~32位字母数字下划线)")
    email: Optional[str] = Field(None, description="修改或绑定邮箱")


@router.get("/me")
async def get_me(user: Dict[str, Any] = Depends(require_user)):
    """获取当前登录用户信息"""
    db_u = user_repo.get_by_id(user["id"]) or user
    return {
        "user": {
            "id": db_u["id"],
            "email": db_u["email"],
            "username": db_u.get("username"),
            "role": db_u.get("role", "user"),
            "tier": db_u.get("tier", "free"),
            "bonus_tokens": int(db_u.get("bonus_tokens") or 0),
            "invite_code": db_u.get("invite_code") or db_u["id"],
            "created_at": db_u.get("created_at"),
        }
    }


@router.post("/profile")
@router.post("/update-profile")
async def update_profile(req: UpdateProfileRequest, user: Dict[str, Any] = Depends(require_user)):
    """修改个人信息：支持自主改名、改绑邮箱"""
    db_user = user_repo.get_by_id(user["id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    new_username = None
    if req.username is not None and req.username.strip():
        u = req.username.strip().lower()
        if not re.match(r"^[a-zA-Z0-9_\-\.]{3,32}$", u):
            raise HTTPException(status_code=400, detail="用户名格式不正确，支持3-32位字母数字、下划线及连字符")
        existing_u = user_repo.get_by_username(u)
        if existing_u and existing_u["id"] != user["id"]:
            raise HTTPException(status_code=400, detail="该用户名已被占用，请换一个")
        new_username = u

    new_email = None
    if req.email is not None and req.email.strip():
        e = normalize_and_validate_email(req.email, check_disposable=True)
        existing_e = user_repo.get_by_email(e)
        if existing_e and existing_e["id"] != user["id"]:
            raise HTTPException(status_code=400, detail="该邮箱已被其他账号占用")
        new_email = e

    if not new_username and not new_email:
        raise HTTPException(status_code=400, detail="未提供需要更新的个人资料")

    user_repo.update_profile(user["id"], username=new_username, email=new_email)
    updated = user_repo.get_by_id(user["id"]) or {}
    return {
        "ok": True,
        "message": "个人资料更新成功！",
        "user": {
            "id": updated.get("id", user["id"]),
            "email": updated.get("email", user.get("email")),
            "username": updated.get("username"),
            "role": updated.get("role", "user"),
            "tier": updated.get("tier", "free"),
            "bonus_tokens": int(updated.get("bonus_tokens") or 0),
            "invite_code": updated.get("invite_code") or user["id"],
        }
    }


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, user: Dict[str, Any] = Depends(require_user)):
    """修改当前用户登录密码"""
    db_user = user_repo.get_by_id(user["id"])
    if not db_user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not verify_password(req.old_password, db_user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码不正确")

    new_hash = hash_password(req.new_password)
    user_repo.update_password(user["id"], new_hash)
    return {"message": "密码修改成功，请使用新密码重新登录"}


@router.post("/logout")
async def logout(response: Response):
    """退出登录并清理凭证 Cookie"""
    response.delete_cookie(key="ds_user_token", path="/")
    response.delete_cookie(key="ds_admin_session", path="/admin")
    return {"message": "已成功退出登录"}
