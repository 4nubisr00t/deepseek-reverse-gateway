"""
Anthropic Claude 兼容路由 (/v1/messages)
供 Claude Code, ChatBox 等工具无缝接入，支持多模态识图
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

logger = logging.getLogger(__name__)

from app.core.security import verify_api_key, get_identity
from app.core.error_catalog import (
    CLIENT_MSG_CONTENT_POLICY,
    CLIENT_MSG_CONCURRENCY_LIMIT,
    CLIENT_MSG_DAILY_QUOTA,
    CLIENT_MSG_UPSTREAM_SESSION_FAILED,
    CLIENT_MSG_UPSTREAM_BAD_GATEWAY,
    CLIENT_MSG_MEDIA_PROCESSING_FAILED,
    CLIENT_MSG_SERVICE_UNAVAILABLE,
    raise_sanitized_sensitive_block,
)
from app.config import (
    config_manager,
    # ── T9 单模型收敛 ──
    UPSTREAM_MODEL_TYPE_VALUES,
    normalize_model_id,
    upstream_model_type,
)
from app.engine.account_pool import (
    account_pool,
    MAX_CONCURRENT_PER_ACCOUNT,
    get_effective_proxy,
    should_trigger_anti_cascade,
)
from app.engine.session_tree import session_tracker, extract_client_session_id, extract_explicit_parent_id
from app.engine.deepseek_client import DeepSeekClient, DeepSeekAuthError
from app.engine.metrics import metrics_engine
from app.engine import passive_metrics
from app.engine.key_usage import key_usage_engine
from app.core.tokenizer import TokenEstimator
from app.engine.vision_pipeline import (
    VisionPipeline,
    UnsupportedFileTypeError,
    DeepSeekMutedError,
    DeepSeekQuotaExceededError,
    DeepSeekUploadRateLimitError,
)
from app.engine.sensitive_filter import sensitive_filter, SafetyOutputGuard
from app.engine.tool_bridge import ToolBridge
from app.engine.disconnect_guard import disconnect_guard
# 复用 OpenAI 路由的上游首事件判定器：限流 / 上下文耗尽 / 真错误必须三路分开。
# 两条路由（OpenAI 与 Claude）对同一个上游错误必须给出**同一套语义**，
# 否则"换了个兼容接口就变成另一种故障表现"。它是纯数据 + 纯函数，无循环依赖
# （openai_routes 不 import claude_routes）。
from app.api.openai_routes import classify_upstream_preflight, apply_preflight_cooldown

router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])


@router.post("/messages")
async def claude_messages(request: Request, identity: Dict[str, Any] = Depends(get_identity)):
    req_start_time = time.time()

    # 客户端主动断连轰炸防御检查
    client_id = str(identity.get("key_id") or identity.get("name") or (request.client.host if request.client else "unknown"))
    cooldown_left = disconnect_guard.check_cooldown(client_id)
    if cooldown_left is not None:
        raise HTTPException(
            status_code=429,
            detail={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": f"短时间内连续主动断开连接，已实施防风控保护性冷却（剩余 {cooldown_left:.1f}s）。",
                }
            },
            headers={"Retry-After": str(int(cooldown_left) + 1)},
        )

    caller_info = {
        "key_id": identity.get("key_id", ""),
        "key_name": identity.get("name") or identity.get("log_label") or "unknown",
        "owner": identity.get("owner") or "public",
        "ip": request.client.host if request.client else "unknown",
        "user_id": identity.get("user_id"),
        "is_vip": bool(identity.get("is_vip")),
        "is_user_key": bool(identity.get("is_user_key")),
    }

    try:
        body = await request.json()
    except ClientDisconnect:
        print(f"[Claude-Routes] 客户端在读取请求体前已主动断开 (499): key={caller_info.get('key_name')} ip={caller_info.get('ip')}", flush=True)
        metrics_engine.record(
            endpoint="/v1/messages",
            model="unknown",
            status_code=499,
            duration_ms=(time.time() - req_start_time) * 1000,
            account=caller_info.get("owner", ""),
            error="client_disconnect_before_body",
            attribute_to_key=False,
        )
        raise HTTPException(status_code=499, detail="Client disconnected")
    messages: List[Dict[str, Any]] = body.get("messages", [])
    system_prompt = body.get("system", "")
    stream: bool = body.get("stream", False)
    tools = body.get("tools")

    # ── T9 单模型收敛：在任何决策之前把别名归一到主模型 ──
    #
    # 收敛前这里用 "opus"/"reasoner" 子串判定 model_type="expert"，
    # 且把客户端原始 `model` 拼进 conv_key。前者对上游无效（实测 sha1 相同），
    # 后者会让不同别名落在**不同的会话桶**里 → 别名之间可观测地不等效。
    # 归一后 `/v1/messages` 与 `/v1/chat/completions` 走同一条上游路径。
    _client_model: str = body.get("model", "") or ""
    model: str = normalize_model_id(_client_model)

    if not messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty")

    # ── 用户敏感词拦截（若该 API Key 未开启白名单豁免） ──
    if identity.get("sensitive_filter", True):
        prompt_text = sensitive_filter.extract_text_from_messages(messages)
        if system_prompt:
            prompt_text += " " + str(system_prompt)
        matched = sensitive_filter.check_content(prompt_text)
        if matched:
            key_name = identity.get("log_label") or identity.get("name") or "unknown"
            logger.warning(
                f"[SensitiveFilter] Key '{key_name}' (id={identity.get('key_id')}) "
                f"Claude 请求触发敏感词前置拦截: [{matched['category_name']}] '{matched['matched']}' (规则: {matched['rule_desc']})"
            )
            raise_sanitized_sensitive_block(
                matched_category=matched["category_name"],
                matched_keyword=matched["matched"],
                rule_desc=matched["rule_desc"],
                key_name=key_name,
                key_id=identity.get("key_id"),
            )

    # 1. 提取多模态图片与文本
    detected_images = []
    normalized_messages = []
    if system_prompt:
        normalized_messages.append({"role": "system", "content": system_prompt})

    for m in messages:
        c = m.get("content", "")
        p_text, imgs = await VisionPipeline.extract_images_and_text_async(c)
        if imgs:
            detected_images.extend(imgs)
        normalized_messages.append({"role": m.get("role", "user"), "content": p_text})

    # ── 上游 `model_type`：恒定值，与有没有图无关（2026-09-11 真图实测改写） ──
    #
    # 收敛前这里是：
    #   `"vision" if is_vision else ("expert" if ("opus"/"reasoner" in model) else "default")`
    # —— 那段子串判断**对上游完全无效**（实测三个值正文 sha1 相同），
    # 只是让 Claude 侧看起来也能"按模型选模式"。
    #
    # T9 把它换成按 `has_images` 推档位，但那仍建立在"识图是独立模式"这个
    # **当时没验证过的假设**上。实测已证伪：发 `vision` 上去，
    # **上游回显改写成 `expert`**；不切档位直接带图引用，模型照样看得见图。
    # （公开信息一致：快速/专家/识图三模式已合并成一个，V4.1 Flash 原生多模态。）
    #
    # ⛔ 仍必须发送该字段且取值合法：上游会校验合法性，非法值 422。
    #    见 app/config.py 的 `UPSTREAM_MODEL_TYPE_VALUES` 说明。
    is_vision = bool(detected_images)
    model_type = upstream_model_type()
    assert model_type in UPSTREAM_MODEL_TYPE_VALUES, (
        f"model_type={model_type!r} 不在上游白名单内，上游会返回 422"
    )

    # 2. 会话识别与粘性查找（T1）
    # allow_reuse 必须在这里算出来：不复用时 resolve 不能交出已有会话的 key，
    # 否则紧接着的 register_session 会覆盖它的绑定（解析与注册语义必须自洽）。
    _client_sid, sid_src = extract_client_session_id(request.headers, body)
    _has_prior = any(m.get("role") in ("assistant", "tool") for m in messages)
    conv_key, conv_src = session_tracker.resolve_conversation(
        normalized_messages, model=model, custom_id=_client_sid,
        allow_reuse=bool(_client_sid) or _has_prior,
        scope_key=identity.get("key_id") or "",
    )
    cached_session = session_tracker.get_session(conv_key)
    preferred_account = cached_session.account_label if cached_session else None
    avoid_account = None
    has_bound_files = bool(cached_session and cached_session.file_ids)

    # ★ 压缩即换号（Session Rolling）：前缀变化且无图片绑定时解除老账号粘性
    if (conv_src.startswith("cid-prefix-changed") or conv_src.startswith("cid-forked") or conv_src.startswith("prefix-forked")) and not has_bound_files:
        avoid_account = preferred_account
        preferred_account = None

    # 3. 调度账号（T2：按身份做归属隔离，别人名下的号不可用）
    # T25: 配额闸门在 acquire 之前，避免被限流的请求挤占别人的并发额度
    _quota_denied = key_usage_engine.check_quota(identity)
    if _quota_denied:
        _reason, _retry_after = _quota_denied
        key_usage_engine.record(
            key_id=identity.get("key_id", ""),
            label=identity.get("log_label", ""),
            rejected=True,
        )
        raise HTTPException(
            status_code=429,
            detail=CLIENT_MSG_DAILY_QUOTA,
            headers={"Retry-After": str(_retry_after), "X-Quota-Exceeded": "daily"},
        )

    account = await account_pool.acquire_for_identity(
        identity,
        preferred_label=preferred_account,
        timeout=30.0,
        immediate=True,
        avoid_label=avoid_account,
        refresh=False,
    )
    if not account:
        deny = account_pool.last_deny_reason
        if deny in ("no_account_assigned", "all_assigned_accounts_invalid", "no_active_account"):
            logger.error(f"[Audit-AccountPool] Claude API: Key '{identity.get('log_label')}' 无可用做功账号 ({deny})")
            raise HTTPException(
                status_code=503,
                detail=CLIENT_MSG_SERVICE_UNAVAILABLE,
            )
        if deny == "own_account_at_capacity":
            retry_after = int(max(2.0, min(15.0, account_pool.last_wait_hint or 6.0)))
            logger.warning(f"[Audit-Concurrency] Claude API: Key '{identity.get('log_label')}' 并发额度满载")
            raise HTTPException(
                status_code=429,
                detail=CLIENT_MSG_CONCURRENCY_LIMIT,
                headers={"Retry-After": str(retry_after)},
            )
        metrics_engine.record(
            endpoint="/v1/messages",
            model=model,
            status_code=503,
            duration_ms=(time.time() - req_start_time) * 1000,
            error="No available accounts",
            attribute_to_key=False,
        )
        logger.error(f"[Audit-AccountPool] Claude API: 系统所有可用账号均在忙碌或熔断")
        raise HTTPException(status_code=503, detail=CLIENT_MSG_SERVICE_UNAVAILABLE)

    # T16/B1 被动指标：账号已定、复用未决 —— 唯一能同时看到"绑定的号"与
    # "这次拿到的号"的时刻（`account_changed` 只有两边都有才成立）。纯观测。
    passive_metrics.note_session_resolution(
        conv_src, cached_session, account.account_label,
        bool(_client_sid) or _has_prior,
    )

    # 4. 上传图片（如有）
    # T5-A：改为**有界并发 + 统一超时预算**（旧串行循环下多图必然超看门狗）
    ref_file_ids = []
    if detected_images:
        upload_success = False
        last_upload_err = None
        for _img_attempt in range(3):
            try:
                ref_file_ids = await VisionPipeline.upload_images(
                    token=account.token,
                    images=detected_images,
                    cookie=account.cookie,
                    proxy=get_effective_proxy(account),
                )
                upload_success = True
                break
            except DeepSeekAuthError as auth_err:
                print(
                    f"[Claude-Routes] 账号 {account.account_label} 图片上传凭证失效: {auth_err}，尝试免密自愈或容灾换号 (attempt {_img_attempt + 1}/3)...",
                    flush=True,
                )
                if await account_pool.recover_or_release_on_auth_error(
                    account, caller_info=caller_info, stage="Claude图片上传"
                ):
                    continue
                prev_proxy = get_effective_proxy(account)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=prev_proxy,
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    last_upload_err = auth_err
            except UnsupportedFileTypeError as ufte:
                logger.warning(
                    f"[Claude-Routes] 账号 {account.account_label} 检测到客户端上传了不受支持的文件格式: {ufte}，安全释放账号且不熔断"
                )
                account_pool.release_account(account, is_muted=False)
                raise HTTPException(
                    status_code=400,
                    detail=f"文件/图片格式不受 DeepSeek 支持: {ufte}",
                )
            except DeepSeekMutedError as muted_err:
                print(
                    f"[Claude-Routes] 账号 {account.account_label} 图片上传被官方明确禁言: {muted_err} (解禁: {muted_err.mute_until})，进行账号隔离与换号容灾...",
                    flush=True,
                )
                last_upload_err = muted_err
                prev_proxy = get_effective_proxy(account)
                account_pool.release_account(account, is_muted=True, mute_until=muted_err.mute_until, caller_info=caller_info)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=prev_proxy,
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break
            except (DeepSeekQuotaExceededError, DeepSeekUploadRateLimitError) as quota_err:
                if isinstance(quota_err, DeepSeekQuotaExceededError):
                    account.apply_quota_cooldown(reset_at=quota_err.reset_at, reason="claude_vision_upload_quota")
                else:
                    account.apply_protective_cooldown(float(quota_err.retry_after or 30), reason="claude_vision_upload_rl")
                print(
                    f"[Claude-Routes] 账号 {account.account_label} 图片上传受限 ({quota_err})，已冷却并尝试换号容灾...",
                    flush=True,
                )
                last_upload_err = quota_err
                prev_proxy = get_effective_proxy(account)
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=prev_proxy,
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break
            except (TimeoutError, asyncio.TimeoutError) as te:
                print(
                    f"[Claude-Routes] 账号 {account.account_label} 图片上传/PoW 超时: {te}，安全释放账号（严禁误熔断为封号），尝试换号容灾 (attempt {_img_attempt + 1}/3)...",
                    flush=True,
                )
                last_upload_err = te
                prev_proxy = get_effective_proxy(account)
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=prev_proxy,
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break
            except Exception as e:
                print(
                    f"[Claude-Routes] 账号 {account.account_label} 图片上传异常 (attempt {_img_attempt + 1}/3): {e}，安全释放（不设为禁言）并换号容灾...",
                    flush=True,
                )
                last_upload_err = e
                prev_proxy = get_effective_proxy(account)
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=prev_proxy,
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break

        if not upload_success:
            print(f"[Claude-Routes] Image upload completely failed: {last_upload_err}", flush=True)
            account_pool.release_account(account)
            metrics_engine.record(
                endpoint="/v1/messages",
                model=model,
                status_code=502,
                duration_ms=(time.time() - req_start_time) * 1000,
                account=account.account_label,
                error=f"Image upload failed: {last_upload_err}",
            )
            logger.error(f"[Audit-Vision] Claude API 图片上传失败: {last_upload_err}")
            raise HTTPException(status_code=502, detail=CLIENT_MSG_MEDIA_PROCESSING_FAILED)

    # 5. 确定 Session
    deepseek_session_id = None
    parent_message_id = None
    _reused_upstream_session = False

    if (cached_session and cached_session.account_label == account.account_label
            and not is_vision
            # ★ A1：同 openai_routes —— 前缀变化时 `resolve_conversation` 会清空
            # `deepseek_session_id`（换会话但不换号），这里必须放行去新建会话
            and cached_session.deepseek_session_id):
        _reused_upstream_session = True
        deepseek_session_id = cached_session.deepseek_session_id
        parent_message_id = extract_explicit_parent_id(request.headers, body) or cached_session.last_message_id
        prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=True)
    else:
        loop = asyncio.get_event_loop()
        deepseek_session_id = None

        for _sess_attempt in range(3):
            _last_acc_label = account.account_label
            try:
                deepseek_session_id = await loop.run_in_executor(
                    None, DeepSeekClient.create_chat_session, account.token, account.cookie, get_effective_proxy(account)
                )
            except DeepSeekAuthError as auth_err:
                print(
                    f"[Claude-Routes] 账号 {account.account_label} 凭证被上游拒绝: {auth_err}，"
                    f"尝试通过受信任免密通道自动刷新 Token...",
                    flush=True,
                )
                relogin_ok = False
                if getattr(account, "password", None):
                    relogin_ok = await account.try_relogin()
                if relogin_ok:
                    print(
                        f"[Claude-Routes] ✅ 账号 {account.account_label} 免密自愈成功，"
                        f"使用新 Token 重新尝试创建会话...",
                        flush=True,
                    )
                    try:
                        deepseek_session_id = await loop.run_in_executor(
                            None, DeepSeekClient.create_chat_session, account.token, account.cookie, get_effective_proxy(account)
                        )
                    except Exception as e2:
                        print(f"[Claude-Routes] 重登后建会话再次失败: {e2}", flush=True)
                        deepseek_session_id = None

                if not deepseek_session_id:
                    from app.engine import trusted_relogin_engine as _tre
                    prev_proxy = get_effective_proxy(account)
                    err_info = getattr(account, "last_relogin_error", None) or {}
                    err_biz = err_info.get("biz_code")
                    err_str = str(err_info.get("error") or "")
                    if err_biz == 14 or "MFA_VERIFICATION_CODE_REQUIRED" in err_str:
                        print(
                            f"[Claude-Routes] ⚠️ 账号 {account.account_label} 触发官方邮件 MFA 校验(biz_code=14)，标记 MFA_REQUIRED 并容灾...",
                            flush=True,
                        )
                        account_pool.mark_mfa_required(account, caller_info=caller_info, stage="Claude-建会话")
                    elif _tre.is_ip_restricted() or err_biz in (11, 40029):
                        print(
                            f"[Claude-Routes] ⚠️ 官方 IP 访问受限(40029)：账号 {account.account_label} "
                            f"仅冷却 {_tre.ip_restrict_remaining()}s，不作失效判定，直接容灾换号...",
                            flush=True,
                        )
                        account_pool.release_account(account, is_muted=True, mute_until=int(time.time()) + 900, permanent=False, caller_info=caller_info, ban_kind_override="backoff")
                    else:
                        print(
                            f"[Claude-Routes] 账号 {account.account_label} 凭证失效且无法自愈，"
                            f"永久摘除并尝试容灾换号 (attempt {_sess_attempt + 1}/3)...",
                            flush=True,
                        )
                        account_pool.release_account(account, is_muted=True, permanent=True, caller_info=caller_info)
                    failover_acc = await account_pool.acquire_for_identity(
                        identity,
                        preferred_label=None,
                        timeout=10.0,
                        immediate=True,
                        avoid_label=account.account_label,
                        avoid_proxy=prev_proxy,
                        refresh=True,
                    )
                    if failover_acc:
                        account = failover_acc
                        if is_vision and detected_images:
                            try:
                                ref_file_ids = await VisionPipeline.upload_images(
                                    token=account.token,
                                    images=detected_images,
                                    cookie=account.cookie,
                                    proxy=get_effective_proxy(account),
                                )
                            except Exception as ex_img:
                                print(f"[Claude-Routes] 换号后图片重传失败: {ex_img}", flush=True)
                        continue
                    else:
                        metrics_engine.record(
                            endpoint="/v1/messages",
                            model=model,
                            status_code=502,
                            duration_ms=(time.time() - req_start_time) * 1000,
                            account=_last_acc_label,
                            error=f"Account auth expired: {auth_err}",
                            attribute_to_key=False,
                        )
                        raise HTTPException(
                            status_code=502,
                            detail="上游账号凭证已失效且无可用备用账号。管理员可在管理页查看与换号。",
                        )

            if not deepseek_session_id:
                deepseek_session_id = account.session_id

            if not deepseek_session_id:
                print(
                    f"[Claude-Routes] DeepSeek session creation failed for account {account.account_label}, "
                    f"尝试容灾换号 (attempt {_sess_attempt + 1}/3)...",
                    flush=True,
                )
                prev_proxy = get_effective_proxy(account)
                account_pool.release_account(account)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=prev_proxy,
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    if is_vision and detected_images:
                        try:
                            ref_file_ids = await VisionPipeline.upload_images(
                                token=account.token,
                                images=detected_images,
                                cookie=account.cookie,
                                proxy=get_effective_proxy(account),
                            )
                        except Exception as ex_img:
                            print(f"[Claude-Routes] 换号后图片重传失败: {ex_img}", flush=True)
                    continue
                else:
                    metrics_engine.record(
                        endpoint="/v1/messages",
                        model=model,
                        status_code=502,
                        duration_ms=(time.time() - req_start_time) * 1000,
                        account=_last_acc_label,
                        error="Session creation failed",
                    )
                    logger.error(f"[Audit-UpstreamSession] Claude API 会话创建失败且无可用备用账号")
                    raise HTTPException(status_code=502, detail=CLIENT_MSG_UPSTREAM_SESSION_FAILED)

            break

        prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False)
        session_tracker.register_session(
        conv_key, account.account_label, deepseek_session_id,
        # T1: 记下消息前缀指纹，供后续轮次唯一定位
        prefix=session_tracker._prefix_signatures(normalized_messages),
    )

    # A1 观测性：`/v1/messages` 此前**一条**会话来源日志都没有，于是
    # "客户端换了发送形态 → 复用静默失效"或"前缀变化 → 换会话不换号"在这条路径上
    # 完全不可观测（`resolve_conversation` 的返回值就地被丢掉）。
    # T16/B1：同时进被动计数器（复用率的分母）。
    passive_metrics.note_session_reuse(_reused_upstream_session, conv_src)
    print(
        f"[Claude-Latency] reuse_session={_reused_upstream_session} "
        f"conv_src={conv_src} account={account.account_label}",
        flush=True,
    )

    prompt_tokens = TokenEstimator.estimate_messages_tokens(normalized_messages, tools=tools)
    if _reused_upstream_session:
        delta_tokens = TokenEstimator.estimate_text_tokens(prompt)
        cached_tokens = max(0, prompt_tokens - delta_tokens)
        uncached_tokens = max(0, prompt_tokens - cached_tokens)
    else:
        cached_tokens = 0
        uncached_tokens = prompt_tokens

    msg_id = f"msg_{uuid.uuid4().hex[:16]}"

    # 6. 流式生成器 (Anthropic SSE 协议)
    async def anthropic_stream():
        is_muted_flag = False
        # T12: 上游给的解禁时间要带下去，否则账号池只能写 None，
        # 而 check_mute_expiry 要求 mute_until 非空 → 熔断后永不恢复
        mute_until_hint = None
        permanent_invalid = False
        status_code = 200
        err_msg = None
        full_text = []
        full_thinking = []
        final_mid = None
        safety_tripped_reason: Optional[str] = None
        safety_guard = SafetyOutputGuard(
            safety_repeat_limit=int(getattr(config_manager.config, "ds_safety_repeat_limit", 3) or 3)
        )
        try:
            # message_start
            start_payload = {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": uncached_tokens,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": cached_tokens,
                    },
                },
            }
            yield f"event: message_start\ndata: {json.dumps(start_payload)}\n\n"

            # content_block_start
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

            async for event in DeepSeekClient.stream_chat(
                token=account.token,
                session_id=deepseek_session_id,
                prompt=prompt,
                parent_message_id=parent_message_id,
                model_type=model_type,
                ref_file_ids=ref_file_ids,
                cookie=account.cookie,
                proxy=get_effective_proxy(account),
            ):
                if await request.is_disconnected():
                    print(f"[Claude-Stream] 客户端 {client_id} 已主动断开连接，中止流传输并回收资源", flush=True)
                    disconnect_guard.record_disconnect(client_id)
                    break

                if event.event_type == "error":
                    _decision = classify_upstream_preflight(event, None, 0.0)
                    if event.error_code == "biz:5":
                        is_muted_flag = True
                        mute_until_hint = getattr(event, "mute_until", None)
                    elif event.error_code in ("account_banned", "USER_IS_BANNED"):
                        permanent_invalid = True
                    elif event.error_code in ("auth_expired", "40003", "40001", "40002"):
                        permanent_invalid = True
                    elif _decision.kind == "upstream_busy":
                        is_muted_flag = False
                        permanent_invalid = False
                        account.apply_protective_cooldown(3.0, reason="claude_upstream_busy_jitter")

                    # ★ 严格错误分级回显：
                    #   · 上下文耗尽 → 终态，清掉会话绑定让下一轮重建，返回 400 语义
                    #   · 限流       → 退避重试，返回 429 语义
                    #   · 上游繁忙   → 零惩罚，返回 503 语义
                    #   · 其它       → 真错误，502 语义
                    passive_metrics.note_preflight(_decision)
                    if _decision.kind in ("context_exhausted", "session_broken"):
                        try:
                            session_tracker.delete_session(conv_key)
                        except Exception as ex:
                            print(f"[Gateway-{_decision.kind}] 清除会话绑定失败: {ex}", flush=True)
                        print(
                            f"[Gateway-{_decision.kind}] {_decision.detail} "
                            f"sid={deepseek_session_id} → 已清除会话绑定（/v1/messages 流式）",
                            flush=True,
                        )
                        status_code = 400 if _decision.kind == "context_exhausted" else 429
                        err_msg = _decision.metric_error
                        message = (
                            f"{_decision.detail}"
                            f"（网关已清除本会话的绑定，客户端原样重发即可，会开一个新会话）"
                        )
                    elif _decision.kind == "rate_limited":
                        apply_preflight_cooldown(
                            account,
                            _decision,
                            getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0),
                            reason="claude_stream_rate_limited",
                        )
                        status_code = 429
                        err_msg = _decision.metric_error
                        message = _decision.detail
                    elif _decision.kind == "upstream_busy":
                        status_code = 503
                        err_msg = _decision.metric_error
                        message = _decision.detail or "上游官方服务繁忙/暂时不可用，请稍后重试"
                    else:
                        status_code = 502
                        err_msg = f"{event.content} ({event.error_code})"
                        message = event.content
                    err_payload = {"type": "error", "error": {"type": "api_error", "message": message}}
                    yield f"event: error\ndata: {json.dumps(err_payload)}\n\n"
                    return

                if event.event_type == "message_id" and event.message_id:
                    final_mid = event.message_id

                if event.event_type == "thinking" and event.content:
                    if safety_guard.feed(event.content):
                        safety_tripped_reason = safety_guard.trip_reason
                        print(f"[Safety-CircuitBreaker] 🛡️ Claude流触发安全复读熔断 ({safety_tripped_reason}): account={account.account_label}", flush=True)
                        try:
                            session_tracker.reset_upstream_session(conv_key, reason="ds_safety_loop")
                        except Exception:
                            pass
                        break
                    full_thinking.append(event.content)
                elif event.event_type == "content" and event.content:
                    if safety_guard.feed(event.content):
                        safety_tripped_reason = safety_guard.trip_reason
                        print(f"[Safety-CircuitBreaker] 🛡️ Claude流触发安全复读熔断 ({safety_tripped_reason}): account={account.account_label}", flush=True)
                        try:
                            session_tracker.reset_upstream_session(conv_key, reason="ds_safety_loop")
                        except Exception:
                            pass
                        break
                    delta_payload = {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": event.content},
                    }
                    yield f"event: content_block_delta\ndata: {json.dumps(delta_payload)}\n\n"

                elif event.event_type == "done":
                    if event.message_id:
                        final_mid = event.message_id
                    if final_mid and full_text:
                        session_tracker.update_message_id(conv_key, final_mid)
                    break
            if not full_text and full_thinking and not safety_tripped_reason:
                think_str = "".join(full_thinking)
                salvaged_text = ToolBridge.salvage_content_from_thinking(think_str)
                if salvaged_text:
                    full_text = [salvaged_text]
                    session_tracker.reset_upstream_session(conv_key, reason="claude_cot_salvage")
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': salvaged_text}})}\n\n"
                else:
                    empty_notice = (
                        f"\n\n【网关提示】：上游本轮只输出了思考过程、没有给出正文或工具调用（思考 {len(think_str)} 字符）。"
                        "本轮**没有产出**，会话上下文已保留，请重试或换个说法继续。"
                    )
                    full_text = [empty_notice]
                    session_tracker.reset_upstream_session(conv_key, reason="claude_thinking_only")
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': empty_notice}})}\n\n"


            # content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

            ans_str = "".join(full_text)
            think_str = "".join(full_thinking)
            content_toks = TokenEstimator.estimate_text_tokens(ans_str)
            reasoning_toks = TokenEstimator.estimate_text_tokens(think_str)
            completion_tokens = content_toks + reasoning_toks

            # message_delta
            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': completion_tokens}})}\n\n"

            # message_stop
            yield "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"

        finally:
            if safety_tripped_reason:
                account_pool.release_with_safety_cooldown(
                    account,
                    reason=safety_tripped_reason,
                    caller_info=caller_info,
                )
            else:
                account_pool.release_account(
                    account, is_muted=is_muted_flag, mute_until=mute_until_hint,
                    permanent=permanent_invalid,
                    caller_info=caller_info,
                )
            ans_str = "".join(full_text)
            think_str = "".join(full_thinking)
            completion_tokens = TokenEstimator.estimate_text_tokens(ans_str) + TokenEstimator.estimate_text_tokens(think_str)
            metrics_engine.record(
                endpoint="/v1/messages",
                model=model,
                status_code=status_code,
                duration_ms=(time.time() - req_start_time) * 1000,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                account=account.account_label,
                error=err_msg,
            )

    if stream:
        return StreamingResponse(
            anthropic_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式
    full_text = []
    full_thinking = []
    is_muted = False
    mute_until_nonstream = None
    permanent_invalid_nonstream = False
    status_code = 200
    err_msg = None
    try:
        async for event in DeepSeekClient.stream_chat(
            token=account.token,
            session_id=deepseek_session_id,
            prompt=prompt,
            parent_message_id=parent_message_id,
            model_type=model_type,
            ref_file_ids=ref_file_ids,
            cookie=account.cookie,
            proxy=get_effective_proxy(account),
        ):
            if event.event_type == "error":
                if event.error_code == "biz:5":
                    is_muted = True
                    mute_until_nonstream = getattr(event, "mute_until", None)
                elif event.error_code == "auth_expired":
                    permanent_invalid_nonstream = True

                # ★ 与流式路径、以及 OpenAI 路由同序：上下文耗尽 **先于** 限流判定
                _decision = classify_upstream_preflight(event, None, 0.0)
                passive_metrics.note_preflight(_decision)
                if _decision.kind in ("context_exhausted", "session_broken"):
                    try:
                        session_tracker.delete_session(conv_key)
                    except Exception as ex:
                        print(f"[Gateway-{_decision.kind}] 清除会话绑定失败: {ex}", flush=True)
                    print(
                        f"[Gateway-{_decision.kind}] {_decision.detail} "
                        f"sid={deepseek_session_id} → 已清除会话绑定（/v1/messages 非流式）",
                        flush=True,
                    )
                    status_code = 400 if _decision.kind == "context_exhausted" else 429
                    err_msg = _decision.metric_error
                    raise HTTPException(
                        status_code=status_code,
                        detail=f"{_decision.detail}（网关已清除本会话的绑定，原样重发即可，会开一个新会话）",
                        headers={"Retry-After": str(_decision.retry_after or 1)} if _decision.retry_after else None,
                    )
                if _decision.kind == "rate_limited":
                    apply_preflight_cooldown(
                        account,
                        _decision,
                        getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0),
                        reason="claude_nonstream_rate_limited",
                    )
                    status_code = 429
                    err_msg = _decision.metric_error
                    retry_after = int(_decision.retry_after or 3)
                    print(
                        f"[Gateway-NonStream-RateLimit] (/v1/messages) 上游限流 → 429 "
                        f"Retry-After={retry_after}: {event.content}",
                        flush=True,
                    )
                    raise HTTPException(
                        status_code=429,
                        detail=f"上游限流: {_decision.detail}",
                        headers={"Retry-After": str(retry_after)},
                    )
                status_code = 502
                err_msg = event.content
                raise HTTPException(status_code=502, detail=event.content)
            elif event.event_type == "thinking":
                full_thinking.append(event.content)
            elif event.event_type == "content":
                full_text.append(event.content)
            elif event.event_type == "message_id" and event.message_id:
                final_mid = event.message_id
            elif event.event_type == "done":
                if event.message_id:
                    final_mid = event.message_id
                if final_mid and full_text:
                    session_tracker.update_message_id(conv_key, final_mid)
                break

        if not full_text and full_thinking:
            think_str = "".join(full_thinking)
            salvaged_text = ToolBridge.salvage_content_from_thinking(think_str)
            if salvaged_text:
                full_text = [salvaged_text]
                session_tracker.reset_upstream_session(conv_key, reason="claude_nonstream_cot_salvage")
            else:
                empty_notice = (
                    f"\n\n【网关提示】：上游本轮只输出了思考过程、没有给出正文或工具调用（思考 {len(think_str)} 字符）。"
                    "本轮**没有产出**，会话上下文已保留，请重试或换个说法继续。"
                )
                full_text = [empty_notice]
                session_tracker.reset_upstream_session(conv_key, reason="claude_nonstream_thinking_only")
    finally:
        account_pool.release_account(
            account, is_muted=is_muted, mute_until=mute_until_nonstream,
            permanent=permanent_invalid_nonstream,
            caller_info=caller_info,
        )
        answer = "".join(full_text)
        think_str = "".join(full_thinking)
        content_toks = TokenEstimator.estimate_text_tokens(answer)
        reasoning_toks = TokenEstimator.estimate_text_tokens(think_str)
        completion_tokens = content_toks + reasoning_toks
        metrics_engine.record(
            endpoint="/v1/messages",
            model=model,
            status_code=status_code,
            duration_ms=(time.time() - req_start_time) * 1000,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            account=account.account_label,
            error=err_msg,
        )

    answer = "".join(full_text)
    think_str = "".join(full_thinking)
    completion_tokens = TokenEstimator.estimate_text_tokens(answer) + TokenEstimator.estimate_text_tokens(think_str)
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": answer}],
        "model": model,
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": uncached_tokens,
            "output_tokens": completion_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached_tokens,
        },
    }
