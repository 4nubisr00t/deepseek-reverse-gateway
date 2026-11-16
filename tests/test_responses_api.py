"""
OpenAI Responses API (/v1/responses) 单元测试套件
验证 input 规整、指令注入、多模态、工具调用输出转换及端到端非流式与流式 SSE 协议
"""

import sys
import unittest
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from unittest.mock import MagicMock, patch, AsyncMock
import types
if "curl_cffi" not in sys.modules:
    m = types.ModuleType("curl_cffi")
    m.__path__ = []
    sys.modules["curl_cffi"] = m
    sys.modules["curl_cffi.requests"] = MagicMock()
    sys.modules["curl_cffi.curl"] = MagicMock()

from fastapi.testclient import TestClient
from main import app
from app.api.responses_routes import parse_responses_input
from app.engine.deepseek_client import ChatEvent


class TestResponsesAPI(unittest.TestCase):

    def setUp(self):
        from app.core.security import verify_api_key, get_identity
        app.dependency_overrides[verify_api_key] = lambda: True
        app.dependency_overrides[get_identity] = lambda: {
            "key_id": "test_key",
            "log_label": "test_label",
            "sensitive_filter": False,
            "pool_fallback": True,
            "account_labels": [],
        }
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_parse_string_input(self):
        """测试纯字符串 input"""
        msgs = parse_responses_input("Hello, write python code")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Hello, write python code")

    def test_parse_instructions_injection(self):
        """测试 instructions 注入为首位 system 消息"""
        msgs = parse_responses_input("Hello", instructions="Be concise")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "Be concise")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "Hello")

    def test_parse_developer_role_mapping(self):
        """测试 Responses API 中的 developer 角色自动映射为 system"""
        raw = [
            {"role": "developer", "content": "You are Codex"},
            {"role": "user", "content": "Fix bug"}
        ]
        msgs = parse_responses_input(raw)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "You are Codex")
        self.assertEqual(msgs[1]["role"], "user")

    def test_parse_function_call_output(self):
        """测试 function_call_output 转换为 tool 角色消息"""
        raw = [
            {"type": "function_call_output", "call_id": "call_999", "output": "File updated successfully"}
        ]
        msgs = parse_responses_input(raw)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "tool")
        self.assertEqual(msgs[0]["tool_call_id"], "call_999")
        self.assertEqual(msgs[0]["content"], "File updated successfully")

    def test_parse_multipart_text(self):
        """测试多段结构化内容合并"""
        raw = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Part 1 "},
                    {"type": "text", "text": "Part 2"}
                ]
            }
        ]
        msgs = parse_responses_input(raw)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "Part 1 Part 2")

    def test_routes_mounted(self):
        """验证 /v1/responses 与 /responses 路由已正确注册至 FastAPI"""
        routes = []
        for route in app.routes:
            if hasattr(route, "path"):
                routes.append(route.path)
            if hasattr(route, "original_router") and hasattr(route.original_router, "routes"):
                for r in route.original_router.routes:
                    if hasattr(r, "path"):
                        routes.append(r.path)
        self.assertTrue(any(r == "/v1/responses" for r in routes))
        self.assertTrue(any(r == "/responses" for r in routes))

    @patch("app.api.responses_routes.DeepSeekClient.create_chat_session", return_value="fake_ds_sid")
    @patch("app.api.responses_routes.DeepSeekClient.stream_chat")
    @patch("app.api.responses_routes.account_pool.acquire_account")
    def test_responses_create_non_streaming(self, mock_acquire, mock_stream, mock_create_session):
        """测试非流式 POST /v1/responses 完整聚合响应"""
        
        # 模拟账号
        mock_acc = unittest.mock.MagicMock()
        mock_acc.token = "fake_token"
        mock_acc.cookie = ""
        mock_acc.account_label = "acc@test.com"
        mock_acquire.return_value = mock_acc

        # 模拟 DeepSeek 流
        async def fake_stream(*args, **kwargs):
            yield ChatEvent("thinking", content="Let me think")
            yield ChatEvent("content", content="Hello world")
            yield ChatEvent("message_id", message_id="12345")
        mock_stream.side_effect = fake_stream

        resp = self.client.post(
            "/v1/responses",
            json={
                "model": "deepseek-chat",
                "input": "Hi",
                "instructions": "Be helpful",
                "stream": False
            },
            headers={"Authorization": "Bearer test-key"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["object"], "response")
        self.assertEqual(data["status"], "completed")
        self.assertTrue(len(data["output"]) > 0)
        self.assertEqual(data["output"][0]["content"][0]["text"], "Hello world")
        self.assertIn("usage", data)

    @patch("app.api.responses_routes.DeepSeekClient.create_chat_session", return_value="fake_ds_sid")
    @patch("app.api.responses_routes.DeepSeekClient.stream_chat")
    @patch("app.api.responses_routes.account_pool.acquire_account")
    def test_responses_create_streaming_sse(self, mock_acquire, mock_stream, mock_create_session):
        """测试流式 POST /v1/responses 产出的语义 SSE 事件序列"""

        mock_acc = unittest.mock.MagicMock()
        mock_acc.token = "fake_token"
        mock_acc.cookie = ""
        mock_acc.account_label = "acc@test.com"
        mock_acquire.return_value = mock_acc

        async def fake_stream(*args, **kwargs):
            yield ChatEvent("thinking", content="Thinking step 1")
            yield ChatEvent("content", content="Code output")
            yield ChatEvent("message_id", message_id="12345")
        mock_stream.side_effect = fake_stream

        resp = self.client.post(
            "/v1/responses",
            json={
                "model": "deepseek-chat",
                "input": "Write code",
                "stream": True
            },
            headers={"Authorization": "Bearer test-key"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        
        body = resp.text
        self.assertIn("event: response.created", body)
        self.assertIn("event: response.output_item.added", body)
        self.assertIn("event: response.reasoning_text.delta", body)
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn("event: response.completed", body)
        self.assertIn("data: [DONE]", body)


if __name__ == "__main__":
    unittest.main()
