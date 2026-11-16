"""
测试多出口代理隔离、avoid_proxy 容灾选择与同 IP 连坐熔断防线
"""

import asyncio
import unittest
from unittest.mock import patch, MagicMock

from app.config import config_manager, GatewayConfig
from app.engine.account_pool import (
    AccountPool,
    AccountRuntime,
    get_effective_proxy,
    should_trigger_anti_cascade,
)
from app.engine.deepseek_client import ChatEvent, DeepSeekClient
from app.api import openai_routes


class TestMultiProxyAndAntiCascade(unittest.TestCase):
    def setUp(self):
        self.orig_config = config_manager.config
        self.test_config = GatewayConfig(
            admin_key="test-key",
            api_keys={"test": ["k1"]},
            accounts=[],
            proxy="http://127.0.0.1:7890",
            proxies=["http://127.0.0.1:7891", "http://127.0.0.1:7892", "http://127.0.0.1:7893"],
            anti_cascade_same_ip=True,
        )
        config_manager.config = self.test_config

    def tearDown(self):
        config_manager.config = self.orig_config

    def test_effective_proxy_resolution(self):
        """测试账号代理优先级：账号专属 > 多代理池哈希分散 > 全局单代理"""
        # 1. 账号专属 proxy
        acc_custom = AccountRuntime({
            "token": "tok-1",
            "account_label": "acc-custom",
            "proxy": "http://127.0.0.1:9999",
        })
        self.assertEqual(acc_custom.effective_proxy, "http://127.0.0.1:9999")
        self.assertEqual(get_effective_proxy(acc_custom), "http://127.0.0.1:9999")

        # 2. 从 proxies 列表中哈希分散
        acc_pooled = AccountRuntime({
            "token": "tok-2",
            "account_label": "acc-pooled",
        })
        expected_proxy = self.test_config.proxies[abs(hash("acc-pooled")) % len(self.test_config.proxies)]
        self.assertEqual(acc_pooled.effective_proxy, expected_proxy)

        # 3. proxies 列表为空时，回退至全局单 proxy
        self.test_config.proxies = []
        self.assertEqual(acc_pooled.effective_proxy, "http://127.0.0.1:7890")

        # 4. 全局 proxy 也为空
        self.test_config.proxy = None
        self.assertIsNone(acc_pooled.effective_proxy)

    def test_should_trigger_anti_cascade(self):
        """测试同 IP 连坐熔断触发条件"""
        # 正常多代理池开启：相同出口代理时触发熔断
        self.assertTrue(should_trigger_anti_cascade("http://127.0.0.1:7891", "http://127.0.0.1:7891"))

        # 不同出口代理时不触发熔断
        self.assertFalse(should_trigger_anti_cascade("http://127.0.0.1:7891", "http://127.0.0.1:7892"))

        # 代理为 None 时不触发
        self.assertFalse(should_trigger_anti_cascade(None, "http://127.0.0.1:7891"))
        self.assertFalse(should_trigger_anti_cascade("http://127.0.0.1:7891", None))
        self.assertFalse(should_trigger_anti_cascade(None, None))

        # 开关关闭时绝对不触发
        self.test_config.anti_cascade_same_ip = False
        self.assertFalse(should_trigger_anti_cascade("http://127.0.0.1:7891", "http://127.0.0.1:7891"))
        self.test_config.anti_cascade_same_ip = True

        # 单代理或无代理池时（len(proxies) <= 1）绝对不触发，保持单 IP 场景既有换号能力
        self.test_config.proxies = ["http://127.0.0.1:7890"]
        self.assertFalse(should_trigger_anti_cascade("http://127.0.0.1:7890", "http://127.0.0.1:7890"))
        self.test_config.proxies = []
        self.assertFalse(should_trigger_anti_cascade("http://127.0.0.1:7890", "http://127.0.0.1:7890"))

    def test_acquire_account_avoid_proxy(self):
        """测试 acquire_account 优先避开指定的 avoid_proxy"""
        pool = AccountPool()
        acc1 = AccountRuntime({
            "token": "tok-1",
            "account_label": "acc-ip1",
            "proxy": "http://127.0.0.1:7891",
        })
        acc2 = AccountRuntime({
            "token": "tok-2",
            "account_label": "acc-ip2",
            "proxy": "http://127.0.0.1:7892",
        })
        pool._accounts = {acc1.account_label: acc1, acc2.account_label: acc2}
        pool.reload = lambda: None

        # 正常获取（LRU）
        acc1.last_used = 100.0
        acc2.last_used = 200.0
        chosen = asyncio.run(pool.acquire_account(immediate=True))
        self.assertEqual(chosen.account_label, "acc-ip1")
        pool.release_account(chosen)

        # 指定 avoid_proxy 为 acc1 的代理，必须跳过 acc1 优先选择 acc2
        chosen_diff = asyncio.run(pool.acquire_account(avoid_proxy="http://127.0.0.1:7891", immediate=True))
        self.assertIsNotNone(chosen_diff)
        self.assertEqual(chosen_diff.account_label, "acc-ip2")
        self.assertEqual(chosen_diff.effective_proxy, "http://127.0.0.1:7892")
        pool.release_account(chosen_diff)

    def test_failover_switches_to_different_proxy_on_biz_5(self):
        """端到端流式：账号 1 (7891) 遭遇 biz:5 禁言，自动容灾选择不同代理的账号 2 (7892) 顺利接力"""
        test_pool = AccountPool()
        acc1 = AccountRuntime({
            "token": "tok-1",
            "account_label": "acc-1",
            "proxy": "http://127.0.0.1:7891",
        })
        acc2 = AccountRuntime({
            "token": "tok-2",
            "account_label": "acc-2",
            "proxy": "http://127.0.0.1:7892",
        })
        test_pool._accounts = {acc1.account_label: acc1, acc2.account_label: acc2}
        test_pool.reload = lambda: None

        call_tokens = []
        call_proxies = []

        async def mock_stream_chat(token, proxy=None, **kwargs):
            call_tokens.append(token)
            call_proxies.append(proxy)
            if token == "tok-1":
                yield ChatEvent("error", content="账号已被禁言", error_code="biz:5")
            else:
                yield ChatEvent("thinking", content="思考中...")
                yield ChatEvent("content", content="我是不同代理的替补回复")
                yield ChatEvent("done", message_id="mid-sub-2")

        with patch("app.api.openai_routes.account_pool", test_pool), \
             patch("app.engine.deepseek_client.DeepSeekClient.stream_chat", side_effect=mock_stream_chat), \
             patch("app.engine.deepseek_client.DeepSeekClient.create_chat_session", return_value="sess-fallback"):

            identity = {
                "key_id": "k1",
                "account_labels": ["acc-1", "acc-2"],
                "pool_fallback": False,
            }

            import json
            from starlette.requests import Request

            payload = {
                "model": "deepseek-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "你好"}],
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
            req = Request(scope, receive)

            resp = asyncio.run(openai_routes.openai_chat_completions(req, identity))

            async def collect_stream(streaming_resp):
                chunks = []
                async for chunk in streaming_resp.body_iterator:
                    chunks.append(chunk)
                return chunks

            chunks = asyncio.run(collect_stream(resp))
            self.assertEqual(call_tokens, ["tok-1", "tok-2"])
            self.assertEqual(call_proxies, ["http://127.0.0.1:7891", "http://127.0.0.1:7892"])
            full_output = "".join(chunks)
            self.assertIn("我是不同代理的替补回复", full_output)
            self.assertTrue(acc1.is_muted_now())

    def test_failover_blocked_on_same_ip_when_anti_cascade_enabled(self):
        """多代理池场景下：备用账号与禁言账号同属相同代理，触发同 IP 熔断防线，拒绝连坐冲锋"""
        test_pool = AccountPool()
        acc1 = AccountRuntime({
            "token": "tok-1",
            "account_label": "acc-1",
            "proxy": "http://127.0.0.1:7891",
        })
        acc2 = AccountRuntime({
            "token": "tok-2",
            "account_label": "acc-2",
            "proxy": "http://127.0.0.1:7891",  # 同样是 7891，同一 IP
        })
        test_pool._accounts = {acc1.account_label: acc1, acc2.account_label: acc2}
        test_pool.reload = lambda: None

        call_tokens = []

        async def mock_stream_chat(token, proxy=None, **kwargs):
            call_tokens.append(token)
            yield ChatEvent("error", content="账号已被禁言", error_code="biz:5")

        with patch("app.api.openai_routes.account_pool", test_pool), \
             patch("app.engine.deepseek_client.DeepSeekClient.stream_chat", side_effect=mock_stream_chat), \
             patch("app.engine.deepseek_client.DeepSeekClient.create_chat_session", return_value="sess-fallback"):

            identity = {
                "key_id": "k1",
                "account_labels": ["acc-1", "acc-2"],
                "pool_fallback": False,
            }

            import json
            from starlette.requests import Request

            payload = {
                "model": "deepseek-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "你好"}],
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
            req = Request(scope, receive)

            resp = asyncio.run(openai_routes.openai_chat_completions(req, identity))

            async def collect_stream(streaming_resp):
                chunks = []
                async for chunk in streaming_resp.body_iterator:
                    chunks.append(chunk)
                return chunks

            chunks = asyncio.run(collect_stream(resp))
            # 关键验证：由于同属 7891 代理，acc-2 被熔断防线保护，绝不发起冲锋！
            self.assertEqual(call_tokens, ["tok-1"])
            self.assertFalse(acc2.is_muted_now(), "acc-2 不应被污染或连坐禁言")
            full_output = "".join(chunks)
            self.assertIn("biz:5", full_output)


if __name__ == "__main__":
    unittest.main()
