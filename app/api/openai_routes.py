"""
OpenAI 兼容 API 路由 (/v1/chat/completions, /v1/models)
支持标准流式与非流式、思考链 (reasoning_content)、会话树状态机、多模态识图与账号池互斥
"""

import asyncio
import json
import logging
import time
import uuid
import random
import threading
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.core.security import verify_api_key, get_identity
from app.config import (
    config_manager,
    # ── T9 单模型收敛 ──
    CANONICAL_MODEL_ID,
    CANONICAL_MODEL_NAME,
    CANONICAL_MODEL_DESCRIPTION,
    CANONICAL_REASONING_EFFORT,
    PRO_REASONING_EFFORT,
    SUPPORTED_REASONING_EFFORTS,
    CANONICAL_MODEL_CONTEXT_WINDOW,
    CANONICAL_MODEL_MAX_OUTPUT_TOKENS,
    PRO_MODEL_ID,
    PRO_MODEL_NAME,
    PRO_MODEL_DESCRIPTION,
    is_pro_model,
    MODEL_ALIASES,
    UPSTREAM_MODEL_TYPE_VALUES,
    is_known_model_id,
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
from app.core.error_catalog import (
    CLIENT_MSG_CONTENT_POLICY,
    CLIENT_MSG_CONCURRENCY_LIMIT,
    CLIENT_MSG_DAILY_QUOTA,
    CLIENT_MSG_UPSTREAM_RATE_LIMIT,
    CLIENT_MSG_UPSTREAM_BAD_GATEWAY,
    CLIENT_MSG_UPSTREAM_SESSION_FAILED,
    CLIENT_MSG_MEDIA_PROCESSING_FAILED,
    CLIENT_MSG_SERVICE_UNAVAILABLE,
    CLIENT_MSG_UPSTREAM_BUSY,
    raise_sanitized_sensitive_block,
    raise_sanitized_concurrency_limit,
    raise_sanitized_pool_empty,
    raise_sanitized_upstream_error,
)
from app.engine.reasoning_director import ReasoningDirector
from app.engine.vision_pipeline import (
    VisionPipeline,
    UnsupportedFileTypeError,
    DeepSeekMutedError,
    DeepSeekQuotaExceededError,
    DeepSeekUploadRateLimitError,
)
from app.engine.sensitive_filter import sensitive_filter, SafetyOutputGuard
from app.engine.tool_bridge import ToolBridge
from app.engine.streaming_parser import StreamingToolParser
from app.engine.disconnect_guard import disconnect_guard

logger = logging.getLogger(__name__)

_get_proxy = get_effective_proxy


router = APIRouter(dependencies=[Depends(verify_api_key)])

# 上游「快速空流」判定阈值：超过该时长仍未收到任何事件，
# 认为属于正常的慢启动（长上下文深度思考），而非限流。
PREFLIGHT_FAST_EMPTY_SECONDS = 5.0

# 上游错误文本/标识中出现这些特征时，判定为限流而非普通错误。
# ⚠️ 这些词是**按实测补齐**的，不是猜的：
#   上游实际的 SSE 限流帧为
#     {"type":"error","content":"消息发送过于频繁，请稍后重试",
#      "clear_response":true,"finish_reason":"rate_limit_reached"}
#   初版清单只写了 429 / rate limit / 限流 / 请求过于频繁，
#   而这帧用的是 `rate_limit_reached` 与「消息发送过于频繁」——**一个都没匹配上**，
#   导致限流被当成普通错误静默放过，客户端连续重试 12 次。
_RATE_LIMIT_HINTS = (
    "429",
    "rate_limit",
    "rate limit",
    "ratelimit",
    "too many requests",
    "限流",
    "请求过于频繁",
    "发送过于频繁",   # 实测上游原文：消息发送过于频繁，请稍后重试
    "操作过于频繁",
    "parallel_chat",  # 实测上游并发冲突原文：有消息正在生成，请稍后再试 (parallel_chat_limit)
    "有消息正在生成",
)

# DeepSeek 2.8.0 配额耗尽（COMPLETION_ERROR_CODE.USAGE_QUOTA_EXCEEDED=29 / THINKING_QUOTA_EXCEEDED=15）
_QUOTA_EXCEEDED_HINTS = (
    "quota_exceeded",
    "usage_quota_exceeded",
    "thinking_quota_exceeded",
    "usage quota",
    "quota exceeded",
    "使用额度已达上限",
    "额度已达上限",
    "biz:29",
    "biz:15",
)

# DeepSeek 2.8.0 行为/人机挑战（GLOBAL_ERROR_BIZ_CODE.CHALLENGE_REQUIRED=40303 / CHALLENGE_FAILED=40304）
_CHALLENGE_HINTS = (
    "challenge_required",
    "challenge_failed",
    "40303",
    "40304",
)

# 上下文窗口耗尽的特征。**实测上游原文**（2026-09-10，E37）：
#   {"type":"error","content":"达到对话长度上限，请开启新对话","clear_response":true,...}
#   · 800k tokens 正常；1.6M tokens 触发
#   · 该错误是**终态**：同一会话无法通过重试恢复，必须开新会话
#   · 报错形态与限流帧完全一致（同为 type:error + clear_response），
#     但语义完全不同——限流可退避重试，长度上限不可。
# 若不区分，网关会报 502，客户端当"服务端故障"无限重试，
# 而每次重试都必然失败 → 表现为"任务做到一半就再也写不出来了"（T19 实证）。
_CONTEXT_EXHAUSTED_HINTS = (
    "达到对话长度上限",
    "对话长度上限",
    "开启新对话",
    "context length",
    "maximum context",
    "too long",
    "exceeds the maximum",
)

# 会话树断裂/无效消息 ID 的特征。**实测上游原文**（2026-09-12）：
#   {"code":0,"msg":"","data":{"biz_code":26,"biz_msg":"invalid message id","biz_data":null}}
#   · 该错误意味着上游会话树父节点已不存在（例如上一轮遭遇限流/清屏，本轮误拿幽灵 ID 续写）
#   · 必须清掉会话绑定，以便客户端下一轮/自动重试时开启全新会话自愈
_SESSION_BROKEN_HINTS = (
    "invalid message id",
    "invalid_message_id",
    "biz:26",
)

# 内容安全拦截的特征。**⚠️ 这一组没有线上实证。**
#
# 与上面两组不同，我们没有一条确证的安全拦截样本（上游的审核文案见过
# "内容审核未通过"这个形态，但它只出现在**我自己写的测试**里，不是抓到的原文）。
# 所以这里刻意只用于**分类与计数**，不参与任何处置决策 —— 见
# `classify_upstream_preflight` 里那段说明：未经实证的判据不能用来改状态码。
#
# 猜错的代价被限制住了：匹配不上 → 仍旧落到通用 `error`（与今天完全一致）；
# 误匹配 → 也不过是把它记成 safety_blocked 再走通用路径。
_SAFETY_BLOCK_HINTS = (
    "内容审核",
    "审核未通过",
    "违规内容",
    "包含违规",
    "content_filter",
    "content policy",
    "safety_check",
    # 刻意**不**放裸 "safety"：它太泛（`safety_parameter` 之类会被误判），
    # 而误判在这里没有任何收益 —— 只是把一个计数器记错而已。
)

# 官方服务器繁忙 / 服务级瞬态故障 / 过载特征（Tier 1）
# 关键铁律：匹配该特征时，绝对不惩罚本地账号！账号保持 ACTIVE 与 valid。
_UPSTREAM_BUSY_HINTS = (
    "upstream_busy",
    "服务器繁忙",
    "系统负载过高",
    "服务繁忙",
    "server busy",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "overloaded",
    "bad_content_type",
    "waf_blocked",
    "cloudflare",
    "500",
    "502",
    "503",
    "504",
    "520",
    "521",
    "522",
    "524",
)


# ══════════════════════════════════════════════════════════════════════════
# T9 第四步：L0 提示词收敛为**唯一一套**，字节级冻结
# ══════════════════════════════════════════════════════════════════════════
#
# ## 冻结的含义（改这个字符串之前必须知道）
#
# 这是**字节级冻结**的常量：它会被拼进每一轮请求的 System 末端，因此它是
# 上游**前缀缓存**的锚点。任何一个字节变化（哪怕只是空格/换行/大小写）
# 都会让全部在跑会话的缓存**同时失效** → 首字延迟集体回退。
#
# ## 为什么必须收敛成一套
#
# 收敛前提示词按模型名分成 4 套（pro / flash / pro-exp / flash-exp），
# 于是"切换模型"实际等于"换掉前缀" → **前缀漂移**，缓存全失效。
# 4 个 ID 既然在上游是同一条通道（实测 sha1 相同），就没有理由带 4 套前缀。
#
# ## ⚠️ 本次**没有**做的事（不要误读为已完成）
#
# 清单 T9 里还有一条**未完成的探索项**：
#   "A/B 度量「`We need to...` 英文 CoT」是否仍值钱"
# 本次**只做结构收敛，不碰提示词语义**。下面的文本是**逐字节照搬**
# 收敛前 `is_pro and is_exp`（即 `deepseek-v4-pro-exp`）分支的原文——
# 一个字符都没有改。语义 A/B（例如"是否该保留 We need to 锚点"、
# "是否该强制英文思考"）**不在本次范围**，仍待度量。
#: 统一核心提示词（大道至简，单模型统一基准）：
#: 消除教条式的 "We need to..." / 强制全英文思考等过度工程化约束；
#: 明确直接、自然、简洁原则，杜绝小题大做；保留工具调用边界铁律。
_CANONICAL_SYSTEM_PROMPT = (
    "You are DeepSeek, a helpful, pragmatic, and concise AI assistant.\n"
    "- Respond directly, accurately, and naturally. Keep answers clear and simple without overcomplicating straightforward questions.\n"
    "- Deliver your response in the user's requested language (default: Chinese).\n"
    "- When tools are enabled, never output tool calls inside your thinking process; emit tool calls exclusively in your final response."
)
_FROZEN_L0_PROMPT = _CANONICAL_SYSTEM_PROMPT  # 兼容引用

#: 多模态视觉指引。同样字节级冻结。
#: 它**不是**"按模型分流"的分支——只要本轮挂了图片就注入，与模型 ID 无关。
_FROZEN_VISION_PROMPT = (
    "【多模态视觉指引】\n"
    "当前会话中用户上传的所有图片/截图已在你的视觉上下文中持续挂载。\n"
    "进行视觉分析、界面还原或代码开发时，请在同一次回答中直接输出完整的分析结论、实现方案或生成代码，严禁仅输出‘我先看一下’等中间停顿短语后停止。"
)


class PreflightDecision:
    """上游首事件预检结论（纯数据，便于单元测试）"""

    __slots__ = ("kind", "detail", "retry_after", "metric_error")

    def __init__(self, kind: str, detail: str = "", retry_after: int = 0, metric_error: str = ""):
        self.kind = kind                  # "ok" | "rate_limited" | "context_exhausted" | "safety_blocked" | "error"
        self.detail = detail
        self.retry_after = retry_after
        self.metric_error = metric_error


def classify_upstream_preflight(first_event, first_exc, elapsed: float) -> PreflightDecision:
    """判断上游首事件是否意味着限流。

    背景（实测）：单账号并发超限或突发限流时，上游返回
      HTTP 200 + 空正文（约 0.19s / 281B），或一个错误体。
    旧实现直接把它当「正常空回答」转发给客户端，导致客户端判定模型没说话 →
    触发客户端重试 + 网关「死磕自愈循环」→ 最终吐出【网关安全拦截】文案。
    由于 StreamingResponse 已经发出 200 响应头，之后再想改状态码是无效的，
    因此必须在返回响应之前完成这一判定。

    :param first_event: 第一个上游事件；空流时为 None
    :param first_exc: 拉取首个事件时抛出的异常；无异常时为 None
    :param elapsed: 拉取首个事件耗时（秒）
    """
    if first_exc is not None:
        # 异常交由上层原有逻辑处理，不在此处改写成限流
        return PreflightDecision("ok")

    if first_event is None:
        if elapsed < PREFLIGHT_FAST_EMPTY_SECONDS:
            return PreflightDecision(
                "rate_limited",
                detail=(
                    f"上游未产生任何输出（{elapsed:.2f}s 内空流结束）。"
                    f"最可能原因是该账号并发超限或触发限流，请稍后重试。"
                ),
                retry_after=3,
                metric_error="upstream_empty_stream_rate_limited",
            )
        # 慢速空流：更像是上游静默超时，交回原有空响应路径
        return PreflightDecision("ok")

    if getattr(first_event, "event_type", None) == "error":
        # 同时看 content 与 error_code：
        # upstream 可能把限流表达在文本里（"消息发送过于频繁"），
        # 也可能只用错误码表达（HTML 过载页 → error_code="rate_limit"）。
        # 只查 content 会漏掉后者（该疏漏由本地验证抓出）。
        text = str(getattr(first_event, "content", "") or "")
        code = str(getattr(first_event, "error_code", "") or "")
        probe = f"{text} {code}".lower()

        # 上下文窗口耗尽：终态错误，必须在限流判定**之前**区分出来。
        # 它不可通过退避重试恢复（重试必然再次失败），正确做法是让会话可自愈
        # —— 上层会清掉会话绑定，下一轮自动开新会话。
        if any(h in probe for h in _CONTEXT_EXHAUSTED_HINTS):
            return PreflightDecision(
                "context_exhausted",
                # ★ 必须含**可被客户端识别**的英文短语 —— 这不是装饰，是功能性的。
                #
                # DSH（agent 侧）只有在失败码严格等于 `CONTEXT_WINDOW_EXCEEDED` 时
                # 才触发**压缩**（`dsh-compaction-basic/lib/index.js:803`），
                # 而那个码由 `isContextWindowExceededError(detail)` 判定，
                # 它的判据**全是英文正则**（`dsh-llm/lib/types/error.js:61`）。
                #
                # 旧文案是纯中文 → 五条正则一条都匹配不上 → 客户端**认不出**
                # "这是上下文超限" → 压缩中间件根本不触发 → 只能无限重试。
                # 于是 agent 明明有压缩能力，却"没有做到即时压缩"。
                #
                # `maximum context length exceeded` 同时命中两条正则：
                #   · STRUCTURED_CONTEXT_OVERFLOW  ← `context length exceeded`
                #   · `maximum ... context length` ← 第二条
                # 双保险，且中文部分保持对用户可读。
                # tests/test_context_exceeded_contract.py 里**照抄了 DSH 的真正则**来钉死它。
                detail=(
                    "本会话上下文已达上游长度上限（maximum context length exceeded），"
                    "无法继续在该会话中作答。"
                    "\n网关已标记该会话需重建，请重试（将自动开启新会话）。"
                ),
                retry_after=0,
                metric_error="upstream_context_exhausted",
            )

        # 会话链断裂（如幽灵 message_id 导致的 invalid message id）：
        # 必须在限流与通用错误之前判定，通知上层清除会话绑定并触发自愈
        if any(h in probe for h in _SESSION_BROKEN_HINTS):
            return PreflightDecision(
                "session_broken",
                detail=(
                    "本会话上游上下文节点已失效（invalid message id），"
                    "网关已自动清除会话缓存，请重试（将自动开辟新会话重建）。"
                ),
                retry_after=1,
                metric_error="upstream_session_broken",
            )

        # 上游官方服务繁忙 / 过载 / 服务级故障 (Tier 1) - 零账号惩罚
        if any(h in probe for h in _UPSTREAM_BUSY_HINTS):
            return PreflightDecision(
                "upstream_busy",
                detail=f"上游服务过载/繁忙: {text or code}",
                retry_after=5,
                metric_error="upstream_server_busy",
            )

        if any(h in probe for h in _QUOTA_EXCEEDED_HINTS):
            mu = getattr(first_event, "mute_until", None)
            now_ts = time.time()
            ra = max(60, min(int(mu - now_ts), 86400)) if (mu and mu > now_ts) else 600
            return PreflightDecision(
                "rate_limited",
                detail=f"上游账号额度已达上限: {text or code}",
                retry_after=ra,
                metric_error="upstream_quota_exceeded",
            )

        if any(h in probe for h in _CHALLENGE_HINTS):
            return PreflightDecision(
                "rate_limited",
                detail=f"上游触发行为挑战({code or 'challenge'})，已隔离冷却并换号重试",
                retry_after=120,
                metric_error="upstream_challenge_required",
            )

        if any(h in probe for h in _RATE_LIMIT_HINTS):
            return PreflightDecision(
                "rate_limited",
                detail=f"上游限流: {text or code}",
                retry_after=3,
                metric_error="upstream_rate_limited_error_event",
            )

        # 内容安全拦截：**单独分类，但不改行为**。
        #
        # 为什么值得单独分出来：它**不是网关故障**，却被混进上一行的通用 `error`
        # 里 —— 结果是既抬高失败率掩盖真故障，又让"安全拦截率"这个指标根本算不出来
        # （T16/B1 要算的四个比率之一）。
        #
        # ⚠️ 这一版**只加分类与计数，不改任何处置**：调用方没有 `safety_blocked`
        # 分支，所以它会走和今天完全一样的通用错误路径（502）。这是刻意的 ——
        # 下面这组特征词**没有经过线上实证**（我们没有一条确证的安全拦截样本），
        # 拿一个未验证的判据去改状态码、加终态语义，就是 T9 那次
        # "从 sha1 相同推断出档位由 has_images 决定"的同类错误。
        # 先让它能被计数；等计数器真的响了，再用真实样本决定怎么处置。
        # 判据来源：中文审核语境用"审核/违规"，英文侧用 content_filter/safety。
        if any(h in probe for h in _SAFETY_BLOCK_HINTS):
            return PreflightDecision(
                "safety_blocked",
                detail=f"上游内容安全拦截: {text or code}",
                retry_after=0,
                metric_error="upstream_safety_blocked",
            )

        return PreflightDecision("error")

    return PreflightDecision("ok")


def apply_preflight_cooldown(account, decision: PreflightDecision, default_cd: float, reason: str) -> float:
    """根据预检决策对账号施加保护性冷却；遇到配额耗尽/挑战时优先采用上游 reset_at 或较长冷却。"""
    if getattr(decision, "metric_error", None) in ("upstream_quota_exceeded", "upstream_challenge_required"):
        cd = max(float(default_cd), float(getattr(decision, "retry_after", 0) or 0))
        effective_reason = str(decision.metric_error)
    else:
        cd = float(default_cd)
        effective_reason = reason
    account.apply_protective_cooldown(cd, reason=effective_reason)
    return cd


async def _fetch_preflight(upstream_aiter):
    """
    预检拉取上游事件：
    跳过纯元数据帧（如 message_id），持续拉取直到捕获首个实体事件
    （thinking / content / error / done）或流结束。

    解决背景：DeepSeek 在 stream 建立后固定先发一帧 message_id 元数据，
    真正的限流/错误帧紧随其后。若仅拉取一次 __anext__()，会误将 message_id
    判定为正常出字从而绕过预检限流判定并向客户端过早发送 HTTP 200。

    返回: (preflight_events, substantive_event, target_event, first_exc, pf_elapsed, decision)
    """
    pf_start = time.time()
    preflight_events = []
    substantive_event = None
    first_exc = None
    try:
        while True:
            ev = await upstream_aiter.__anext__()
            preflight_events.append(ev)
            # 实体事件特征：thinking / content / error / done
            if getattr(ev, "event_type", None) in ("thinking", "content", "error", "done"):
                substantive_event = ev
                break
    except StopAsyncIteration:
        pass
    except BaseException as ex:
        first_exc = ex
    pf_elapsed = time.time() - pf_start
    target_event = substantive_event if substantive_event is not None else (preflight_events[-1] if preflight_events else None)
    decision = classify_upstream_preflight(target_event, first_exc, pf_elapsed)
    return preflight_events, substantive_event, target_event, first_exc, pf_elapsed, decision


# SSE 保活心跳间隔（秒）。
# 背景（交接文档 P2，提出两天未落地）：上游在长上下文深度思考时可能 40~55s 不出字，
# 期间网关完全不向下游写任何字节 → 客户端（DSH 实测看门狗约 50s）判定传输静默而掐断，
# 表现为 code:"TRANSPORT", message:"terminated"，并触发客户端连续重试。
# 解决方式：让包装器在静默超过该间隔时向下游写入一条 SSE 注释行。
SSE_KEEPALIVE_INTERVAL = 3.0

# 包装器用于标记"本次是心跳而非真实事件"的哨兵
_KEEPALIVE_SENTINEL = object()


async def _aiter_with_keepalive(source, interval: float = SSE_KEEPALIVE_INTERVAL):
    """把上游事件流转成"静默超时即周期性产出心跳"的流。

    产出值：
      · 真实事件对象           —— 原样透传
      · SSE_KEEPALIVE_SENTINEL —— 表示应向下游写一条心跳注释

    设计要点（含一次修正）：
      · 关键：pending 取事件的协程必须**跨心跳复用**，并在心跳后**按新超时再次竞速**。
        初版实现在产出一次心跳后就 `await next_task` 一直到真事件到达，
        结果长静默只发一次心跳，之后仍是死寂——那依然会让客户端看门狗掐断。
        测试 test_keepalive_emitted_during_long_silence 正好抓出了这一点。
      · 上游迭代结束时返回；异常原样向上抛（不吞）
      · 心跳是 SSE 注释行，标准客户端（OpenAI SDK / DSH）会忽略，只刷新读超时
    """
    aiter = source.__aiter__()
    next_task = None
    while True:
        if next_task is None:
            next_task = asyncio.ensure_future(aiter.__anext__())
        try:
            done, _pending = await asyncio.wait({next_task}, timeout=interval)
        except asyncio.CancelledError:
            next_task.cancel()
            raise
        if next_task in done:
            try:
                ev = next_task.result()
            except StopAsyncIteration:
                return
            next_task = None
            yield ev
            continue

        # 超时：发一次心跳，保留取事件任务，下一轮用新超时继续竞速
        yield _KEEPALIVE_SENTINEL


def _format_model_info(
    model_id: str,
    name: str,
    description: str,
    reasoning_effort: str = "high",
    is_vision: bool = True,
) -> Dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 1700000000,
        "owned_by": "deepseek",
        "name": name,
        "description": description,
        "context_window": CANONICAL_MODEL_CONTEXT_WINDOW,
        # ⚠️ 这里曾是 `CANONICAL_MODEL_CONTEXT_WINDOW` —— 错的。
        # `max_tokens` 是**单次最大输出**，不是上下文窗口（那是两个不同的量）。
        # 声明成 1M 会让把它当默认输出上限用的客户端发出不合理的请求。
        # 正确的输出上限在下一行的 `max_output_tokens`。
        "max_tokens": CANONICAL_MODEL_MAX_OUTPUT_TOKENS,
        "max_output_tokens": CANONICAL_MODEL_MAX_OUTPUT_TOKENS,
        # 1. 现代 OpenAI / WebUI / DSH 规范
        "input": ["text", "image"] if is_vision else ["text"],
        "modalities": ["text", "image"] if is_vision else ["text"],
        "vision": is_vision,
        # 2. LiteLLM / LangChain / Cline / Roo Code / Cherry Studio 规范
        "supports_vision": is_vision,
        "supports_tools": True,
        "supports_function_calling": True,
        "mode": "chat",
        "reasoning_effort": reasoning_effort,
        "default_reasoning_effort": reasoning_effort,
        "supported_reasoning_efforts": list(SUPPORTED_REASONING_EFFORTS),
        # 3. Ollama / LM Studio / LocalAI 规范
        "capabilities": {
            "vision": is_vision,
            "tools": True,
            "function_calling": True,
            "reasoning": True,
            "reasoning_effort": True,
            "supported_reasoning_efforts": list(SUPPORTED_REASONING_EFFORTS),
        },
        # 4. OpenRouter 架构规范
        "architecture": {
            "modality": "text+image->text" if is_vision else "text->text",
            "input_modalities": ["text", "image"] if is_vision else ["text"],
            "output_modalities": ["text"],
            "tokenizer": "deepseek",
        },
        # 5. 标准 OpenAI 权限项
        "permission": [
            {
                "id": f"modelperm-{model_id}",
                "object": "model_permission",
                "created": 1700000000,
                "allow_create_engine": False,
                "allow_sampling": True,
                "allow_logprobs": True,
                "allow_search_indices": False,
                "allow_view": True,
                "allow_fine_tuning": False,
                "organization": "*",
                "group": None,
                "is_blocking": False,
            }
        ],
    }


