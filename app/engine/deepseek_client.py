"""
DeepSeek Web 原生通信客户端
- 统一使用现代桌面 Chrome 124+ 指纹 (TLS JA3/JA4, HTTP/2, Client Hints)
- 支持 PoW 异步求解
- 流式 SSE 解析器 (支持思考链与正式回答，提取真实 message_id)
"""

import os
import json
import uuid
import time
import hashlib
import asyncio
from typing import AsyncGenerator, Dict, Any, Optional, Tuple, List
from curl_cffi import requests as cffi_requests
from app.core.pow_solver import DeepSeekPOW
from app.config import UPSTREAM_MODEL_TYPE, config_manager


class ChatEvent:
    def __init__(
        self,
        event_type: str,
        content: str = "",
        message_id: Optional[str] = None,
        error_code: Optional[str] = None,
        mute_until: Optional[int] = None,
    ):
        self.event_type = event_type  # "thinking" | "content" | "message_id" | "error" | "done"
        self.content = content
        self.message_id = message_id
        self.error_code = error_code
        # T12: 上游有时会**直接告诉我们禁言到什么时候**（`biz_data.mute_until`）。
        # 旧实现已经在检测这个字段，却把它丢掉了 —— 于是账号池只能瞎猜，
        # 而路由传的又是 None，导致账号被熔断后**永不恢复**。
        self.mute_until = mute_until


