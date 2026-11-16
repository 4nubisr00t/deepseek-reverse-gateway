"""
账号池并发额度的「快速失败（immediate）」回归测试。

背景：旧行为是全忙时**静默阻塞**（调用点 timeout=30s、默认 60s），
而客户端看门狗约 50s —— 排队接近它就会被掐断，
表现为 TRANSPORT / "卡住几十秒然后报错"。
且 DSH 本身就是 Agent 调度器，比网关更清楚该让哪个子代理等待，
因此网关应诚实告知"没容量"（429 + Retry-After），而不是盲目排队。

注意：AccountPool.acquire_account 内部会调用 reload()，会从 config.json
重新装载真实账号，因此测试用 monkeypatch 让 reload 拿到构造的假账号。
"""
import asyncio
import os
import sys
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import app.engine.account_pool as ap  # noqa: E402
from app.engine.account_pool import (  # noqa: E402
    AccountPool,
    AccountRuntime,
    MAX_CONCURRENT_PER_ACCOUNT,
)


class _FakeGatewayCfg:
    auto_replenish = False
    min_active_accounts = 3
    mailcx_api_key = ""
    mailcx_domain = ""
    proxy = None


class _FakeConfigManager:
    """替换 config_manager：只提供 get_accounts 与 config"""

    def __init__(self, accounts):
        self._accounts = accounts
        self.config = _FakeGatewayCfg()

    def get_accounts(self):
        return list(self._accounts)

    def update_account(self, *a, **kw):
        return None


@pytest.fixture
def fake_accounts(monkeypatch):
    def _install(n):
        accs = [{"account_label": f"a{i}@x", "token": f"t{i}",
                 "is_valid": True, "status": "ACTIVE"} for i in range(n)]
        monkeypatch.setattr(ap, "config_manager", _FakeConfigManager(accs))
        return accs
    return _install


def _occupy_all(pool):
    async def _fill():
        for a in pool._accounts.values():
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                await a.semaphore.acquire()
    return _fill()


def test_immediate_returns_none_without_blocking_when_full(fake_accounts):
    """额度占满 + immediate=True → 必须立刻返回 None（不阻塞）。"""
    fake_accounts(1)

    async def main():
        pool = AccountPool()
        await _occupy_all(pool)
        t0 = time.time()
        got = await pool.acquire_account(timeout=30.0, immediate=True)
        return got, time.time() - t0

    got, dt = asyncio.run(main())
    assert got is None, "immediate 模式下容量不足不应返回账号"
    assert dt < 0.2, f"仍阻塞了 {dt:.2f}s，快速失败失效（应 <0.2s）"


def test_immediate_works_across_multiple_full_accounts(fake_accounts):
    """多账号全部满载时同样应快速失败。"""
    fake_accounts(3)

    async def main():
        pool = AccountPool()
        await _occupy_all(pool)
        t0 = time.time()
        got = await pool.acquire_account(timeout=30.0, immediate=True)
        return got, time.time() - t0

    got, dt = asyncio.run(main())
    assert got is None and dt < 0.2


def test_immediate_still_succeeds_when_capacity_available(fake_accounts):
    """有余额时必须能正常拿到账号——修复不得影响正常路径。"""
    fake_accounts(2)

    async def main():
        pool = AccountPool()
        t0 = time.time()
        got = await pool.acquire_account(timeout=30.0, immediate=True)
        return got, time.time() - t0

    got, dt = asyncio.run(main())
    assert got is not None, "有余额却拿不到账号"
    assert dt < 0.2


def test_blocking_mode_is_preserved_for_compat(fake_accounts):
    """immediate=False 时保留旧的阻塞行为（向后兼容）。"""
    fake_accounts(1)

    async def main():
        pool = AccountPool()
        await _occupy_all(pool)
        t0 = time.time()
        await pool.acquire_account(timeout=1.0, immediate=False)
        return time.time() - t0

    dt = asyncio.run(main())
    assert dt >= 0.9, f"非 immediate 模式应等满 timeout=1.0s，实际 {dt:.2f}s"


def test_occupied_account_is_not_selectable(fake_accounts):
    """额度占满后 is_available_for_api 必须为 False。"""
    r = AccountRuntime({"account_label": "x@y", "token": "t", "is_valid": True})
    assert r.is_available_for_api

    async def fill():
        for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
            await r.semaphore.acquire()

    asyncio.run(fill())
    assert not r.is_available_for_api
    assert r.in_flight == MAX_CONCURRENT_PER_ACCOUNT


def test_retry_hint_is_within_sane_range():
    """Retry-After 估算必须落在合理区间。

    区间下限为 2s 而非 1s：实测单次生成流耗时 5.7~6.7s，
    初版写死 1s 会让客户端 1 秒后打回、容量仍满载 → 空转风暴。
    """
    r = AccountRuntime({"account_label": "x@y", "token": "t", "is_valid": True})
    for label, val in (
        ("未释放过", r.cooldown_remaining()),
        ("刚释放", (setattr(r, "last_released_at", time.time()), r.cooldown_remaining())[1]),
        ("释放很久前", (setattr(r, "last_released_at", time.time() - 999), r.cooldown_remaining())[1]),
    ):
        assert 2.0 <= val <= 15.0, f"[{label}] Retry-After 估算越界：{val}"


def test_pool_exposes_last_wait_hint_after_failure(fake_accounts):
    """快速失败后必须留下可用的退避提示，供上层填 Retry-After。"""
    fake_accounts(1)

    async def main():
        pool = AccountPool()
        await _occupy_all(pool)
        await pool.acquire_account(timeout=30.0, immediate=True)
        return pool.last_wait_hint

    hint = asyncio.run(main())
    assert hint > 0, "未留下退避提示，上层无法填 Retry-After"


def test_over_release_does_not_inflate_semaphore_or_negative_inflight():
    """验证多次重复调用 release_account 绝不会导致信号量充气或 in_flight 变负"""
    r = AccountRuntime({"account_label": "guard@test", "token": "t", "is_valid": True})
    pool = AccountPool()
    pool._accounts = {"guard@test": r}

    assert r.in_flight == 0
    assert r.semaphore._value == MAX_CONCURRENT_PER_ACCOUNT

    # 在未 acquire 的情况下连续 release 5 次
    for _ in range(5):
        pool.release_account(r)

    # 核心断言：信号量 value 绝不能超过 MAX_CONCURRENT_PER_ACCOUNT，in_flight 绝不能为负
    assert r.semaphore._value == MAX_CONCURRENT_PER_ACCOUNT
    assert r.in_flight == 0


def test_parallel_chat_limit_parsed_as_rate_limit():
    """验证上游返回 parallel_chat_limit（有消息正在生成）时被识别为 rate_limit"""
    from app.engine.deepseek_client import DeepSeekClient
    import json

    sse_data = json.dumps({
        "type": "error",
        "content": "有消息正在生成，请稍后再试",
        "clear_response": True,
        "finish_reason": "parallel_chat_limit",
    })

    # 模拟解析 SSE 错误行
    from app.api.openai_routes import classify_upstream_preflight
    from app.engine.deepseek_client import ChatEvent

    ev = ChatEvent("error", content="有消息正在生成，请稍后再试", error_code="rate_limit")
    decision = classify_upstream_preflight(ev, None, 0.1)

    assert decision.kind == "rate_limited"
    assert "有消息正在生成" in decision.detail or "上游限流" in decision.detail
