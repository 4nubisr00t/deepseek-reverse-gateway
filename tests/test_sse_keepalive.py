"""
SSE 保活心跳的回归测试。

背景：上游长上下文深度思考时可能 40~55s 不出字，期间网关不向下游写任何字节，
客户端看门狗（DSH 实测约 50s）判定传输静默而掐断 → TRANSPORT terminated → 连续重试。
本测试锁住包装器的行为契约：静默时产出心跳、有事件时透传、结束时正常收尾、异常不吞。
"""
import asyncio
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import (  # noqa: E402
    SSE_KEEPALIVE_INTERVAL,
    _KEEPALIVE_SENTINEL,
    _aiter_with_keepalive,
)


async def _collect(source, interval):
    out = []
    async for item in _aiter_with_keepalive(source, interval=interval):
        out.append(item)
    return out


def test_keepalive_emitted_during_long_silence():
    """上游长时间不出事件时，必须产出心跳（否则客户端看门狗掐断）。"""
    async def slow_source():
        await asyncio.sleep(0.45)          # 远大于 interval
        yield "real"

    got = asyncio.run(_collect(slow_source(), 0.1))
    beats = sum(1 for g in got if g is _KEEPALIVE_SENTINEL)
    reals = [g for g in got if g is not _KEEPALIVE_SENTINEL]
    assert beats >= 3, f"静默 0.45s / 间隔 0.1s 应至少 3 次心跳，实际 {beats}"
    assert reals == ["real"], f"真实事件被破坏：{reals}"


def test_no_keepalive_when_events_are_fast():
    """事件及时到达时不应产生心跳（避免污染正常流）。"""
    async def fast_source():
        for i in range(5):
            yield f"chunk-{i}"

    got = asyncio.run(_collect(fast_source(), 5.0))   # 间隔远大于总耗时
    beats = sum(1 for g in got if g is _KEEPALIVE_SENTINEL)
    reals = [g for g in got if g is not _KEEPALIVE_SENTINEL]
    assert beats == 0, f"快速流不应有心跳，实际 {beats}"
    assert reals == [f"chunk-{i}" for i in range(5)]


def test_order_preserved_between_beats():
    """心跳不得打乱真实事件顺序。"""
    async def mixed():
        yield "a"
        await asyncio.sleep(0.25)
        yield "b"

    got = asyncio.run(_collect(mixed(), 0.1))
    reals = [g for g in got if g is not _KEEPALIVE_SENTINEL]
    assert reals == ["a", "b"], f"顺序被破坏：{reals}"

    # 心跳应出现在 a 之后、b 之前
    i_a = got.index("a")
    i_b = got.index("b")
    assert any(g is _KEEPALIVE_SENTINEL for g in got[i_a + 1:i_b]), "心跳未出现在静默区段内"


def test_empty_source_terminates_cleanly():
    """空流应正常结束，不抛异常、不产出心跳。"""
    async def empty():
        if False:
            yield "never"

    got = asyncio.run(_collect(empty(), 0.1))
    assert got == [], f"空流不应产出任何东西，实际 {got}"


def test_exception_propagates_not_swallowed():
    """上游异常必须向上抛，不能被心跳包装器吞掉。"""
    async def boom():
        yield "ok"
        raise RuntimeError("upstream exploded")

    async def run():
        seen = []
        with pytest.raises(RuntimeError, match="upstream exploded"):
            async for item in _aiter_with_keepalive(boom(), interval=0.1):
                seen.append(item)
        return seen

    seen = asyncio.run(run())
    assert "ok" in seen, "异常前的真实事件应已产出"


def test_interval_is_configurable_and_sane():
    """间隔必须可配置，且默认值应远小于客户端 50s 看门狗。"""
    assert isinstance(SSE_KEEPALIVE_INTERVAL, float)
    assert 0 < SSE_KEEPALIVE_INTERVAL < 45, (
        f"默认心跳间隔 {SSE_KEEPALIVE_INTERVAL}s 必须显著小于客户端看门狗"
    )
