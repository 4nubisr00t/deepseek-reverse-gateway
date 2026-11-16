# -*- coding: utf-8 -*-
"""单账号纯 HTTP 快速注册与受信任 P-256 设备铸造引擎（教学/研究范本）。

展示完整 7 步协议握手闭环：
1. PoW 出口合规预检（识别大陆受限出口）
2. 解耦打码与数美设备指纹获取（对接 blacklight-vlm-solver 或人工输入 Token）
3. 触发注册邮件验证码
4. Guest PoW 计算与纯 HTTP 提交建号
5. RSA+AES 加密登录握手获取 mfa_session_id 与二次确权发码
6. 核验 OTP 换取 otpTicket → 内存生成 ECDSA P-256 密钥对 → 登记公钥并按官方规范签名换发 30 天受信任凭据
7. 启动全新会话纯 HTTP 免密重登自检（biz_code=13 → P1363 签章验证），成功后直接入池
"""

from __future__ import annotations

import base64
import json
import secrets
import string
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests
from curl_cffi import requests as cffi_requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import config_manager
from app.core.pow_solver import DeepSeekPOW
from app.engine.http_auth_engine import (
    encrypt_auth_payload,
    is_valid_shumei_device_id,
    renew_account_token,
    sign_challenge_p256,
)

AUTH_BASE = "https://auth.deepseek.com/auth-api/v0/users"
CREATE_CODE_URL = f"{AUTH_BASE}/create_verification_code"
GUEST_CHALLENGE_URL = f"{AUTH_BASE}/create_guest_challenge"
REGISTER_URL = f"{AUTH_BASE}/register"
LOGIN_URL = f"{AUTH_BASE}/login"
MFA_CREATE_CODE_URL = f"{AUTH_BASE}/mfa/create_verification_code"
MFA_CHECK_CODE_URL = f"{AUTH_BASE}/mfa/check_verification_code"
TRUSTED_REGISTER_URL = f"{AUTH_BASE}/mfa/trusted_device/register"
TRUSTED_VERIFY_URL = f"{AUTH_BASE}/mfa/trusted_device/verify"

DEFAULT_FARM_SOLVE_URL = "http://127.0.0.1:9527/farm/solve"

_PLATFORM_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://platform.deepseek.com",
    "Referer": "https://platform.deepseek.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "x-client-locale": "zh_CN",
    "x-client-platform": "web",
    "x-client-version": "1.6.1",
}

_IOS_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "User-Agent": "DeepSeek/2.1.0 (iPhone; iOS 17.5.1; Scale/3.00)",
    "x-client-platform": "ios",
    "x-client-version": "2.1.0",
    "x-app-version": "20260201.1",
    "x-client-locale": "zh_CN",
}


def generate_strong_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    core = "".join(secrets.choice(alphabet) for _ in range(max(12, length - 4)))
    return f"Ds{core}9!"


def generate_fido2_p256_keypair() -> Tuple[str, str]:
    """在内存中生成 ECDSA P-256 (ES256) 公私钥对，返回 (priv_b64, pub_b64)。"""
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_der = priv.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(priv_der).decode("ascii"), base64.b64encode(pub_der).decode("ascii")


