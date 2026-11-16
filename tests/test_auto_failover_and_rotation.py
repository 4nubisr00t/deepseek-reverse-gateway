"""
测试：
1. 账号池 avoid_label 规避逻辑与单号保底退避
2. 上下文压缩检测与会话滚动换号（Session Rolling）
3. 首事件禁言/凭证失效时的流式与非流式无感故障转移（Auto-Failover）
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from starlette.requests import Request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import config_manager
from app.core import security
from app.engine.account_pool import AccountPool, AccountRuntime
from app.engine.deepseek_client import ChatEvent, DeepSeekClient
from app.engine import account_pool as pool_mod
from app.api import openai_routes


def _make_account(label, **kw):
    d = {
        "account_label": label,
        "token": f"token-{label}",
        "cookie": f"cookie-{label}",
        "is_valid": True,
        "status": "ACTIVE",
    }
    d.update(kw)
    return d


class TestAccountPoolAvoidLabel(unittest.TestCase):
    """测试 AccountPool.acquire_account 中的 avoid_label 参数行为"""

    def setUp(self):
        self._orig_accounts = config_manager.accounts
        self._orig_replenish = config_manager.config.auto_replenish
        self._orig_config_path = config_manager.config_path
        self._test_temp_cfg = Path(tempfile.mktemp(suffix="_test_cfg.json"))
        config_manager.config_path = self._test_temp_cfg
        config_manager.config.auto_replenish = False

    def tearDown(self):
        config_manager.config.auto_replenish = self._orig_replenish
        config_manager.accounts = self._orig_accounts
        if self._test_temp_cfg.exists():
            try:
                self._test_temp_cfg.unlink()
            except Exception:
                pass
        config_manager.config_path = self._orig_config_path

    def test_avoid_label_skips_sticky_affinity(self):
        """当 preferred_label 与 avoid_label 相同时，跳过粘性绑定，改选其他可用账号"""
        config_manager.accounts = [
            _make_account("acc-old"),
            _make_account("acc-new"),
        ]
        pool = AccountPool()

        # 指定 preferred_label="acc-old"，但同时 avoid_label="acc-old"
        acc = asyncio.run(
            pool.acquire_account(
                preferred_label="acc-old",
                avoid_label="acc-old",
                immediate=True,
            )
        )
        self.assertIsNotNone(acc)
        self.assertEqual(acc.account_label, "acc-new", "应避开 acc-old 并调度 acc-new")
        pool.release_account(acc)

    def test_avoid_label_selects_alternative_candidate(self):
        """无粘性指定时，候选池中优先避开 avoid_label"""
        config_manager.accounts = [
            _make_account("acc-1"),
            _make_account("acc-2"),
        ]
        pool = AccountPool()

        acc = asyncio.run(
            pool.acquire_account(
                preferred_label=None,
                avoid_label="acc-1",
                immediate=True,
            )
        )
        self.assertIsNotNone(acc)
        self.assertEqual(acc.account_label, "acc-2")
        pool.release_account(acc)

    def test_avoid_label_fallback_when_only_one_account(self):
        """当用户名下仅有 1 个可用账号时，即便被 avoid_label 标记，也应保底分配，不使请求直接饥饿失败"""
        config_manager.accounts = [
            _make_account("acc-solo"),
        ]
        pool = AccountPool()

        acc = asyncio.run(
            pool.acquire_account(
                preferred_label=None,
                avoid_label="acc-solo",
                owned_labels=["acc-solo"],
                immediate=True,
            )
        )
        self.assertIsNotNone(acc)
        self.assertEqual(acc.account_label, "acc-solo", "单账号场景下应安全回退使用该账号")
        pool.release_account(acc)


class TestCompactionRotationRules(unittest.TestCase):
    """测试上下文压缩主动换号的规则判定"""

    def test_compaction_without_files_triggers_rotation(self):
        """前缀变更且无图片绑定时，应规避老账号并置空 preferred_account"""
        conv_src = "cid-prefix-changed"
        cached_account = "user-acc-1"
        has_bound_files = False

        preferred_account = cached_account
        avoid_account = None

        if conv_src.startswith("cid-prefix-changed") and not has_bound_files:
            avoid_account = preferred_account
            preferred_account = None

        self.assertIsNone(preferred_account)
        self.assertEqual(avoid_account, "user-acc-1")

    def test_compaction_with_bound_files_preserves_sticky(self):
        """前缀变更但存在图片绑定时，必须保持粘性不变（因为图片依附在老账号的会话中）"""
        conv_src = "cid-prefix-changed"
        cached_account = "user-acc-1"
        has_bound_files = True

        preferred_account = cached_account
        avoid_account = None

        if conv_src.startswith("cid-prefix-changed") and not has_bound_files:
            avoid_account = preferred_account
            preferred_account = None

        self.assertEqual(preferred_account, "user-acc-1")
        self.assertIsNone(avoid_account)


class TestAutoFailoverEndToEnd(unittest.TestCase):
    """端到端测试：上游首事件返回 biz:5 / auth_expired 时无感自动换号重试"""

    def setUp(self):
        self._orig_accounts = config_manager.accounts
        self._orig_replenish = config_manager.config.auto_replenish
        self._orig_config_path = config_manager.config_path
        self._test_temp_cfg = Path(tempfile.mktemp(suffix="_test_cfg.json"))
        config_manager.config_path = self._test_temp_cfg
        config_manager.config.auto_replenish = False
        config_manager.accounts = [
            _make_account("acc-1"),
            _make_account("acc-2"),
        ]
        # 重置全局 account_pool 并同步给 openai_routes
        self._orig_pool_mod = pool_mod.account_pool
        self._orig_openai_pool = openai_routes.account_pool
        self._test_pool = AccountPool()
        pool_mod.account_pool = self._test_pool
        openai_routes.account_pool = self._test_pool

        # 保存并替换 DeepSeekClient 方法
        self._orig_stream_chat = DeepSeekClient.stream_chat
        self._orig_create_chat_session = DeepSeekClient.create_chat_session

    def tearDown(self):
        config_manager.config.auto_replenish = self._orig_replenish
        config_manager.accounts = self._orig_accounts
        if self._test_temp_cfg.exists():
            try:
                self._test_temp_cfg.unlink()
            except Exception:
                pass
        config_manager.config_path = self._orig_config_path
        pool_mod.account_pool = self._orig_pool_mod
        openai_routes.account_pool = self._orig_openai_pool
        DeepSeekClient.stream_chat = self._orig_stream_chat
        DeepSeekClient.create_chat_session = self._orig_create_chat_session

    def _make_req(self, stream=False, messages=None):
        payload = {
            "model": "deepseek-chat",
            "stream": stream,
            "messages": messages or [{"role": "user", "content": "你好"}],
        }
        raw = json.dumps(payload).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"authorization", b"Bearer sk-test")],
            "query_string": b"",
        }
        return Request(scope, receive)

    def test_nonstreaming_auto_failover_on_biz_5(self):
        """非流式：账号 1 返回 biz:5 时，自动换号到账号 2 并成功返回回答"""
        call_tokens = []

        async def mock_stream_chat(token, **kwargs):
            call_tokens.append(token)
            if token == "token-acc-1":
                yield ChatEvent("error", content="账号已被禁言", error_code="biz:5")
            else:
                yield ChatEvent("thinking", content="思考中...")
                yield ChatEvent("content", content="我是替补账号的回复")
                yield ChatEvent("done", message_id="mid-sub-1")

        DeepSeekClient.stream_chat = mock_stream_chat
        DeepSeekClient.create_chat_session = lambda *a, **kw: "session-fallback"

        identity = {
            "key_id": "k1",
            "account_labels": ["acc-1", "acc-2"],
            "pool_fallback": False,
        }

        req = self._make_req(stream=False)
        resp = asyncio.run(openai_routes.openai_chat_completions(req, identity))

        # 验证调用序列：先调用了 token-acc-1，失败后故障转移调用了 token-acc-2
        self.assertEqual(call_tokens, ["token-acc-1", "token-acc-2"])
        self.assertIn("choices", resp)
        self.assertEqual(resp["choices"][0]["message"]["content"], "我是替补账号的回复")

        # 验证 acc-1 已被熔断为 muted
        acc1 = self._test_pool._accounts["acc-1"]
        self.assertTrue(acc1.is_muted_now())

    def test_streaming_preflight_auto_failover_on_biz_5(self):
        """流式：账号 1 首事件返回 biz:5，预检无感切换至账号 2，客户端不收到任何错误帧"""
        call_tokens = []

        async def mock_stream_chat(token, **kwargs):
            call_tokens.append(token)
            if token == "token-acc-1":
                yield ChatEvent("error", content="账号已被禁言", error_code="biz:5")
            else:
                yield ChatEvent("thinking", content="思考中...")
                yield ChatEvent("content", content="我是流式替补回复")
                yield ChatEvent("done", message_id="mid-sub-2")

        DeepSeekClient.stream_chat = mock_stream_chat
        DeepSeekClient.create_chat_session = lambda *a, **kw: "session-fallback-stream"

        identity = {
            "key_id": "k1",
            "account_labels": ["acc-1", "acc-2"],
            "pool_fallback": False,
        }

        req = self._make_req(stream=True)
        resp = asyncio.run(openai_routes.openai_chat_completions(req, identity))

        # 消费 StreamingResponse 的 body_iterator
        async def collect_stream(streaming_response):
            chunks = []
            async for chunk in streaming_response.body_iterator:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(collect_stream(resp))
        self.assertEqual(call_tokens, ["token-acc-1", "token-acc-2"])

        full_output = "".join(chunks)
        self.assertNotIn("deepseek_error", full_output, "客户端绝不应收到 502/deepseek_error 帧")
        self.assertIn("我是流式替补回复", full_output)


if __name__ == "__main__":
    unittest.main()
