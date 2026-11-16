import asyncio
import os
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import _fetch_preflight, classify_upstream_preflight
from app.engine.deepseek_client import ChatEvent


async def _stream_with_events(events):
    for ev in events:
        yield ev


@pytest.mark.anyio
async def test_fetch_preflight_catches_rate_limit_after_message_id():
    """复现核心故障：首帧为 message_id 元数据，紧接着第二帧为 rate_limit 错误。
    预检必须穿透 message_id 捕获到第二帧 rate_limit，从而触发容灾换号！"""
    ev1 = ChatEvent("message_id", message_id="24")
    ev2 = ChatEvent("error", content="消息发送过于频繁，请稍后重试", error_code="rate_limit")

    aiter = _stream_with_events([ev1, ev2]).__aiter__()
    preflight_events, substantive_event, target_event, first_exc, elapsed, decision = await _fetch_preflight(aiter)

    assert len(preflight_events) == 2, f"应暂存 message_id 与 error 两个事件，实际: {len(preflight_events)}"
    assert substantive_event is not None
    assert substantive_event.event_type == "error"
    assert decision.kind == "rate_limited"


@pytest.mark.anyio
async def test_fetch_preflight_passes_ok_on_thinking_after_message_id():
    """正常请求：首帧为 message_id，第二帧为 thinking。预检必须判定为 ok，并完整保留两个事件"""
    ev1 = ChatEvent("message_id", message_id="24")
    ev2 = ChatEvent("thinking", content="Thinking process...", message_id="24")
    ev3 = ChatEvent("content", content="Hello", message_id="24")

    aiter = _stream_with_events([ev1, ev2, ev3]).__aiter__()
    preflight_events, substantive_event, target_event, first_exc, elapsed, decision = await _fetch_preflight(aiter)

    assert len(preflight_events) == 2  # message_id 与 thinking
    assert substantive_event.event_type == "thinking"
    assert decision.kind == "ok"

    # 验证后续迭代仍能拿到剩余的 content
    remaining = []
    async for rem in aiter:
        remaining.append(rem)
    assert len(remaining) == 1
    assert remaining[0].content == "Hello"


@pytest.mark.anyio
async def test_fetch_preflight_catches_fast_empty_stream():
    """快速空流必须被判定为 rate_limited 以触发换号"""
    aiter = _stream_with_events([]).__aiter__()
    preflight_events, substantive_event, target_event, first_exc, elapsed, decision = await _fetch_preflight(aiter)

    assert len(preflight_events) == 0
    assert substantive_event is None
    assert decision.kind == "rate_limited"
