"""
ds-gateway 用户认证与鉴权核心模块
- 密码加盐哈希与校验 (Bcrypt + SHA-256 兼容自愈)
- JWT 会话 Token 签发与解析 (HS256, 7天免登)
- 多模式身份解析 (Bearer Header, HttpOnly Cookie, Admin Session 兼容)
- FastAPI 角色守卫依赖项 (require_user, require_admin_user)
"""

import time
import secrets
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import bcrypt
import jwt
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import config_manager
from app.db.repositories import settings_repo, user_repo
from app.core.security import verify_admin_session, basic_security

JWT_ALGORITHM = "HS256"
DEFAULT_TOKEN_EXPIRE_DAYS = 7


def get_jwt_secret() -> str:
    """获取或初始化持久化在 SQLite 的 JWT 签名私钥"""
    secret = settings_repo.get("jwt_secret")
    if not secret or not isinstance(secret, str):
        secret = secrets.token_hex(32)
        settings_repo.set("jwt_secret", secret)
    return secret


def hash_password(raw_password: str) -> str:
    """使用 Bcrypt 对密码进行单向哈希"""
    pwd_bytes = raw_password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(raw_password: str, hashed_password: str) -> bool:
    """安全校验密码（支持 Bcrypt 及历史 SHA-256 哈希）"""
    if not raw_password or not hashed_password:
        return False

    # 1. Bcrypt 标准哈希
    if hashed_password.startswith("$2b$") or hashed_password.startswith("$2a$"):
        try:
            return bcrypt.checkpw(raw_password.encode("utf-8"), hashed_password.encode("utf-8"))
        except Exception:
            return False

    # 2. SHA-256 兼容分支（用于历史种子管理员密码的初次校验）
    if len(hashed_password) == 64:
        calc = hashlib.sha256(raw_password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(calc, hashed_password)

    return False


def create_access_token(user_id: str, email: str, role: str = "user", tier: str = "free") -> str:
    """签发包含基础身份信息的 JWT Token"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email.strip().lower(),
        "role": role,
        "tier": tier,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=DEFAULT_TOKEN_EXPIRE_DAYS)).timestamp()),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """解码并校验 JWT Token，无效或过期返回 None"""
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        return payload
    except Exception:
        return None


def get_current_user_optional(
    request: Request,
    basic: Optional[HTTPBasicCredentials] = Depends(basic_security),
) -> Optional[Dict[str, Any]]:
    """
    尽力解析当前请求中的用户身份：
    1. 优先提取 Authorization: Bearer <jwt>
    2. 次选提取 Cookie: ds_user_token=<jwt>
    3. 兼顾旧版 Admin Cookie: ds_admin_session
    4. 兼顾 CLI Basic Auth
    """
    token = None
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        token = auth_hdr[7:].strip()
    elif "ds_user_token" in request.cookies:
        token = request.cookies.get("ds_user_token", "").strip()

    # A. 走 JWT 凭据验证
    if token:
        payload = decode_access_token(token)
        if payload and "sub" in payload:
            uid = payload["sub"]
            u = user_repo.get_by_id(uid)
            if u:
                return {k: v for k, v in u.items() if k != "password_hash"}

    # B. 走兼容老版 Admin Session Cookie
    sid = request.cookies.get("ds_admin_session")
    if sid and verify_admin_session(sid):
        admin_u = user_repo.get_by_email("admin@localhost")
        if admin_u:
            return {k: v for k, v in admin_u.items() if k != "password_hash"}
        return {
            "id": "admin_session_legacy",
            "email": "admin@localhost",
            "role": "admin",
            "tier": "ultra",
            "is_active": 1,
            "created_at": int(time.time()),
        }

    # C. 走兼容 Basic Auth (CLI 运维)
    if basic is not None:
        correct_password = config_manager.config.admin_password
        if hmac.compare_digest(basic.username.encode(), b"admin") and \
           hmac.compare_digest(basic.password.encode(), correct_password.encode()):
            admin_u = user_repo.get_by_email("admin@localhost")
            if admin_u:
                return {k: v for k, v in admin_u.items() if k != "password_hash"}
            return {
                "id": "admin_basic",
                "email": "admin@localhost",
                "role": "admin",
                "tier": "ultra",
                "is_active": 1,
                "created_at": int(time.time()),
            }

    return None


def require_user(user: Optional[Dict[str, Any]] = Depends(get_current_user_optional)) -> Dict[str, Any]:
    """强依赖：必须已登录且账号正常"""
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if not user.get("is_active", 1):
        raise HTTPException(status_code=403, detail="该账号已被停用，请联系管理员")
    return user


def require_admin_user(user: Optional[Dict[str, Any]] = Depends(get_current_user_optional)) -> Dict[str, Any]:
    """强依赖：必须具备超级管理员 (role == 'admin') 权限"""
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="权限不足：需要管理员权限")
    return user