def _parse_mute_until(raw: Any) -> Optional[int]:
    """把上游给的禁言截止时间规整成 unix 秒。

    上游可能给秒、可能给毫秒、也可能是相对秒数 —— 无法事前确定，
    所以按数值量级判断：>1e12 视为毫秒；<1e9 且远小于当前时间视为"还要等多久"。
    拿不准就返回 None，由调用方退避兜底（**绝不猜一个具体时间点**：
    猜错会把一个还在禁言的账号放回池子，然后继续失败）。
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    now = time.time()
    if v > 1e12:                 # 毫秒时间戳
        cand = int(v / 1000)
    elif v > 1e9:                # 秒时间戳
        cand = int(v)
    else:                        # 相对秒数（还要等多久）
        cand = int(now + v)
    # 合理区间校验：必须在未来、且不超过 15 天（DeepSeek 官方禁言常见周期为 7~10 天）
    if cand <= now or cand > now + 15 * 86400:
        return None
    return cand


def extract_quota_reset_until(biz_data: Any) -> Optional[int]:
    """从新版 `biz_data`（`mute` + `quotas`）或旧版 `mute_until` 中提取恢复时间戳（unix 秒）。"""
    if not isinstance(biz_data, dict):
        return None

    candidates: List[int] = []
    mute_obj = biz_data.get("mute")
    if isinstance(mute_obj, dict):
        mu = _parse_mute_until(mute_obj.get("mute_until"))
        if mu:
            candidates.append(mu)

    direct_mu = _parse_mute_until(biz_data.get("mute_until"))
    if direct_mu:
        candidates.append(direct_mu)

    quotas = biz_data.get("quotas")
    if isinstance(quotas, list):
        for q in quotas:
            if isinstance(q, dict) and q.get("is_exhausted"):
                ra = _parse_mute_until(q.get("reset_at"))
                if ra:
                    candidates.append(ra)

    return max(candidates) if candidates else None


def classify_upstream_error(
    code: Any,
    biz_code: Any,
    biz_msg: str = "",
    biz_data: Any = None,
) -> Tuple[str, Optional[int]]:
    """统一分类 DeepSeek 2.8.0 上游返回的 JSON 业务/全局错误码，并提取解禁或配额重置时间。"""
    msg_str = str(biz_msg or "")
    msg_lower = msg_str.lower()
    bd = biz_data if isinstance(biz_data, dict) else {}
    quotas = bd.get("quotas") if isinstance(bd.get("quotas"), list) else []
    has_exhausted_quota = any(isinstance(q, dict) and bool(q.get("is_exhausted")) for q in quotas)
    mute_obj = bd.get("mute") if isinstance(bd.get("mute"), dict) else None

    is_permanently_banned = (
        code == 40012
        or biz_code in (10, 40012)
        or "USER_IS_BANNED" in msg_str
        or "banned" in msg_lower
        or "account suspended" in msg_lower
        or "永久封禁" in msg_str
    )
    is_muted = (
        not is_permanently_banned
        and (
            code == 50006
            or biz_code in (5, 50006)
            or "muted" in msg_lower
            or "mute_until" in bd
            or (mute_obj is not None)
        )
    )
    is_quota_exceeded = (
        not is_permanently_banned
        and not is_muted
        and (
            biz_code in (15, 29)
            or "usage_quota_exceeded" in msg_lower
            or "thinking_quota_exceeded" in msg_lower
            or "usage quota" in msg_lower
            or "quota exceeded" in msg_lower
            or "使用额度已达上限" in msg_str
            or "额度已达上限" in msg_str
            or has_exhausted_quota
        )
    )
    is_challenge_required = (
        code in (40303, 40304)
        or biz_code in (40303, 40304)
        or "challenge_required" in msg_lower
        or "challenge_failed" in msg_lower
    )
    is_rate_limited = (
        code in (429, 40029)
        or biz_code in (7, 8, 40029)
        or "ip_access_restricted" in msg_lower
        or "operation too fast" in msg_lower
        or "too many requests" in msg_lower
        or "操作过快" in msg_str
        or "过于频繁" in msg_str
    )
    is_server_busy = (
        code in (500, 502, 503, 504)
        or "服务器繁忙" in msg_str
        or "系统负载过高" in msg_str
        or "服务繁忙" in msg_str
        or "server busy" in msg_lower
        or "service unavailable" in msg_lower
    )
    is_invalid_ref_file = (
        biz_code == 9
        or "invalid ref file id" in msg_lower
        or "ref file id" in msg_lower
    )
    is_invalid_mid = (
        biz_code in (1, 26)
        or "invalid message id" in msg_lower
        or "invalid chat session id" in msg_lower
    )
    is_auth_expired = (
        code in (40001, 40002, 40003)
        or "invalid token" in msg_lower
        or "invalid_token" in msg_lower
        or "authorization failed" in msg_lower
    )

    err_tag = (
        "account_banned" if is_permanently_banned
        else "biz:5" if is_muted
        else "quota_exceeded" if is_quota_exceeded
        else "challenge_required" if is_challenge_required
        else "rate_limit" if is_rate_limited
        else "upstream_busy" if is_server_busy
        else "invalid_ref_file" if is_invalid_ref_file
        else "invalid_message_id" if is_invalid_mid
        else "auth_expired" if is_auth_expired
        else f"biz:{biz_code}" if biz_code
        else str(code)
    )
    return err_tag, extract_quota_reset_until(bd)


class DeepSeekAuthError(Exception):
    """账号凭证被上游拒绝 —— **等多久都不会好，必须换号**。

    2026-09-11 实测到的真实响应：
        HTTP 200  {"code": 40003, "msg": "Authorization Failed (invalid token)", "data": null}

    ⚠️ 两个坑叠在一起：
    1. **HTTP 状态码是 200**，所以按状态码判断"凭证失效"（本模块 T12 给流式路径
       加的那条 401/403 检查）**根本不会触发**；
    2. `data` 是 `null`，旧代码 `data.get("biz_data", {})` 直接崩成
       `'NoneType' object has no attribute 'get'`，最终对外只是含糊的
       502「DeepSeek 会话创建失败」。

    合起来的后果：**一个 token 失效的号会永远留在池子里**，
    被分配到的每个请求都失败，而没有任何地方能看出"是这个号废了"。
    所以这里必须把它**单独识别成一种错误**，让上层能把这个号摘出去。
    """


_AUTH_FAIL_CODES = {40003, 40001, 40002, 401, 403}
_AUTH_FAIL_HINTS = ("invalid token", "authorization failed", "unauthorized",
                    "token expired", "login required", "账号异常", "登录已过期")


class DeepSeekClient:
    """与 chat.deepseek.com 交互的高可用原生客户端"""

    CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    IOS_UA = "DeepSeek/2.1.0 (iPhone; iOS 17.5.1; Scale/3.00)"
    WEB_CLIENT_VERSION = "2.8.0"

    @staticmethod
    def _web_device_id(seed: str = "") -> str:
        """生成符合官方前端校验的确定性 UUID v4 设备标识。"""
        digest = bytearray(hashlib.sha256((seed or "deepseek-web").encode("utf-8")).digest()[:16])
        digest[6] = (digest[6] & 0x0F) | 0x40
        digest[8] = (digest[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(digest)))

    @classmethod
    def get_client_platform(cls, platform: Optional[str] = None) -> str:
        """解析生效的上游协议端（参数 -> DSG_CLIENT_PLATFORM -> config.client_platform -> ios）。"""
        raw = (
            platform
            or os.environ.get("DSG_CLIENT_PLATFORM")
            or getattr(config_manager.config, "client_platform", "ios")
            or "ios"
        )
        val = str(raw).strip().lower()
        return val if val in ("ios", "web") else "ios"

    @classmethod
    def get_impersonate(cls, platform: Optional[str] = None) -> str:
        """返回与当前协议端匹配的 curl_cffi TLS 指纹。"""
        return "safari17_0" if cls.get_client_platform(platform) == "ios" else "chrome124"

    @staticmethod
    def _format_resp_diag(resp, context: str, platform: str = "") -> str:
        """生成不泄漏凭证的上游诊断摘要（HTTP/CT/cf-ray/Set-Cookie键/截断body）。"""
        if resp is None:
            return f"context={context} platform={platform or '?'} resp=None"
        hdrs = getattr(resp, "headers", None) or {}
        try:
            low_hdrs = {str(k).lower(): str(v) for k, v in dict(hdrs).items()}
        except Exception:
            low_hdrs = {}
        ct = low_hdrs.get("content-type", "?")
        cf_ray = low_hdrs.get("cf-ray", "-")
        set_ck = low_hdrs.get("set-cookie", "")
        set_ck_keys: List[str] = []
        if set_ck:
            for part in str(set_ck).split(","):
                name = part.strip().split("=", 1)[0].strip()
                if name and len(name) <= 40 and " " not in name:
                    set_ck_keys.append(name)
        body = (getattr(resp, "text", "") or "")[:220].replace("\n", " ")
        return (
            f"context={context} platform={platform or '?'} http={getattr(resp, 'status_code', '?')} "
            f"ct={ct!r} cf_ray={cf_ray!r} set_cookie_keys={set_ck_keys[:8]} body={body!r}"
        )

    @staticmethod
    def _safe_json(resp, context: str):
        """安全解析响应体，解析不出来时返回 None 并打出可诊断的信息。"""
        try:
            data = resp.json()
        except Exception as e:
            body = (getattr(resp, "text", "") or "")[:200].replace("\n", " ")
            print(
                f"[Client] {context}: 响应非 JSON (HTTP {getattr(resp, 'status_code', '?')}, "
                f"{type(e).__name__}) body={body!r}",
                flush=True,
            )
            return None
        if data is None:
            body = (getattr(resp, "text", "") or "")[:200].replace("\n", " ")
            ct = (getattr(resp, "headers", {}) or {}).get("content-type", "?")
            print(
                f"[Client] {context}: 响应体解析为 None (HTTP {getattr(resp, 'status_code', '?')}, "
                f"ct={ct}) body={body!r}"
                + ("  ← 疑似上游返回 HTML 错误页（过载/CDN 拦截）"
                   if "html" in str(ct).lower() or "<html" in body.lower() else ""),
                flush=True,
            )
            return None
        return data

    @classmethod
    def build_headers(
        cls,
        token: str,
        cookie: str = "",
        session_id: Optional[str] = None,
        platform: Optional[str] = None,
        include_content_type: bool = True,
    ) -> Dict[str, str]:
        plat = cls.get_client_platform(platform)
        if plat == "ios":
            headers = {
                "accept": "*/*",
                "accept-language": "zh-Hans-CN;q=1, en-CN;q=0.9",
                "authorization": f"Bearer {token}",
                "user-agent": cls.IOS_UA,
                "x-app-version": "20260201.1",
                "x-client-platform": "ios",
                "x-client-version": "2.1.0",
                "x-client-locale": "zh_CN",
            }
        else:
            headers = {
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "authorization": f"Bearer {token}",
                "origin": "https://chat.deepseek.com",
                "referer": f"https://chat.deepseek.com/a/chat/s/{session_id}" if session_id else "https://chat.deepseek.com/",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": cls.CHROME_UA,
                "x-client-bundle-id": "com.deepseek.chat",
                "x-client-platform": "web",
                "x-client-version": cls.WEB_CLIENT_VERSION,
                "x-client-locale": "zh_CN",
                "x-client-timezone-offset": "28800",
                "x-device-id": cls._web_device_id(token),
                "x-device-model": "",
            }
        if include_content_type:
            headers["content-type"] = "application/json"
        if cookie:
            headers["cookie"] = cookie
        return headers

    @classmethod
    @classmethod
    def _post_with_failover(
        cls,
        url: str,
        *,
        json: Any,
        headers: Dict[str, str],
        impersonate: str,
        timeout: int,
        proxy: Optional[str] = None,
        stream: bool = False,
        context: str = "上游请求",
    ):
        from app.engine.proxy_manager import execute_with_proxy_failover

        return execute_with_proxy_failover(
            lambda p_url: cffi_requests.post(
                url,
                json=json,
                headers=headers,
                impersonate=impersonate,
                timeout=timeout,
                stream=stream,
                proxies={"http": p_url, "https": p_url} if p_url else None,
            ),
            primary_proxy=proxy,
            context=context,
        )

    @classmethod
    def _get_with_failover(
        cls,
        url: str,
        *,
        headers: Dict[str, str],
        impersonate: str,
        timeout: int,
        proxy: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        context: str = "上游请求",
    ):
        from app.engine.proxy_manager import execute_with_proxy_failover

        return execute_with_proxy_failover(
            lambda p_url: cffi_requests.get(
                url,
                params=params,
                headers=headers,
                impersonate=impersonate,
                timeout=timeout,
                proxies={"http": p_url, "https": p_url} if p_url else None,
            ),
            primary_proxy=proxy,
            context=context,
        )

    @classmethod
    def get_pow_challenge(cls, token: str, cookie: str = "", proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
        plat = cls.get_client_platform()
        impersonate = cls.get_impersonate(plat)
        headers = cls.build_headers(token, cookie, platform=plat)
        t0 = time.time()
        try:
            resp = cls._post_with_failover(
                "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
                json={"target_path": "/api/v0/chat/completion"},
                headers=headers,
                impersonate=impersonate,
                timeout=15,
                proxy=proxy,
                context="获取 PoW 挑战",
            )
            dt_ms = (time.time() - t0) * 1000
            if resp.status_code == 200:
                data = cls._safe_json(resp, "获取 PoW 挑战")
                if not isinstance(data, dict):
                    try:
                        from app.engine.drop_probe import record_event
                        record_event("pow_challenge", token=token, platform=plat, impersonate=impersonate,
                                     has_cookie=bool(cookie), proxy_primary=proxy, http_status=resp.status_code,
                                     error_kind="non_json", latency_ms=dt_ms, snippet=(resp.text or "")[:200])
                    except Exception:
                        pass
                    print(f"[Client] 获取 PoW 挑战非字典响应: {cls._format_resp_diag(resp, 'PoW非Dict', plat)}", flush=True)
                    return None
                data_obj = data.get("data") if isinstance(data.get("data"), dict) else {}
                biz_data = data_obj.get("biz_data") if isinstance(data_obj.get("biz_data"), dict) else {}
                ch = biz_data.get("challenge")
                try:
                    from app.engine.drop_probe import record_event
                    record_event("pow_challenge", token=token, platform=plat, impersonate=impersonate,
                                 has_cookie=bool(cookie), proxy_primary=proxy, http_status=resp.status_code,
                                 code=data.get("code"), biz_code=data_obj.get("biz_code"),
                                 msg=str(data_obj.get("biz_msg") or data.get("msg") or ""),
                                 error_kind="" if ch else "missing_challenge", latency_ms=dt_ms)
                except Exception:
                    pass
                if ch:
                    return ch
                print(
                    f"[Client] 获取 PoW 挑战缺失 challenge: code={data.get('code')} msg={data.get('msg')!r} "
                    f"biz_code={data_obj.get('biz_code')} biz_msg={data_obj.get('biz_msg')!r} "
                    f"| {cls._format_resp_diag(resp, 'PoW缺失', plat)}",
                    flush=True,
                )
                return None
            try:
                from app.engine.drop_probe import record_event
                record_event("pow_challenge", token=token, platform=plat, impersonate=impersonate,
                             has_cookie=bool(cookie), proxy_primary=proxy, http_status=resp.status_code,
                             error_kind="http_error", latency_ms=dt_ms, snippet=(resp.text or "")[:200])
            except Exception:
                pass
            print(f"[Client] 获取 PoW 挑战失败: {cls._format_resp_diag(resp, 'PoW失败', plat)}", flush=True)
        except Exception as e:
            print(f"[Client] 获取 PoW 挑战异常 (platform={plat}): {type(e).__name__}: {e}", flush=True)
        return None

    @classmethod
    def create_chat_session(cls, token: str, cookie: str = "", proxy: Optional[str] = None) -> Optional[str]:
        plat = cls.get_client_platform()
        impersonate = cls.get_impersonate(plat)
        headers = cls.build_headers(token, cookie, platform=plat)
        t0 = time.time()
        try:
            resp = cls._post_with_failover(
                "https://chat.deepseek.com/api/v0/chat_session/create",
                json={"character_id": None} if plat == "ios" else {},
                headers=headers,
                impersonate=impersonate,
                timeout=15,
                proxy=proxy,
                context="创建会话",
            )
            dt_ms = (time.time() - t0) * 1000
            if resp.status_code == 200:
                data = cls._safe_json(resp, "创建会话")
                if data is None:
                    return None
                biz = (data.get("data") or {}).get("biz_data") or {}
                sid = biz.get("chat_session", {}).get("id") or biz.get("id")
                code = data.get("code")
                msg = str(data.get("msg") or "")
                low = msg.lower()
                is_auth = (isinstance(code, int) and code in _AUTH_FAIL_CODES) or any(h in low for h in _AUTH_FAIL_HINTS)
                try:
                    from app.engine.drop_probe import record_event
                    record_event(
                        "create_session",
                        token=token,
                        platform=plat,
                        impersonate=impersonate,
                        has_cookie=bool(cookie),
                        proxy_primary=proxy,
                        http_status=resp.status_code,
                        code=code,
                        biz_code=(data.get("data") or {}).get("biz_code"),
                        msg=msg,
                        error_kind="auth_expired" if is_auth else ("" if sid else "no_sid"),
                        latency_ms=dt_ms,
                    )
                except Exception:
                    pass
                if sid:
                    return sid
                if is_auth:
                    raise DeepSeekAuthError(
                        f"创建会话被拒: code={code} msg={msg!r} ({cls._format_resp_diag(resp, '创建会话鉴权拒', plat)})"
                    )
                print(
                    f"[Client] 创建会话成功但无 session id: {cls._format_resp_diag(resp, '创建会话无SID', plat)}",
                    flush=True,
                )
                return None
            if resp.status_code == 401:
                try:
                    from app.engine.drop_probe import record_event
                    record_event("create_session", token=token, platform=plat, impersonate=impersonate,
                                 has_cookie=bool(cookie), proxy_primary=proxy, http_status=401,
                                 error_kind="auth_expired", latency_ms=dt_ms, snippet=(resp.text or "")[:200])
                except Exception:
                    pass
                raise DeepSeekAuthError(
                    f"创建会话被拒: {cls._format_resp_diag(resp, '创建会话401', plat)}"
                )
            if resp.status_code == 403:
                body_low = (resp.text or "")[:400].lower()
                is_waf = any(w in body_low for w in ("cloudflare", "waf", "<html", "<!doctype", "just a moment"))
                try:
                    from app.engine.drop_probe import record_event
                    record_event("create_session", token=token, platform=plat, impersonate=impersonate,
                                 has_cookie=bool(cookie), proxy_primary=proxy, http_status=403,
                                 error_kind="waf_blocked" if is_waf else "auth_expired",
                                 latency_ms=dt_ms, snippet=(resp.text or "")[:200])
                except Exception:
                    pass
                if is_waf:
                    print(f"[Client] 创建会话被 Cloudflare/WAF 拦截: {cls._format_resp_diag(resp, 'WAF403', plat)}", flush=True)
                    return None
                raise DeepSeekAuthError(
                    f"创建会话被拒: {cls._format_resp_diag(resp, '创建会话403', plat)}"
                )
            if resp.status_code in (500, 502, 503, 504, 520, 521, 522, 524):
                print(f"[Client] 创建会话遭遇官方服务故障: {cls._format_resp_diag(resp, '上游5xx', plat)}", flush=True)
                return None
            print(f"[Client] 创建会话失败: {cls._format_resp_diag(resp, '创建会话失败', plat)}", flush=True)
        except DeepSeekAuthError:
            raise
        except Exception as e:
            print(f"[Client] 创建会话异常 (platform={plat}): {type(e).__name__}: {e}", flush=True)
        return None

    @classmethod
    def delete_chat_session(cls, token: str, session_id: str, cookie: str = "", proxy: Optional[str] = None) -> bool:
        """删除 DeepSeek 云端历史会话，通过 biz_code==0 判断是否成功。"""
        plat = cls.get_client_platform()
        headers = cls.build_headers(token, cookie, session_id=session_id, platform=plat)
        try:
            resp = cls._post_with_failover(
                "https://chat.deepseek.com/api/v0/chat_session/delete",
                json={"chat_session_id": session_id},
                headers=headers,
                impersonate=cls.get_impersonate(plat),
                timeout=15,
                proxy=proxy,
                context="删除会话",
            )
            if resp.status_code != 200:
                print(f"[Client] 删除会话失败: sid={session_id} {cls._format_resp_diag(resp, '删除会话', plat)}")
                return False
            obj = cls._safe_json(resp, "删除会话")
            if obj is None:
                print(f"[Client] 删除会话响应不可解析: {session_id}", flush=True)
                return False
            data_block = obj.get("data") or {}
            biz_code = data_block.get("biz_code", None)
            if biz_code is None:
                biz_code = obj.get("code", 0)
            ok = biz_code == 0
            if not ok:
                print(
                    f"[Client] 删除会话被拒: sid={session_id} biz_code={biz_code} "
                    f"msg={data_block.get('biz_msg') or obj.get('msg')}"
                )
            return ok
        except Exception as e:
            print(f"[Client] 删除远端会话失败 (platform={plat}): {e}")
            return False

    @classmethod
    def stop_stream(
        cls,
        token: str,
        session_id: str,
        message_id: Optional[Any] = None,
        cookie: str = "",
        proxy: Optional[str] = None
    ) -> bool:
        """向 DeepSeek 官方下发 /api/v0/chat/stop_stream 中断生成指令，解除远端 WIP 锁"""
        if not session_id:
            return False
        mid = None
        if message_id is not None and str(message_id).strip() != "":
            try:
                mid = int(message_id)
            except (ValueError, TypeError):
                mid = message_id
        # 实测上游 /chat/stop_stream 对 message_id=None 会返回 HTTP 422 (StopStreamPayloadTarget)
        if mid is None:
            print(f"[Client] stop_stream 跳过: 尚无有效 message_id (sid={session_id})", flush=True)
            return False
        plat = cls.get_client_platform()
        headers = cls.build_headers(token, cookie, session_id=session_id, platform=plat)
        body = {
            "chat_session_id": session_id,
            "message_id": mid,
        }
        try:
            resp = cls._post_with_failover(
                "https://chat.deepseek.com/api/v0/chat/stop_stream",
                json=body,
                headers=headers,
                impersonate=cls.get_impersonate(plat),
                timeout=10,
                proxy=proxy,
                context="下发 stop_stream",
            )
            ok = resp.status_code == 200
            if ok:
                print(f"[Client] 下发 stop_stream 完成: HTTP {resp.status_code}, sid={session_id}, mid={mid}", flush=True)
            else:
                print(f"[Client] 下发 stop_stream 被拒: sid={session_id}, mid={mid}, {cls._format_resp_diag(resp, 'stop_stream', plat)}", flush=True)
            return ok
        except Exception as e:
            print(f"[Client] 下发 stop_stream 异常 (platform={plat}): {e}", flush=True)
            return False

    @classmethod
    async def stream_chat(
        cls,
        token: str,
        session_id: str,
        prompt: str,
        parent_message_id: Optional[str] = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        # T9：默认值走 config 的唯一来源，避免与白名单漂移。
        model_type: str = UPSTREAM_MODEL_TYPE,
        ref_file_ids: Optional[List[str]] = None,
        cookie: str = "",
        proxy: Optional[str] = None,
        _wip_retry_count: int = 0,
    ) -> AsyncGenerator[ChatEvent, None]:
        """执行流式聊天，异步 yield 事件流"""
        plat = cls.get_client_platform()
        impersonate = cls.get_impersonate(plat)
        headers = cls.build_headers(token, cookie, session_id, platform=plat)

        # 1. 求解 PoW
        challenge_cfg = await asyncio.to_thread(cls.get_pow_challenge, token, cookie, proxy)
        if challenge_cfg:
            try:
                pow_resp = await DeepSeekPOW.solve_async(challenge_cfg)
                if pow_resp:
                    headers["x-ds-pow-response"] = pow_resp
            except Exception as e:
                print(f"[Client] PoW 求解失败: {e}")

        # 2. 构造请求体
        p_mid = None
        if parent_message_id is not None:
            try:
                p_mid = int(parent_message_id)
            except (ValueError, TypeError):
                p_mid = parent_message_id

        body = {
            "chat_session_id": session_id,
            "parent_message_id": p_mid,
            "prompt": prompt,
            "ref_file_ids": list(dict.fromkeys(ref_file_ids or [])),
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled,
            # ── `model_type`：**恒定值**，不是模型选择，也不是"档位" ──
            #
            # 实测（2026-09-11 用真图探针）：
            #   · 取 "default" / "expert" / "vision" 上游**全部接受**，正文 sha1 相同
            #   · **发 `vision` 上去，上游回显把它改写成 `expert`** ——
            #     也就是说这个档位在上游已经不存在了（快速/专家/识图三模式已合并）
            #   · 只有**非法值**才 422
            # 所以：
            #   · 改它**不会**改变任何行为
            #   · 但它**必须**继续发送，且取值必须在白名单内
            #
            # ⛔ 不要"清理死代码"把它删掉，也不要传白名单外的值：
            #    上游会校验合法性，删掉/传错 = 把每个请求打成 422 故障。
            #    唯一来源见 app/config.py 的 `UPSTREAM_MODEL_TYPE`，
            #    并有 tests/test_model_convergence.py 钉死。
            "model_type": model_type,
        }

        # 3. 发起请求并解析 SSE
        def _fetch_stream():
            return cls._post_with_failover(
                "https://chat.deepseek.com/api/v0/chat/completion",
                json=body,
                headers=headers,
                impersonate=impersonate,
                timeout=30,
                stream=True,
                proxy=proxy,
                context="流式对话",
            )

        resp = await asyncio.to_thread(_fetch_stream)

        # 检查响应头
        ct = resp.headers.get("content-type", "")
        print(f"[DEBUG-DS-CLIENT] platform={plat} HTTP {resp.status_code}, ct={ct}, p_mid={p_mid}, model_type={model_type}, files={ref_file_ids}", flush=True)
        try:
            from app.engine.drop_probe import record_event
            if resp.status_code != 200 or ("text/event-stream" not in ct and "application/json" not in ct and "application/octet-stream" not in ct):
                record_event(
                    "completion_http",
                    token=token,
                    platform=plat,
                    impersonate=impersonate,
                    has_cookie=bool(cookie),
                    proxy_primary=proxy,
                    http_status=resp.status_code,
                    error_kind="auth_expired" if resp.status_code in (401, 403) else f"http_{resp.status_code}",
                    msg=f"ct={ct} sid={str(session_id)[:12]} files={len(ref_file_ids or [])}",
                    snippet=(resp.text or "")[:200] if resp.status_code != 200 else "",
                )
            else:
                record_event(
                    "completion_open",
                    token=token,
                    platform=plat,
                    impersonate=impersonate,
                    has_cookie=bool(cookie),
                    proxy_primary=proxy,
                    http_status=resp.status_code,
                    msg=f"sid={str(session_id)[:12]} files={len(ref_file_ids or [])} think={thinking_enabled}",
                )
        except Exception:
            pass

        # 1. 明确的服务器级瞬态过载/故障 (HTTP 500, 502, 503, 504, 520~524)，绝不惩罚账号
        if resp.status_code in (500, 502, 503, 504, 520, 521, 522, 523, 524):
            print(f"[DeepSeekClient] 官方服务级瞬态故障，绝不惩罚账号 | {cls._format_resp_diag(resp, 'stream5xx', plat)}", flush=True)
            yield ChatEvent(
                "error",
                content=f"DeepSeek 官方服务器繁忙/暂时不可用 (HTTP {resp.status_code})",
                error_code=f"upstream_busy_{resp.status_code}",
            )
            return

        # 2. 账号凭证被拒 (401/403)
        if resp.status_code == 401:
            print(f"[DeepSeekClient] 账号凭证被拒 (HTTP 401) | {cls._format_resp_diag(resp, 'stream401', plat)}", flush=True)
            yield ChatEvent(
                "error",
                content="账号凭证已失效 (HTTP 401)，需要重新登录或更换账号",
                error_code="auth_expired",
            )
            return

        if resp.status_code == 403:
            body_head = (resp.text or "")[:400].lower()
            if any(w in body_head for w in ("cloudflare", "waf", "<html", "<!doctype", "just a moment")):
                print(f"[DeepSeekClient] 遭遇 Cloudflare/WAF 403 拦截 | {cls._format_resp_diag(resp, 'streamWAF', plat)}", flush=True)
                yield ChatEvent(
                    "error",
                    content="遭遇出口网络安全拦截 (WAF 403)，请稍后重试",
                    error_code="waf_blocked",
                )
                return
            print(f"[DeepSeekClient] 账号凭证被拒 (HTTP 403) | {cls._format_resp_diag(resp, 'stream403', plat)}", flush=True)
            yield ChatEvent(
                "error",
                content="账号凭证被拒 (HTTP 403)",
                error_code="auth_expired",
            )
            return

        if "text/event-stream" not in ct and "application/json" not in ct and "application/octet-stream" not in ct:
            # 上游过载时会返回 **HTML 错误页**（非 JSON、非 SSE）。
            # 实测（2026-09-10 高并发压测）：25 账号 × 2 并发被瞬间打满后，
            # 80 个请求中有 22 个收到 text/html，旧实现一律报
            # `bad_content_type` → 上层转 502，客户端当作服务端故障而非限流，
            # 于是继续重试，进一步加剧过载。
            # 这与"rate_limit_reached 帧被静默丢弃"是**同一类问题的不同变体**：
            # 上游表达"太频繁了"有多种形态，必须逐一识别，不能只认一种。
            body_head = ""
            try:
                body_head = (resp.text or "")[:400]
            except Exception:
                pass
            looks_like_overload = (
                "html" in ct.lower()
                or "<html" in body_head.lower()
                or "<!doctype" in body_head.lower()
            )
            if looks_like_overload:
                print(
                    f"[DeepSeekClient] 上游返回 HTML 错误页（判定为过载/限流）: "
                    f"HTTP {resp.status_code} ct={ct} body={body_head[:120]!r}",
                    flush=True,
                )
                yield ChatEvent(
                    "error",
                    content="上游过载（返回 HTML 错误页），请稍后重试",
                    error_code="rate_limit",
                )
                return
            yield ChatEvent("error", content=f"DeepSeek 异常响应: Content-Type={ct}", error_code="bad_content_type")
            return

        # 逐行异步解析
        fragment_type = "RESPONSE"
        last_message_id = None
        notice_type = ""
        notice_buf: List[str] = []

        def _flush_notice():
            nonlocal notice_type, notice_buf
            if notice_buf:
                joined = "".join(notice_buf).strip()
                if joined:
                    print(
                        f"[Upstream-Notice] platform={plat} sid={session_id} mid={last_message_id} "
                        f"type={notice_type or 'TIP'} text={joined[:300]!r}",
                        flush=True,
                    )
                notice_buf.clear()
            notice_type = ""

        def _append_notice(ftype: str, txt: str):
            nonlocal notice_type
            if not txt:
                return
            if notice_type and notice_type != ftype:
                _flush_notice()
            notice_type = ftype
            if sum(len(x) for x in notice_buf) < 600:
                notice_buf.append(txt)

        def _line_generator():
            buf = b""
            for chunk in resp.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode("utf-8", errors="ignore").strip()
            if buf.strip():
                yield buf.decode("utf-8", errors="ignore").strip()

        gen = _line_generator()

        try:
            while True:
                line = await asyncio.to_thread(lambda: next(gen, None))
                if line is None:
                    break
                if not line:
                    continue

                if getattr(config_manager.config, "debug_raw_sse", False) and (line.startswith("data: ") or line.startswith("{") or "[DONE]" in line or "error" in line):
                    print(f"[DEBUG-DS-RAW] {line[:120]}", flush=True)

                # 检测非 SSE 的普通 JSON 报错
                if line.startswith("{"):
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict) and "code" in obj:
                            code = obj.get("code", 0)
                            data_block = obj.get("data") or {}
                            biz_code = data_block.get("biz_code", 0) or 0
                            biz_msg = data_block.get("biz_msg", "") or obj.get("msg", "")
                            if biz_code == 11 or "still wip" in str(biz_msg).lower():
                                print(f"[DeepSeekClient] 检测到会话 {session_id} 处于 WIP 状态，自动发送 stop_stream 解锁...", flush=True)
                                cls.stop_stream(token, session_id, message_id=p_mid, cookie=cookie, proxy=proxy)
                                if _wip_retry_count < 1:
                                    print(f"[DeepSeekClient] stop_stream 解锁后等待 0.5s 自动重试当前请求 (wip_retry={_wip_retry_count+1})...", flush=True)
                                    await asyncio.sleep(0.5)
                                    async for retry_ev in cls.stream_chat(
                                        token=token,
                                        session_id=session_id,
                                        prompt=prompt,
                                        parent_message_id=parent_message_id,
                                        thinking_enabled=thinking_enabled,
                                        search_enabled=search_enabled,
                                        model_type=model_type,
                                        ref_file_ids=ref_file_ids,
                                        cookie=cookie,
                                        proxy=proxy,
                                        _wip_retry_count=_wip_retry_count + 1,
                                    ):
                                        yield retry_ev
                                    return
                            err_tag, mu = classify_upstream_error(
                                code,
                                biz_code,
                                biz_msg=biz_msg,
                                biz_data=data_block.get("biz_data"),
                            )
                            try:
                                from app.engine.drop_probe import record_event
                                record_event(
                                    "completion_biz_err",
                                    token=token,
                                    platform=plat,
                                    impersonate=impersonate,
                                    has_cookie=bool(cookie),
                                    proxy_primary=proxy,
                                    code=code,
                                    biz_code=biz_code,
                                    msg=str(biz_msg or ""),
                                    error_kind=err_tag,
                                )
                            except Exception:
                                pass
                            if mu:
                                print(
                                    f"[DeepSeekClient] 上游给出恢复/解禁时间: "
                                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mu))}",
                                    flush=True,
                                )
                            yield ChatEvent(
                                "error",
                                content=biz_msg or "DeepSeek 业务错误",
                                error_code=err_tag,
                                mute_until=mu,
                            )
                            return
                    except Exception as e:
                        print(f"[DeepSeekClient] 解析上游 JSON 报错帧异常: {e}, line={line[:120]}", flush=True)
                    continue

                if line.startswith(":") or line == ":":
                    continue

                data_str = line[6:] if line.startswith("data: ") else line
                if data_str.strip() == "[DONE]":
                    yield ChatEvent("done", message_id=last_message_id)
                    return

                try:
                    obj = json.loads(data_str)
                    if not isinstance(obj, dict):
                        continue

                    # 0. 【关键修复】上游的限流/错误也会以 SSE data 帧送达，例如：
                    #      data: {"type":"error","content":"消息发送过于频繁，请稍后重试",
                    #             "clear_response":true,"finish_reason":"rate_limit_reached"}
                    #    上面的 `if line.startswith("{")` 分支只覆盖**裸 JSON** 形态，
                    #    而带 `data: ` 前缀的这帧不满足该条件，于是被当成普通事件解析；
                    #    它既没有 `v` 字段也不匹配任何分支 → **整帧被静默丢弃**。
                    #    后果：网关零输出 → 发空 stop → 客户端报 EMPTY_RESPONSE 并重试，
                    #    而上游其实明确说了"消息发送过于频繁"。日志实测该形态连续出现 12 次。
                    if obj.get("type") == "error" or "finish_reason" in obj:
                        content_msg = str(obj.get("content") or obj.get("message") or "DeepSeek 上游错误")
                        fr = str(obj.get("finish_reason") or "")
                        fr_lower = fr.lower()
                        cm_lower = content_msg.lower()
                        is_quota = (
                            "usage_quota_exceeded" in fr_lower
                            or "thinking_quota_exceeded" in fr_lower
                            or "quota_exceeded" in fr_lower
                            or "usage_quota_exceeded" in cm_lower
                            or "thinking_quota_exceeded" in cm_lower
                            or "usage quota" in cm_lower
                            or "quota exceeded" in cm_lower
                            or "使用额度已达上限" in content_msg
                            or "额度已达上限" in content_msg
                        )
                        is_challenge = (
                            "challenge_required" in fr_lower
                            or "challenge_failed" in fr_lower
                            or "challenge_required" in cm_lower
                            or "challenge_failed" in cm_lower
                        )
                        is_rl = (
                            "rate_limit" in fr_lower
                            or "parallel_chat" in fr_lower
                            or "过于频繁" in content_msg
                            or "有消息正在生成" in content_msg
                            or "rate limit" in cm_lower
                            or "too many requests" in cm_lower
                        )
                        is_ref_file = (
                            "invalid ref file id" in cm_lower
                            or "ref file id" in cm_lower
                        )
                        err_tag = (
                            "quota_exceeded" if is_quota
                            else "challenge_required" if is_challenge
                            else "rate_limit" if is_rl
                            else "invalid_ref_file" if is_ref_file
                            else (fr or "upstream_error")
                        )
                        mu = extract_quota_reset_until(obj.get("data") if isinstance(obj.get("data"), dict) else obj)
                        print(
                            f"[DeepSeekClient] 上游以 SSE 帧返回错误: tag={err_tag} "
                            f"content={content_msg[:120]!r} clear_response={obj.get('clear_response')}",
                            flush=True,
                        )
                        yield ChatEvent("error", content=content_msg, error_code=err_tag, mute_until=mu)
                        return

                    # 1. 提取 response_message_id (来自 event: ready 之后的 data)
                    if "response_message_id" in obj:
                        last_message_id = str(obj["response_message_id"])
                        yield ChatEvent("message_id", message_id=last_message_id)

                    # 2. 检查初始 response 对象
                    v_obj = obj.get("v")
                    if isinstance(v_obj, dict):
                        resp_meta = v_obj.get("response") or {}
                        if isinstance(resp_meta, dict):
                            mid = resp_meta.get("message_id")
                            if mid:
                                last_message_id = str(mid)
                                yield ChatEvent("message_id", message_id=last_message_id)
                            # 检查 fragments 初始化内容
                            for frag in resp_meta.get("fragments", []):
                                if isinstance(frag, dict):
                                    fragment_type = (frag.get("type") or "RESPONSE").upper()
                                    c = frag.get("content", "")
                                    if c:
                                        if fragment_type == "THINK":
                                            _flush_notice()
                                            if thinking_enabled:
                                                yield ChatEvent("thinking", content=c, message_id=last_message_id)
                                        elif fragment_type in ("TIP", "WARNING", "NOTE"):
                                            _append_notice(fragment_type, c)
                                        else:
                                            _flush_notice()
                                            yield ChatEvent("content", content=c, message_id=last_message_id)
                            init_content = resp_meta.get("content")
                            if init_content and not resp_meta.get("fragments"):
                                yield ChatEvent("content", content=init_content, message_id=last_message_id)
                        continue

                    p = obj.get("p", "")

                    # 3. 片段追加 (例如从 THINK 切换到 RESPONSE，或追加新的 RESPONSE 片段)
                    if p == "response/fragments" and isinstance(v_obj, list):
                        for frag in v_obj:
                            if isinstance(frag, dict):
                                fragment_type = (frag.get("type") or "RESPONSE").upper()
                                c = frag.get("content", "")
                                if c:
                                    if fragment_type == "THINK":
                                        _flush_notice()
                                        if thinking_enabled:
                                            yield ChatEvent("thinking", content=c, message_id=last_message_id)
                                    elif fragment_type in ("TIP", "WARNING", "NOTE"):
                                        _append_notice(fragment_type, c)
                                    else:
                                        _flush_notice()
                                        yield ChatEvent("content", content=c, message_id=last_message_id)
                        continue

                    # 片段类型更新 (如 response/fragments/-1/type)
                    if p.endswith("/type") and isinstance(v_obj, str):
                        fragment_type = v_obj.upper()
                        if fragment_type not in ("TIP", "WARNING", "NOTE"):
                            _flush_notice()
                        continue

                    # 4. Message ID 更新
                    if p == "response/message_id" and v_obj:
                        last_message_id = str(v_obj)
                        yield ChatEvent("message_id", message_id=last_message_id)
                        continue

                    # 5. 增量字符 Token
                    if isinstance(v_obj, str) and v_obj:
                        if "thinking" in p:
                            if thinking_enabled:
                                yield ChatEvent("thinking", content=v_obj, message_id=last_message_id)
                        elif "content" in p:
                            # 如 response/fragments/-1/content
                            if fragment_type == "THINK":
                                if thinking_enabled:
                                    yield ChatEvent("thinking", content=v_obj, message_id=last_message_id)
                            elif fragment_type in ("TIP", "WARNING", "NOTE"):
                                _append_notice(fragment_type, v_obj)
                            else:
                                yield ChatEvent("content", content=v_obj, message_id=last_message_id)
                        elif not p:
                            # 延续当前 fragment 状态的增量 token
                            if fragment_type == "THINK":
                                if thinking_enabled:
                                    yield ChatEvent("thinking", content=v_obj, message_id=last_message_id)
                            elif fragment_type in ("TIP", "WARNING", "NOTE"):
                                _append_notice(fragment_type, v_obj)
                            else:
                                yield ChatEvent("content", content=v_obj, message_id=last_message_id)

                except Exception:
                    continue

            _flush_notice()
            yield ChatEvent("done", message_id=last_message_id)
        finally:
            _flush_notice()
            try:
                resp.close()
            except Exception:
                pass

    @classmethod
    def fetch_chat_status(
        cls,
        token: str,
        cookie: str = "",
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """调用 DeepSeek Web 2.8.0 `GET /api/v0/chat/status`，查询账号 mute 与按模型细分的 quotas 状态。"""
        plat = cls.get_client_platform()
        headers = cls.build_headers(token, cookie, platform=plat, include_content_type=False)
        url = "https://chat.deepseek.com/api/v0/chat/status"
        impersonate = cls.get_impersonate(plat)

        try:
            r = cls._get_with_failover(
                url,
                headers=headers,
                impersonate=impersonate,
                timeout=15,
                proxy=proxy,
                context="查询账号状态",
            )
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        try:
            payload = r.json() if r.content else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return {"ok": False, "error": f"non_json_http_{r.status_code}"}

        code = payload.get("code")
        try:
            from app.engine.drop_probe import record_event
            d_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            record_event(
                "chat_status_probe",
                token=token,
                platform=plat,
                impersonate=impersonate,
                has_cookie=bool(cookie),
                proxy_primary=proxy,
                http_status=r.status_code,
                code=code,
                biz_code=d_obj.get("biz_code"),
                msg=str(d_obj.get("biz_msg") or payload.get("msg") or ""),
                error_kind="auth_expired" if code in _AUTH_FAIL_CODES else "",
            )
        except Exception:
            pass
        if code in _AUTH_FAIL_CODES:
            return {"ok": False, "auth_expired": True, "code": code, "error": str(payload.get("msg") or "")}

        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        biz_code = data_obj.get("biz_code")
        if code != 0 or biz_code != 0:
            return {
                "ok": False,
                "code": code,
                "biz_code": biz_code,
                "error": str(data_obj.get("biz_msg") or payload.get("msg") or f"http_{r.status_code}"),
            }

        biz_data = data_obj.get("biz_data") if isinstance(data_obj.get("biz_data"), dict) else {}
        mute_obj = biz_data.get("mute") if isinstance(biz_data.get("mute"), dict) else None
        quotas = biz_data.get("quotas") if isinstance(biz_data.get("quotas"), list) else []
        is_muted = (mute_obj is not None) or (biz_data.get("is_muted") in (1, True))
        mute_until = (
            _parse_mute_until(mute_obj.get("mute_until"))
            if mute_obj
            else _parse_mute_until(biz_data.get("mute_until"))
        )
        exhausted = [q for q in quotas if isinstance(q, dict) and q.get("is_exhausted")]
        quota_exhausted = len(exhausted) > 0
        reset_at = extract_quota_reset_until({"quotas": exhausted}) if exhausted else None

        return {
            "ok": True,
            "code": code,
            "is_muted": is_muted,
            "mute_until": mute_until,
            "quota_exhausted": quota_exhausted,
            "reset_at": reset_at,
            "quotas": quotas,
            "fetch_after_sec": r.headers.get("x-fetch-after-sec"),
        }