@router.get("/v1/models")
@router.get("/models")
async def list_models():
    """对外宣称主模型与敏捷行动模型（Action专用）。"""
    models = [
        _format_model_info(
            CANONICAL_MODEL_ID,
            CANONICAL_MODEL_NAME,
            CANONICAL_MODEL_DESCRIPTION,
            reasoning_effort=CANONICAL_REASONING_EFFORT,
            is_vision=True,
        ),
        _format_model_info(
            PRO_MODEL_ID,
            PRO_MODEL_NAME,
            PRO_MODEL_DESCRIPTION,
            reasoning_effort=PRO_REASONING_EFFORT,
            is_vision=True,
        ),
    ]
    return {"object": "list", "data": models}


@router.get("/v1/usage")
@router.get("/usage")
async def my_usage(identity: Dict[str, Any] = Depends(get_identity)):
    """自助用量查询（T25）：用哪个 key 查就只返回哪个 key 的数据。

    刻意不返回别人的用量 —— 这样这把钥发给谁都不会泄露其他人的使用情况。
    配额为 0 表示不限。`retry_after` 只在已超配额时有意义。
    """
    snap = key_usage_engine.snapshot(identity.get("key_id", ""))
    used_req = int(snap.get("requests", 0))
    used_tok = int(snap.get("prompt_tokens", 0)) + int(snap.get("completion_tokens", 0))
    limit_req = int(identity.get("quota_daily_requests") or 0)
    limit_tok = int(identity.get("quota_daily_tokens") or 0)

    def _pct(used, limit):
        if limit <= 0:
            return None
        return round(min(100.0, used * 100.0 / limit), 1)

    return {
        "object": "usage",
        "key": {
            "id": identity.get("key_id", ""),
            "name": identity.get("name", ""),
            "owner": identity.get("owner", ""),
        },
        "today": {
            "date": snap.get("day", ""),
            "requests": used_req,
            "prompt_tokens": int(snap.get("prompt_tokens", 0)),
            "completion_tokens": int(snap.get("completion_tokens", 0)),
            "total_tokens": used_tok,
            "errors": int(snap.get("errors", 0)),
            "rejected_by_quota": int(snap.get("rejected", 0)),
            "avg_duration_ms": round(
                float(snap.get("duration_ms", 0.0)) / used_req, 1
            ) if used_req else 0.0,
        },
        "lifetime": {
            "requests": int(snap.get("total_requests", 0)),
            "total_tokens": int(snap.get("total_tokens", 0)),
            "last_used_at": snap.get("last_used_at"),
        },
        "quota": {
            "daily_requests": limit_req,
            "daily_tokens": limit_tok,
            "used_percent_requests": _pct(used_req, limit_req),
            "used_percent_tokens": _pct(used_tok, limit_tok),
        },
        "capacity": {
            "owned_accounts": len(identity.get("account_labels") or []),
            "max_concurrent_streams": len(identity.get("account_labels") or []) * MAX_CONCURRENT_PER_ACCOUNT,
            "pool_fallback": bool(identity.get("pool_fallback")),
        },
    }


