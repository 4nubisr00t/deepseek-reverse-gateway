import sys
import os
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from main import app
from app.core.security import verify_api_key
from app.engine.tool_bridge import ToolBridge
from app.engine.streaming_parser import StreamingToolParser

client = TestClient(app)

def test_tool_bridge_sanitize_leaks():
    """验证各种变形的 DSML、闭合标签、invoke、parameter 标签均被彻底清除"""
    dirty_samples = [
        '<｜｜DSML｜｜ calls>',
        '<｜｜DSML｜｜ invoke name="bash">',
        '<｜｜DSML｜｜ parameter name="code" string="true">',
        '</｜｜DSML｜｜ parameter>',
        '</｜｜DSML｜｜>',
        '</｜｜>',
        '</｜｜ parameter>',
        '</｜｜ calls>',
        '</｜｜ invoke>',
        '<invoke name="test">',
        '</invoke>',
        '<parameter name="test">',
        '</parameter>',
        '<tool_call>',
        '</tool_call>',
        '<|tool_calls|>',
        '</|tool_calls|>'
    ]
    for sample in dirty_samples:
        cleaned = ToolBridge.sanitize_internal_tags(sample)
        assert cleaned == "", f"Failed to clean {sample!r}, got {cleaned!r}"

def test_streaming_parser_no_leak_on_trailing_tags():
    """验证流式解析器在遇到没有 DSML 前缀的闭合标签时不会泄露在文本中"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}
            }
        }
    ]
    p = StreamingToolParser(tools)
    stream_chunks = [
        '开始执行任务。',
        '<｜｜DSML｜｜ calls>',
        '<｜｜DSML｜｜ invoke name="bash">',
        '<｜｜DSML｜｜ parameter name="command" string="true">uptime</｜｜DSML｜｜ parameter>',
        '</｜｜DSML｜｜ invoke>',
        '</｜｜DSML｜｜ calls>',
        '</｜｜>',
        '</｜｜ parameter>',
    ]
    events = []
    for c in stream_chunks:
        events.extend(p.feed(c))
    events.extend(p.flush())

    text_pieces = [d for ev, d in events if ev == "text"]
    tool_calls = [d for ev, d in events if ev == "tool_call"]

    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "bash"
    full_text = "".join(text_pieces)
    assert "</｜｜>" not in full_text
    assert "parameter" not in full_text
    assert full_text == "开始执行任务。"

def test_responses_api_tool_and_dsml_nonstream():
    """验证 /v1/responses 非流式输出在存在 DSML 上游时，正确分离 function_call 且正文无泄露"""
    fake_identity = {
        "key_id": "test_k1",
        "name": "test_key",
        "owner": "test_owner",
        "account_labels": ["acc1"],
        "pool_fallback": True,
        "sensitive_filter": False,
    }

    class MockEvent:
        def __init__(self, ev_type, content=None, message_id=None):
            self.event_type = ev_type
            self.content = content
            self.message_id = message_id
            self.error_code = None

    async def mock_stream_chat(*args, **kwargs):
        yield MockEvent("content", "准备执行命令。\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name=\"bash\">\n<｜｜DSML｜｜ parameter name=\"command\" string=\"true\">whoami</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>\n</｜｜>")
        yield MockEvent("message_id", message_id="1001")

    with patch("app.api.responses_routes.verify_api_key", return_value=fake_identity), \
         patch("app.engine.account_pool.account_pool.acquire_account", new_callable=AsyncMock) as mock_acquire, \
         patch("app.engine.deepseek_client.DeepSeekClient.create_chat_session", return_value="ds_sess_1"), \
         patch("app.engine.deepseek_client.DeepSeekClient.stream_chat", side_effect=mock_stream_chat):

        mock_acc = MagicMock()
        mock_acc.account_label = "acc1"
        mock_acc.token = "tok"
        mock_acc.cookie = ""
        mock_acquire.return_value = mock_acc

        app.dependency_overrides[verify_api_key] = lambda: fake_identity

        res = client.post("/v1/responses", json={
            "model": "deepseek-chat",
            "input": "请帮我执行 whoami",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "bash",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}
                }
            }],
            "stream": False
        }, headers={"Authorization": "Bearer sk-test"})

        app.dependency_overrides.clear()

        assert res.status_code == 200, res.text
        data = res.json()
        assert data["object"] == "response"
        outputs = data["output"]

        types = [item["type"] for item in outputs]
        assert "message" in types
        assert "function_call" in types

        msg_item = [item for item in outputs if item["type"] == "message"][0]
        text = msg_item["content"][0]["text"]
        assert "DSML" not in text
        assert "</｜｜>" not in text
        assert "准备执行命令。" in text

        fc_item = [item for item in outputs if item["type"] == "function_call"][0]
        assert fc_item["name"] == "bash"
        assert json.loads(fc_item["arguments"])["command"] == "whoami"

def test_responses_api_tool_and_dsml_stream():
    """验证 /v1/responses 流式输出在存在 DSML 上游时，正确发射 function_call 事件且流中无 DSML 泄露"""
    fake_identity = {
        "key_id": "test_k1",
        "name": "test_key",
        "owner": "test_owner",
        "account_labels": ["acc1"],
        "pool_fallback": True,
        "sensitive_filter": False,
    }

    class MockEvent:
        def __init__(self, ev_type, content=None, message_id=None):
            self.event_type = ev_type
            self.content = content
            self.message_id = message_id
            self.error_code = None

    async def mock_stream_chat(*args, **kwargs):
        yield MockEvent("content", "正在为您处理。")
        yield MockEvent("content", "\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name=\"bash\">\n<｜｜DSML｜｜ parameter name=\"command\" string=\"true\">date</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>\n</｜｜>")
        yield MockEvent("message_id", message_id="1002")

    with patch("app.api.responses_routes.verify_api_key", return_value=fake_identity), \
         patch("app.engine.account_pool.account_pool.acquire_account", new_callable=AsyncMock) as mock_acquire, \
         patch("app.engine.deepseek_client.DeepSeekClient.create_chat_session", return_value="ds_sess_1"), \
         patch("app.engine.deepseek_client.DeepSeekClient.stream_chat", side_effect=mock_stream_chat):

        mock_acc = MagicMock()
        mock_acc.account_label = "acc1"
        mock_acc.token = "tok"
        mock_acc.cookie = ""
        mock_acquire.return_value = mock_acc

        app.dependency_overrides[verify_api_key] = lambda: fake_identity

        res = client.post("/v1/responses", json={
            "model": "deepseek-chat",
            "input": "请查看日期",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "bash",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}
                }
            }],
            "stream": True
        }, headers={"Authorization": "Bearer sk-test"})

        app.dependency_overrides.clear()

        assert res.status_code == 200, res.text
        lines = [line.strip() for line in res.text.split("\n") if line.startswith("data: ") and line.strip() != "data: [DONE]"]
        events = [json.loads(line[6:]) for line in lines]

        # 验证文本 delta 中绝对不含 DSML 标签
        text_deltas = [e.get("delta", "") for e in events if e.get("type") == "response.output_text.delta"]
        joined_delta = "".join(text_deltas)
        assert "DSML" not in joined_delta
        assert "</｜｜>" not in joined_delta
        assert "正在为您处理。" in joined_delta

        # 验证产生了标准的 function_call 事件
        event_types = [e.get("type") for e in events]
        assert "response.function_call_arguments.done" in event_types
        fc_done = [e for e in events if e.get("type") == "response.function_call_arguments.done"][0]
        assert json.loads(fc_done["arguments"])["command"] == "date"
