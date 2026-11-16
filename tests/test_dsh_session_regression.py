import pytest
import json
from app.engine.tool_bridge import ToolBridge
from app.engine.streaming_parser import StreamingToolParser

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "run_in_background": {"type": "boolean"},
                    "timeoutMs": {"type": "number"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_output",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "wait": {"type": "boolean"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_list",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


def test_dsh_session_dirty_argument_sanitization():
    """验证从 session.v4.jsonl 抓取的真实上游脏数据均能精准清洗并完成 Schema 类型转换"""
    raw_args = {
        "command": "Get-ChildItem C:\\",
        "description": "Check system files</｜｜DSML｜｜",
        "run_in_background": "true</｜｜DSML｜｜",
        "timeoutMs": "900000</｜｜DSML｜｜",
    }
    cleaned = ToolBridge._normalize_tool_args("pwsh", raw_args, SAMPLE_TOOLS)
    assert cleaned["command"] == "Get-ChildItem C:\\"
    assert cleaned["description"] == "Check system files"
    assert cleaned["run_in_background"] is True
    assert cleaned["timeoutMs"] == 900000
    assert isinstance(cleaned["timeoutMs"], (int, float))
    assert isinstance(cleaned["run_in_background"], bool)
    assert "DSML" not in str(cleaned)


def test_dsh_session_job_output_args():
    raw_args = {
        "job_id": "pwsh-224</｜｜DSML｜｜",
        "wait": "true</｜｜DSML｜｜",
    }
    cleaned = ToolBridge._normalize_tool_args("job_output", raw_args, SAMPLE_TOOLS)
    assert cleaned["job_id"] == "pwsh-224"
    assert cleaned["wait"] is True
    assert isinstance(cleaned["wait"], bool)
    assert "DSML" not in str(cleaned)


def test_dsh_session_job_list_empty_body():
    """无参工具如 job_list 输出空或残留闭标签时，应规整为 {}"""
    res = ToolBridge.parse_invoke_body("</｜｜DSML｜｜", "job_list", SAMPLE_TOOLS)
    norm = ToolBridge._normalize_tool_args("job_list", res, SAMPLE_TOOLS)
    assert norm == {}

    res2 = ToolBridge.parse_invoke_body("</｜｜DSML｜｜>", "job_list", SAMPLE_TOOLS)
    norm2 = ToolBridge._normalize_tool_args("job_list", res2, SAMPLE_TOOLS)
    assert norm2 == {}


def test_streaming_parser_multi_parameter_dsh_cases():
    """验证流式状态机在处理含未闭合 '>' 的参数时，不会提前截断 invoke"""
    stream = (
        '<｜｜DSML｜｜ invoke name="job_output">'
        '<｜｜DSML｜｜ parameter name="job_id" string="true">pwsh-224</｜｜DSML｜｜'
        '<｜｜DSML｜｜ parameter name="wait" string="true">true</｜｜DSML｜｜'
        '</｜｜DSML｜｜ invoke>'
    )
    p = StreamingToolParser(SAMPLE_TOOLS)
    events = list(p.feed(stream))
    assert len(events) == 1
    assert events[0][0] == "tool_call"
    tc = events[0][1]
    assert tc["function"]["name"] == "job_output"
    args = json.loads(tc["function"]["arguments"])
    assert args["job_id"] == "pwsh-224"
    assert args["wait"] is True
    assert "DSML" not in str(args)


def test_streaming_parser_parallel_tool_calls_no_cross_leak():
    """验证并行多工具调用时，前一个调用的参数闭合不会截断或污染后一个工具"""
    stream = (
        '<｜｜DSML｜｜ invoke name="pwsh">'
        '<｜｜DSML｜｜ parameter name="command" string="true">ls</｜｜DSML｜｜'
        '</｜｜DSML｜｜ invoke>'
        '<｜｜DSML｜｜ invoke name="job_output">'
        '<｜｜DSML｜｜ parameter name="job_id" string="true">pwsh-101</｜｜DSML｜｜'
        '</｜｜DSML｜｜ invoke>'
    )
    p = StreamingToolParser(SAMPLE_TOOLS)
    events = list(p.feed(stream))
    tc_events = [ev[1] for ev in events if ev[0] == "tool_call"]
    assert len(tc_events) == 2
    assert tc_events[0]["function"]["name"] == "pwsh"
    assert tc_events[1]["function"]["name"] == "job_output"
    args2 = json.loads(tc_events[1]["function"]["arguments"])
    assert args2["job_id"] == "pwsh-101"
    assert "DSML" not in str(args2)
