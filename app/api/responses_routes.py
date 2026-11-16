"""
OpenAI Responses API 兼容路由 (/v1/responses)
专为接入支持 Responses API 的新版客户端（如 Codex CLI, CC-Switch, OpenCode, Hermes Agent 等）设计。
支持非流式返回与标准语义事件流式 SSE，无缝集成多账号调度、会话粘性、多模态及思考链。
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.security import verify_api_key, get_identity
from app.core.request_context import set_current_identity
from app.config import (
    config_manager,
    UPSTREAM_MODEL_TYPE_VALUES,
    normalize_model_id,
    upstream_model_type,
)
from app.engine.account_pool import (
    account_pool,
    MAX_CONCURRENT_PER_ACCOUNT,
    get_effective_proxy,
)
from app.engine.session_tree import session_tracker, extract_client_session_id, extract_explicit_parent_id
from app.engine.reasoning_director import ReasoningDirector
from app.core.error_catalog import (
    CLIENT_MSG_CONTENT_POLICY,
    CLIENT_MSG_CONCURRENCY_LIMIT,
    CLIENT_MSG_DAILY_QUOTA,
    CLIENT_MSG_UPSTREAM_SESSION_FAILED,
    CLIENT_MSG_MEDIA_PROCESSING_FAILED,
    CLIENT_MSG_SERVICE_UNAVAILABLE,
    raise_sanitized_sensitive_block,
)
from app.engine.deepseek_client import DeepSeekClient, DeepSeekAuthError
from app.engine.metrics import metrics_engine
from app.engine import passive_metrics
from app.engine.key_usage import key_usage_engine
from app.core.tokenizer import TokenEstimator
from app.engine.vision_pipeline import (
    VisionPipeline,
    DeepSeekMutedError,
    DeepSeekQuotaExceededError,
    DeepSeekUploadRateLimitError,
)
from app.engine.sensitive_filter import sensitive_filter
from app.api.openai_routes import classify_upstream_preflight, apply_preflight_cooldown
from app.engine.disconnect_guard import disconnect_guard
from app.engine.tool_bridge import ToolBridge
from app.engine.streaming_parser import StreamingToolParser

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_api_key)])


def parse_responses_input(
    input_data: Any,
    instructions: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """将 Responses API 的 input 与 instructions 规整为内部统一的 messages 列表。

    Responses API 规范支持：
    1. input 为纯字符串：`"Write a fibonacci function in Python"`
    2. input 为条目列表：包含 `role` 与 `content`，或带 `type: "message"`、`type: "function_call_output"`
    3. instructions 顶层字段：作为系统级提示词注入首位（developer/system 角色）
    """
    messages: List[Dict[str, Any]] = []

    # 1. 注入 top-level instructions (相当于系统指令)
    if instructions and isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})

    # 2. 解析 input 字段
    if isinstance(input_data, str):
        if input_data.strip():
            messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                item_type = item.get("type")

                # 处理函数/工具调用输出
                if item_type == "function_call_output":
                    call_id = item.get("call_id") or item.get("tool_call_id") or "call_0"
                    output = item.get("output", "")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(call_id),
                        "content": str(output),
                    })
                    continue

                role = item.get("role") or "user"
                if role == "developer":
                    role = "system"

                raw_content = item.get("content")
                if raw_content is None and "text" in item:
                    raw_content = item.get("text")

                if isinstance(raw_content, list):
                    text_parts = []
                    has_image = False
                    for part in raw_content:
                        if isinstance(part, str):
                            text_parts.append(part)
                        elif isinstance(part, dict):
                            p_type = part.get("type", "")
                            if p_type in ("text", "input_text"):
                                text_parts.append(part.get("text", ""))
                            elif p_type in ("image_url", "input_image"):
                                has_image = True
                    if not has_image:
                        content = "".join(text_parts)
                    else:
                        content = raw_content
                else:
                    content = str(raw_content) if raw_content is not None else ""

                messages.append({"role": role, "content": content})

    return messages


@router.post("/v1/responses")
@router.post("/responses")
async def responses_create(request: Request, identity: Dict[str, Any] = Depends(get_identity)):
    """OpenAI Responses API 核心端点 (POST /v1/responses)"""
    req_start_time = time.time()
    set_current_identity(identity)

    # 客户端主动断连轰炸防御检查
    client_id = str(identity.get("key_id") or identity.get("name") or (request.client.host if request.client else "unknown"))
    cooldown_left = disconnect_guard.check_cooldown(client_id)
    if cooldown_left is not None:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"短时间内连续主动断开连接，已实施防风控保护性冷却（剩余 {cooldown_left:.1f}s）。",
                    "type": "client_disconnect_flood",
                    "code": "client_disconnect_cooldown",
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

    body = await request.json()

    raw_model: str = body.get("model", "") or ""
    model: str = normalize_model_id(raw_model)

    input_data = body.get("input")
    messages_data = body.get("messages")
    instructions = body.get("instructions") or body.get("system")
    stream: bool = body.get("stream", False)
    tools = body.get("tools")
    conversation_id = body.get("conversation_id")

    # 1. 消息解析归一化
    if input_data is not None:
        normalized_messages = parse_responses_input(input_data, instructions)
    elif messages_data:
        normalized_messages = []
        if instructions and isinstance(instructions, str) and instructions.strip():
            normalized_messages.append({"role": "system", "content": instructions.strip()})
        normalized_messages.extend(messages_data)
    else:
        if instructions and isinstance(instructions, str) and instructions.strip():
            normalized_messages = [{"role": "system", "content": instructions.strip()}]
        else:
            raise HTTPException(status_code=400, detail="input or messages cannot be empty")

    if not normalized_messages:
        raise HTTPException(status_code=400, detail="parsed input produced empty messages")

    # 2. 敏感词前置合规过滤
    if identity.get("sensitive_filter", True):
        prompt_text = sensitive_filter.extract_text_from_messages(normalized_messages)
        matched = sensitive_filter.check_content(prompt_text)
        if matched:
            key_name = identity.get("log_label") or identity.get("name") or "unknown"
            logger.warning(
                f"[SensitiveFilter] Key '{key_name}' (id={identity.get('key_id')}) "
                f"Responses 请求触发敏感词拦截: [{matched['category_name']}] '{matched['matched']}'"
            )
            raise_sanitized_sensitive_block(
                matched_category=matched["category_name"],
                matched_keyword=matched["matched"],
                rule_desc=matched["rule_desc"],
                key_name=identity.get("log_label") or "unknown",
                key_id=identity.get("key_id"),
            )

    # 3. 提取多模态图片
    detected_images = []
    clean_messages = []
    for m in normalized_messages:
        c = m.get("content", "")
        p_text, imgs = await VisionPipeline.extract_images_and_text_async(c)
        if imgs:
            detected_images.extend(imgs)
        clean_messages.append({"role": m.get("role", "user"), "content": p_text})

    # 3.1 若请求包含 tools，将 tools 提示词强注入 System Prompt（同步 OpenAI 路由契约）
    if tools:
        tools_prompt = ToolBridge.format_tools_system_prompt(tools)
        has_system = False
        for m in clean_messages:
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + "\n" + tools_prompt
                has_system = True
                break
        if not has_system:
            clean_messages.insert(0, {"role": "system", "content": tools_prompt})

    raw_effort = body.get("reasoning_effort")
    if raw_effort is None and isinstance(body.get("reasoning"), dict):
        raw_effort = body.get("reasoning", {}).get("effort")
    effort = ReasoningDirector.normalize_effort(raw_effort, client_model=body.get("model"))
    thinking_enabled = not ReasoningDirector.is_thinking_disabled(effort)

    is_vision = bool(detected_images)
    model_type = upstream_model_type()
    assert model_type in UPSTREAM_MODEL_TYPE_VALUES, f"model_type={model_type!r} 不在白名单内"

    # 4. 会话识别与粘性查找（支持 previous_response_id 树状分叉与消息前缀树 LCA 回溯）
    previous_response_id = body.get("previous_response_id")
    _explicit_parent_mid = extract_explicit_parent_id(request.headers, body)
    _client_sid, sid_src = extract_client_session_id(request.headers, body)
    if conversation_id:
        _client_sid = str(conversation_id)
        sid_src = "conversation_id"

    _resp_branch_hit = (
        session_tracker.resolve_by_response_id(str(previous_response_id))
        if previous_response_id
        else None
    )
    _has_prior = any(m.get("role") in ("assistant", "tool") for m in clean_messages) or bool(previous_response_id)
    if _resp_branch_hit is not None:
        conv_key, _target_mid = _resp_branch_hit
        conv_src = "resp-id-branched"
        session_tracker.register_prefix(conv_key, clean_messages)
    else:
        conv_key, conv_src = session_tracker.resolve_conversation(
            clean_messages,
            model=model,
            custom_id=_client_sid,
            allow_reuse=bool(_client_sid) or _has_prior,
        )
    cached_session = session_tracker.get_session(conv_key)
    preferred_account = cached_session.account_label if cached_session else None
    avoid_account = None
    has_bound_files = bool(cached_session and cached_session.file_ids)

    # 压缩即换号（Session Rolling）
    if (conv_src.startswith("cid-prefix-changed") or conv_src.startswith("cid-forked") or conv_src.startswith("prefix-forked")) and not has_bound_files:
        avoid_account = preferred_account
        preferred_account = None

    # 5. 配额检查与调度账号
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

    account = await account_pool.acquire_account(
        preferred_label=preferred_account,
        timeout=30.0,
        immediate=True,
        owned_labels=identity.get("account_labels") or [],
        pool_fallback=bool(identity.get("pool_fallback")),
        avoid_label=avoid_account,
    )
    if not account:
        deny = account_pool.last_deny_reason
        if deny == "no_account_assigned":
            logger.error(f"[Audit-AccountPool] Responses API: Key '{identity.get('log_label')}' 未分配账号")
            raise HTTPException(
                status_code=503,
                detail=CLIENT_MSG_SERVICE_UNAVAILABLE,
            )
        if deny == "own_account_at_capacity":
            retry_after = int(max(2.0, min(15.0, account_pool.last_wait_hint or 6.0)))
            logger.warning(f"[Audit-Concurrency] Responses API: Key '{identity.get('log_label')}' 并发额度满载")
            raise HTTPException(
                status_code=429,
                detail=CLIENT_MSG_CONCURRENCY_LIMIT,
                headers={"Retry-After": str(retry_after)},
            )
        metrics_engine.record(
            endpoint="/v1/responses",
            model=model,
            status_code=503,
            duration_ms=(time.time() - req_start_time) * 1000,
            error="No available accounts",
            attribute_to_key=False,
        )
        logger.error(f"[Audit-AccountPool] Responses API: 系统所有可用账号均在忙碌或熔断")
        raise HTTPException(status_code=503, detail=CLIENT_MSG_SERVICE_UNAVAILABLE)

    passive_metrics.note_session_resolution(
        conv_src, cached_session, account.account_label,
        bool(_client_sid) or _has_prior,
    )

    # 6. 上传图片（如有）
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
                if await account_pool.recover_or_release_on_auth_error(
                    account, caller_info=caller_info, stage="Responses图片上传"
                ):
                    continue
                failover_acc = await account_pool.acquire_account(
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    owned_labels=identity.get("account_labels") or [],
                    pool_fallback=bool(identity.get("pool_fallback")),
                )
                if failover_acc:
                    account = failover_acc
                    continue
                break
            except DeepSeekMutedError as muted_err:
                last_upload_err = muted_err
                account_pool.release_account(account, is_muted=True, mute_until=muted_err.mute_until, caller_info=caller_info)
                failover_acc = await account_pool.acquire_account(
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    owned_labels=identity.get("account_labels") or [],
                    pool_fallback=bool(identity.get("pool_fallback")),
                    avoid_label=account.account_label,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                break
            except (DeepSeekQuotaExceededError, DeepSeekUploadRateLimitError) as quota_err:
                if isinstance(quota_err, DeepSeekQuotaExceededError):
                    account.apply_quota_cooldown(reset_at=quota_err.reset_at, reason="responses_vision_upload_quota")
                else:
                    account.apply_protective_cooldown(float(quota_err.retry_after or 30), reason="responses_vision_upload_rl")
                last_upload_err = quota_err
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_account(
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    owned_labels=identity.get("account_labels") or [],
                    pool_fallback=bool(identity.get("pool_fallback")),
                    avoid_label=account.account_label,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                break
            except Exception as e:
                last_upload_err = e
                await asyncio.sleep(0.5)

        if not upload_success:
            account_pool.release_account(account)
            logger.error(f"[Audit-Vision] Responses API: 图片上传官方服务失败: {last_upload_err}")
            raise HTTPException(status_code=502, detail=CLIENT_MSG_MEDIA_PROCESSING_FAILED)

    # 7. 会话复用与建立
    deepseek_session_id = None
    parent_message_id = None
    _reused_upstream_session = False
    _is_same_account = (cached_session and cached_session.account_label == account.account_label)

    if _is_same_account and cached_session and cached_session.deepseek_session_id:
        deepseek_session_id = cached_session.deepseek_session_id
        parent_message_id = _explicit_parent_mid or cached_session.last_message_id
        prompt, _ = session_tracker.prepare_prompt(clean_messages, has_existing_session=True, reasoning_effort=effort)
        _reused_upstream_session = True
    else:
        for _attempt in range(3):
            _last_acc_label = account.account_label
            try:
                deepseek_session_id = await asyncio.to_thread(
                    DeepSeekClient.create_chat_session,
                    account.token,
                    account.cookie,
                    get_effective_proxy(account),
                )
            except DeepSeekAuthError as auth_err:
                if await account_pool.recover_or_release_on_auth_error(
                    account, caller_info=caller_info, stage="Responses建会话"
                ):
                    deepseek_session_id = None
                    continue
                if not deepseek_session_id:
                    failover_acc = await account_pool.acquire_account(
                        preferred_label=None,
                        timeout=10.0,
                        immediate=True,
                        owned_labels=identity.get("account_labels") or [],
                        pool_fallback=bool(identity.get("pool_fallback")),
                    )
                    if failover_acc:
                        account = failover_acc
                        continue
                    logger.error(f"[Audit-UpstreamSession] Responses API: 会话创建失败且无可用备用账号")
                    raise HTTPException(status_code=502, detail=CLIENT_MSG_UPSTREAM_SESSION_FAILED)

            if not deepseek_session_id:
                account_pool.release_account(account, is_muted=True, permanent=False)
                failover_acc = await account_pool.acquire_account(
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    owned_labels=identity.get("account_labels") or [],
                    pool_fallback=bool(identity.get("pool_fallback")),
                )
                if failover_acc:
                    account = failover_acc
                    continue
                logger.error(f"[Audit-UpstreamSession] Responses API: 会话创建失败且无可用备用账号")
                raise HTTPException(status_code=502, detail=CLIENT_MSG_UPSTREAM_SESSION_FAILED)
            break

        prompt, _ = session_tracker.prepare_prompt(clean_messages, has_existing_session=False, reasoning_effort=effort)
        session_tracker.register_session(
            conv_key,
            account.account_label,
            deepseek_session_id,
            prefix=session_tracker._prefix_signatures(clean_messages),
        )

    passive_metrics.note_session_reuse(_reused_upstream_session, conv_src)
    prompt_tokens = TokenEstimator.estimate_messages_tokens(clean_messages, tools=tools)
    if _reused_upstream_session:
        delta_tokens = TokenEstimator.estimate_text_tokens(prompt)
        cached_tokens = max(0, prompt_tokens - delta_tokens)
    else:
        cached_tokens = 0

    response_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"item_{uuid.uuid4().hex}"
    created_at = int(time.time())

    # 8. 流式处理 (Responses API SSE 规范，100% 对齐 OpenAI 工具状态机与防泄漏)
    if stream:
        async def responses_stream():
            is_muted_flag = False
            mute_until_hint = None
            permanent_invalid = False
            status_code = 200
            err_msg = None
            full_text = []
            full_thinking = []
            final_mid = None
            captured_tool_calls = []
            streaming_parser = StreamingToolParser(tools) if tools else None
            text_item_started = False

            try:
                # 8.1 初始生命周期事件
                created_evt = {
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created_at,
                        "status": "in_progress",
                        "model": model,
                        "output": [],
                    },
                }
                yield f"event: response.created\ndata: {json.dumps(created_evt, ensure_ascii=False)}\n\n"

                # 8.2 逐帧消费 DeepSeek 原生 SSE 流
                async for event in DeepSeekClient.stream_chat(
                    token=account.token,
                    session_id=deepseek_session_id,
                    prompt=prompt,
                    parent_message_id=parent_message_id,
                    thinking_enabled=thinking_enabled,
                    model_type=model_type,
                    ref_file_ids=ref_file_ids,
                    cookie=account.cookie,
                    proxy=get_effective_proxy(account),
                ):
                    if await request.is_disconnected():
                        print(f"[Responses-Stream] 客户端 {client_id} 已主动断开连接，中止流传输并回收资源", flush=True)
                        disconnect_guard.record_disconnect(client_id)
                        break

                    if event.event_type == "error":
                        if event.error_code == "biz:5":
                            is_muted_flag = True
                            mute_until_hint = getattr(event, "mute_until", None)
                        elif event.error_code == "auth_expired":
                            permanent_invalid = True

                        decision = classify_upstream_preflight(event, None, 0.0)
                        passive_metrics.note_preflight(decision)
                        if decision.kind == "rate_limited":
                            apply_preflight_cooldown(
                                account,
                                decision,
                                getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0),
                                reason="responses_stream_rate_limited",
                            )
                        if decision.kind in ("context_exhausted", "session_broken"):
                            try:
                                session_tracker.delete_session(conv_key)
                            except Exception:
                                pass
                        status_code = (
                            400 if decision.kind == "context_exhausted"
                            else 429 if decision.kind in ("rate_limited", "session_broken")
                            else 502
                        )
                        err_code = decision.metric_error or decision.kind or "upstream_error"
                        err_msg = event.content or decision.detail or err_code

                        failed_evt = {
                            "type": "response.failed",
                            "response_id": response_id,
                            "error": {
                                "message": err_msg,
                                "code": err_code,
                            },
                        }
                        yield f"event: response.failed\ndata: {json.dumps(failed_evt, ensure_ascii=False)}\n\n"
                        return

                    if event.event_type == "thinking" and event.content:
                        full_thinking.append(event.content)
                        reasoning_delta_evt = {
                            "type": "response.reasoning_text.delta",
                            "response_id": response_id,
                            "item_id": item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "delta": event.content,
                        }
                        yield f"event: response.reasoning_text.delta\ndata: {json.dumps(reasoning_delta_evt, ensure_ascii=False)}\n\n"

                    elif event.event_type == "content" and event.content:
                        if streaming_parser:
                            for ev_type, data in streaming_parser.feed(event.content):
                                if ev_type == "text" and data:
                                    if not text_item_started:
                                        text_item_started = True
                                        item_added_evt = {
                                            "type": "response.output_item.added",
                                            "response_id": response_id,
                                            "output_index": 0,
                                            "item": {
                                                "id": item_id,
                                                "type": "message",
                                                "status": "in_progress",
                                                "role": "assistant",
                                                "content": [],
                                            },
                                        }
                                        yield f"event: response.output_item.added\ndata: {json.dumps(item_added_evt, ensure_ascii=False)}\n\n"
                                        part_added_evt = {
                                            "type": "response.content_part.added",
                                            "response_id": response_id,
                                            "item_id": item_id,
                                            "output_index": 0,
                                            "content_index": 0,
                                            "part": {"type": "text", "text": ""},
                                        }
                                        yield f"event: response.content_part.added\ndata: {json.dumps(part_added_evt, ensure_ascii=False)}\n\n"

                                    full_text.append(data)
                                    text_delta_evt = {
                                        "type": "response.output_text.delta",
                                        "response_id": response_id,
                                        "item_id": item_id,
                                        "output_index": 0,
                                        "content_index": 0,
                                        "delta": data,
                                    }
                                    yield f"event: response.output_text.delta\ndata: {json.dumps(text_delta_evt, ensure_ascii=False)}\n\n"
                                elif ev_type == "tool_call":
                                    captured_tool_calls.append(data)
                        else:
                            if not text_item_started:
                                text_item_started = True
                                item_added_evt = {
                                    "type": "response.output_item.added",
                                    "response_id": response_id,
                                    "output_index": 0,
                                    "item": {
                                        "id": item_id,
                                        "type": "message",
                                        "status": "in_progress",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                }
                                yield f"event: response.output_item.added\ndata: {json.dumps(item_added_evt, ensure_ascii=False)}\n\n"
                                part_added_evt = {
                                    "type": "response.content_part.added",
                                    "response_id": response_id,
                                    "item_id": item_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "text", "text": ""},
                                }
                                yield f"event: response.content_part.added\ndata: {json.dumps(part_added_evt, ensure_ascii=False)}\n\n"

                            full_text.append(event.content)
                            text_delta_evt = {
                                "type": "response.output_text.delta",
                                "response_id": response_id,
                                "item_id": item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": event.content,
                            }
                            yield f"event: response.output_text.delta\ndata: {json.dumps(text_delta_evt, ensure_ascii=False)}\n\n"

                    elif event.event_type == "message_id" and event.message_id:
                        final_mid = event.message_id

                # 8.3 刷新流式状态机并做安全网兜底与思考链打捞
                if streaming_parser:
                    flush_events = streaming_parser.flush()
                    for ev_type, data in flush_events:
                        if ev_type == "text" and data:
                            if not text_item_started:
                                text_item_started = True
                                item_added_evt = {
                                    "type": "response.output_item.added",
                                    "response_id": response_id,
                                    "output_index": 0,
                                    "item": {
                                        "id": item_id,
                                        "type": "message",
                                        "status": "in_progress",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                }
                                yield f"event: response.output_item.added\ndata: {json.dumps(item_added_evt, ensure_ascii=False)}\n\n"
                                part_added_evt = {
                                    "type": "response.content_part.added",
                                    "response_id": response_id,
                                    "item_id": item_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "text", "text": ""},
                                }
                                yield f"event: response.content_part.added\ndata: {json.dumps(part_added_evt, ensure_ascii=False)}\n\n"
                            full_text.append(data)
                            text_delta_evt = {
                                "type": "response.output_text.delta",
                                "response_id": response_id,
                                "item_id": item_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": data,
                            }
                            yield f"event: response.output_text.delta\ndata: {json.dumps(text_delta_evt, ensure_ascii=False)}\n\n"
                        elif ev_type == "tool_call":
                            captured_tool_calls.append(data)

                raw_full_str = "".join(full_text)
                full_thinking_str = "".join(full_thinking)

                if not captured_tool_calls and tools and raw_full_str:
                    _, fallback_tcs = ToolBridge.extract_tool_calls_from_text(raw_full_str, tools)
                    if fallback_tcs:
                        captured_tool_calls = fallback_tcs

                if not captured_tool_calls and tools and full_thinking_str:
                    _, thinking_tcs = ToolBridge.extract_tool_calls_from_text(full_thinking_str, tools)
                    if thinking_tcs:
                        captured_tool_calls = thinking_tcs

                # 彻底净化正文标签，防止 DSML / 内部标签残留泄露
                complete_text = "".join(full_text)
                if captured_tool_calls:
                    complete_text = ToolBridge.sanitize_internal_tags(complete_text).strip()
                else:
                    complete_text = ToolBridge.sanitize_internal_tags(complete_text)

                completion_tokens = TokenEstimator.estimate_text_tokens(complete_text)
                reasoning_tokens = TokenEstimator.estimate_text_tokens(full_thinking_str)

                # 8.4 发射完成事件 (分别发射 thinking、text 及 function_call)
                if full_thinking:
                    reasoning_done_evt = {
                        "type": "response.reasoning_text.done",
                        "response_id": response_id,
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": full_thinking_str,
                    }
                    yield f"event: response.reasoning_text.done\ndata: {json.dumps(reasoning_done_evt, ensure_ascii=False)}\n\n"

                if text_item_started:
                    text_done_evt = {
                        "type": "response.output_text.done",
                        "response_id": response_id,
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": complete_text,
                    }
                    yield f"event: response.output_text.done\ndata: {json.dumps(text_done_evt, ensure_ascii=False)}\n\n"

                    part_done_evt = {
                        "type": "response.content_part.done",
                        "response_id": response_id,
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "text", "text": complete_text},
                    }
                    yield f"event: response.content_part.done\ndata: {json.dumps(part_done_evt, ensure_ascii=False)}\n\n"

                    item_done_evt = {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": 0,
                        "item": {
                            "id": item_id,
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "text", "text": complete_text}],
                        },
                    }
                    yield f"event: response.output_item.done\ndata: {json.dumps(item_done_evt, ensure_ascii=False)}\n\n"

                output_items = []
                if text_item_started:
                    output_items.append({
                        "id": item_id,
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "text", "text": complete_text}],
                    })

                for tc in captured_tool_calls:
                    tc_idx = len(output_items)
                    fc_id = f"fc_{uuid.uuid4().hex}"
                    call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    fn_name = tc.get("function", {}).get("name", "")
                    fn_args = tc.get("function", {}).get("arguments", "{}")

                    fc_added = {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": tc_idx,
                        "item": {
                            "id": fc_id,
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": call_id,
                            "name": fn_name,
                            "arguments": "",
                        },
                    }
                    yield f"event: response.output_item.added\ndata: {json.dumps(fc_added, ensure_ascii=False)}\n\n"

                    fc_delta = {
                        "type": "response.function_call_arguments.delta",
                        "response_id": response_id,
                        "item_id": fc_id,
                        "output_index": tc_idx,
                        "call_id": call_id,
                        "delta": fn_args,
                    }
                    yield f"event: response.function_call_arguments.delta\ndata: {json.dumps(fc_delta, ensure_ascii=False)}\n\n"

                    fc_args_done = {
                        "type": "response.function_call_arguments.done",
                        "response_id": response_id,
                        "item_id": fc_id,
                        "output_index": tc_idx,
                        "call_id": call_id,
                        "arguments": fn_args,
                    }
                    yield f"event: response.function_call_arguments.done\ndata: {json.dumps(fc_args_done, ensure_ascii=False)}\n\n"

                    fc_item_done = {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": tc_idx,
                        "item": {
                            "id": fc_id,
                            "type": "function_call",
                            "status": "completed",
                            "call_id": call_id,
                            "name": fn_name,
                            "arguments": fn_args,
                        },
                    }
                    yield f"event: response.output_item.done\ndata: {json.dumps(fc_item_done, ensure_ascii=False)}\n\n"

                    output_items.append({
                        "id": fc_id,
                        "type": "function_call",
                        "status": "completed",
                        "call_id": call_id,
                        "name": fn_name,
                        "arguments": fn_args,
                    })

                completed_evt = {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created_at,
                        "status": "completed",
                        "model": model,
                        "output": output_items if output_items else [
                            {
                                "id": item_id,
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [{"type": "text", "text": complete_text}],
                            }
                        ],
                        "usage": {
                            "total_tokens": prompt_tokens + completion_tokens + reasoning_tokens,
                            "input_tokens": prompt_tokens,
                            "output_tokens": completion_tokens + reasoning_tokens,
                            "input_tokens_details": {"cached_tokens": cached_tokens},
                            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                        },
                    },
                }
                yield f"event: response.completed\ndata: {json.dumps(completed_evt, ensure_ascii=False)}\n\n"

                # 兜底支持传统 SSE 客户端结束标志
                yield "data: [DONE]\n\n"

            except Exception as e:
                status_code = 500
                err_msg = str(e)
                logger.exception(f"[Responses-Routes] 流式生成异常: {e}")
                fail_evt = {
                    "type": "response.failed",
                    "response_id": response_id,
                    "error": {"message": str(e), "code": "internal_error"},
                }
                yield f"event: response.failed\ndata: {json.dumps(fail_evt, ensure_ascii=False)}\n\n"
            finally:
                complete_text = "".join(full_text)
                complete_thinking = "".join(full_thinking)
                completion_tokens = TokenEstimator.estimate_text_tokens(complete_text)
                reasoning_tokens = TokenEstimator.estimate_text_tokens(complete_thinking)
                total_tokens = prompt_tokens + completion_tokens + reasoning_tokens

                if final_mid:
                    session_tracker.update_message_id(conv_key, final_mid)
                    session_tracker.bind_response_id(response_id, conv_key, final_mid)

                account_pool.release_account(
                    account,
                    is_muted=is_muted_flag,
                    mute_until=mute_until_hint,
                    permanent=permanent_invalid,
                    caller_info=caller_info,
                )

                duration_ms = (time.time() - req_start_time) * 1000
                metrics_engine.record(
                    endpoint="/v1/responses",
                    model=model,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens + reasoning_tokens,
                    cached_tokens=cached_tokens,
                    account=account.account_label,
                    error=err_msg,
                    attribute_to_key=True,
                )
                # 注：metrics_engine.record(attribute_to_key=True) 内部已自动入账 key_usage_engine，此处不再重复调用以免双倍扣费

        return StreamingResponse(
            responses_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 9. 非流式处理 (JSON 直接返回，对齐 ToolBridge 提取与标签清洗)
    is_muted_flag = False
    mute_until_hint = None
    permanent_invalid = False
    status_code = 200
    err_msg = None
    full_text = []
    full_thinking = []
    final_mid = None

    try:
        async for event in DeepSeekClient.stream_chat(
            token=account.token,
            session_id=deepseek_session_id,
            prompt=prompt,
            parent_message_id=parent_message_id,
            thinking_enabled=thinking_enabled,
            model_type=model_type,
            ref_file_ids=ref_file_ids,
            cookie=account.cookie,
            proxy=get_effective_proxy(account),
        ):
            if event.event_type == "error":
                if event.error_code == "biz:5":
                    is_muted_flag = True
                    mute_until_hint = getattr(event, "mute_until", None)
                elif event.error_code == "auth_expired":
                    permanent_invalid = True
                decision = classify_upstream_preflight(event, None, 0.0)
                passive_metrics.note_preflight(decision)
                if decision.kind == "rate_limited":
                    apply_preflight_cooldown(
                        account,
                        decision,
                        getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0),
                        reason="responses_nonstream_rate_limited",
                    )
                if decision.kind in ("context_exhausted", "session_broken"):
                    try:
                        session_tracker.delete_session(conv_key)
                    except Exception:
                        pass
                status_code = (
                    400 if decision.kind == "context_exhausted"
                    else 429 if decision.kind in ("rate_limited", "session_broken")
                    else 502
                )
                err_msg = event.content or decision.detail or decision.metric_error or "upstream_error"
                raise HTTPException(status_code=status_code, detail=err_msg)

            if event.event_type == "thinking" and event.content:
                full_thinking.append(event.content)
            elif event.event_type == "content" and event.content:
                full_text.append(event.content)
            elif event.event_type == "message_id" and event.message_id:
                final_mid = event.message_id

        raw_text = "".join(full_text)
        complete_thinking = "".join(full_thinking)
        tool_calls = []

        if tools:
            clean_text, tool_calls = ToolBridge.extract_tool_calls_from_text(raw_text, tools)
            if not tool_calls and complete_thinking:
                _, cot_tool_calls = ToolBridge.extract_tool_calls_from_text(complete_thinking, tools)
                if cot_tool_calls:
                    tool_calls = cot_tool_calls
        else:
            clean_text = raw_text

        # 无论是否有 tools，彻底清洗内部特殊标签防泄露
        clean_text = ToolBridge.sanitize_internal_tags(clean_text).strip() if tool_calls else ToolBridge.sanitize_internal_tags(clean_text)

        completion_tokens = TokenEstimator.estimate_text_tokens(clean_text)
        reasoning_tokens = TokenEstimator.estimate_text_tokens(complete_thinking)
        total_tokens = prompt_tokens + completion_tokens + reasoning_tokens

        if final_mid:
            session_tracker.update_message_id(conv_key, final_mid)
            session_tracker.bind_response_id(response_id, conv_key, final_mid)

        output_items = []
        if clean_text or not tool_calls:
            output_items.append({
                "id": item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": clean_text,
                    }
                ],
            })
        for tc in tool_calls:
            output_items.append({
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            })

        resp_payload = {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": model,
            "output": output_items,
            "usage": {
                "total_tokens": total_tokens,
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens + reasoning_tokens,
                "input_tokens_details": {"cached_tokens": cached_tokens},
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            },
        }
        return JSONResponse(content=resp_payload)

    finally:
        account_pool.release_account(
            account,
            is_muted=is_muted_flag,
            mute_until=mute_until_hint,
            permanent=permanent_invalid,
            caller_info=caller_info,
        )
        duration_ms = (time.time() - req_start_time) * 1000
        complete_text = "".join(full_text)
        complete_thinking = "".join(full_thinking)
        completion_tokens = TokenEstimator.estimate_text_tokens(complete_text)
        reasoning_tokens = TokenEstimator.estimate_text_tokens(complete_thinking)
        total_tokens = prompt_tokens + completion_tokens + reasoning_tokens

        metrics_engine.record(
            endpoint="/v1/responses",
            model=model,
            status_code=status_code,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens + reasoning_tokens,
            cached_tokens=cached_tokens,
            account=account.account_label,
            error=err_msg,
            attribute_to_key=True,
        )
        # 注：metrics_engine.record(attribute_to_key=True) 内部已自动入账 key_usage_engine，此处不再重复调用以免双倍扣费


@router.get("/v1/responses/{response_id}")
@router.get("/responses/{response_id}")
async def responses_retrieve(response_id: str, identity: Dict[str, Any] = Depends(get_identity)):
    """查询 Responses 详情存根"""
    return JSONResponse(
        content={
            "id": response_id,
            "object": "response",
            "status": "completed",
            "message": "Responses retrieved",
        }
    )
