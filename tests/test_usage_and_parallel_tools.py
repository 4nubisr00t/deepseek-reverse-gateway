import sys
import json
import pytest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.engine.tool_bridge import ToolBridge
from app.engine.streaming_parser import StreamingToolParser
from app.engine.metrics import MetricsEngine
from app.engine.key_usage import KeyUsageEngine


def test_tool_prompt_encourages_parallel_tools():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read content of a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
    ]

    prompt = ToolBridge.format_tools_system_prompt(tools)
    # 验证已经移除了历史遗留的单工具限制文字
    assert "最多调用 1 个工具" not in prompt
    assert "单次回复最多调用" not in prompt
    # 验证已明确鼓励多工具并发调用
    assert "单次回复调用多个工具" in prompt or "一次性连续输出多个" in prompt
    assert "<tool_call>" in prompt
    assert "</tool_call>" in prompt


def test_extract_multiple_parallel_tool_calls():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    model_output = (
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "file1.txt"}}\n</tool_call>\n'
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "file2.txt"}}\n</tool_call>\n'
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "file3.txt"}}\n</tool_call>'
    )

    clean_text, tool_calls = ToolBridge.extract_tool_calls_from_text(model_output, tools)
    assert len(tool_calls) == 3
    paths = [json.loads(tc["function"]["arguments"])["path"] for tc in tool_calls]
    assert paths == ["file1.txt", "file2.txt", "file3.txt"]
    assert clean_text.strip() == ""


def test_streaming_parser_multiple_tool_calls():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    parser = StreamingToolParser(tools)
    chunk1 = '<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>\n'
    chunk2 = '<tool_call>\n{"name": "read_file", "arguments": {"path": "b.py"}}\n</tool_call>'

    events1 = parser.feed(chunk1)
    events2 = parser.feed(chunk2)
    all_events = events1 + events2 + parser.flush()

    tc_events = [ev for ev in all_events if ev[0] == "tool_call"]
    assert len(tc_events) == 2
    assert json.loads(tc_events[0][1]["function"]["arguments"])["path"] == "a.py"
    assert json.loads(tc_events[1][1]["function"]["arguments"])["path"] == "b.py"


def test_metrics_and_key_usage_cached_tokens(tmp_path):
    m_file = tmp_path / "metrics.json"
    u_file = tmp_path / "key_usage.json"

    metrics = MetricsEngine(storage_file=m_file)
    key_usage = KeyUsageEngine(storage_file=u_file)

    # 模拟一次大量上下文复用的长对话（如 50000 tokens，其中 49000 为缓存命中）
    metrics.record(
        endpoint="/v1/chat/completions",
        model="deepseek-chat",
        status_code=200,
        duration_ms=1500.0,
        prompt_tokens=50000,
        completion_tokens=800,
        cached_tokens=49000,
        attribute_to_key=False,
    )

    summary = metrics.get_summary()
    assert summary["total_prompt_tokens"] == 50000
    assert summary["total_cached_tokens"] == 49000
    assert summary["total_completion_tokens"] == 800
    assert summary["cache_hit_rate"] == 98.0

    # 模拟 key 记账
    key_usage.record(
        key_id="test_key_1",
        label="测试圣钥",
        prompt_tokens=50000,
        completion_tokens=800,
        cached_tokens=49000,
        duration_ms=1500.0,
    )

    snap = key_usage.snapshot("test_key_1")
    assert snap["prompt_tokens"] == 50000
    assert snap["cached_tokens"] == 49000
    assert snap["total_tokens"] == 50800
    assert snap["cache_hit_rate"] == 98.0
