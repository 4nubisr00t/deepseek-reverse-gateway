# -*- coding: utf-8 -*-
"""
app/engine/http_auth_engine.py
================================================================================
DeepSeek 生产级纯 HTTP 免密登录与滑动换签引擎 (Zero-Chromium HTTP Auth Engine)
================================================================================
核心职责:
  1. 1:1 复刻官方前端敏感认证接口的混合加密协议 (RSA-OAEP SHA-256 + AES-128-CBC PKCS7);
  2. 智能提取数美真实硬件特征码 ("B..." 指纹)，杜绝内部 UUID 导致的 biz_code=11 风控;
  3. 双阶段自适应免密认证:
     - 阶段一: 加密调用 /api/v0/users/login
       * 若 biz_code=0: 官方直接下发新 Token (滑动换签成功)
       * 若 biz_code=13: 命中受信任设备签名挑战，触发阶段二
     - 阶段二: 明文调用 /api/v0/users/mfa/verify_signature
       * 按照 [mfa_challenge, mfa_session_id, credential_id, "verify"].join("\\n") 组装
       * 使用 ECDSA P-256 计算 IEEE P1363 格式 (64B raw r||s) 签名并明文提交
  4. 支持 Web 与移动端 (Android 2.0.0+) 多平台协议特征无缝切换;
  5. 严格零 Chromium 依赖，绝不拉起任何浏览器进程。
================================================================================
"""

from __future__ import annotations

import os
import re
import sys
import uuid
import json
import time
import base64
import hashlib
from typing import Dict, Any, Tuple, Optional

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    # 允许在缺乏 curl_cffi 环境下优雅退化或由调用方捕获
    cffi_requests = None

from cryptography.hazmat.primitives import hashes, serialization, padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

LOGIN_URL = "https://chat.deepseek.com/api/v0/users/login"
VERIFY_SIG_URL = "https://chat.deepseek.com/api/v0/users/mfa/verify_signature"

# 官方生产环境 RSA SPKI 2048 位公钥
PRODUCTION_RSA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0RLAhx1TOJhAQBHS1LHX"
    "lnkfOg86O+uhsmKWNNkmugVrRqO6cl13SnjBs2otRYZGJd50xpBG4We2i6/oCJCw"
    "xA/49YZfIwaeDmTx+WBIN7qrwhwP7vGCi6Osesmn/Ddgdye6mifYkRpRMonQjGY6"
    "VPaUMTw1jjOAIKph5YZn0bDVmGLvs5eH6CoQYHM5/Z1Nfhj36VL28eZi3elJHoz/"
    "L6Tzi2wY1rCYqYHfepq3Ysy8y5Y5BeFJXN60RYLlbs+MRC+2kyJqcfApUnn1O/W2"
    "om9dhxc6Rc5dqas1YB4X4CWCttrKk/suhpIx9zpQY9kqtotbe5zweYaKv0rCVeqx"
    "8wIDAQAB"
)