@router.get("/v1/models/{model_id}")
@router.get("/models/{model_id}")
async def get_model(model_id: str):
    """返回**单一**模型卡（T9）。

    已登记的 4 个 ID 返回完全相同的卡；未登记的 ID 也返回**同一张卡**
    （只是 `id` 回显调用方给的名字）——这是收敛前的既有兜底行为，
    保持它才不会在客户端换个新名字时打断服务。
    """
    res = await list_models()
    for m in res["data"]:
        if m["id"] == model_id:
            return m
    # T9 兜底：未登记 ID 不再按 "pro/expert/reasoner" 子串分流档位
    # （那是已死的 model_type 语义的残留），一律回同一张主模型卡。
    if is_pro_model(model_id):
        return _format_model_info(
            model_id=model_id,
            name=PRO_MODEL_NAME,
            description=PRO_MODEL_DESCRIPTION,
            reasoning_effort=PRO_REASONING_EFFORT,
            is_vision=True,
        )
    return _format_model_info(
        model_id=model_id,
        name=CANONICAL_MODEL_NAME,
        description=CANONICAL_MODEL_DESCRIPTION,
        reasoning_effort=CANONICAL_REASONING_EFFORT,
        is_vision=True,
    )


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def openai_chat_completions(request: Request, identity: Dict[str, Any] = Depends(get_identity)):
    req_start_time = time.time()

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
        "auto_replenish": bool(identity.get("auto_replenish", True)),
    }

    try:
        body = await request.json()
    except ClientDisconnect:
        print(f"[Gateway] 客户端在读取请求体前已主动断开 (499): key={caller_info.get('key_name')} ip={caller_info.get('ip')}", flush=True)
        metrics_engine.record(
            endpoint="/v1/chat/completions",
            model="unknown",
            status_code=499,
            duration_ms=(time.time() - req_start_time) * 1000,
            account=caller_info.get("owner", ""),
            error="client_disconnect_before_body",
            attribute_to_key=False,
        )
        raise HTTPException(status_code=499, detail="Client disconnected")
    messages: List[Dict[str, Any]] = body.get("messages", [])
    stream: bool = body.get("stream", False)
    tools: List[Dict[str, Any]] = body.get("tools", [])
    stream_options = body.get("stream_options") or {}
    include_usage = bool(stream_options.get("include_usage", False))

    # ── T9 单模型收敛：在**任何决策之前**把别名归一到主模型 ──
    #
    # 为什么必须在这里归一、而且必须早于后面所有分支：
    # 收敛要求 4 个 ID **在任何可观测行为上都不可区分**。而 `model` 会流向
    # 两个会改变行为的地方（都由归一后的 `model` 驱动，见下）：
    #   1. `_model_for_session` → `resolve_conversation(model=...)`，
    #      而 `session_tree` 把 model 拼进 conv_key：
    #        `f"{model}:cid:{custom_id}"` / `bucket = f"{model}|{head}"`
    #      → 不归一的话，两个客户端用不同别名问同一个问题时，会落在
    #        **不同的会话桶**里，拿不到对方的会话亲和（= 可观测的行为差异）。
    #   2. 提示词选择（旧实现按 pro/exp 选 4 套 CoT 之一）。
    #
    # 归一后这两条路径对所有别名完全一致，这才叫等效的同一条路径。
    _client_model: str = body.get("model", "") or ""
    model: str = normalize_model_id(_client_model)
    # 观测用：客户端原始 ID。归一后 `model` 恒为主模型，
    # 若不复用原始值，日志里就再也看不出"是哪个别名打进来的"，排障会瞎。
    if _client_model and not is_known_model_id(_client_model):
        print(
            f"[Gateway-OpenAI] 未登记的 model={_client_model!r}，"
            f"按 T9 归一到主模型 {model}（不报错，保持既有兜底行为）",
            flush=True,
        )

    print(f"\n[Gateway-OpenAI] Incoming call: owner={caller_info.get('owner')} key={caller_info.get('key_name')} model={_client_model or model} (归一→{model}), stream={stream}, tools={len(tools)}, msgs={len(messages)}")
    for t in tools:
        fn = t.get("function", t)
        print(f"  [Tool Schema] {fn.get('name')}: properties={list(fn.get('parameters', {}).get('properties', {}).keys())}, required={fn.get('parameters', {}).get('required', [])}")
    if getattr(config_manager.config, "debug_log_messages", False):
        for idx, m in enumerate(messages):
            c_prev = str(m.get("content", ""))[:60].replace("\n", " ")
            print(f"  [{idx}] {m.get('role')}: {c_prev}")

    if not messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty")

    # ── 用户敏感词拦截（若该 API Key 未开启白名单豁免） ──
    if identity.get("sensitive_filter", True):
        prompt_text = sensitive_filter.extract_text_from_messages(messages)
        matched = sensitive_filter.check_content(prompt_text)
        if matched:
            key_name = identity.get("log_label") or identity.get("name") or "unknown"
            logger.warning(
                f"[SensitiveFilter] Key '{key_name}' (id={identity.get('key_id')}) "
                f"请求触发敏感词前置拦截: [{matched['category_name']}] '{matched['matched']}' (规则: {matched['rule_desc']})"
            )
            raise_sanitized_sensitive_block(
                matched_category=matched["category_name"],
                matched_keyword=matched["matched"],
                rule_desc=matched["rule_desc"],
                key_name=key_name,
                key_id=identity.get("key_id"),
            )

    # 1. 提取多模态图片与规范化消息文本 (支持 content 结构化/内嵌/顶层 extra_images)
    detected_images = []
    normalized_messages = []
    for m in messages:
        c = m.get("content", "")
        extra_imgs = m.get("images", [])
        p_text, imgs = await VisionPipeline.extract_images_and_text_async(c, extra_images=extra_imgs)
        if imgs:
            detected_images.extend(imgs)

        # ★ 这里**不能只保留 role/content** —— 下游还会读另外三个字段：
        #   · `tool_call_id`：session_tree.py:261/630/684 用它拼 `[工具返回 {cid}]`
        #     并让它进会话指纹。旧实现把它丢掉，于是 **cid 恒为空串** ——
        #     多轮工具对话在模型眼里就变成"一堆不知道对应哪个调用的返回"。
        #   · `name`：session_tree.py:263 用它区分工具消息的来源。
        #   · `tool_calls`：session_tree.py:265/669 重构建历史里的工具轮次，
        #     tool_bridge.py:111 判断是否是工具轮，tokenizer.py:81 做 token 计量。
        # 只补**确实存在**的键，不塞 None —— 免得改变已有行为。
        normalized = {"role": m.get("role", "user"), "content": p_text}
        for _field in ("tool_call_id", "name", "tool_calls"):
            if m.get(_field) is not None:
                normalized[_field] = m[_field]
        normalized_messages.append(normalized)

    # 1.1 若请求包含 tools，将 tools 提示词强注入 System Prompt
    if tools:
        tools_prompt = ToolBridge.format_tools_system_prompt(tools)
        has_system = False
        for m in normalized_messages:
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + "\n" + tools_prompt
                has_system = True
                break
        if not has_system:
            normalized_messages.insert(0, {"role": "system", "content": tools_prompt})

    # ── T9：模型分流已收敛，这里不再按模型名分支 ──
    #
    # 收敛前这里有 `is_exp` / `is_pro` 两个子串判断，用来在 4 套 CoT 提示词与
    # 两个 model_type 之间分流。实测证明上游对 model_type 完全无视
    # （default/expert/vision 正文 sha1 相同），且 4 个 ID 已被归一到同一个
    # `model`，所以这些分支**永远只会走同一条路**——留着只会让人误以为
    # "模型选择"仍然生效。故删除。
    #
    # `model_lower` 仍然基于**客户端原始 ID** 计算：它是纯粹的客户端行为开关
    # （`-nothink` 后缀），不属于模型路由语义，归一化不能把它吃掉。
    model_lower = _client_model.lower()
    #: 视觉路径只看"本轮是否真的带了图片"。
    #: 旧代码还接受模型名里含 "vision" 作为触发条件——现在没有 vision 模型
    #: 了（并入主模型的多模态能力），保留子串判断只会让未登记 ID 意外改行为。
    is_vision = bool(detected_images)

    # 1.2 多模态视觉任务专用指引 (消除模型“我先看一眼”的口头停顿)
    # 只在挂了图片时注入；与模型 ID 无关（T9：4 个别名必须走同一条路径）。
    if is_vision:
        has_system = False
        for m in normalized_messages:
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + "\n" + _FROZEN_VISION_PROMPT
                has_system = True
                break
        if not has_system:
            normalized_messages.insert(
                0, {"role": "system", "content": _FROZEN_VISION_PROMPT.strip()}
            )

    # 1.3 核心提示词注入 (锚定在 System 消息最末端)
    cot_prompt = _CANONICAL_SYSTEM_PROMPT

    last_system_idx = -1
    for idx, m in enumerate(normalized_messages):
        if m.get("role") == "system":
            last_system_idx = idx
    if last_system_idx != -1:
        normalized_messages[last_system_idx]["content"] = (
            str(normalized_messages[last_system_idx].get("content", "")).rstrip()
            + "\n\n"
            + cot_prompt.strip()
        )
    else:
        normalized_messages.insert(0, {"role": "system", "content": cot_prompt.strip()})

    # 2.5 推理强度解析 (Reasoning Effort & Director)
    raw_effort = body.get("reasoning_effort")
    if raw_effort is None and isinstance(body.get("reasoning"), dict):
        raw_effort = body.get("reasoning", {}).get("effort")
    effort = ReasoningDirector.normalize_effort(raw_effort, client_model=_client_model)

    # 1. 深度思考 (Thinking)：
    # 若客户端显式传递了 thinking 参数，最高优先级遵从客户端设定；
    # 若 effort 为 off/none，强制关闭深度思考以实现极速免思考直出；
    # 针对 -act 后缀模型或 -nothink 后缀，默认关闭思考；
    # 其余模型保持默认开启深度思考。
    user_thinking = body.get("thinking")
    if user_thinking is not None:
        thinking_enabled = bool(user_thinking)
    elif ReasoningDirector.is_thinking_disabled(effort):
        thinking_enabled = False
    elif "-act" in model_lower or "-nothink" in model_lower or "-nothinking" in model_lower:
        thinking_enabled = False
    else:
        thinking_enabled = True

    # 2. 联网搜索 (Search)：在 Agent 工具调用与多模态下默认关闭，消除官方连接中断 (curl 56)
    user_search = body.get("search")
    if user_search is None:
        user_search = body.get("web_search")
    if user_search is not None:
        search_enabled = bool(user_search)
    else:
        search_enabled = False

    # ── 3. 推理档位 (T9 第三步：清理已死的 model_type 路由语义) ──
    #
    # 【收敛前】这里把 `model_type` 当"模型选择"用：
    #     pro → "expert"，flash → "default"，有图 → "vision"
    # 而**实测**（清单 T9）证明上游对这三个值**完全无视**：
    #     default / expert / vision → 上游一律返回 `conversation_mode: "DEFAULT"`，
    #     正文 **sha1 完全相同**；只有**非法值**才 422。
    # 也就是说这段 `if/elif/else` 对上游没有任何效果，只是让代码"看起来"
    # 在做模型路由。收敛后 4 个 ID 已归一到同一模型，连这层伪装都没必要了。
    #
    # 【收敛后】仍保留这个参数，但语义被明确降级为**兼容档位**：
    #   · 它**不是**模型选择，改它不会换模型（上游不理会）
    #   · 它**必须**继续发送，且取值必须落在白名单内
    #
    # ⛔ 为什么不能删掉它、也不能传非法值：
    #     上游**会校验这个字段的合法性**，"非法值才 422"（清单 T9 实测）。
    #     删掉字段或传到白名单外 = 把**每一个**请求打成 422 故障。
    #     `tests/test_model_convergence.py::TestModelTypeWireContract`
    #     有用例钉死"字段存在 + 取值合法"，防止后人"清理死代码"时踩雷。
    #
    # ★ `model_type` **恒定**，与"有没有图"无关（2026-09-11 真图实测改写）。
    #
    # T9 曾经在这里按 `has_images` 推档位（有图 → vision）。那个推断已被实测证伪：
    # 把 `vision` 发上去，**上游回显把它改写成 `expert`**；而不切档位、
    # 直接带 `ref_file_ids`，模型照样准确描述图片内容（左红右蓝的 PNG 答对了）。
    # 公开信息也一致：快速 / 专家 / 识图三个模式**已合并成一个**
    # （V4.1 Flash，原生多模态，不用再手动切换）。
    #
    # 所以这里只剩一件事：**有图时关掉联网搜索**（原因见上面 598 行那段，
    # 图片 + 联网会触发上游连接中断 curl 56）。
    model_type = upstream_model_type()
    if is_vision:
        search_enabled = False
    # ⚠️ 不要在这里动 `thinking_enabled`。
    #    它的值在 1. 段由 `body["thinking"]` 与 `-nothink` 后缀共同决定，
    #    是**客户端可控的行为开关**，与图片无关。
    #    收敛前那两行 `thinking_enabled = True` 会把客户端的 `-nothink`
    #    直接吃掉——现在删除。

    # 收尾自检：值必须落在上游白名单内，否则宁可在网关侧炸掉，
    # 也不要发出一个必然 422 的请求（那样故障点会跑到上游，更难定位）。
    assert model_type in UPSTREAM_MODEL_TYPE_VALUES, (
        f"model_type={model_type!r} 不在上游白名单 {sorted(UPSTREAM_MODEL_TYPE_VALUES)} 内，"
        f"上游会返回 422。请修 `upstream_model_type()`，不要绕过这个断言。"
    )

    # T1: 会话身份解析
    #  1) 客户端显式给了会话标识就必须用（比内容指纹可靠得多）
    #  2) 否则按"首条用户消息"分桶，再在桶内用消息前缀唯一定位
    #  3) 定位不唯一 → 判定为新会话。宁可多建一次上游会话，
    #     也绝不把 A 的问题发进 B 的上下文里（那是答案串线，不是效率问题）
    #
    # ⚠️ `allow_reuse` 必须在这里就算出来并传下去：本请求若不复用，
    # resolve 交回的**不能**是已有会话的 key —— 否则紧接着的
    # register_session 会把那个会话的绑定覆盖掉，串线又回来了。
    _client_sid, sid_src = extract_client_session_id(request.headers, body)
    _has_prior_turns_early = any(m.get("role") in ("assistant", "tool") for m in messages)
    conv_key, conv_src = session_tracker.resolve_conversation(
        normalized_messages,
        model=model,
        custom_id=_client_sid,
        allow_reuse=bool(_client_sid) or _has_prior_turns_early,
        scope_key=identity.get("key_id") or "",
    )
    # `sid_src` 只回答"标识来自哪个头"，`conv_src` 回答"这次的会话身份是怎么定的"
    # —— 后者才是有用的那一个（cid / cid-prefix-changed / prefix / new / ambiguous-new）。
    # 旧写法在有显式 ID 时用 `sid_src` **整条覆盖** `conv_src`，于是 A1 特意返回的
    # `cid-prefix-changed` 永远进不了日志：改了也看不见，观测性等于零。
    # 两者都要，所以拼起来而不是覆盖。
    if _client_sid:
        conv_src = f"{conv_src}({sid_src})"
    cached_session = session_tracker.get_session(conv_key)
    preferred_account = cached_session.account_label if cached_session else None
    avoid_account = None
    has_bound_files = bool(cached_session and cached_session.file_ids)

    # ★ 压缩即换号（Session Rolling）：
    # 当检测到显式会话前缀发生变化（上下文压缩/新排查分支），上游本来就必须新建会话（旧会话无法缓存命中）。
    # 只要会话未绑定不可跨号的图片资产，主动解除对老账号的强制粘性，让调度器优先选择用户名下其他空闲账号，
    # 实现多账号交替轮换做功，从根源消除单个账号被连续狂薅 150+ 轮的风控特征。
    if (conv_src.startswith("cid-prefix-changed") or conv_src.startswith("cid-forked") or conv_src.startswith("prefix-forked")) and not has_bound_files:
        avoid_account = preferred_account
        preferred_account = None
        print(
            f"[SessionTree] 🔄 显式会话 {_client_sid!r} 上下文压缩/前缀变更且无图片绑定，"
            f"主动解除老账号 {avoid_account} 的强制粘性，触发平滑轮换",
            flush=True,
        )

    # 3. 从账号池调度可用账号额度
    # 阶段计时（延迟归因）：实测同一请求直连上游 TTFB≈0.17s、经网关≈1.2s，
    # 网关自身开销主要在 session_create≈180ms，其余为上游首字生成。
    #
    # 【快速失败】不再静默阻塞等待（旧行为最多等 30s）。
    # 原因：客户端看门狗约 50s，排队接近它就会被掐断（表现为"卡住几十秒然后报错"）；
    # 且 DSH 本身就是 Agent 调度器，比网关更清楚该让哪个子代理等待。
    # 所以改为：容量不足 → 立即 429 + Retry-After，把排队决策交回客户端。
    # T25: 配额闸门 —— 必须放在 acquire_account **之前**。
    # 若放在后面，超配额的请求会先占住一个账号并发额度再被拒，
    # 于是"被限流的用户"反而在挤占别人的容量，配额就从保护变成了帮凶。
    _quota_denied = key_usage_engine.check_quota(identity)
    if _quota_denied:
        _reason, _retry_after = _quota_denied
        key_usage_engine.record(
            key_id=identity.get("key_id", ""),
            label=identity.get("log_label", ""),
            rejected=True,
        )
        print(f"[Gateway-Quota] key={identity.get('log_label')} {_reason}", flush=True)
        metrics_engine.record(
            endpoint="/v1/chat/completions",
            model=model,
            status_code=429,
            duration_ms=(time.time() - req_start_time) * 1000,
            error="quota_exceeded",
            account=identity.get("log_label", ""),
            # 被配额挡下不算消耗 —— 否则每次重试都推高计数，用户永久触顶
            attribute_to_key=False,
        )
        raise HTTPException(
            status_code=429,
            detail=CLIENT_MSG_DAILY_QUOTA,
            headers={"Retry-After": str(_retry_after), "X-Quota-Exceeded": "daily"},
        )

    _phase_t0 = time.time()
    account = await account_pool.acquire_for_identity(
        identity,
        preferred_label=preferred_account,
        timeout=30.0,
        immediate=True,
        avoid_label=avoid_account,
        refresh=False,
    )
    _phase_acquire_ms = (time.time() - _phase_t0) * 1000
    if not account:
        deny = account_pool.last_deny_reason
        my_labels = list(identity.get("account_labels") or [])
        # 区分两种失败：
        #  · own_account_at_capacity                        —— 有账号但并发额度用尽 / 短冷却中（可 429 退避重试）
        #  · no_account_assigned / all_assigned_accounts_invalid / no_active_account —— 无可用做功账号（必须 503，绝不误报 429 引起空转风暴）
        if deny in ("no_account_assigned", "all_assigned_accounts_invalid", "no_active_account"):
            logger.error(
                f"[Audit-AccountPool] Key '{identity.get('log_label')}' (id={identity.get('key_id')}) "
                f"无可用做功账号 (reason={deny})，拒绝借用他池"
            )
            metrics_engine.record(
                endpoint="/v1/chat/completions", model=model, status_code=503,
                duration_ms=(time.time() - req_start_time) * 1000,
                error=deny, account=identity.get("log_label", ""),
                attribute_to_key=False,
            )
            raise HTTPException(status_code=503, detail=CLIENT_MSG_SERVICE_UNAVAILABLE)

        active = [a for a in account_pool._accounts.values()
                  if a.is_valid and a.status == "ACTIVE"]
        if active:
            # Retry-After 取账号池的实测估算（典型占用 ~6s，夹在 [2,15]s）。
            # 初版写死 1 秒 → 客户端 1 秒后打回，容量仍满载 → 空转风暴。
            retry_after = int(max(2.0, min(15.0, account_pool.last_wait_hint or 6.0)))
            if my_labels:
                scope = f"本 Key 名下 {len(my_labels)} 个账号"
            else:
                scope = f"公共池 {len(active)} 个可用账号"
            detail = (
                f"账号并发额度已用尽（每账号上限 {MAX_CONCURRENT_PER_ACCOUNT}，{scope}均已满载）。"
                f"请 {retry_after}s 后重试，或减少并行任务数（子代理并发上限 = 你名下账号数 × {MAX_CONCURRENT_PER_ACCOUNT}）。"
            )
            # 粘性会话满载时，上面的模板会误导：池里可能还有几十个空闲账号，
            # 只是**这一个会话绑定的号**满了。用账号池给出的细节覆盖它。
            if account_pool.last_deny_detail:
                detail = (
                    f"账号并发额度已用尽（每账号上限 {MAX_CONCURRENT_PER_ACCOUNT}）。"
                    f"{account_pool.last_deny_detail}。请 {retry_after}s 后重试。"
                )
            print(
                f"[Gateway-OverCapacity] key={identity.get('log_label')} {detail} "
                f"本次 acquire 仅耗时 {_phase_acquire_ms:.0f}ms",
                flush=True,
            )
            metrics_engine.record(
                endpoint="/v1/chat/completions",
                model=model,
                status_code=429,
                duration_ms=(time.time() - req_start_time) * 1000,
                error="all_accounts_at_capacity",
                account=identity.get("log_label", ""),
                # 容量紧张是网关侧问题，不该吃掉用户的日配额
                attribute_to_key=False,
            )
            raise HTTPException(
                status_code=429,
                detail=CLIENT_MSG_CONCURRENCY_LIMIT,
                headers={"Retry-After": str(retry_after)},
            )
        metrics_engine.record(
            endpoint="/v1/chat/completions",
            model=model,
            status_code=503,
            duration_ms=(time.time() - req_start_time) * 1000,
            error="No available accounts",
            attribute_to_key=False,
        )
        logger.error(f"[Audit-AccountPool] Key '{identity.get('log_label')}' 请求时系统所有可用账号均忙碌或已被熔断")
        raise HTTPException(status_code=503, detail=CLIENT_MSG_SERVICE_UNAVAILABLE)

    # 4. 如果有图片，在内存中直接上传至 DeepSeek 官方文件服务
    # T5-A：改为**有界并发 + 统一超时预算**。旧的串行 for 循环下
    # 5 张图各 45 秒 → 最坏 225 秒，必然被客户端看门狗掐断。
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
                    proxy=_get_proxy(account),
                )
                upload_success = True
                break
            except DeepSeekAuthError as auth_err:
                logger.warning(
                    f"[OpenAI-Routes] 账号 {account.account_label} 图片上传凭证失效: {auth_err}，尝试免密自愈与容灾..."
                )
                if await account_pool.recover_or_release_on_auth_error(
                    account, caller_info=caller_info, stage="图片上传"
                ):
                    continue
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=_get_proxy(account),
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    last_upload_err = auth_err
                    break
            except UnsupportedFileTypeError as ufte:
                logger.warning(
                    f"[OpenAI-Routes] 账号 {account.account_label} 检测到客户端上传了不受支持的文件格式: {ufte}，安全释放账号且不熔断"
                )
                account_pool.release_account(account, is_muted=False)
                raise HTTPException(
                    status_code=400,
                    detail=f"文件/图片格式不受 DeepSeek 支持: {ufte}",
                )
            except DeepSeekMutedError as muted_err:
                logger.warning(
                    f"[OpenAI-Routes] 账号 {account.account_label} 图片上传被官方明确禁言: {muted_err} (解禁: {muted_err.mute_until})，进行账号隔离与换号容灾..."
                )
                last_upload_err = muted_err
                account_pool.release_account(account, is_muted=True, mute_until=muted_err.mute_until, caller_info=caller_info)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=_get_proxy(account),
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break
            except (DeepSeekQuotaExceededError, DeepSeekUploadRateLimitError) as quota_err:
                if isinstance(quota_err, DeepSeekQuotaExceededError):
                    account.apply_quota_cooldown(reset_at=quota_err.reset_at, reason="vision_upload_quota_exceeded")
                else:
                    account.apply_protective_cooldown(float(quota_err.retry_after or 30), reason="vision_upload_rate_limit")
                logger.warning(
                    f"[OpenAI-Routes] 账号 {account.account_label} 图片上传受限 ({quota_err})，已冷却并尝试换号容灾..."
                )
                last_upload_err = quota_err
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=_get_proxy(account),
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break
            except (TimeoutError, asyncio.TimeoutError) as te:
                logger.warning(
                    f"[OpenAI-Routes] 账号 {account.account_label} 图片上传/PoW 超时: {te}，安全释放账号（严禁误熔断为封号），尝试换号容灾 (attempt {_img_attempt + 1}/3)..."
                )
                last_upload_err = te
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=_get_proxy(account),
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break
            except Exception as e:
                logger.warning(
                    f"[OpenAI-Routes] 账号 {account.account_label} 图片上传异常 (attempt {_img_attempt + 1}/3): {e}，安全释放（不设为禁言）并换号容灾..."
                )
                last_upload_err = e
                account_pool.release_account(account, is_muted=False)
                failover_acc = await account_pool.acquire_for_identity(
                    identity,
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    avoid_label=account.account_label,
                    avoid_proxy=_get_proxy(account),
                    refresh=True,
                )
                if failover_acc:
                    account = failover_acc
                    continue
                else:
                    break

        if not upload_success:
            logger.error(f"[OpenAI-Routes] Image upload completely failed: {last_upload_err}", exc_info=True)
            account_pool.release_account(account)
            metrics_engine.record(
                endpoint="/v1/chat/completions",
                model=model,
                status_code=502,
                duration_ms=(time.time() - req_start_time) * 1000,
                account=account.account_label,
                error=f"Image upload failed: {last_upload_err}",
            )
            logger.error(f"[Audit-Vision] 图片上传官方服务失败: {last_upload_err}")
            raise HTTPException(status_code=502, detail=CLIENT_MSG_MEDIA_PROCESSING_FAILED)

    # 5. 确定当前会话上下文并维护多模态文件资产连续性
    deepseek_session_id = None
    parent_message_id = None

    is_compatible_session = False
    has_prior_turns = any(m.get("role") in ("assistant", "tool") for m in messages)

    # 复用闸门说明（本轮修复）：
    # 旧实现要求 has_prior_turns=True，即客户端**必须把 assistant/tool 消息回灌**，
    # 否则即使 conv_key 命中也会新建 web 会话。
    # 实测（2026-09-10）：
    #   · 客户端发完整历史（含 assistant）      → reuse_session=True ✅
    #   · 客户端只发 system + 当前 user         → reuse_session=False（每轮新建）
    # 后者代价：每轮多付约 180ms 的 create_chat_session，且上游 web 会话历史恒为空。
    # 这是一个**隐藏的协议依赖**：客户端一旦改变发送形态（例如为省 token 只发最新一轮），
    # 会话复用会静默失效——不报错、只是每轮略慢，极难察觉。
    # 现改为：只要客户端提供了**显式会话标识**（x-session-id / conversation-id → custom_id），
    # 就认为会话身份明确，不再依赖角色构成即可复用；无显式标识时才退回旧判定，
    # 避免仅凭内容哈希误复用（那正是历史串线的来源）。
    _has_explicit_session_id = bool(_client_sid)
    _reuse_allowed = _has_explicit_session_id or has_prior_turns

    # T16/B1 被动指标：会话身份相关的比率（歧义错位 / 换号 / 压缩重建）。
    # 位置是刻意的 —— 这里"账号已定、复用未决"，是**唯一**能同时看到
    # "绑定的号"和"这次拿到的号"的时刻，`account_changed` 只有两边都有才成立。
    # 纯观测，不参与任何判定。
    passive_metrics.note_session_resolution(
        conv_src, cached_session, account.account_label, _reuse_allowed,
    )

    # 本请求是否新建了上游会话（用于零产出时回收，见 T13）
    _session_created_by_this_request = False

    if (_reuse_allowed and cached_session
            and cached_session.account_label == account.account_label
            # ★ A1：还必须**确实有一个上游会话可复用**。
            # `resolve_conversation` 在"显式 ID 命中但消息前缀已变"时会把
            # `deepseek_session_id` 清空（换会话但不换号）—— 那种情况下
            # 这里必须放行去新建会话，否则又去复用那个已经填满的会话、再撞墙。
            and cached_session.deepseek_session_id):
        cached_type = getattr(cached_session, "model_type", "default")
        # T9：收敛后这里**不再需要**放行任何"跨模型"组合。
        #
        # 收敛前 `model` 被拼进 conv_key（session_tree: `f"{model}:cid:{cid}"`），
        # 不同模型各自落在不同的 conv_key 上，所以能走到这里的 cached_session
        # 必然来自**同一个模型**；那时的 `cached_type == model_type` 判断
        # 实际上是在比对"同一模型下图片有无"导致的档位差异。
        #
        # 现在 4 个 ID 已归一到同一个 `model`，所有别名落在**同一个 conv_key**，
        # 于是 `cached_type` 与 `model_type` 的差异只剩"上一轮有没有图"。
        # 主模型原生融合视觉（实测 vision 与 expert 在上游同一条通道），
        # 因此**任何档位组合都可以安全复用同一会话**——纯文本追问继承历史图片
        # 正是 828 行那段逻辑有意提供的功能。
        #
        # 保留 `cached_type` 的读取只为日志可读性；判定本身恒为 True。
        _ = cached_type  # 仅用于上方说明的语义对照，不参与判定
        is_compatible_session = True

    if is_compatible_session:
        # 同一账号长对话复用 (无论纯文本还是多模态视觉，全程维持会话树与视觉记忆)
        deepseek_session_id = cached_session.deepseek_session_id
        parent_message_id = extract_explicit_parent_id(request.headers, body) or cached_session.last_message_id
        prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=True, tools=tools, reasoning_effort=effort)
        if ref_file_ids:
            # 本轮有新增图片，追加到会话资产库
            cached_session.add_file_ids(ref_file_ids)
        else:
            # 本轮为纯文本追问，自动继承该会话已看过的所有历史图片
            ref_file_ids = cached_session.get_file_ids()
    else:
        # 新对话或因模型类型不兼容重新建会话
        _sess_t0 = time.time()
        loop = asyncio.get_event_loop()
        deepseek_session_id = None

        for _sess_attempt in range(3):
            _last_acc_label = account.account_label
            try:
                deepseek_session_id = await loop.run_in_executor(
                    None, DeepSeekClient.create_chat_session, account.token, account.cookie, _get_proxy(account)
                )
            except DeepSeekAuthError as auth_err:
                print(
                    f"[OpenAI-Routes] 账号 {account.account_label} 凭证被上游拒绝: {auth_err}，"
                    f"尝试通过受信任设备免密自愈刷新 Token...",
                    flush=True,
                )
                relogin_ok = False
                if getattr(account, "password", None):
                    relogin_ok = await account.try_relogin()
                if relogin_ok:
                    print(
                        f"[OpenAI-Routes] ✅ 账号 {account.account_label} 免密自愈成功，"
                        f"使用新 Token 重新尝试创建会话...",
                        flush=True,
                    )
                    try:
                        deepseek_session_id = await loop.run_in_executor(
                            None, DeepSeekClient.create_chat_session, account.token, account.cookie, _get_proxy(account)
                        )
                    except Exception as e2:
                        print(f"[OpenAI-Routes] 自愈后建会话再次失败: {e2}", flush=True)
                        deepseek_session_id = None

                if not deepseek_session_id:
                    from app.engine import trusted_relogin_engine as _tre
                    err_info = getattr(account, "last_relogin_error", None) or {}
                    err_biz = err_info.get("biz_code")
                    err_kind = err_info.get("kind")
                    err_str = str(err_info.get("error") or "")
                    if err_biz == 14 or "MFA_VERIFICATION_CODE_REQUIRED" in err_str:
                        print(
                            f"[OpenAI-Routes] ⚠️ 账号 {account.account_label} 触发官方邮件 MFA 校验(biz_code=14)，标记为 MFA_REQUIRED 并退回容灾...",
                            flush=True,
                        )
                        account_pool.mark_mfa_required(account, caller_info=caller_info, stage="建会话")
                    elif _tre.is_ip_restricted() or err_biz in (11, 40029) or err_kind == "network_error":
                        # 官方 40029/11 IP/设备风控或网络异常：冷却 15 分钟，绝不做 30 天永久误封
                        print(
                            f"[OpenAI-Routes] ⚠️ 官方限流/设备风控或网络波动 (biz={err_biz}, kind={err_kind})：账号 {account.account_label} "
                            f"进入 15 分钟冷却并容灾换号...",
                            flush=True,
                        )
                        account_pool.release_account(
                            account,
                            is_muted=True,
                            mute_until=int(time.time()) + 900,
                            permanent=False,
                            caller_info=caller_info,
                        )
                    else:
                        print(
                            f"[OpenAI-Routes] 账号 {account.account_label} 凭证失效且无法自愈，"
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
                        avoid_proxy=_get_proxy(account),
                        refresh=True,
                    )
                    if failover_acc:
                        account = failover_acc
                        if detected_images:
                            try:
                                ref_file_ids = await VisionPipeline.upload_images(
                                    token=account.token,
                                    images=detected_images,
                                    cookie=account.cookie,
                                    proxy=_get_proxy(account),
                                )
                            except Exception as ex_img:
                                print(f"[OpenAI-Routes] 换号后图片重传失败: {ex_img}", flush=True)
                        continue
                    else:
                        metrics_engine.record(
                            endpoint="/v1/chat/completions",
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
                logger.error(
                    f"[OpenAI-Routes] DeepSeek session creation failed for account {account.account_label}, "
                    f"尝试容灾换号 (attempt {_sess_attempt + 1}/3)..."
                )
                account_pool.release_account(account)
                failover_acc = await account_pool.acquire_account(
                    preferred_label=None,
                    timeout=10.0,
                    immediate=True,
                    owned_labels=identity.get("account_labels") or [],
                    pool_fallback=bool(identity.get("pool_fallback")),
                    avoid_label=account.account_label,
                    avoid_proxy=_get_proxy(account),
                    is_vip=bool(identity.get("is_vip")),
                )
                if failover_acc:
                    account = failover_acc
                    if detected_images:
                        try:
                            ref_file_ids = await VisionPipeline.upload_images(
                                token=account.token,
                                images=detected_images,
                                cookie=account.cookie,
                                proxy=_get_proxy(account),
                            )
                        except Exception as ex_img:
                            print(f"[OpenAI-Routes] 换号后图片重传失败: {ex_img}", flush=True)
                    continue
                else:
                    metrics_engine.record(
                        endpoint="/v1/chat/completions",
                        model=model,
                        status_code=502,
                        duration_ms=(time.time() - req_start_time) * 1000,
                        account=_last_acc_label,
                        error="Session creation failed",
                    )
                    logger.error(f"[Audit-UpstreamSession] 会话创建失败且无可用备用账号")
                    raise HTTPException(status_code=502, detail=CLIENT_MSG_UPSTREAM_SESSION_FAILED)

            break

        prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
        new_ctx = session_tracker.register_session(
            conv_key, account.account_label, deepseek_session_id,
            model_type=model_type,
            # T1: 记下消息前缀指纹，供后续轮次唯一定位（见 resolve_conversation）
            prefix=session_tracker._prefix_signatures(normalized_messages),
        )
        # 标记：本次请求**新建**了上游会话。若最终零产出，则需回收它，
        # 否则每次失败/限流都会在上游留下一个空会话
        # （实测某测试号因此累积了上百个会话，见 T13）。
        _session_created_by_this_request = True
        if ref_file_ids:
            new_ctx.add_file_ids(ref_file_ids)

    # T16/B1：复用闸门的最终结论（复用率的分母，也是 A1 的验收口径）
    passive_metrics.note_session_reuse(bool(is_compatible_session), conv_src)
    try:
        from app.engine.drop_probe import update_context
        update_context(
            account=account.account_label,
            token=account.token,
            token_updated_at=float(getattr(account, "token_updated_at", 0) or 0.0),
            caller_owner=str(identity.get("owner") or ""),
            caller_key=str(identity.get("key_name") or ""),
            model=str(model or ""),
            session_id=str(deepseek_session_id or ""),
            reuse_session=bool(is_compatible_session),
            in_flight=getattr(account, "in_flight", None),
        )
    except Exception:
        pass

    # 阶段计时汇总（延迟归因）：定位网关自身那约 1s 固定开销落在哪一段
    if not is_compatible_session:
        _phase_session_ms = (time.time() - _sess_t0) * 1000
    else:
        _phase_session_ms = 0.0
    print(
        f"[Gateway-Latency] acquire={_phase_acquire_ms:.0f}ms "
        f"session_create={_phase_session_ms:.0f}ms "
        f"reuse_session={is_compatible_session} "
        # T1: 会话身份是怎么定的。走 hash/桶前缀说明客户端没给显式标识，
        # 若这里长期只有 ambiguous-new，说明同开头会话很多、复用被安全地放弃了
        f"conv_src={conv_src} "
        f"preflight_to_first_event=见下方 TTFT "
        f"model={model} account={account.account_label}",
        flush=True,
    )

    # 只要存在图片资产就关闭联网搜索（纯文本追问会从会话继承历史 file_ids，
    # 所以"本轮没传图"也可能走到这里）。
    # ★ 这里**不再切 `model_type`**：实测上游把 `vision` 改写成 `expert`，
    #   不切档位直接带 `ref_file_ids` 图片照样被看见 ——
    #   "识图"不是独立模式，而是同一个模型的固有能力。
    if ref_file_ids:
        search_enabled = False
        ref_file_ids = list(dict.fromkeys(ref_file_ids))

    prompt_tokens = TokenEstimator.estimate_messages_tokens(normalized_messages, tools=tools)
    if is_compatible_session:
        delta_tokens = TokenEstimator.estimate_text_tokens(prompt)
        cached_tokens = max(0, prompt_tokens - delta_tokens)
        uncached_tokens = max(0, prompt_tokens - cached_tokens)
    else:
        cached_tokens = 0
        uncached_tokens = prompt_tokens

    def _build_openai_usage(c_toks: int, r_toks: int = 0) -> Dict[str, Any]:
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": c_toks,
            "total_tokens": prompt_tokens + c_toks,
            "prompt_tokens_details": {
                "cached_tokens": cached_tokens,
            },
            "prompt_cache_hit_tokens": cached_tokens,
            "prompt_cache_miss_tokens": uncached_tokens,
            "completion_tokens_details": {
                "reasoning_tokens": r_toks,
            },
        }

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_time = int(time.time())

    # 6. 执行流式调用
    async def sse_stream_generator():
        nonlocal account, deepseek_session_id, parent_message_id, _session_created_by_this_request, prompt, ref_file_ids
        is_muted_flag = False
        # T12: 上游若直接给了禁言截止时间就带下去，账号池就不必瞎猜退避。
        # 旧实现这一路完全没传 mute_until，导致账号池只能写 None →
        # check_mute_expiry 永远不满足唤醒条件 → 账号被熔断后永不恢复。
        mute_until_hint = None
        # T12: 凭证失效是**永久性**的，等多久都不会好，不能按临时禁言处理
        permanent_invalid = False
        final_message_id = None
        status_code = 200
        safety_tripped_reason: Optional[str] = None
        safety_guard = SafetyOutputGuard(
            safety_repeat_limit=int(getattr(config_manager.config, "ds_safety_repeat_limit", 3) or 3)
        )
        err_msg = None

        def _trip_safety_if_needed(txt: str) -> bool:
            nonlocal safety_tripped_reason
            if not txt or not safety_guard.feed(txt):
                return False
            safety_tripped_reason = safety_guard.trip_reason
            print(
                f"[Safety-CircuitBreaker] 🛡️ 拦截到异常安全标签连续复读 ({safety_tripped_reason}): "
                f"account={account.account_label}, key={caller_info.get('key_name')}, sid={deepseek_session_id}",
                flush=True,
            )
            if deepseek_session_id:
                threading.Thread(
                    target=DeepSeekClient.stop_stream,
                    kwargs={
                        "token": account.token,
                        "session_id": deepseek_session_id,
                        "message_id": final_message_id,
                        "cookie": account.cookie,
                        "proxy": _get_proxy(account),
                    },
                    daemon=True,
                ).start()
            try:
                session_tracker.reset_upstream_session(conv_key, reason="ds_safety_loop")
            except Exception:
                pass
            return True

        def _build_safety_stop_chunk():
            return {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        failover_done = False
        account_released_by_failover = False
        completion_tokens = 0
        full_model_output = []
        full_thinking_output = []
        streaming_parser = StreamingToolParser(tools) if tools else None
        captured_tool_calls = []
        emitted_tool_count = 0
        emitted_text_count = 0
        pending_text_chunks = []
        was_aborted = False

        try:
            print(f"[DEBUG-STREAM] session_id={deepseek_session_id}, parent={parent_message_id}, model_type={model_type}, ref_files={ref_file_ids}, prompt_len={len(prompt)}", flush=True)
            print(f"[DEBUG-STREAM-PROMPT] {prompt[:400]!r}", flush=True)

            # ── 首事件预检 (Pre-flight Gate) ────────────────────────────────
            # 目的：上游在「单账号并发超限 / 突发限流」时会返回
            #   HTTP 200 + 空正文（实测 0.19s / 281B）或一个错误体，
            # 而旧实现直接把它当「正常空回答」转发 → 客户端判定模型没说话 →
            # 触发客户端重试 + 网关「死磕自愈循环」→ 最终吐出【网关安全拦截】。
            # 由于 StreamingResponse 已发出 200 头，之后再改状态码是无效的，
            # 所以必须在返回响应之前拿到第一个事件，才能把限流转成真正的 429。
            upstream_aiter = DeepSeekClient.stream_chat(
                token=account.token,
                session_id=deepseek_session_id,
                prompt=prompt,
                parent_message_id=parent_message_id,
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
                model_type=model_type,
                ref_file_ids=ref_file_ids,
                cookie=account.cookie,
                proxy=_get_proxy(account),
            ).__aiter__()

            preflight_events, substantive_event, first_event, first_exc, pf_elapsed, decision = await _fetch_preflight(upstream_aiter)
            # T16/B1：上游分类结论进被动计数器（限流率 / 超限率 / 安全拦截率）
            passive_metrics.note_preflight(decision)

            # ── 上下文窗口耗尽：终态错误，需让会话可自愈 ──────────────
            # 上游原文（实测）：{"type":"error","content":"达到对话长度上限，请开启新对话",
            #                    "clear_response":true}
            # 该错误**无法通过重试恢复**——同一会话已达上限，重试必然再失败。
            # 旧行为：当普通错误 → 502 → 客户端视作服务端故障无限重试，
            # 每次都失败，表现为"任务做到一半就再也写不出来了"（T19 实证）。
            # 现在：清掉会话绑定，使下一轮自动开新会话；并返回明确的 4xx
            # 让客户端知道"这不是故障，而是该换会话了"。
            if decision.kind == "context_exhausted":
                print(
                    f"[Gateway-ContextExhausted] {decision.detail} "
                    f"sid={deepseek_session_id} → 已清除会话绑定以便下一轮重建",
                    flush=True,
                )
                try:
                    session_tracker.delete_session(conv_key)
                except Exception as ex:
                    print(f"[Gateway-ContextExhausted] 清除会话绑定失败: {ex}", flush=True)
                metrics_engine.record(
                    endpoint="/v1/chat/completions",
                    model=model,
                    status_code=400,
                    duration_ms=(time.time() - req_start_time) * 1000,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    account=account.account_label,
                    error=decision.metric_error,
                )
                account_pool.release_account(account)
                raise HTTPException(status_code=400, detail=decision.detail)

            # context_exhausted / session_broken 自愈清理：上游会话失效时释放绑定
            if decision.kind == "session_broken":
                print(
                    f"[Gateway-SessionBroken] {decision.detail} "
                    f"sid={deepseek_session_id} → 已清除会话绑定以便下一轮重建",
                    flush=True,
                )
                try:
                    session_tracker.delete_session(conv_key)
                except Exception as ex:
                    print(f"[Gateway-SessionBroken] 清除会话绑定失败: {ex}", flush=True)
                metrics_engine.record(
                    endpoint="/v1/chat/completions",
                    model=model,
                    status_code=429,
                    duration_ms=(time.time() - req_start_time) * 1000,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    account=account.account_label,
                    error=decision.metric_error,
                )
                account_pool.release_account(account)
                raise HTTPException(
                    status_code=429,
                    detail=decision.detail,
                    headers={"Retry-After": str(decision.retry_after or 1)},
                )

            if decision.kind == "upstream_busy":
                reason = decision.detail or "上游官方服务繁忙/过载"
                print(f"[Gateway-Preflight] ⚠️ {reason} account={account.account_label}（零惩罚，保持健康状态）", flush=True)
                account.apply_protective_cooldown(3.0, reason="preflight_upstream_busy_jitter")

                # ★ 无感容灾换号：下游尚未输出任何数据，且有备用账号可用时，自动尝试调度备用号
                if not failover_done:
                    failover_done = True
                    try:
                        account_pool.release_account(account, is_muted=False, permanent=False)
                    except Exception:
                        pass
                    failover_acc = await account_pool.acquire_for_identity(
                        identity,
                        preferred_label=None,
                        timeout=5.0,
                        immediate=True,
                        avoid_label=account.account_label,
                        avoid_proxy=_get_proxy(account),
                        refresh=True,
                    )
                    if failover_acc:
                        print(
                            f"[Gateway-Busy-Failover] 账号 {account.account_label} 遭遇上游繁忙，"
                            f"自动容灾调度备用账号 {failover_acc.account_label} (proxy: {_get_proxy(failover_acc)}) 接力重试...",
                            flush=True,
                        )
                        account = failover_acc
                        try:
                            deepseek_session_id = await loop.run_in_executor(
                                None, DeepSeekClient.create_chat_session, account.token, account.cookie, _get_proxy(account)
                            )
                        except Exception as e_cs:
                            print(f"[Gateway-Busy-Failover] 备用号建会话失败: {e_cs}", flush=True)
                            deepseek_session_id = None
                        if deepseek_session_id:
                            prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
                            parent_message_id = None
                            _session_created_by_this_request = True
                            session_tracker.register_session(
                                conv_key,
                                account.account_label,
                                deepseek_session_id,
                                prefix=session_tracker._prefix_signatures(normalized_messages),
                            )
                            upstream_aiter = DeepSeekClient.stream_chat(
                                token=account.token,
                                session_id=deepseek_session_id,
                                prompt=prompt,
                                parent_message_id=parent_message_id,
                                thinking_enabled=thinking_enabled,
                                search_enabled=search_enabled,
                                model_type=model_type,
                                ref_file_ids=ref_file_ids,
                                cookie=account.cookie,
                                proxy=_get_proxy(account),
                            ).__aiter__()
                            preflight_events, substantive_event, first_event, first_exc, pf_elapsed, decision = await _fetch_preflight(upstream_aiter)
                            if decision.kind not in ("upstream_busy", "rate_limited", "error"):
                                pass  # 成功自愈恢复！
                            else:
                                account_pool.release_account(account, is_muted=False, permanent=False)
                                account_released_by_failover = True
                                raise HTTPException(
                                    status_code=503,
                                    detail=CLIENT_MSG_UPSTREAM_BUSY,
                                    headers={"Retry-After": "5"},
                                )
                        else:
                            account_pool.release_account(account, is_muted=False, permanent=False)
                            account_released_by_failover = True
                            raise HTTPException(
                                status_code=503,
                                detail=CLIENT_MSG_UPSTREAM_BUSY,
                                headers={"Retry-After": "5"},
                            )
                    else:
                        account_pool.release_account(account, is_muted=False, permanent=False)
                        account_released_by_failover = True
                        raise HTTPException(
                            status_code=503,
                            detail=CLIENT_MSG_UPSTREAM_BUSY,
                            headers={"Retry-After": "5"},
                        )
                else:
                    account_pool.release_account(account, is_muted=False, permanent=False)
                    account_released_by_failover = True
                    raise HTTPException(
                        status_code=503,
                        detail=CLIENT_MSG_UPSTREAM_BUSY,
                        headers={"Retry-After": "5"},
                    )

            if decision.kind == "rate_limited":
                reason = decision.detail or (
                    f"上游未产生任何输出（{pf_elapsed:.2f}s 内空流结束）。"
                    f"最可能原因是该账号并发超限或触发限流，请稍后重试。"
                )
                print(f"[Gateway-Preflight] ⛔ {reason} account={account.account_label}", flush=True)
                prot_cd = getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0)
                apply_preflight_cooldown(account, decision, prot_cd, "preflight_rate_limited")

                # ★ 无感容灾换号：下游尚未输出任何数据，且有备用账号可用时，自动尝试调度备用号
                if not failover_done:
                    failover_done = True
                    try:
                        account_pool.release_account(account)
                    except Exception:
                        pass
                    failover_acc = await account_pool.acquire_for_identity(
                        identity,
                        preferred_label=None,
                        timeout=5.0,
                        immediate=True,
                        avoid_label=account.account_label,
                        avoid_proxy=_get_proxy(account),
                        refresh=True,
                    )
                    if failover_acc:
                        print(
                            f"[Gateway-RateLimit-Failover] 账号 {account.account_label} 触发限流，"
                            f"自动容灾调度备用账号 {failover_acc.account_label} (proxy: {_get_proxy(failover_acc)}) 接力重试...",
                            flush=True,
                        )
                        account = failover_acc
                        try:
                            deepseek_session_id = await loop.run_in_executor(
                                None, DeepSeekClient.create_chat_session, account.token, account.cookie, _get_proxy(account)
                            )
                        except Exception as e_cs:
                            print(f"[Gateway-RateLimit-Failover] 备用号建会话失败: {e_cs}", flush=True)
                            deepseek_session_id = None
                        if deepseek_session_id:
                            prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
                            parent_message_id = None
                            _session_created_by_this_request = True
                            session_tracker.register_session(
                                conv_key,
                                account.account_label,
                                deepseek_session_id,
                                prefix=session_tracker._prefix_signatures(normalized_messages),
                            )
                            upstream_aiter = DeepSeekClient.stream_chat(
                                token=account.token,
                                session_id=deepseek_session_id,
                                prompt=prompt,
                                parent_message_id=parent_message_id,
                                thinking_enabled=thinking_enabled,
                                search_enabled=search_enabled,
                                model_type=model_type,
                                ref_file_ids=ref_file_ids,
                                cookie=account.cookie,
                                proxy=_get_proxy(account),
                            ).__aiter__()
                            preflight_events, substantive_event, first_event, first_exc, pf_elapsed, decision = await _fetch_preflight(upstream_aiter)
                            if decision.kind not in ("rate_limited", "error"):
                                pass  # 成功自愈恢复！
                            else:
                                if decision.kind == "rate_limited":
                                    apply_preflight_cooldown(account, decision, prot_cd, "preflight_rate_limited")
                                account_pool.release_account(account)
                                account_released_by_failover = True
                                raise HTTPException(
                                    status_code=429,
                                    detail=CLIENT_MSG_UPSTREAM_RATE_LIMIT,
                                    headers={"Retry-After": str(decision.retry_after or 3)},
                                )
                        else:
                            account_pool.release_account(account)
                            account_released_by_failover = True
                            raise HTTPException(
                                status_code=429,
                                detail=CLIENT_MSG_UPSTREAM_RATE_LIMIT,
                                headers={"Retry-After": str(decision.retry_after or 3)},
                            )
                    else:
                        metrics_engine.record(
                            endpoint="/v1/chat/completions",
                            model=model,
                            status_code=429,
                            duration_ms=(time.time() - req_start_time) * 1000,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=0,
                            account=account.account_label,
                            error=decision.metric_error,
                        )
                        account_pool.release_account(account)
                        account_released_by_failover = True
                        raise HTTPException(
                            status_code=429,
                            detail=CLIENT_MSG_UPSTREAM_RATE_LIMIT,
                            headers={"Retry-After": str(decision.retry_after or 3)},
                        )
                else:
                    metrics_engine.record(
                        endpoint="/v1/chat/completions",
                        model=model,
                        status_code=429,
                        duration_ms=(time.time() - req_start_time) * 1000,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=0,
                        account=account.account_label,
                        error=decision.metric_error,
                    )
                    account_pool.release_account(account)
                    account_released_by_failover = True
                    raise HTTPException(
                        status_code=429,
                        detail=CLIENT_MSG_UPSTREAM_RATE_LIMIT,
                        headers={"Retry-After": str(decision.retry_after or 3)},
                    )

            if first_exc is not None:
                raise first_exc

            # 首事件本身是错误 → 在响应头发出前判定，必要时给 429 或执行 1 轮自动换号故障转移
            if decision.kind == "error":
                first_code = str(getattr(first_event, "error_code", "") or "")
                first_text = str(getattr(first_event, "content", "") or "")
                is_busy_err = any(h in first_text.lower() or h in first_code.lower() for h in _UPSTREAM_BUSY_HINTS)
                is_ref_file_err = (
                    first_code in ("invalid_ref_file", "biz:9")
                    or "invalid ref file id" in first_text.lower()
                    or "ref file id" in first_text.lower()
                )

                # ★ 关键自愈：文件引用失效（如跨号继承了失效 file-xxx 或附件已过期）
                # 绝非账号凭据或封禁问题，严禁惩罚账号，原地剥离失效文件 ID 并立即重试！
                if is_ref_file_err:
                    is_muted_flag = False
                    permanent_invalid = False
                    if ref_file_ids:
                        print(
                            f"[Gateway-FileRecovery] 命中失效文件引用 ({first_code}: {first_text})，"
                            f"自动剥离失效文件 ID ({ref_file_ids}) 并立即原地自愈重试，绝不扣罚账号！",
                            flush=True,
                        )
                        ref_file_ids = []
                        if cached_session and hasattr(cached_session, "file_ids"):
                            cached_session.file_ids = []
                        upstream_aiter = DeepSeekClient.stream_chat(
                            token=account.token,
                            session_id=deepseek_session_id,
                            prompt=prompt,
                            parent_message_id=parent_message_id,
                            thinking_enabled=thinking_enabled,
                            search_enabled=search_enabled,
                            model_type=model_type,
                            ref_file_ids=[],
                            cookie=account.cookie,
                            proxy=_get_proxy(account),
                        ).__aiter__()
                        preflight_events, substantive_event, first_event, first_exc, pf_elapsed, decision = await _fetch_preflight(upstream_aiter)
                        if decision.kind != "error" and first_exc is None:
                            print(f"[Gateway-FileRecovery] 🎉 剥离失效文件后原地自愈成功！", flush=True)
                        else:
                            first_code = str(getattr(first_event, "error_code", "") or "")
                            first_text = str(getattr(first_event, "content", "") or "")

                if first_code in ("biz:5", "50006"):
                    is_muted_flag = True
                    mute_until_hint = getattr(first_event, "mute_until", None)
                elif first_code in ("account_banned", "USER_IS_BANNED", "40012"):
                    permanent_invalid = True
                elif first_code in ("auth_expired", "40003", "40001", "40002"):
                    permanent_invalid = True
                elif first_code in ("quota_exceeded", "challenge_required", "40303", "40304"):
                    is_muted_flag = False
                    permanent_invalid = False
                    if first_code == "quota_exceeded":
                        account.apply_quota_cooldown(
                            reset_at=getattr(first_event, "mute_until", None),
                            reason="preflight_quota_exceeded",
                        )
                    else:
                        account.apply_protective_cooldown(120.0, reason="preflight_challenge_required")
                elif is_busy_err or is_ref_file_err:
                    is_muted_flag = False
                    permanent_invalid = False

                # ★ 核心无感自愈 (Auto-Failover)：上游账号被禁言、凭证失效、或遇到官方繁忙
                # 调度用户可用池中健康备用号并无缝重试，绝不给客户端抛 502！
                can_failover = (
                    not failover_done
                    and (
                        is_busy_err
                        or first_code in (
                            "biz:5", "50006", "auth_expired", "40003", "40001", "40002",
                            "account_banned", "40012", "rate_limit", "quota_exceeded",
                            "challenge_required", "40303", "40304", "bad_content_type", "waf_blocked"
                        )
                        or first_code.startswith("upstream_busy")
                    )
                )
                if can_failover:
                    failover_done = True
                    err_tag = first_event.error_code
                    relogin_recovered = False

                    relogin_mfa_required = False
                    relogin_cooldown_only = False
                    # 若凭证失效且拥有密码，优先通过受信任免密通道原地自愈
                    if err_tag in ("auth_expired", "40003", "40001", "40002") and getattr(account, "password", None):
                        print(
                            f"[Gateway-Failover] 账号 {account.account_label} 首事件鉴权失效，"
                            f"尝试通过受信任免密通道自动自愈刷新 Token...",
                            flush=True,
                        )
                        relogin_succeeded = await account.try_relogin()
                        if relogin_succeeded:
                            print(
                                f"[Gateway-Failover] ✅ 账号 {account.account_label} 免密自愈成功，"
                                f"重新建立上游会话并原地恢复流...",
                                flush=True,
                            )
                            is_muted_flag = False
                            mute_until_hint = None
                            permanent_invalid = False
                            try:
                                new_sid = await asyncio.to_thread(
                                    DeepSeekClient.create_chat_session,
                                    account.token,
                                    account.cookie,
                                    _get_proxy(account),
                                )
                            except Exception:
                                new_sid = None

                            if new_sid:
                                deepseek_session_id = new_sid
                                parent_message_id = None
                                _session_created_by_this_request = True
                                session_tracker.register_session(
                                    conv_key,
                                    account.account_label,
                                    deepseek_session_id,
                                    prefix=session_tracker._prefix_signatures(normalized_messages),
                                )
                                prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
                                upstream_aiter = DeepSeekClient.stream_chat(
                                    token=account.token,
                                    session_id=deepseek_session_id,
                                    prompt=prompt,
                                    parent_message_id=parent_message_id,
                                    thinking_enabled=thinking_enabled,
                                    search_enabled=search_enabled,
                                    model_type=model_type,
                                    ref_file_ids=ref_file_ids,
                                    cookie=account.cookie,
                                    proxy=_get_proxy(account),
                                ).__aiter__()
                                preflight_events, substantive_event, first_event, first_exc, pf_elapsed, decision = await _fetch_preflight(upstream_aiter)
                                if decision.kind != "error":
                                    relogin_recovered = True
                        else:
                            err_info = getattr(account, "last_relogin_error", None) or {}
                            err_biz = err_info.get("biz_code")
                            err_kind = err_info.get("kind")
                            err_str = str(err_info.get("error") or "")
                            if err_biz == 14 or "MFA_VERIFICATION_CODE_REQUIRED" in err_str:
                                relogin_mfa_required = True
                                is_muted_flag = False
                                mute_until_hint = None
                                permanent_invalid = False
                            elif err_biz in (11, 40029) or err_kind == "network_error":
                                relogin_cooldown_only = True
                                is_muted_flag = True
                                mute_until_hint = int(time.time()) + 900
                                permanent_invalid = False

                    if not relogin_recovered:
                        print(
                            f"[Gateway-Failover] ⚡ 账号 {account.account_label} 首事件返回 {err_tag} "
                            f"({first_event.content})，正在执行 1 轮自动换号故障转移...",
                            flush=True,
                        )
                    prev_proxy = _get_proxy(account)
                    prev_muted = is_muted_flag and not relogin_cooldown_only

                    # 1. 释放/分类处理故障账号（避免 biz_code=14/11/40029 误判为 30 天永久死号）
                    try:
                        if relogin_mfa_required:
                            account_pool.mark_mfa_required(account, caller_info=caller_info, stage="流式容灾")
                        else:
                            account_pool.release_account(
                                account,
                                is_muted=is_muted_flag,
                                mute_until=mute_until_hint,
                                permanent=permanent_invalid,
                                caller_info=caller_info,
                            )
                    except Exception as ex:
                        print(f"[Gateway-Failover] 释放故障账号异常: {ex}", flush=True)

                    # 2. 被动 Mute 监听切 IP：当且仅当被动监听到官方禁言/封号时，才触发出口 IP 切换！
                    if prev_muted:
                        try:
                            from app.api.admin_routes import record_system_alert
                            record_system_alert(
                                account.account_label,
                                "MUTED",
                                f"调用期间上游返回官方禁言封禁，预计解禁时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mute_until_hint)) if mute_until_hint else '未知'}，已自动隔离该号并执行容灾接管",
                                mute_until_hint
                            )
                        except Exception:
                            pass
                        print(
                            f"[Gateway-Failover] 🚨 账号 {account.account_label} 触发上游禁言(MUTE)，"
                            f"被动触发出口 IP 切换防连坐...",
                            flush=True,
                        )
                        try:
                            from app.engine.proxy_manager import proxy_manager
                            await proxy_manager.switch_next_healthy_node()
                        except Exception as e_pm:
                            print(f"[Gateway-Failover] 被动切 IP 失败（降级维持）: {e_pm}", flush=True)

                    # 3. 调度替补账号（严格遵守归属隔离，避开刚失效的账号及出口代理，并重读最新快照）
                    failover_acc = await account_pool.acquire_for_identity(
                        identity,
                        preferred_label=None,
                        timeout=10.0,
                        immediate=True,
                        avoid_label=account.account_label,
                        avoid_proxy=prev_proxy,
                        refresh=True,
                    )

                    # 3. 同 IP 连坐熔断防护：若前序账号是因官方禁言触发，且备用号仍与故障号同属一个出口代理，
                    # 严禁在 0 秒内用相同指纹冲锋，主动熔断以保全备用号资产（仅在配置了多代理池时生效）
                    if failover_acc and prev_muted and should_trigger_anti_cascade(prev_proxy, _get_proxy(failover_acc)):
                        print(
                            f"[Gateway-Failover] ⚠️ 备用账号 {failover_acc.account_label} 与故障禁言账号同属出口代理 {prev_proxy}，"
                            f"触发同 IP 熔断防线，拒绝秒级换号冲锋以防连坐！",
                            flush=True,
                        )
                        account_pool.release_account(failover_acc)
                        failover_acc = None

                    if failover_acc:
                        print(
                            f"[Gateway-Failover] ✅ 成功获取替补账号: {failover_acc.account_label} (proxy: {_get_proxy(failover_acc)})，"
                            f"重新建立上游会话并恢复流...",
                            flush=True,
                        )
                        account = failover_acc
                        is_muted_flag = False
                        mute_until_hint = None
                        permanent_invalid = False
                        # ★ 严防跨账号文件引用泄露：替补账号绝不能携带前序账号的 ref_file_ids！
                        ref_file_ids = []

                        # 在替补账号上创建新会话
                        try:
                            new_sid = await asyncio.to_thread(
                                DeepSeekClient.create_chat_session,
                                account.token,
                                account.cookie,
                                _get_proxy(account),
                            )
                        except Exception:
                            new_sid = None

                        if not new_sid:
                            new_sid = account.session_id

                        if new_sid:
                            deepseek_session_id = new_sid
                            parent_message_id = None
                            _session_created_by_this_request = True
                            session_tracker.register_session(
                                conv_key,
                                account.account_label,
                                deepseek_session_id,
                                prefix=session_tracker._prefix_signatures(normalized_messages),
                            )
                            # 关键自愈：新会话必须重构包含完整系统约束、工具 Schema 与历史摘要的复合 Prompt
                            prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)

                            # 重新向上游发起 stream_chat
                            upstream_aiter = DeepSeekClient.stream_chat(
                                token=account.token,
                                session_id=deepseek_session_id,
                                prompt=prompt,
                                parent_message_id=parent_message_id,
                                thinking_enabled=thinking_enabled,
                                search_enabled=search_enabled,
                                model_type=model_type,
                                ref_file_ids=ref_file_ids,
                                cookie=account.cookie,
                                proxy=_get_proxy(account),
                            ).__aiter__()

                            # 重新执行首事件预检
                            preflight_events, substantive_event, first_event, first_exc, pf_elapsed, decision = await _fetch_preflight(upstream_aiter)

                            # 检查新预检结果
                            if decision.kind == "error":
                                if first_event.error_code == "biz:5":
                                    is_muted_flag = True
                                    mute_until_hint = getattr(first_event, "mute_until", None)
                                elif first_event.error_code == "auth_expired":
                                    permanent_invalid = True
                            elif decision.kind != "error" and first_exc is None:
                                print(f"[Gateway-Failover] 🎉 替补账号 {account.account_label} 预检通过，已无缝接管！", flush=True)
                        else:
                            account_released_by_failover = True
                    else:
                        print(f"[Gateway-Failover] ❌ 未能获取到替补账号（名下无其他空闲可用账号），回退至错误响应", flush=True)
                        account_released_by_failover = True

                if decision.kind == "error":
                    status_code = 502
                    err_msg = f"{first_event.content} ({first_event.error_code})"
                    print(f"[Gateway-Preflight] 上游首事件即为错误: {err_msg}", flush=True)
                    err_chunk = {
                        "error": {
                            "message": first_event.content,
                            "type": "deepseek_error",
                            "code": first_event.error_code,
                        }
                    }
                    yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                    return

            # 预检通过：把所有预检暂存的事件（包括元数据帧与首个实体帧）完整重放继续走处理流程
            remaining_aiter = upstream_aiter
            if preflight_events:
                async def _replay_then_rest(cached_events, rest):
                    for cev in cached_events:
                        yield cev
                    async for ev in rest:
                        yield ev
                remaining_aiter = _replay_then_rest(preflight_events, upstream_aiter)

            # 套上 SSE 保活心跳：上游静默超过间隔时向下游写注释行，避免客户端看门狗掐断
            async for event in _aiter_with_keepalive(remaining_aiter):
                if await request.is_disconnected():
                    print(f"[Gateway-Stream] 客户端 {client_id} 已主动断开连接，立即中止流传输并回收资源", flush=True)
                    disconnect_guard.record_disconnect(client_id)
                    break
                if event is _KEEPALIVE_SENTINEL:
                    yield ": keep-alive\n\n"
                    continue
                if event.event_type != "thinking" and event.event_type != "content":
                    print(f"[DEBUG-STREAM-EV] type={event.event_type}, mid={event.message_id}, code={event.error_code}, content={event.content[:100] if event.content else ''}", flush=True)
                if event.event_type == "error":
                    if event.error_code in ("biz:5", "50006"):
                        is_muted_flag = True
                        mute_until_hint = getattr(event, "mute_until", None)
                    elif event.error_code in ("auth_expired", "account_banned", "40012"):
                        permanent_invalid = True
                    elif event.error_code == "quota_exceeded" or any(h in (f"{event.content} {event.error_code}").lower() for h in _QUOTA_EXCEEDED_HINTS):
                        sec = account.apply_quota_cooldown(
                            reset_at=getattr(event, "mute_until", None),
                            reason="stream_quota_exceeded",
                        )
                        print(f"[Gateway-Stream] 账号 {account.account_label} 在流中触发额度上限，已配额冷却 {sec}s", flush=True)
                    elif event.error_code in ("challenge_required", "40303", "40304") or any(h in (f"{event.content} {event.error_code}").lower() for h in _CHALLENGE_HINTS):
                        account.apply_protective_cooldown(120.0, reason="stream_challenge_required")
                        print(f"[Gateway-Stream] 账号 {account.account_label} 在流中触发行为挑战，已保护性冷却 120s", flush=True)
                    elif event.error_code == "rate_limit" or any(h in (f"{event.content} {event.error_code}").lower() for h in _RATE_LIMIT_HINTS):
                        prot_cd = getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0)
                        account.apply_protective_cooldown(prot_cd, reason="stream_rate_limited")
                        print(f"[Gateway-Stream] 账号 {account.account_label} 在流中触发限流，已实施保护性冷却 {prot_cd}s", flush=True)
                    status_code = 502
                    err_msg = f"{event.content} ({event.error_code})"
                    err_chunk = {
                        "error": {
                            "message": event.content,
                            "type": "deepseek_error",
                            "code": event.error_code,
                        }
                    }
                    yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                    return

                if event.event_type == "message_id" and event.message_id:
                    final_message_id = event.message_id
                    # ⚠️ 幽灵 ID 防护：不在此刻过早提交 session_tracker。
                    # 若后续出现限流或错误（clear_response: true），该 message_id 上游未持久化，
                    # 过早更新会导致下轮请求命中 biz_code: 26 (invalid message id) 死锁。

                if event.event_type == "thinking" and event.content:
                    if _trip_safety_if_needed(event.content):
                        yield f"data: {json.dumps(_build_safety_stop_chunk(), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    full_thinking_output.append(event.content)
                    chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": event.content},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                elif event.event_type == "content" and event.content:
                    if _trip_safety_if_needed(event.content):
                        yield f"data: {json.dumps(_build_safety_stop_chunk(), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    full_model_output.append(event.content)
                    if streaming_parser:
                        for ev_type, data in streaming_parser.feed(event.content):
                            if ev_type == "text" and data:
                                if captured_tool_calls:
                                    # 动态自导自演防火墙：已经发射过至少一个工具调用，若模型输出非空白自然语言正文，判定开始编造假进度
                                    if data.strip():
                                        print(f"[Gateway-Firewall] 检测到工具发射后的后置自然语言（自导自演假进度防护触发），立即平稳截断！text_snippet={data[:50]!r}", flush=True)
                                        if final_message_id:
                                            session_tracker.update_message_id(conv_key, final_message_id)
                                        c_toks = TokenEstimator.estimate_text_tokens("".join(full_model_output))
                                        r_toks = TokenEstimator.estimate_text_tokens("".join(full_thinking_output))
                                        t_toks = sum(TokenEstimator.estimate_tool_call_tokens(tc) for tc in captured_tool_calls)
                                        u_data = _build_openai_usage(c_toks + r_toks + t_toks, r_toks)
                                        stop_chunk = {
                                            "id": chat_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_time,
                                            "model": model,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {},
                                                    "finish_reason": "tool_calls",
                                                }
                                            ],
                                            "usage": u_data,
                                        }
                                        yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
                                        if include_usage:
                                            usage_chunk = {
                                                "id": chat_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_time,
                                                "model": model,
                                                "choices": [],
                                                "usage": u_data,
                                            }
                                            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
                                        yield "data: [DONE]\n\n"
                                        return
                                else:
                                    # 实时流式发射纯文本正文给客户端（即时打字机体验，消灭大段输出卡顿）
                                    chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": data},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                                    emitted_text_count += 1
                            elif ev_type == "tool_call":
                                if ToolBridge.check_duplicate_loop(messages, [data]):
                                    print(f"[Loop-Guard] 触发连续重复调用熔断: {data['function']['name']}，强制平稳收尾！", flush=True)
                                    captured_tool_calls = []
                                    break

                                tool_index = len(captured_tool_calls)
                                captured_tool_calls.append(data)
                                pending_text_chunks = []  # 工具生效，彻底清除文本缓冲区

                                if final_message_id:
                                    session_tracker.update_message_id(conv_key, final_message_id)
                                full_str = "".join(full_model_output)
                                print(f"[Gateway-Extracted-Tools] (Emitted parallel tool #{tool_index}): {data['function']['name']} -> {data['function']['arguments'][:80]}\n", flush=True)
                                passive_metrics.note_tool_emitted(data['function']['name'])

                                tc_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": tool_index,
                                                        "id": data["id"],
                                                        "type": "function",
                                                        "function": data["function"],
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"
                                emitted_tool_count += 1
                                # ★ 并发工具解禁：不再执行 return！保持流式循环，持续收集并发射后续工具！
                    else:
                        chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": event.content},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                elif event.event_type == "done":
                    if event.message_id:
                        final_message_id = event.message_id

                    if streaming_parser:
                        flush_events = streaming_parser.flush()
                        flush_texts = [d for ev, d in flush_events if ev == "text" and d]
                        for ev, d in flush_events:
                            if ev == "tool_call":
                                captured_tool_calls.append(d)

                        full_str = "".join(full_model_output)
                        full_thinking_str = "".join(full_thinking_output)
                        print(f"\n[Gateway-Model-Output] (len={len(full_str)}):\n{full_str}\n", flush=True)
                        if full_thinking_str:
                            print(f"[Gateway-Model-Thinking] (len={len(full_thinking_str)})\n", flush=True)
                        print(f"[Gateway-Extracted-Tools] count={len(captured_tool_calls)}: {[{'name': tc['function']['name'], 'args': tc['function']['arguments'][:60]} for tc in captured_tool_calls]}\n", flush=True)
                        passive_metrics.note_tool_emitted(count=len(captured_tool_calls))

                        if not captured_tool_calls and tools and full_str:
                            # 最终安全网 (Safety Net)：若流式状态机因任何不可抗力未捕获到工具，但模型总输出包含工具，执行全局容错提取
                            _, fallback_tcs = ToolBridge.extract_tool_calls_from_text(full_str, tools)
                            if fallback_tcs:
                                print(f"[Gateway-Safety-Net] 流式状态机遗漏兜底，从正文全文成功提取到 {len(fallback_tcs)} 个工具，启动强制工具发射！", flush=True)
                                # T16/B1：工具格式里最严重的一档 —— 状态机整体漏掉了一个工具调用
                                passive_metrics.note_tool_safety_net(len(fallback_tcs))
                                captured_tool_calls = fallback_tcs

                        # 深度打捞 (Thinking CoT Salvage)：若正文未产生工具调用，但模型在思考链中生成了工具调用
                        if not captured_tool_calls and tools and full_thinking_str:
                            _, thinking_tcs = ToolBridge.extract_tool_calls_from_text(full_thinking_str, tools)
                            if thinking_tcs:
                                print(f"[Gateway-CoT-Salvage] ⚡ 从思考链 (reasoning_content) 中成功打捞到 {len(thinking_tcs)} 个工具调用！直接放行执行！", flush=True)
                                passive_metrics.note_tool_cot_salvage(len(thinking_tcs))
                                captured_tool_calls = thinking_tcs

                        # 深度打捞 (Thinking Content Salvage)：若正文未产生任何输出且未捕获工具，尝试从思考链中智能打捞代码与结论
                        if not captured_tool_calls and not full_str.strip() and full_thinking_str:
                            salvaged_text = ToolBridge.salvage_content_from_thinking(full_thinking_str)
                            if salvaged_text:
                                print(f"[Gateway-CoT-Content-Salvage] ⚡ 从思考链中成功打捞出代码与正文！len={len(salvaged_text)}", flush=True)
                                full_str = salvaged_text
                                full_model_output = [salvaged_text]
                                pending_text_chunks.append(salvaged_text)
                                # 思考链超长说明上游单轮已达极限，重置上游会话 ID 避免下一轮踩进同一死循环
                                session_tracker.reset_upstream_session(conv_key, reason="cot_content_salvage")

                        # 检查正文全文是否包含损坏的工具协议标签（思考链绝不计入可疑判定）
                        is_suspicious = False
                        if not captured_tool_calls and tools and full_str.strip():
                            is_suspicious = ToolBridge.check_zero_tolerance_suspicion(full_str, tools)

                        if not captured_tool_calls and tools and full_str.strip() and final_message_id and is_suspicious:
                            print(f"[Gateway-Reflection] ⚠️ 检测到非标/损坏工具调用标签，启动自愈纠错！sid={deepseek_session_id}, mid={final_message_id}", flush=True)
                            passive_metrics.note_tool_reflection(0, nonstream=False)
                            pending_text_chunks = []
                            flush_texts = []  # 拦截损坏碎片

                            # 快速失败：最坏情况放大到 2 倍，保留一次自纠机会
                            MAX_RETRIES = 1
                            retry_round = 0
                            _tn_list = [t.get("function", t).get("name") for t in (tools or []) if t.get("function", t).get("name")]
                            _tn_hint = f"（当前可用工具：{', '.join(_tn_list)}）\n" if _tn_list else ""
                            while retry_round < MAX_RETRIES and not captured_tool_calls:
                                retry_round += 1
                                print(f"[Gateway-Reflection] 🔄 [第 {retry_round}/{MAX_RETRIES} 轮打回重试] 开始下发纠错指令...", flush=True)
                                retry_prompt = (
                                    "【网关提示：工具调用格式异常，已被打回重试】\n"
                                    "检测到上一轮尝试发起的工具调用未能成功解析（可能存在未闭合标签、JSON语法错误或参数不合法）。\n"
                                    "请重新检查并修正：\n"
                                    "- 若需要调用工具，请严格使用如下标准格式输出：\n"
                                    "  <tool_call>\n"
                                    '  {"name": "工具名", "arguments": {"参数名": "参数值"}}\n'
                                    "  </tool_call>\n"
                                    f"  {_tn_hint}"
                                    "- 若确认不需要调用工具，请直接用清晰自然的语言回复用户。"
                                )

                                retry_parser = StreamingToolParser(tools)
                                retry_final_id = None
                                retry_output = []
                                try:
                                    async for r_event in DeepSeekClient.stream_chat(
                                        token=account.token,
                                        session_id=deepseek_session_id,
                                        prompt=retry_prompt,
                                        parent_message_id=final_message_id,
                                        thinking_enabled=False,
                                        search_enabled=False,
                                        model_type=model_type,
                                        ref_file_ids=ref_file_ids,
                                        cookie=account.cookie,
                                        proxy=_get_proxy(account),
                                    ):
                                        if r_event.event_type == "message_id" and r_event.message_id:
                                            retry_final_id = r_event.message_id
                                        elif r_event.event_type == "content" and r_event.content:
                                            completion_tokens += max(1, len(r_event.content) // 2)
                                            retry_output.append(r_event.content)
                                            for ev_t, d in retry_parser.feed(r_event.content):
                                                if ev_t == "tool_call":
                                                    captured_tool_calls.append(d)
                                                    break
                                            if captured_tool_calls:
                                                break

                                    if retry_final_id:
                                        final_message_id = retry_final_id
                                        session_tracker.update_message_id(conv_key, final_message_id)

                                    if not captured_tool_calls:
                                        r_flush = retry_parser.flush()
                                        for ev_t, d in r_flush:
                                            if ev_t == "tool_call":
                                                captured_tool_calls.append(d)
                                        if not captured_tool_calls and retry_output:
                                            r_full = "".join(retry_output)
                                            _, r_fallback = ToolBridge.extract_tool_calls_from_text(r_full, tools)
                                            if r_fallback:
                                                captured_tool_calls = r_fallback
                                            else:
                                                # 模型在重试中选择直接自然语言回复！只要没有损坏的工具标签，立即放行
                                                if not ToolBridge.check_zero_tolerance_suspicion(r_full, tools):
                                                    is_suspicious = False
                                                    pending_text_chunks = [r_full]
                                                    flush_texts = []
                                                    print(f"[Gateway-Reflection] 💬 模型在重试中选择直接自然语言回复，通过合规检查，放行输出！len={len(r_full)}", flush=True)
                                                    break

                                    if captured_tool_calls:
                                        print(f"[Gateway-Reflection] ✅ 第 {retry_round} 轮打回自愈成功！捕获工具: {captured_tool_calls[0]['function']['name']}", flush=True)
                                        break
                                except Exception as e:
                                    print(f"[Gateway-Reflection] ❌ 第 {retry_round} 轮打回执行异常: {e}", flush=True)

                        if len(full_str) == 0 and not captured_tool_calls:
                            # 旧实现此处直接 session_tracker.delete_session(conv_key)。
                            # 那等于把「本轮正文为空」判定为「会话树错位」，并立即销毁会话缓存。
                            # 但正文为空有多种成因：上游限流空响应、模型只思考未作答、
                            # 客户端在正文前中断等——其中只有极少数才是会话错位。
                            # 结果是：一次空响应就把整段上下文丢掉，下一轮从头再来
                            # （这正是日志中「反射自愈 963 次 / 客户端断开 637 次」的燃料之一）。
                            # 现改为：只记录诊断，保留会话缓存，把决策权交回客户端。
                            _thinking_len = len("".join(full_thinking_output))
                            print(
                                f"[Gateway-EmptyOutput] 本轮正文为空且未捕获工具，"
                                f"已保留会话缓存以维持上下文连续（不销毁）。"
                                f"sid={deepseek_session_id}, mid={final_message_id}, "
                                f"thinking_len={_thinking_len}",
                                flush=True,
                            )
                            # ★ 但「保留缓存」只解决**下一次**，这一轮仍然是空的 ——
                            # 客户端把"completed with no content"判为 EMPTY_RESPONSE 并重试，
                            # 重试再拿到空的 → 重试预算烧完 → 表现为「死也不输出」。
                            # （2026-09-12 实测：真实会话最后一步 thinking_len=1869、
                            #   正文与工具均为空，客户端连试 4 次后报 TRANSPORT/terminated，
                            #   用户手动掐断整轮。）
                            #
                            # 响应头 200 已经发出、状态码改不了，所以照上面「循环熔断」
                            # 同一手法发一条**显式诊断正文**：客户端拿到的就不是"空"了，
                            # 也就不会把它判成 EMPTY_RESPONSE、盲目重试同一个必死的请求。
                            #
                            # 只在**确实思考过**时才补这句：
                            #   thinking_len > 0 → 上游"只想不说"，值得告诉用户
                            #   thinking_len == 0 → 真·空响应（限流/中断），补字反而干扰判断
                            if _thinking_len > 0:
                                _eo_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": (
                                                "\n\n【网关提示】：上游本轮只输出了思考过程、"
                                                f"没有给出正文或工具调用（思考 {_thinking_len} 字符）。"
                                                "本轮**没有产出**，会话上下文已保留，"
                                                "请重试或换个说法继续。"
                                            )},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(_eo_chunk, ensure_ascii=False)}\n\n"
                                err_msg = "upstream_thinking_only"
                                session_tracker.reset_upstream_session(conv_key, reason="upstream_thinking_only")

                        if captured_tool_calls and ToolBridge.check_duplicate_loop(messages, captured_tool_calls):
                            _looped_tool = captured_tool_calls[0]['function']['name']
                            print(f"[Loop-Guard] 触发连续重复调用熔断: {_looped_tool}，强制平稳收尾！", flush=True)
                            captured_tool_calls = []
                            # ★ 熔断必须让客户端**看得出来**。
                            # 旧实现只是清空工具列表，然后走正常收尾 ——
                            # 客户端收到 `finish_reason: "stop"`，把一次循环故障
                            # 当成任务正常完成（静默丢任务，事后无人可追溯）。
                            #
                            # 流式响应头（200）已经发出，改不了状态码，
                            # 所以照"格式可疑"分支（下面的【网关安全拦截】）同一手法
                            # 发一条**显式诊断正文**；同时把原因写进 metrics 的 err_msg。
                            _lg_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": (
                                            "\n\n【网关循环熔断】：检测到模型连续重复调用同一个工具"
                                            f"（{_looped_tool}），网关已中断本次执行以避免无限循环。"
                                            "本轮任务**并未正常完成**，请检查已产生的改动后重试。"
                                        )},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(_lg_chunk, ensure_ascii=False)}\n\n"
                            err_msg = f"loop_guard_tripped:{_looped_tool}"

                        if captured_tool_calls:
                            # 补发尚未在流式循环中实时发射的工具调用（例如来自 Safety-Net / CoT-Salvage / flush 的工具）
                            for idx, tc in enumerate(captured_tool_calls):
                                if idx >= emitted_tool_count:
                                    tc_delta = [
                                        {
                                            "index": idx,
                                            "id": tc["id"],
                                            "type": "function",
                                            "function": tc["function"],
                                        }
                                    ]
                                    tc_chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"tool_calls": tc_delta},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n"

                            c_toks = TokenEstimator.estimate_text_tokens("".join(full_model_output))
                            r_toks = TokenEstimator.estimate_text_tokens("".join(full_thinking_output))
                            t_toks = sum(TokenEstimator.estimate_tool_call_tokens(tc) for tc in captured_tool_calls)
                            u_data = _build_openai_usage(c_toks + r_toks + t_toks, r_toks)
                            stop_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": "tool_calls",
                                    }
                                ],
                                "usage": u_data,
                            }
                            yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
                            if include_usage:
                                usage_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [],
                                    "usage": u_data,
                                }
                                yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        if is_suspicious:
                            try:
                                scrubbed_full = ToolBridge.clean_tags(full_str).strip()
                            except Exception:
                                scrubbed_full = ToolBridge.sanitize_internal_tags(full_str).strip()
                            if emitted_text_count > 0:
                                out_text = "\n\n【网关提示】：模型尝试调用工具但格式残缺未能成功解析，请重试或更换提问方式。"
                            else:
                                out_text = scrubbed_full or "【网关提示】：模型尝试调用工具但输出格式残缺未能成功解析，请重试或更换提问方式。"
                            u_data = _build_openai_usage(TokenEstimator.estimate_text_tokens(out_text), 0)
                            err_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": out_text},
                                        "finish_reason": "stop",
                                    }
                                ],
                                "usage": u_data,
                            }
                            yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                            if include_usage:
                                usage_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [],
                                    "usage": u_data,
                                }
                                yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        else:
                            # 确认 100% 为纯自然语言正常回答，发射 flush 残余文本（如果有未在流中下发完毕的碎片）
                            all_clean_texts = flush_texts
                            for t_val in all_clean_texts:
                                if t_val:
                                    chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": t_val},
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                    # 流式正常结束，仅在产生有效文本或捕获工具时才持久化最新消息 ID 至会话树
                    full_str = "".join(full_model_output)
                    if final_message_id and (len(full_str) > 0 or captured_tool_calls):
                        session_tracker.update_message_id(conv_key, final_message_id)

                    c_toks = TokenEstimator.estimate_text_tokens("".join(full_model_output))
                    r_toks = TokenEstimator.estimate_text_tokens("".join(full_thinking_output))
                    t_toks = sum(TokenEstimator.estimate_tool_call_tokens(tc) for tc in captured_tool_calls)
                    u_data = _build_openai_usage(c_toks + r_toks + t_toks, r_toks)
                    stop_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": u_data,
                    }
                    yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
                    if include_usage:
                        usage_chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": model,
                            "choices": [],
                            "usage": u_data,
                        }
                        yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

        except (asyncio.CancelledError, GeneratorExit):
            was_aborted = True
            print(f"[Stream] 客户端主动中断连接 (Client Abort): sid={deepseek_session_id}, mid={final_message_id}", flush=True)
            if deepseek_session_id:
                threading.Thread(
                    target=DeepSeekClient.stop_stream,
                    kwargs={
                        "token": account.token,
                        "session_id": deepseek_session_id,
                        "message_id": final_message_id,
                        "cookie": account.cookie,
                        "proxy": _get_proxy(account),
                    },
                    daemon=True,
                ).start()
            raise
        except Exception as e:
            status_code = 500
            err_msg = str(e)
            raise
        finally:
            # 1. 第一优先级：无论发生任何异常或取消，无条件立即释放账号互斥锁！
            if not account_released_by_failover:
                try:
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
                except Exception as ex:
                    print(f"[AccountPool] 释放账号互斥锁异常: {ex}", flush=True)

            # 1.5 会话回收（T13）：与非流式路径**同一语义** ——
            #     仅回收「本次新建 且 全程零产出」的会话。
            #
            # 旧实现只在非流式的 finally 里做了回收，**流式没有**。
            # 而流式才是 DSH Agent 的主路径（走 SSE）→
            # **每个被上游限流 / 空响应的流式请求都会稳定留下一个上游空会话**，
            # 这正是"某个测试号上累积了上百个空会话"的机制。
            #
            # 放在账号释放之后、metrics 之前：释放是第一优先级（不能被 I/O 拖住），
            # 而回收本身要发一次上游请求，必须包在 try 里且失败只记日志。
            try:
                produced = bool(
                    "".join(full_model_output).strip()
                    or "".join(full_thinking_output).strip()
                    or (captured_tool_calls or [])
                )
                if _session_created_by_this_request and deepseek_session_id and not produced:
                    print(
                        f"[SessionReclaim] 本次新建的会话零产出，回收以避免上游堆积空会话: "
                        f"sid={deepseek_session_id}",
                        flush=True,
                    )
                    session_tracker.delete_session(conv_key)
                    await asyncio.to_thread(
                        DeepSeekClient.delete_chat_session,
                        account.token,
                        deepseek_session_id,
                        account.cookie,
                        _get_proxy(account),
                    )
            except Exception as ex:
                print(f"[SessionReclaim] 回收失败（已忽略）: {ex}", flush=True)

            # 2. 【已移除】拟真人呼吸阻尼
            # 旧实现在每次工具调用后强制 await asyncio.sleep(random.uniform(1.2, 1.8))。
            # 实测代价：单次平均 1.62s；而 DSH Agent 任务上百轮工具调用是常态
            # → 纯等待累计 150~225 秒，是"每一步工具调用之间都要等一会"的直接来源。
            # 防风控诉求改由更精准的机制承担：
            #   · 每账号并发闸门（Semaphore=2，实测即上游上限）
            #   · 上游真实限流 → 首事件预检判定 → 429 + Retry-After 退避
            #   · 账号池冷却 cooldown_seconds
            # 即：按"是否需要退避"来等待，而不是无条件盲等。

            # 高精度核算流式输出 Tokens (正文 + 思考链 + 工具调用)
            full_str = "".join(full_model_output)
            thinking_str = "".join(full_thinking_output)
            content_toks = TokenEstimator.estimate_text_tokens(full_str)
            reasoning_toks = TokenEstimator.estimate_text_tokens(thinking_str)
            tool_toks = sum(TokenEstimator.estimate_tool_call_tokens(tc) for tc in captured_tool_calls)
            completion_tokens = content_toks + reasoning_toks + tool_toks

            metrics_engine.record(
                endpoint="/v1/chat/completions",
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
            sse_stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式处理
    failover_nonstream_done = False
    account_released_by_nonstream_failover = False
    full_content = []
    full_thinking = []
    tool_calls = []
    is_muted = False
    mute_until_nonstream = None
    permanent_invalid_nonstream = False
    final_mid = None
    status_code = 200
    err_msg = None

    try:
        while True:
            retry_needed = False
            async for event in DeepSeekClient.stream_chat(
                token=account.token,
                session_id=deepseek_session_id,
                prompt=prompt,
                parent_message_id=parent_message_id,
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
                model_type=model_type,
                ref_file_ids=ref_file_ids,
                cookie=account.cookie,
                proxy=_get_proxy(account),
            ):
                if event.event_type == "error":
                    if event.error_code == "biz:5":
                        is_muted = True
                        mute_until_nonstream = getattr(event, "mute_until", None)
                    elif event.error_code == "auth_expired":
                        permanent_invalid_nonstream = True
                    # 非流式路径也必须区分「限流（可退避重试）」与「真错误（需排查）」。
                    # 旧实现一律 502 —— 而 502 会被客户端当作服务端故障，
                    # 于是继续重试、进一步加剧上游过载（实测压测中 30/80 命中此路径）。
                    # 统一复用流式路径的判定，避免两条路径语义不一致。
                    _decision = classify_upstream_preflight(event, None, 0.0)
                    # T16/B1：与流式路径同一口径，避免两条路径的比率不可比
                    passive_metrics.note_preflight(_decision)

                    # ★ 上下文耗尽必须**先于**限流判定 —— 与流式路径（:1030 早于 :1053）同序。
                    # 两者在上游的表现都是"没有输出"，但处置完全不同：限流该退避重试，
                    # 上下文耗尽是"这个会话满了，换一个就好"。
                    #
                    # 旧实现只在流式路径做了这件事，非流式直接落到下面的 502：
                    #   · 客户端把 502 当服务端故障 → 无限重试，每次都失败
                    #   · **会话绑定不会被清** → 下一轮继续复用那个已经满了的会话，一直坏下去
                    # 即"任务做到一半就再也写不出来了"，而根因是会话满 + 不清绑定。
                    if _decision.kind == "context_exhausted":
                        print(
                            f"[Gateway-ContextExhausted] {_decision.detail} "
                            f"sid={deepseek_session_id} → 已清除会话绑定以便下一轮重建（非流式路径）",
                            flush=True,
                        )
                        try:
                            session_tracker.delete_session(conv_key)
                        except Exception as ex:
                            print(f"[Gateway-ContextExhausted] 清除会话绑定失败: {ex}", flush=True)
                        status_code = 400
                        err_msg = _decision.metric_error
                        raise HTTPException(status_code=400, detail=_decision.detail)

                    # context_exhausted / session_broken 自愈清理
                    if _decision.kind == "session_broken":
                        print(
                            f"[Gateway-SessionBroken] {_decision.detail} "
                            f"sid={deepseek_session_id} → 已清除会话绑定以便下一轮重建（非流式路径）",
                            flush=True,
                        )
                        try:
                            session_tracker.delete_session(conv_key)
                        except Exception as ex:
                            print(f"[Gateway-SessionBroken] 清除会话绑定失败: {ex}", flush=True)
                        status_code = 429
                        err_msg = _decision.metric_error
                        raise HTTPException(
                            status_code=429,
                            detail=_decision.detail,
                            headers={"Retry-After": str(_decision.retry_after or 1)},
                        )

                    if _decision.kind == "rate_limited":
                        prot_cd = getattr(config_manager.config, "rate_limit_cooldown_seconds", 20.0)
                        used_cd = apply_preflight_cooldown(account, _decision, prot_cd, "nonstream_rate_limited")
                        print(f"[Gateway-NonStream] 账号 {account.account_label} 触发限流/配额，已实施保护性冷却 {used_cd:.0f}s", flush=True)
                        if not failover_nonstream_done:
                            failover_nonstream_done = True
                            try:
                                account_pool.release_account(account)
                            except Exception:
                                pass
                            failover_acc = await account_pool.acquire_account(
                                preferred_label=None,
                                timeout=5.0,
                                immediate=True,
                                owned_labels=identity.get("account_labels") or [],
                                pool_fallback=bool(identity.get("pool_fallback")),
                                avoid_label=account.account_label,
                                avoid_proxy=_get_proxy(account),
                                is_vip=bool(identity.get("is_vip")),
                            )
                            if failover_acc:
                                print(
                                    f"[Gateway-NonStream-RateLimit-Failover] 账号 {account.account_label} 触发限流，"
                                    f"自动调度备用账号 {failover_acc.account_label} 接力重试...",
                                    flush=True,
                                )
                                account = failover_acc
                                try:
                                    deepseek_session_id = await loop.run_in_executor(
                                        None, DeepSeekClient.create_chat_session, account.token, account.cookie, _get_proxy(account)
                                    )
                                except Exception as e_cs:
                                    print(f"[Gateway-NonStream-RateLimit-Failover] 备用号建会话失败: {e_cs}", flush=True)
                                    deepseek_session_id = None
                                if deepseek_session_id:
                                    prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
                                    parent_message_id = None
                                    session_tracker.register_session(
                                        conv_key,
                                        account.account_label,
                                        deepseek_session_id,
                                        model_type=model_type,
                                        prefix=session_tracker._prefix_signatures(normalized_messages),
                                    )
                                    retry_needed = True
                                    break
                        if not retry_needed:
                            status_code = 429
                            err_msg = f"{event.content} ({event.error_code})"
                            retry_after = int(_decision.retry_after or 3)
                            print(
                                f"[Gateway-NonStream-RateLimit] 上游限流且无可用备用号 → 429 Retry-After={retry_after}: "
                                f"{event.content}",
                                flush=True,
                            )
                            account_released_by_nonstream_failover = True
                            raise HTTPException(
                                status_code=429,
                                detail=CLIENT_MSG_UPSTREAM_RATE_LIMIT,
                                headers={"Retry-After": str(retry_after)},
                            )

                    # ★ 非流式无感自愈 (Auto-Failover)：上游禁言/凭证失效时，在抛出 502 前自动换号重试 1 轮
                    if not failover_nonstream_done and event.error_code in ("biz:5", "auth_expired", "40003", "40001", "40002"):
                        failover_nonstream_done = True
                        relogin_nonstream_ok = False
                        relogin_ns_mfa_required = False
                        relogin_ns_cooldown_only = False

                        if event.error_code in ("auth_expired", "40003", "40001", "40002") and getattr(account, "password", None):
                            print(
                                f"[Gateway-NonStream-Failover] 账号 {account.account_label} 鉴权失效，"
                                f"尝试通过受信任免密通道自动自愈刷新 Token...",
                                flush=True,
                            )
                            relogin_nonstream_ok = await account.try_relogin()
                            if not relogin_nonstream_ok:
                                err_info = getattr(account, "last_relogin_error", None) or {}
                                err_biz = err_info.get("biz_code")
                                err_kind = err_info.get("kind")
                                err_str = str(err_info.get("error") or "")
                                if err_biz == 14 or "MFA_VERIFICATION_CODE_REQUIRED" in err_str:
                                    relogin_ns_mfa_required = True
                                    is_muted = False
                                    mute_until_nonstream = None
                                    permanent_invalid_nonstream = False
                                elif err_biz in (11, 40029) or err_kind == "network_error":
                                    relogin_ns_cooldown_only = True
                                    is_muted = True
                                    mute_until_nonstream = int(time.time()) + 900
                                    permanent_invalid_nonstream = False

                        if relogin_nonstream_ok:
                            print(
                                f"[Gateway-NonStream-Failover] ✅ 账号 {account.account_label} 免密自愈成功，"
                                f"原地重新创建会话并重试生成...",
                                flush=True,
                            )
                            is_muted = False
                            mute_until_nonstream = None
                            permanent_invalid_nonstream = False
                            try:
                                new_sid = await asyncio.to_thread(
                                    DeepSeekClient.create_chat_session,
                                    account.token,
                                    account.cookie,
                                    _get_proxy(account),
                                )
                            except Exception:
                                new_sid = None
                            deepseek_session_id = new_sid or account.session_id
                            parent_message_id = None
                            _session_created_by_this_request = True
                            session_tracker.register_session(
                                conv_key,
                                account.account_label,
                                deepseek_session_id,
                                prefix=session_tracker._prefix_signatures(normalized_messages),
                            )
                            prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
                            full_content = []
                            full_thinking = []
                            tool_calls = []
                            retry_needed = True
                            break

                        prev_proxy_ns = _get_proxy(account)
                        prev_muted_ns = is_muted and not relogin_ns_cooldown_only

                        print(
                            f"[Gateway-NonStream-Failover] ⚡ 账号 {account.account_label} 返回 {event.error_code}，"
                            f"正在执行 1 轮自动换号故障转移...",
                            flush=True,
                        )
                        try:
                            if relogin_ns_mfa_required:
                                account_pool.mark_mfa_required(account, caller_info=caller_info, stage="非流式容灾")
                            else:
                                account_pool.release_account(
                                    account, is_muted=is_muted, mute_until=mute_until_nonstream,
                                    permanent=permanent_invalid_nonstream,
                                    caller_info=caller_info,
                                )
                        except Exception as ex:
                            print(f"[Gateway-NonStream-Failover] 释放故障账号异常: {ex}", flush=True)

                        # 被动 Mute 监听切 IP：当且仅当被动监听到官方禁言/封号时，才触发出口 IP 切换！
                        if prev_muted_ns:
                            try:
                                from app.api.admin_routes import record_system_alert
                                record_system_alert(
                                    account.account_label,
                                    "MUTED",
                                    f"[非流式] 调用期间上游返回官方禁言封禁，预计解禁时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mute_until_nonstream)) if mute_until_nonstream else '未知'}，已自动隔离该号并执行容灾接管",
                                    mute_until_nonstream
                                )
                            except Exception:
                                pass
                            print(
                                f"[Gateway-NonStream-Failover] 🚨 账号 {account.account_label} 触发上游禁言(MUTE)，"
                                f"被动触发出口 IP 切换防连坐...",
                                flush=True,
                            )
                            try:
                                from app.engine.proxy_manager import proxy_manager
                                await proxy_manager.switch_next_healthy_node()
                            except Exception as e_pm:
                                print(f"[Gateway-NonStream-Failover] 被动切 IP 失败（降级维持）: {e_pm}", flush=True)

                        failover_acc = await account_pool.acquire_for_identity(
                            identity,
                            preferred_label=None,
                            timeout=10.0,
                            immediate=True,
                            avoid_label=account.account_label,
                            avoid_proxy=prev_proxy_ns,
                            refresh=True,
                        )

                        # 同 IP 连坐熔断防护：若前序账号是因官方禁言触发，且备用号同属一个出口代理，阻断秒换（仅在多代理池时生效）
                        if failover_acc and prev_muted_ns and should_trigger_anti_cascade(prev_proxy_ns, _get_proxy(failover_acc)):
                            print(
                                f"[Gateway-NonStream-Failover] ⚠️ 备用账号 {failover_acc.account_label} 与故障禁言账号同属出口代理 {prev_proxy_ns}，"
                                f"触发同 IP 熔断防线，拒绝秒级换号冲锋！",
                                flush=True,
                            )
                            account_pool.release_account(failover_acc)
                            failover_acc = None

                        if failover_acc:
                            print(f"[Gateway-NonStream-Failover] ✅ 成功获取替补账号: {failover_acc.account_label} (proxy: {_get_proxy(failover_acc)})，准备重试", flush=True)
                            account = failover_acc
                            is_muted = False
                            mute_until_nonstream = None
                            permanent_invalid_nonstream = False
                            ref_file_ids = []
                            try:
                                new_sid = await asyncio.to_thread(
                                    DeepSeekClient.create_chat_session,
                                    account.token,
                                    account.cookie,
                                    _get_proxy(account),
                                )
                            except Exception:
                                new_sid = None
                            except Exception:
                                new_sid = None

                            deepseek_session_id = new_sid or account.session_id
                            parent_message_id = None
                            _session_created_by_this_request = True
                            session_tracker.register_session(
                                conv_key,
                                account.account_label,
                                deepseek_session_id,
                                prefix=session_tracker._prefix_signatures(normalized_messages),
                            )
                            prompt, _ = session_tracker.prepare_prompt(normalized_messages, has_existing_session=False, tools=tools, reasoning_effort=effort)
                            full_content = []
                            full_thinking = []
                            tool_calls = []
                            retry_needed = True
                            break
                        else:
                            account_released_by_nonstream_failover = True

                    status_code = 502
                    err_msg = f"{event.content} ({event.error_code})"
                    logger.error(f"[Audit-UpstreamError] 上游错误: {event.content} ({event.error_code})")
                    raise HTTPException(status_code=502, detail=CLIENT_MSG_UPSTREAM_BAD_GATEWAY)
                elif event.event_type == "content":
                    full_content.append(event.content)
                elif event.event_type == "thinking":
                    full_thinking.append(event.content)
                elif event.event_type == "message_id" and event.message_id:
                    final_mid = event.message_id
                elif event.event_type == "done":
                    if event.message_id:
                        final_mid = event.message_id
                    if final_mid and (full_content or tool_calls):
                        session_tracker.update_message_id(conv_key, final_mid)
                    break

            if retry_needed:
                continue
            break

    except HTTPException:
        # ★ 必须排在下面那条 `except Exception` 之前。
        # HTTPException 也是 Exception，会被下面那条捕获并把 status_code 改写成 502 ——
        # 于是"限流记成 502、上下文耗尽也记成 502"，metrics 里再也看不出真实原因。
        # 这里只放行、不改动：具体分支在上面已经设好了自己的 status_code / err_msg。
        raise
    except Exception as e:
        status_code = 502
        err_msg = str(e)
        raise
    finally:
        # 1. 第一优先级：释放账号互斥锁
        if not account_released_by_nonstream_failover:
            try:
                account_pool.release_account(
                    account, is_muted=is_muted, mute_until=mute_until_nonstream,
                    permanent=permanent_invalid_nonstream,
                    caller_info=caller_info,
                )
            except Exception as ex:
                print(f"[AccountPool] 释放账号互斥锁异常: {ex}", flush=True)

        # 2. 会话回收（T13）：仅回收「本次新建 且 全程零产出」的会话
        try:
            produced = bool("".join(full_content).strip() or "".join(full_thinking).strip()
                            or (tool_calls or []))
            if _session_created_by_this_request and deepseek_session_id and not produced:
                print(
                    f"[SessionReclaim] 本次新建的会话零产出，回收以避免上游堆积空会话: "
                    f"sid={deepseek_session_id}",
                    flush=True,
                )
                session_tracker.delete_session(conv_key)
                await asyncio.to_thread(
                    DeepSeekClient.delete_chat_session,
                    account.token,
                    deepseek_session_id,
                    account.cookie,
                    _get_proxy(account),
                )
        except Exception as ex:
            print(f"[SessionReclaim] 回收失败（已忽略）: {ex}", flush=True)

        if status_code != 200 or err_msg:
            content_str = "".join(full_content)
            thinking_str = "".join(full_thinking)
            content_toks = TokenEstimator.estimate_text_tokens(content_str)
            reasoning_toks = TokenEstimator.estimate_text_tokens(thinking_str)
            completion_tokens = content_toks + reasoning_toks
            metrics_engine.record(
                endpoint="/v1/chat/completions",
                model=model,
                status_code=status_code,
                duration_ms=(time.time() - req_start_time) * 1000,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                account=account.account_label,
                error=err_msg,
            )

    content_str = "".join(full_content)
    thinking_str = "".join(full_thinking)

    resp_message = {"role": "assistant", "content": content_str}
    finish_reason = "stop"

    if tools:
        clean_text, tool_calls = ToolBridge.extract_tool_calls_from_text(content_str, tools)
        if not tool_calls and thinking_str:
            _, cot_tool_calls = ToolBridge.extract_tool_calls_from_text(thinking_str, tools)
            if cot_tool_calls:
                print(f"[Gateway-CoT-Salvage-NonStream] ⚡ 从思考链中打捞到 {len(cot_tool_calls)} 个工具调用！", flush=True)
                passive_metrics.note_tool_cot_salvage(len(cot_tool_calls), nonstream=True)
                tool_calls = cot_tool_calls

        is_suspicious = False
        if not tool_calls and final_mid and content_str.strip():
            is_suspicious = ToolBridge.check_zero_tolerance_suspicion(content_str, tools)
            if is_suspicious:
                print(f"[Gateway-Reflection-NonStream] ⚠️ 非流式检测到损坏工具调用标签，启动自愈纠错！mid={final_mid}", flush=True)
                passive_metrics.note_tool_reflection(0, nonstream=True)
                clean_text = ""
                # 快速失败（同流式路径，见上方说明）：3 -> 1
                MAX_RETRIES = 1
                retry_round = 0
                _tn_list = [t.get("function", t).get("name") for t in (tools or []) if t.get("function", t).get("name")]
                _tn_hint = f"（当前可用工具：{', '.join(_tn_list)}）\n" if _tn_list else ""
                while retry_round < MAX_RETRIES and not tool_calls:
                    retry_round += 1
                    print(f"[Gateway-Reflection-NonStream] 🔄 [第 {retry_round}/{MAX_RETRIES} 轮打回重试] 开始下发纠错...", flush=True)
                    retry_prompt = (
                        "【网关提示：工具调用格式异常，已被打回重试】\n"
                        "检测到上一轮尝试发起的工具调用未能成功解析（可能存在未闭合标签、JSON语法错误或参数不合法）。\n"
                        "请重新检查并修正：\n"
                        "- 若需要调用工具，请严格使用如下标准格式输出：\n"
                        "  <tool_call>\n"
                        '  {"name": "工具名", "arguments": {"参数名": "参数值"}}\n'
                        "  </tool_call>\n"
                        f"  {_tn_hint}"
                        "- 若确认不需要调用工具，请直接用清晰自然的语言回复用户。"
                    )
                    try:
                        retry_content = []
                        retry_mid = None
                        async for r_event in DeepSeekClient.stream_chat(
                            token=account.token,
                            session_id=deepseek_session_id,
                            prompt=retry_prompt,
                            parent_message_id=final_mid,
                            thinking_enabled=False,
                            search_enabled=False,
                            model_type=model_type,
                            ref_file_ids=ref_file_ids,
                            cookie=account.cookie,
                            proxy=_get_proxy(account),
                        ):
                            if r_event.event_type == "message_id" and r_event.message_id:
                                retry_mid = r_event.message_id
                            elif r_event.event_type == "content" and r_event.content:
                                retry_content.append(r_event.content)
                        if retry_mid:
                            final_mid = retry_mid
                            session_tracker.update_message_id(conv_key, final_mid)
                        if retry_content:
                            r_full = "".join(retry_content)
                            clean_text, tool_calls = ToolBridge.extract_tool_calls_from_text(r_full, tools)
                            if not tool_calls:
                                # 模型在重试中选择直接自然语言回复！只要没有损坏的工具标签，立即放行
                                if not ToolBridge.check_zero_tolerance_suspicion(r_full, tools):
                                    is_suspicious = False
                                    clean_text = r_full
                                    print(f"[Gateway-Reflection-NonStream] 💬 模型在重试中选择直接自然语言回复，通过合规检查，放行输出！len={len(r_full)}", flush=True)
                                    break
                        if tool_calls:
                            print(f"[Gateway-Reflection-NonStream] ✅ 第 {retry_round} 轮打回自愈成功！", flush=True)
                            break
                    except Exception as ex:
                        print(f"[Gateway-Reflection-NonStream] ❌ 第 {retry_round} 轮自愈异常: {ex}", flush=True)

        if tool_calls and ToolBridge.check_duplicate_loop(messages, tool_calls):
            _looped_tool = tool_calls[0]['function']['name']
            print(f"[Loop-Guard] 触发连续重复调用熔断: {_looped_tool}，强制平稳收尾！", flush=True)
            tool_calls = []
            # ★ 旧实现在这里塞的是「相关任务已连续执行并验证完毕，会话正常结束。」
            # —— 那是**把一次循环故障伪装成正常完成**：
            #   · 客户端无从分辨，会把被中断的任务当成成功
            #   · metrics 里也看不出问题（status_code 仍是 200、err_msg 仍是 None）
            # 现在改成明确告知"本轮被网关中断、任务并未完成"，并把原因写进 metrics。
            clean_text = (
                "【网关循环熔断】：检测到模型连续重复调用同一个工具"
                f"（{_looped_tool}），网关已中断本次执行以避免无限循环。\n"
                "本轮任务**并未正常完成**。请检查已产生的改动，然后重试或换个说法。"
            )
            err_msg = f"loop_guard_tripped:{_looped_tool}"

        if tool_calls:
            # 移除单工具硬切片 tool_calls = tool_calls[:1]，完整保留所有并发工具调用
            resp_message["content"] = clean_text or None
            resp_message["tool_calls"] = tool_calls
            finish_reason = "tool_calls"
        else:
            if is_suspicious:
                try:
                    scrubbed_content = ToolBridge.clean_tags(content_str).strip()
                except Exception:
                    scrubbed_content = ToolBridge.sanitize_internal_tags(content_str).strip()
            else:
                resp_message["content"] = clean_text

    if not resp_message.get("content") and not resp_message.get("tool_calls") and thinking_str:
        salvaged_text = ToolBridge.salvage_content_from_thinking(thinking_str)
        if salvaged_text:
            print(f"[Gateway-CoT-Content-Salvage-NonStream] ⚡ 从思考链中打捞出正文与代码！len={len(salvaged_text)}", flush=True)
            resp_message["content"] = salvaged_text
            session_tracker.reset_upstream_session(conv_key, reason="nonstream_cot_salvage")
        else:
            resp_message["content"] = (
                f"\n\n【网关提示】：上游本轮只输出了思考过程、没有给出正文或工具调用（思考 {len(thinking_str)} 字符）。"
                "本轮**没有产出**，会话上下文已保留，请重试或换个说法继续。"
            )
            session_tracker.reset_upstream_session(conv_key, reason="nonstream_thinking_only")
            err_msg = "upstream_thinking_only"

    if thinking_str:
        resp_message["reasoning_content"] = thinking_str

    tool_toks = sum(TokenEstimator.estimate_tool_call_tokens(tc) for tc in (tool_calls or []))
    content_toks = TokenEstimator.estimate_text_tokens(resp_message.get("content", ""))
    reasoning_toks = TokenEstimator.estimate_text_tokens(thinking_str)
    completion_tokens = content_toks + reasoning_toks + tool_toks

    metrics_engine.record(
        endpoint="/v1/chat/completions",
        model=model,
        status_code=status_code,
        duration_ms=(time.time() - req_start_time) * 1000,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        account=account.account_label,
        error=err_msg,
    )

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_time,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": resp_message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": _build_openai_usage(completion_tokens, reasoning_toks),
    }