def _new_cffi_session(proxy: Optional[str] = None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return cffi_requests.Session(impersonate="chrome131", proxies=proxies, timeout=20)


def _post_json(sess, url: str, retries: int = 2, **kwargs) -> Dict[str, Any]:
    last_err = ""
    for attempt in range(max(1, retries + 1)):
        try:
            r = sess.post(url, **kwargs)
            try:
                return r.json()
            except Exception:
                last_err = f"HTTP {r.status_code}: {r.text[:240]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(0.8)
    return {"code": -1, "msg": last_err, "data": {}}


def request_captcha_from_farm(
    email: str,
    scenario: str,
    account_seed: str,
    farm_url: str = DEFAULT_FARM_SOLVE_URL,
    timeout: int = 240,
) -> Tuple[str, str]:
    """向解耦打码农场 (blacklight-vlm-solver) 请求单次验证码 Token 与设备指纹。"""
    u = str(farm_url or DEFAULT_FARM_SOLVE_URL).strip().rstrip("/")
    if not u.endswith("/farm/solve"):
        u = u + "/farm/solve"
    resp = requests.post(
        u,
        json={
            "email": email,
            "scenario": scenario,
            "account_seed": account_seed,
            "timeout": timeout,
        },
        timeout=timeout + 15,
    )
    data = resp.json() if resp.content else {}
    if not resp.ok or not data.get("ok"):
        raise RuntimeError(f"打码农场返回失败 ({resp.status_code}): {data.get('error') or resp.text[:160]}")
    return str(data.get("token") or ""), str(data.get("device_id") or "")


def release_captcha_seed(account_seed: str, farm_url: str = DEFAULT_FARM_SOLVE_URL) -> None:
    try:
        base = str(farm_url or DEFAULT_FARM_SOLVE_URL).strip().rstrip("/")
        if base.endswith("/farm/solve"):
            base = base[: -len("/farm/solve")]
        requests.post(f"{base}/farm/release_seed", json={"account_seed": account_seed}, timeout=5)
    except Exception:
        pass


class QuickRegisterEngine:
    """面向单个账号的交互式/三段式快速注册状态机（无任何批量/接码硬编码依赖）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def _log(self, job_id: str, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["logs"].append(line)
                if len(job["logs"]) > 200:
                    job["logs"] = job["logs"][-200:]

    def preflight_probe(self, proxy: Optional[str] = None) -> Tuple[bool, str]:
        """0 成本 PoW 出口合规预检，拦截会被官方拒绝的大陆 IP (biz_code=6)。"""
        eff_proxy = (proxy or config_manager.config.proxy or "").strip() or None
        try:
            sess = _new_cffi_session(eff_proxy)
            r_pow = _post_json(
                sess,
                GUEST_CHALLENGE_URL,
                retries=1,
                headers=_PLATFORM_HEADERS,
                json={"target_path": "/auth-api/v0/users/register"},
                timeout=10,
            )
            ch = ((r_pow.get("data") or {}).get("biz_data") or {}).get("guest_challenge")
            if not isinstance(ch, dict):
                return False, f"获取 guest_challenge 失败: {r_pow}"
            ch["target_path"] = "/auth-api/v0/users/register"
            pow_tok = DeepSeekPOW.solve(ch)
            h = dict(_PLATFORM_HEADERS)
            h["x-ds-guest-pow-response"] = pow_tok
            r_probe = _post_json(
                sess,
                REGISTER_URL,
                retries=1,
                headers=h,
                json={
                    "region": "US",
                    "locale": "zh_CN",
                    "device_id": "",
                    "payload": {
                        "email": "preflight_probe@example.com",
                        "email_verification_code": "000000",
                        "password": "ProbeInvalidPassword1!",
                    },
                    "os": "web",
                },
                timeout=12,
            )
            biz_code = (r_probe.get("data") or {}).get("biz_code")
            biz_msg = str((r_probe.get("data") or {}).get("biz_msg") or "")
            if biz_code == 6 or "MAINLAND" in biz_msg.upper():
                return False, f"出口 IP 受限 ({biz_msg or 'REGISTER_FROM_MAINLAND'})，请更换海外代理"
            return True, f"出口可用 (proxy={eff_proxy or 'direct'}, biz_code={biz_code})"
        except Exception as e:
            return False, f"预检通信异常: {e}"

    def start(
        self,
        *,
        email: str,
        password: str = "",
        proxy: str = "",
        farm_url: str = DEFAULT_FARM_SOLVE_URL,
        captcha_token: str = "",
        device_id: str = "",
        pool_tag: str = "public",
        skip_preflight: bool = False,
    ) -> Dict[str, Any]:
        target_email = str(email or "").strip().lower()
        if "@" not in target_email or "." not in target_email.split("@", 1)[1]:
            raise ValueError("请输入有效的邮箱地址")

        eff_proxy = (proxy or config_manager.config.proxy or "").strip() or None
        eff_pass = str(password or "").strip() or generate_strong_password()
        eff_farm = str(farm_url or DEFAULT_FARM_SOLVE_URL).strip()
        job_id = f"qr_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        account_seed = f"quick_{uuid.uuid4().hex[:10]}"

        job = {
            "job_id": job_id,
            "email": target_email,
            "password": eff_pass,
            "proxy": eff_proxy or "",
            "farm_url": eff_farm,
            "account_seed": account_seed,
            "pool_tag": str(pool_tag or "public").strip() or "public",
            "device_id": str(device_id or "").strip(),
            "stage": "preflight",
            "logs": [],
            "error": "",
            "created_at": int(time.time()),
            "account_summary": None,
        }
        with self._lock:
            self._jobs[job_id] = job

        if not skip_preflight:
            ok_probe, msg_probe = self.preflight_probe(eff_proxy)
            self._log(job_id, f"[预检] {msg_probe}")
            if not ok_probe:
                self._fail_job(job_id, msg_probe)
                raise RuntimeError(msg_probe)

        tok1 = str(captcha_token or "").strip()
        dev1 = str(device_id or "").strip()
        if not tok1:
            self._log(job_id, f"[阶段 1] 向打码农场请求注册验证码凭据 ({eff_farm})...")
            tok1, dev1 = request_captcha_from_farm(
                target_email, scenario="register", account_seed=account_seed, farm_url=eff_farm
            )
        if not is_valid_shumei_device_id(dev1):
            err = f"未取得有效设备指纹 device_id ({dev1!r})"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        job["device_id"] = dev1
        sess = _new_cffi_session(eff_proxy)
        r_code = _post_json(
            sess,
            CREATE_CODE_URL,
            retries=2,
            headers=_PLATFORM_HEADERS,
            json={
                "email": target_email,
                "locale": "zh_CN",
                "type": "register",
                "hcaptcha_token": tok1,
                "turnstile_token": tok1,
            },
            timeout=15,
        )
        biz_code = (r_code.get("data") or {}).get("biz_code")
        if r_code.get("code") != 0 or biz_code not in (0, None):
            err = f"发送注册验证码被拒: {r_code}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        with self._lock:
            job["stage"] = "awaiting_register_otp"
        self._log(job_id, f"[阶段 1] 注册验证码已发往 {target_email}，等待输入 6 位注册验证码...")
        return self.get_job(job_id)

    def verify_register(
        self,
        *,
        job_id: str,
        register_otp: str,
        login_captcha_token: str = "",
        login_device_id: str = "",
    ) -> Dict[str, Any]:
        job = self._require_job(job_id)
        code1 = str(register_otp or "").strip()
        if not code1:
            raise ValueError("请输入注册验证码")

        eff_proxy = job["proxy"] or None
        sess = _new_cffi_session(eff_proxy)

        self._log(job_id, "[阶段 2] 求解 Guest PoW 并提交 /register...")
        r_pow = _post_json(
            sess,
            GUEST_CHALLENGE_URL,
            retries=2,
            headers=_PLATFORM_HEADERS,
            json={"target_path": "/auth-api/v0/users/register"},
            timeout=10,
        )
        ch = ((r_pow.get("data") or {}).get("biz_data") or {}).get("guest_challenge")
        if not isinstance(ch, dict):
            err = f"获取 register guest_challenge 失败: {r_pow}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)
        ch["target_path"] = "/auth-api/v0/users/register"
        pow_tok = DeepSeekPOW.solve(ch)

        h_reg = dict(_PLATFORM_HEADERS)
        h_reg["x-ds-guest-pow-response"] = pow_tok
        r_reg = _post_json(
            sess,
            REGISTER_URL,
            retries=2,
            headers=h_reg,
            json={
                "region": "US",
                "locale": "zh_CN",
                "device_id": "",
                "payload": {
                    "email": job["email"],
                    "email_verification_code": code1,
                    "password": job["password"],
                },
                "os": "web",
            },
            timeout=15,
        )
        reg_biz = (r_reg.get("data") or {}) if isinstance(r_reg.get("data"), dict) else {}
        if r_reg.get("code") != 0 or reg_biz.get("biz_code") not in (0, None):
            err = f"注册提交被拒: {r_reg}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)
        self._log(job_id, "[阶段 2] 账号已写入上游数据库，开始加密登录握手...")

        body_enc, key_enc = encrypt_auth_payload({
            "email": job["email"],
            "mobile": "",
            "area_code": "",
            "password": job["password"],
            "device_id": job["device_id"],
            "os": "ios",
            "credential_id": "",
        })
        h_login = dict(_IOS_HEADERS)
        h_login["x-request-encryption-key"] = key_enc
        r_log = _post_json(sess, LOGIN_URL, retries=2, data=body_enc, headers=h_login, timeout=15)
        mfa_session_id = ((r_log.get("data") or {}).get("biz_data") or {}).get("mfa_session_id")
        if not mfa_session_id:
            err = f"首次登录未返回 mfa_session_id: {r_log}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)
        job["mfa_session_id"] = mfa_session_id
        self._log(job_id, f"[阶段 3] 已捕获 mfa_session_id={mfa_session_id[:16]}...，准备触发二次确权发码")

        tok2 = str(login_captcha_token or "").strip()
        dev2 = str(login_device_id or "").strip() or job["device_id"]
        if not tok2:
            tok2, dev2 = request_captcha_from_farm(
                job["email"],
                scenario="login",
                account_seed=job["account_seed"],
                farm_url=job["farm_url"],
            )
        if dev2 and str(dev2).strip() != job["device_id"]:
            err = "二次打码设备指纹与注册端不一致，为避免信任绑定错位已中止"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        r_mfa = _post_json(
            sess,
            MFA_CREATE_CODE_URL,
            retries=2,
            headers=_IOS_HEADERS,
            json={
                "mfa_session_id": mfa_session_id,
                "turnstile_token": tok2,
                "hcaptcha_token": tok2,
                "shumei_device_id": job["device_id"],
            },
            timeout=15,
        )
        release_captcha_seed(job["account_seed"], farm_url=job["farm_url"])
        if r_mfa.get("code") != 0 or ((r_mfa.get("data") or {}).get("biz_code") not in (0, None)):
            err = f"发送 MFA 二次确权邮件失败: {r_mfa}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        with self._lock:
            job["stage"] = "awaiting_mfa_otp"
        self._log(job_id, f"[阶段 3] 二次确权验证码已发往 {job['email']}，等待输入第 2 封邮件验证码...")
        return self.get_job(job_id)

    def complete(self, *, job_id: str, mfa_otp: str) -> Dict[str, Any]:
        job = self._require_job(job_id)
        code2 = str(mfa_otp or "").strip()
        if not code2:
            raise ValueError("请输入二次确权验证码")
        mfa_session_id = str(job.get("mfa_session_id") or "")
        if not mfa_session_id:
            raise RuntimeError("缺少 mfa_session_id，请先执行 verify_register")

        eff_proxy = job["proxy"] or None
        sess = _new_cffi_session(eff_proxy)

        self._log(job_id, "[阶段 4] 核验二次确权码换取 otpTicket 与会话 Token...")
        r_chk = _post_json(
            sess,
            MFA_CHECK_CODE_URL,
            retries=2,
            headers=_IOS_HEADERS,
            json={"mfa_session_id": mfa_session_id, "verification_code": code2},
            timeout=15,
        )
        biz_chk = (r_chk.get("data") or {}).get("biz_data") or {}
        otp_ticket = biz_chk.get("mfa_ticket")
        user_token = (biz_chk.get("user") or {}).get("token") or biz_chk.get("token")
        if not otp_ticket or not user_token:
            err = f"换取 otpTicket 失败: {r_chk}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        self._log(job_id, "[阶段 5] 内存铸造 ECDSA P-256 密钥对并登记设备公钥...")
        priv_b64, pub_b64 = generate_fido2_p256_keypair()
        h_auth = dict(_IOS_HEADERS)
        h_auth["Authorization"] = f"Bearer {user_token}"
        r_dev = _post_json(
            sess,
            TRUSTED_REGISTER_URL,
            retries=2,
            headers=h_auth,
            json={"mfa_ticket": otp_ticket, "mfa_public_key": pub_b64, "mfa_algorithm": "ES256"},
            timeout=15,
        )
        biz_dev = (r_dev.get("data") or {}).get("biz_data") or {}
        cred_id = biz_dev.get("credential_id")
        mfa_challenge = biz_dev.get("mfa_challenge")
        register_ticket = biz_dev.get("mfa_ticket")
        if not cred_id or not mfa_challenge:
            err = f"设备公钥登记失败: {r_dev}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        self._log(job_id, "[阶段 6] 签署注册考卷并换取 30 天受信任签证...")
        raw_text = "\n".join([str(mfa_challenge), str(register_ticket), str(cred_id), "register"])
        sig = sign_challenge_p256(priv_b64, raw_text)
        r_ver = _post_json(
            sess,
            TRUSTED_VERIFY_URL,
            retries=2,
            headers=h_auth,
            json={"mfa_ticket": register_ticket, "mfa_signature": sig},
            timeout=15,
        )
        ver_biz = (r_ver.get("data") or {}).get("biz_data") or {}
        expire_at = ver_biz.get("expire_at")
        if (r_ver.get("data") or {}).get("biz_code") not in (0, None) and not expire_at:
            err = f"受信任设备确权失败: {r_ver}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        now_ts = int(time.time())
        record = {
            "account_label": job["email"],
            "email": job["email"],
            "_email": job["email"],
            "password": job["password"],
            "_password": job["password"],
            "token": user_token,
            "cookie": "",
            "session_id": "",
            "proxy": job["proxy"],
            "is_valid": True,
            "status": "ACTIVE",
            "pool_tag": job["pool_tag"],
            "credential_id": cred_id,
            "mfa_private_key": priv_b64,
            "mfa_public_key": pub_b64,
            "mfa_key_version": "1",
            "device_id": job["device_id"],
            "device_fingerprint": json.dumps({"device_id": job["device_id"]}),
            "expire_at": expire_at,
            "token_updated_at": now_ts,
        }

        self._log(job_id, "[阶段 7] 独立会话免密签章重登自检 (verify_relogin)...")
        ok_ver = False
        ver_res = None
        for v_try in range(3):
            ok_ver, ver_res = renew_account_token(record, platform="ios", proxy_url=eff_proxy, timeout=20)
            if ok_ver or not isinstance(ver_res, dict) or ver_res.get("kind") != "network_error":
                break
            time.sleep(1.2)
        if not ok_ver or not isinstance(ver_res, dict) or not ver_res.get("token"):
            err = f"免密签章自检失败: {ver_res}"
            self._fail_job(job_id, err)
            raise RuntimeError(err)

        record["token"] = ver_res["token"]
        record["token_updated_at"] = int(time.time())
        config_manager.add_account(record)
        try:
            from app.engine.account_pool import account_pool
            account_pool.reload()
        except Exception:
            pass

        with self._lock:
            job["stage"] = "completed"
            job["account_summary"] = {
                "email": job["email"],
                "credential_id": cred_id,
                "expire_at": expire_at,
                "device_id": job["device_id"][:16] + "...",
            }
        self._log(job_id, f"[完成] 账号 {job['email']} 五件套铸造并入池成功 (CredentialId={cred_id})")
        return self.get_job(job_id)

    def _require_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(f"注册任务不存在: {job_id}")
            return job

    def _fail_job(self, job_id: str, err: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["stage"] = "failed"
                job["error"] = err

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {}
            return {
                "job_id": job["job_id"],
                "email": job["email"],
                "stage": job["stage"],
                "error": job["error"],
                "pool_tag": job["pool_tag"],
                "device_id": job["device_id"][:16] + ("..." if len(job["device_id"]) > 16 else ""),
                "logs": list(job["logs"]),
                "account_summary": job["account_summary"],
                "created_at": job["created_at"],
            }

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(self._jobs.keys())[-20:]
        return [self.get_job(jid) for jid in reversed(ids)]


quick_register_engine = QuickRegisterEngine()