def encrypt_auth_payload(json_obj: dict, rsa_pub_b64: str = PRODUCTION_RSA_PUBLIC_KEY_B64) -> Tuple[bytes, str]:
    """
    1:1 复刻官方前端 eR 加密函数:
    1. json -> utf-8 bytes
    2. 生成 16B 随机 AES Key + 16B 随机 IV
    3. AES-128-CBC + PKCS7 填充加密明文字节流
    4. RSA-OAEP (SHA-256) 加密 key(16B) || iv(16B) (共 32 字节)
    5. 返回 (密文字节流, base64(RSA密文))
    """
    raw_str = json.dumps(json_obj, separators=(',', ':'), ensure_ascii=False)
    raw_bytes = raw_str.encode('utf-8')

    aes_key = os.urandom(16)
    aes_iv = os.urandom(16)

    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(raw_bytes) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv))
    encryptor = cipher.encryptor()
    cipher_body = encryptor.update(padded_data) + encryptor.finalize()

    key_iv_block = aes_key + aes_iv
    rsa_der = base64.b64decode(rsa_pub_b64)
    pub_key = serialization.load_der_public_key(rsa_der)

    rsa_encrypted = pub_key.encrypt(
        key_iv_block,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    enc_key_b64 = base64.b64encode(rsa_encrypted).decode('ascii')
    return cipher_body, enc_key_b64


def der_to_raw_p1363_signature(der_sig: bytes, key_size_bytes: int = 32) -> bytes:
    """将 OpenSSL/Python 的 DER 签名转换为 WebCrypto 的 IEEE P1363 原始 r||s 格式 (64B)"""
    r, s = decode_dss_signature(der_sig)
    r_bytes = r.to_bytes(key_size_bytes, byteorder='big')
    s_bytes = s.to_bytes(key_size_bytes, byteorder='big')
    return r_bytes + s_bytes


def sign_challenge_p256(private_key_b64: str, plain_text: str) -> str:
    """
    使用 Base64 编码的 PKCS8 ECDSA P-256 私钥对文本进行 SHA256 签名，输出 Base64 P1363 格式签名。
    """
    key_der = base64.b64decode(private_key_b64)
    priv_key = serialization.load_der_private_key(key_der, password=None)
    data = plain_text.encode('utf-8')

    der_sig = priv_key.sign(data, ec.ECDSA(hashes.SHA256()))
    raw_sig = der_to_raw_p1363_signature(der_sig, key_size_bytes=32)
    return base64.b64encode(raw_sig).decode('ascii')


def extract_shumei_device_id(account: Dict[str, Any]) -> str:
    """
    【避坑铁律一】智能提取真实数美 "B..." 特征码:
    优先从 device_fingerprint 列的 JSON 串中提取 device_id 或 smid；
    若提取出的字符串以 "B" 开头，则为真实数美指纹；
    若只有纯 UUID 或提取不到，则回退并记录警告。
    """
    fp_raw = account.get("device_fingerprint")
    if fp_raw:
        if isinstance(fp_raw, str) and fp_raw.strip().startswith("B"):
            return fp_raw.strip()
        try:
            fp_dict = json.loads(fp_raw) if isinstance(fp_raw, str) else fp_raw
            if isinstance(fp_dict, dict):
                cand = fp_dict.get("device_id") or fp_dict.get("smid") or ""
                if cand and cand.startswith("B"):
                    return str(cand)
        except Exception:
            pass

    # 兜底：检查 account 顶层的 device_id 字段
    top_did = account.get("device_id") or ""
    if top_did and str(top_did).startswith("B"):
        return str(top_did)

    # 再次兜底返回原值（即使是 UUID）
    return str(top_did)


def is_valid_shumei_device_id(device_id: Any) -> bool:
    """严格校验数美设备指纹：必须以大写 'B' 开头、长度合理且非样本占位值。"""
    s = str(device_id or "").strip()
    if not s or any(ch.isspace() for ch in s) or s.endswith(".thumbcache_sample"):
        return False
    return bool(s.startswith("B") and len(s) >= 12)


def parse_expire_at(val: Any) -> Optional[int]:
    """解析受信任设备到期时间戳（统一规范为 Unix 秒；无效或 <= 0 返回 None）。"""
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        ts = int(float(val))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def extract_token_issued_at(token: str, now_ts: Optional[int] = None) -> Optional[int]:
    """尝试从标准 3 段式 JWT payload 提取签发时间 iat (秒)；非 JWT 或缺失返回 None。"""
    tok = str(token or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    parts = tok.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    try:
        padded = parts[1] + ("=" * ((4 - len(parts[1]) % 4) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        iat = parse_expire_at(payload.get("iat"))
        upper = int(now_ts if now_ts is not None else time.time()) + 300
        if iat and 1_600_000_000 <= iat <= upper:
            return iat
    except Exception:
        return None
    return None


def is_disposable_account(account: Dict[str, Any]) -> bool:
    """判断账号是否被标记为一次性耗材（禁止自动滑动续杯与免密重登）。"""
    if not isinstance(account, dict):
        return False
    return bool(
        account.get("disposable") is True
        or account.get("is_disposable") is True
        or str(account.get("pool_tag") or "").strip().lower() == "disposable"
    )


def evaluate_trusted_device(account: Dict[str, Any], now_ts: Optional[float] = None) -> Dict[str, Any]:
    """
    严格评估账号是否具备真实有效的受信任设备（缺任意一项均判定为未受信任）：
      1. 具备 credential_id + mfa_private_key；
      2. 具备有效的数美 'B...' 指纹（支持 cookie ds_smid/smid 兜底）；
      3. 具备有效的未来受信任到期时间戳 expire_at（> now）。
    """
    now_sec = int(now_ts if now_ts is not None else time.time())
    is_disp = is_disposable_account(account)
    has_mfa_5 = bool(account.get("credential_id") and account.get("mfa_private_key"))

    fp = str(extract_shumei_device_id(account) or "").strip()
    if not is_valid_shumei_device_id(fp) and account.get("cookie"):
        m_smid = re.search(r"(?:^|;\s*)(?:ds_smid|smid)=(B[^;\s]+)", str(account.get("cookie") or ""))
        if m_smid:
            fp = m_smid.group(1).strip()
    has_valid_fp = is_valid_shumei_device_id(fp)

    exp_ts = parse_expire_at(account.get("expire_at") or account.get("mfa_expire_at"))
    has_valid_exp = bool(exp_ts is not None and exp_ts > now_sec)

    device_trusted = bool(has_mfa_5 and has_valid_fp and has_valid_exp)
    is_renewable = bool(device_trusted and not is_disp)

    if is_disp:
        reason = "disposable"
    elif not has_mfa_5:
        reason = "missing_mfa"
    elif not has_valid_fp:
        reason = "invalid_fingerprint"
    elif exp_ts is None:
        reason = "missing_expire_at"
    elif not has_valid_exp:
        reason = "expired_trusted_device"
    else:
        reason = "ok"

    return {
        "is_disposable": is_disp,
        "has_mfa_5": has_mfa_5,
        "shumei_device_id": fp if has_valid_fp else "",
        "has_valid_shumei_fp": has_valid_fp,
        "expire_at": exp_ts,
        "has_valid_expire_at": has_valid_exp,
        "device_trusted": device_trusted,
        "is_renewable": is_renewable,
        "reason": reason,
    }


def _extract_user_chat_state(biz_data: Dict[str, Any]) -> Tuple[Optional[str], bool, Any]:
    """从登录或验签响应的 biz_data 中提取 token 与账号禁言状态。"""
    user = biz_data.get("user") or {}
    tok = user.get("token") or biz_data.get("token")
    chat = user.get("chat") or {}
    is_muted = bool(chat.get("is_muted"))
    mute_until = chat.get("mute_until") if is_muted else None
    return tok, is_muted, mute_until


def renew_account_token(
    account: Dict[str, Any],
    platform: str = "ios",
    proxy_url: Optional[str] = None,
    timeout: int = 20
) -> Tuple[bool, Dict[str, Any]]:
    """
    执行纯 HTTP 免密登录与滑动换签 (支持 Web 与 Android 模式)
    :param account: 账号信息字典，需包含 email, password, credential_id, mfa_private_key, device_fingerprint
    :param platform: "web" 或 "android"
    :param proxy_url: 可选的代理地址
    :param timeout: 请求超时时间
    :return: (is_success, result_dict)
    """
    if cffi_requests is None:
        return False, {"error": "缺少 curl_cffi 依赖", "kind": "dependency_error"}

    email = account.get("_email") or account.get("email") or account.get("account_label") or ""
    password = account.get("password") or account.get("_password") or ""
    credential_id = account.get("credential_id") or ""
    mfa_private_key = account.get("mfa_private_key") or ""
    device_id = str(extract_shumei_device_id(account) or "").strip()
    if not is_valid_shumei_device_id(device_id) and account.get("cookie"):
        m_smid = re.search(r"(?:^|;\s*)(?:ds_smid|smid)=(B[^;\s]+)", str(account.get("cookie") or ""))
        if m_smid:
            device_id = m_smid.group(1).strip()

    if not email or not password:
        return False, {"error": "缺少必填的 email 或 password", "kind": "param_error"}

    if not is_valid_shumei_device_id(device_id):
        return False, {
            "error": f"缺少或无效的数美 B 设备指纹 ({device_id!r})，拒绝使用随机/伪造指纹登录",
            "kind": "missing_device_id",
        }

    if not credential_id or not mfa_private_key:
        return False, {
            "error": "缺少 MFA credential_id 或 mfa_private_key，非受信任设备禁止重登",
            "kind": "missing_mfa",
        }

    impersonate = "safari17_0" if platform == "ios" else "chrome124"
    sess = cffi_requests.Session(impersonate=impersonate)
    if proxy_url:
        sess.proxies = {"http": proxy_url, "https": proxy_url}

    def _det_uuid4(seed: str) -> str:
        digest = bytearray(hashlib.sha256((seed or "deepseek-web").encode("utf-8")).digest()[:16])
        digest[6] = (digest[6] & 0x0F) | 0x40
        digest[8] = (digest[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(digest)))

    # 构造请求头与系统参数 (全面以 iOS 原生极简标头为主力，Web 同步最新 2.8.0 前端规范)
    extra_web_headers: Dict[str, str] = {}
    if platform == "android":
        os_param = "android"
        platform_header = "android"
        client_ver = "2.1.0"
        app_ver = "20260201.1"
        ua = "DeepSeek/2.1.0 (Linux; U; Android 12; Pixel 6 Build/SP2A.220505.002)"
        origin_header = None
        referer_header = None
    elif platform == "web":
        os_param = "web"
        platform_header = "web"
        client_ver = "2.8.0"
        app_ver = ""
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        origin_header = "https://chat.deepseek.com"
        referer_header = "https://chat.deepseek.com/sign_in"
        extra_web_headers = {
            "x-client-bundle-id": "com.deepseek.chat",
            "x-client-timezone-offset": "28800",
            "x-device-id": _det_uuid4(email or str(account.get("account_label") or "") or credential_id),
            "x-device-model": "",
        }
    else:  # 默认与主力：ios
        platform = "ios"
        os_param = "ios"
        platform_header = "ios"
        client_ver = "2.1.0"
        app_ver = "20260201.1"
        ua = "DeepSeek/2.1.0 (iPhone; iOS 17.5.1; Scale/3.00)"
        origin_header = None
        referer_header = None

    # 阶段一：加密 POST /api/v0/users/login
    # 严格保持真实登记的设备指纹，携带合法指纹进入登录流程
    final_device_id = device_id or ""

    login_payload = {
        "email": email,
        "mobile": "",
        "area_code": "",
        "password": password,
        "device_id": final_device_id,
        "os": os_param,
        "credential_id": credential_id
    }

    try:
        enc_body, enc_key_hdr = encrypt_auth_payload(login_payload)
    except Exception as e:
        return False, {"error": f"加密请求体失败: {e}", "kind": "encrypt_error"}

    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "x-request-encryption-key": enc_key_hdr,
        "User-Agent": ua,
        "x-client-platform": platform_header,
        "x-client-version": client_ver,
        "x-client-locale": "zh_CN",
    }
    if app_ver:
        headers["x-app-version"] = app_ver
    if extra_web_headers:
        headers.update(extra_web_headers)
    if origin_header:
        headers["Origin"] = origin_header
    if referer_header:
        headers["Referer"] = referer_header

    t0 = time.time()
    try:
        r_login = sess.post(LOGIN_URL, data=enc_body, headers=headers, timeout=timeout)
        cost_login = time.time() - t0
        resp = r_login.json()
    except Exception as e:
        return False, {"error": f"登录请求通信失败: {e}", "kind": "network_error", "cost": time.time() - t0}

    code = resp.get("code")
    data = resp.get("data") or {}
    biz_code = data.get("biz_code")
    biz_msg = data.get("biz_msg")
    biz_data = data.get("biz_data") or {}

    # 情况 A: 官方直接放行并下发 Token (biz_code=0)
    tok, is_muted, mute_until = _extract_user_chat_state(biz_data)
    if code == 0 and (biz_code == 0 or biz_code is None) and tok:
        return True, {
            "token": tok,
            "biz_code": 0,
            "mode": "direct_login",
            "cost": cost_login,
            "platform": platform,
            "is_muted": is_muted,
            "mute_until": mute_until,
            "status": "MUTED" if is_muted else "ACTIVE",
        }

    # 情况 B: 命中受信任设备签名挑战 (biz_code=13)
    if biz_code == 13:
        mfa_session_id = biz_data.get("mfa_session_id")
        mfa_challenge = biz_data.get("mfa_challenge")

        if not mfa_private_key:
            return False, {
                "error": "命中 biz_code=13 挑战但缺少 mfa_private_key 私钥",
                "kind": "missing_private_key",
                "biz_code": 13,
                "mfa_session_id": mfa_session_id,
                "cost": cost_login
            }

        # 【避坑铁律二】：待签文本规范 [mfa_challenge, mfa_session_id, credential_id, "verify"].join("\n")
        raw_text_to_sign = "\n".join([str(mfa_challenge), str(mfa_session_id), str(credential_id), "verify"])
        try:
            mfa_signature = sign_challenge_p256(mfa_private_key, raw_text_to_sign)
        except Exception as e_sign:
            return False, {"error": f"计算 ECDSA 签名失败: {e_sign}", "kind": "sign_error"}

        # 【避坑铁律二】：verify_signature 必须发明文 JSON，绝不能带 x-request-encryption-key
        ver_payload = {
            "mfa_session_id": mfa_session_id,
            "mfa_signature": mfa_signature
        }

        ver_headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "User-Agent": ua,
            "x-client-platform": platform_header,
            "x-client-version": client_ver,
            "x-client-locale": "zh_CN",
        }
        if app_ver:
            ver_headers["x-app-version"] = app_ver
        if extra_web_headers:
            ver_headers.update(extra_web_headers)
        if origin_header:
            ver_headers["Origin"] = origin_header
        if referer_header:
            ver_headers["Referer"] = referer_header

        t_ver = time.time()
        try:
            r_ver = sess.post(VERIFY_SIG_URL, json=ver_payload, headers=ver_headers, timeout=timeout)
            ver_resp = r_ver.json()
            total_cost = time.time() - t0
        except Exception as e_ver:
            return False, {"error": f"验签通信失败: {e_ver}", "kind": "network_error", "cost": time.time() - t0}

        v_code = ver_resp.get("code")
        v_data = ver_resp.get("data") or {}
        v_biz_code = v_data.get("biz_code")
        v_biz_msg = v_data.get("biz_msg")
        v_biz_data = v_data.get("biz_data") or {}
        v_tok, v_is_muted, v_mute_until = _extract_user_chat_state(v_biz_data)

        if v_code == 0 and (v_biz_code == 0 or v_biz_code is None) and v_tok:
            return True, {
                "token": v_tok,
                "biz_code": 0,
                "mode": "signature_challenge",
                "cost": total_cost,
                "platform": platform,
                "is_muted": v_is_muted,
                "mute_until": v_mute_until,
                "status": "MUTED" if v_is_muted else "ACTIVE",
            }
        else:
            return False, {
                "error": "验签完成但未成功签发 Token (设备信任关系或已在服务端被注销)",
                "kind": "challenge_rejected",
                "v_code": v_code,
                "v_biz_code": v_biz_code,
                "v_biz_msg": v_biz_msg,
                "biz_code": v_biz_code if v_biz_code is not None else v_code,
                "cost": total_cost,
                "platform": platform
            }

    # 情况 C: 风控或其他状态拦截 (b11=数美风控, b14=强制邮件OTP, b4=参数异常等)
    eff_biz = biz_code if biz_code is not None else code
    eff_msg = biz_msg or resp.get("msg")
    return False, {
        "error": f"登录被官方拦截 biz_code={eff_biz} msg={eff_msg}",
        "kind": f"biz_{eff_biz}",
        "code": code,
        "biz_code": eff_biz,
        "biz_msg": eff_msg,
        "cost": cost_login,
        "platform": platform
    }
