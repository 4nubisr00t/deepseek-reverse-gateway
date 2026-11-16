"""
四大防封号防御机制单元测试集 (test_antiban_defenses.py)
1. 防御一：单会话轮数上限强制分叉 (Auto-Fork on Long Sessions)
2. 防御二：429 / 限流保护性冷却与无感容灾换号 (Protective Cooldown & Auto-Failover)
3. 防御三：自适应请求间隔随机抖动 (Adaptive Cooldown Jitter)
4. 防御四：历史超长 Tool 结果智能剪枝与压缩 (Historical Tool Output Truncation)
"""

import time
import unittest
from pathlib import Path
import tempfile
import asyncio
from unittest.mock import patch

from app.engine.session_tree import SessionTracker, SessionContext
from app.engine.account_pool import AccountRuntime, AccountPool
from app.config import config_manager


class AntiBanDefensesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tracker = SessionTracker(storage_file=Path(self._tmp.name) / "bindings.json")
        self._orig_max_turns = config_manager.config.max_session_turns

    def tearDown(self):
        config_manager.config.max_session_turns = self._orig_max_turns
        self._tmp.cleanup()

    # ── 1. 单会话轮数上限强制分叉测试 ──

    def test_session_auto_fork_when_turn_limit_reached_cid(self):
        """测试使用显式 custom_id 时，达到轮数阈值自动触发分叉：保留绑号、清空上游 session_id"""
        config_manager.config.max_session_turns = 35
        conv_key = "cid:test-agent-sess"
        account_label = "acc_worker_1@test.com"
        old_sid = "upstream-session-123"

        ctx = self.tracker.register_session(
            conv_key=conv_key,
            account_label=account_label,
            deepseek_session_id=old_sid,
        )
        ctx.turn_count = 35  # 达到配置的 35 轮阈值

        msgs = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "next task"},
        ]

        resolved_key, src = self.tracker.resolve_conversation(
            msgs,
            custom_id="test-agent-sess",
            allow_reuse=True,
        )

        self.assertEqual(resolved_key, conv_key)
        self.assertEqual(src, "cid-forked")

        # 核心断言：上游会话被清除（换新会话），但账号绑定粘性保持不变（不换号），轮数归零
        cached = self.tracker.get_session(conv_key)
        self.assertEqual(cached.deepseek_session_id, "")
        self.assertEqual(cached.account_label, account_label)
        self.assertEqual(cached.turn_count, 0)
        self.assertIsNone(cached.last_message_id)

    def test_session_auto_fork_when_turn_limit_reached_prefix(self):
        """测试使用无显式 ID 的前缀会话时，达到轮数阈值自动触发分叉"""
        config_manager.config.max_session_turns = 35
        msgs = [
            {"role": "user", "content": "unique initial task prompt for prefix"},
            {"role": "assistant", "content": "understood"},
            {"role": "user", "content": "follow up 1"},
        ]

        resolved_key, _ = self.tracker.resolve_conversation(msgs, allow_reuse=True)
        ctx = self.tracker.register_session(
            conv_key=resolved_key,
            account_label="acc_worker_prefix@test.com",
            deepseek_session_id="upstream-prefix-sid",
            prefix=self.tracker._prefix_signatures(msgs),
        )
        ctx.turn_count = 35

        # 延伸消息再次请求
        msgs_next = list(msgs) + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "follow up 2"}]
        next_key, src = self.tracker.resolve_conversation(msgs_next, allow_reuse=True)

        self.assertEqual(next_key, resolved_key)
        self.assertEqual(src, "prefix-forked")
        cached = self.tracker.get_session(resolved_key)
        self.assertEqual(cached.deepseek_session_id, "")
        self.assertEqual(cached.turn_count, 0)

    def test_session_never_forks_when_limit_is_zero_or_negative(self):
        """测试默认 max_session_turns=0 时，任意长会话（如 100 轮）绝对不截断、零丢上下文"""
        config_manager.config.max_session_turns = 0
        conv_key = "cid:long-coding-session"
        account_label = "acc_worker_agent@test.com"
        sid = "upstream-long-session-456"

        ctx = self.tracker.register_session(
            conv_key=conv_key,
            account_label=account_label,
            deepseek_session_id=sid,
        )
        ctx.turn_count = 150  # 超长会话

        msgs = [
            {"role": "user", "content": "keep coding task"},
            {"role": "assistant", "content": "ok working on it"},
            {"role": "user", "content": "continue"},
        ]

        resolved_key, src = self.tracker.resolve_conversation(
            msgs,
            custom_id="long-coding-session",
            allow_reuse=True,
        )

        self.assertEqual(resolved_key, conv_key)
        self.assertEqual(src, "cid")
        cached = self.tracker.get_session(conv_key)
        self.assertEqual(cached.deepseek_session_id, sid)
        self.assertEqual(cached.turn_count, 150)

    # ── 2. 429 保护性冷却测试 ──

    def test_protective_cooldown_on_account(self):
        """测试给账号施加保护性冷却后，is_usable_state 与 cooldown_remaining 正确响应"""
        acc_data = {
            "account_label": "acc_rate_limited@test.com",
            "token": "tok-123",
            "is_valid": True,
            "status": "ACTIVE",
        }
        acc = AccountRuntime(acc_data)
        self.assertTrue(acc.is_usable_state)

        # 施加 20 秒保护性冷却
        acc.apply_protective_cooldown(20.0, reason="test_rate_limited")
        self.assertFalse(acc.is_usable_state)
        self.assertGreater(acc.cooldown_remaining(), 15.0)

        # 模拟冷却到期
        acc.cooldown_until = time.time() - 1.0
        self.assertTrue(acc.is_usable_state)

    # ── 3. 自适应随机抖动测试 ──

    def test_cooldown_jitter_in_release_account(self):
        """测试账号释放时计算出的 effective_cooldown 包含随机抖动且在合理区间内"""
        pool = AccountPool()
        acc_data = {
            "account_label": "acc_jitter@test.com",
            "token": "tok-jit",
            "is_valid": True,
            "status": "ACTIVE",
        }
        acc = AccountRuntime(acc_data)
        pool._accounts[acc.account_label] = acc

        # 多次释放并观察 effective_cooldown 的离散性
        cooldowns = set()
        for _ in range(5):
            pool.release_account(acc)
            cooldowns.add(round(acc.effective_cooldown, 3))
            self.assertGreaterEqual(acc.effective_cooldown, 1.0)
            self.assertLessEqual(acc.effective_cooldown, 5.0)

        # 确保产生了一定随机变化，不是固定常数
        self.assertGreater(len(cooldowns), 1)

    def test_jitter_soft_fallback_in_acquire_account(self):
        """测试当单账号在抖动冷却中时，软降级逻辑依然能够获取该号，避免误报 429"""
        pool = AccountPool()
        acc_data = {
            "account_label": "acc_single@test.com",
            "token": "tok-single",
            "is_valid": True,
            "status": "ACTIVE",
        }
        acc = AccountRuntime(acc_data)
        with patch.object(pool, "reload", lambda: None), \
             patch.object(pool, "allowed_labels", return_value=None):
            pool._accounts[acc.account_label] = acc

            # 模拟刚刚释放，处于 3 秒抖动冷却期内
            acc.last_released_at = time.time()
            acc.effective_cooldown = 3.0
            # 此时 is_available_for_api 为 False (因处于抖动期)
            self.assertFalse(acc.is_available_for_api)

            # 但在 immediate 快速失败模式下，acquire_account 应软降级放行该健康账号
            chosen = asyncio.run(pool.acquire_account(immediate=True))
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen.account_label, "acc_single@test.com")

    # ── 4. 历史超长 Tool 结果智能剪枝与压缩测试 ──

    def test_historical_tool_result_truncation(self):
        """测试历史多轮工具调用输出在拼接 composite prompt 时被自动紧凑折叠，而当前轮保持高保真"""
        giant_tool_output = "X" * 2000  # 2000 字符的工具输出

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "find the bug"},
            {"role": "assistant", "content": "running tool"},
            {"role": "tool", "content": giant_tool_output},  # 历史工具调用
            {"role": "user", "content": "continue fixing"},   # 当前提问
        ]

        prompt, _ = self.tracker.prepare_prompt(messages, has_existing_session=False)

        # 核心断言：历史工具结果已被折叠截断至 400 字符以内
        self.assertIn("...[前序工具输出过长已折叠", prompt)
        self.assertLess(len(prompt), 1000)


if __name__ == "__main__":
    unittest.main()
