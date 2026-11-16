import json
import os
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.tool_bridge import ToolBridge
from app.engine.streaming_parser import StreamingToolParser

WRITE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "write",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
}]

EDIT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "edit",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
}]


def test_write_list_content_coerced_to_json_string():
    """复现 session line 35-37: content 为 list 时，自动转为合法 JSON 字符串"""
    raw_list = [
        "site:pic.people.com.cn 贫困村 探访",
        "site:photo.gmw.cn 贫困村 走访",
    ]
    input_args = {
        "file_path": "D:\\test\\q.json",
        "content": raw_list,
    }
    normalized = ToolBridge._normalize_tool_args("write", input_args, WRITE_TOOLS)
    assert isinstance(normalized["content"], str), f"content 必须被强制对齐为 string，实际得到 {type(normalized['content'])}"
    parsed_back = json.loads(normalized["content"])
    assert parsed_back == raw_list


def test_edit_malformed_dsml_tags_extracted_without_swallowing():
    """复现 session line 97: 模型偶发生成带有前置斜杠的开标签及未闭合标签"""
    body = (
        '<｜｜DSML｜｜ parameter name="file_path" string="true">D:\\hanako\\projects\\village-photo-hunt\\sg2.py'
        '</｜｜DSML｜｜ parameter name="old_string" string="true">queries = json.load(open(sys.argv[1], encoding=\'utf-8\'))</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="new_string" string="true">queries = json.load(open(sys.argv[1], encoding=\'utf-8-sig\'))</｜｜DSML｜｜ parameter>'
    )
    parsed = ToolBridge.parse_invoke_body(body, "edit", EDIT_TOOLS)
    assert parsed.get("file_path") == "D:\\hanako\\projects\\village-photo-hunt\\sg2.py"
    assert "old_string" in parsed
    assert parsed["old_string"] == "queries = json.load(open(sys.argv[1], encoding='utf-8'))"
    assert "new_string" in parsed
    assert parsed["new_string"] == "queries = json.load(open(sys.argv[1], encoding='utf-8-sig'))"


def test_edit_parameter_aliases_derived():
    """测试 old_str/search 与 new_str/replace 到 old_string/new_string 的别名映射"""
    args = {
        "file_path": "a.py",
        "old_str": "hello",
        "new_str": "world",
    }
    normalized = ToolBridge._normalize_tool_args("edit", args, EDIT_TOOLS)
    assert normalized.get("old_string") == "hello"
    assert normalized.get("new_string") == "world"


def test_schema_type_guard_coerces_primitives():
    """测试整数、浮点数、布尔以及字典的反向自愈"""
    tools = [{
        "type": "function",
        "function": {
            "name": "calc",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "rate": {"type": "number"},
                    "enable": {"type": "boolean"},
                    "meta": {"type": "object"},
                },
            },
        },
    }]
    args = {
        "count": "42",
        "rate": "3.14",
        "enable": "true",
        "meta": '{"user": "alice"}',
    }
    normalized = ToolBridge._normalize_tool_args("calc", args, tools)
    assert normalized["count"] == 42
    assert normalized["rate"] == 3.14
    assert normalized["enable"] is True
    assert normalized["meta"] == {"user": "alice"}


def test_command_not_swallowed_with_subsequent_dsml_tag():
    """复现用户最新截屏：pwsh 命令后紧跟未带 parameter 关键字的 description 标签，绝不得渗透进 command 命令行"""
    pwsh_tools = [{
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    }]
    body = (
        '<｜｜DSML｜｜ parameter name="command" string="true">cmd /c dir /b F:\\projects\\*.pptx</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ description" string="true">列出工作区pptx文件</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ invoke>'
    )
    parsed = ToolBridge.parse_invoke_body(body, "pwsh", pwsh_tools)
    normalized = ToolBridge._normalize_tool_args("pwsh", parsed, pwsh_tools)

    assert normalized["command"] == "cmd /c dir /b F:\\projects\\*.pptx"
    assert "DSML" not in normalized["command"]
    assert "parameter" not in normalized["command"]
    assert "description" not in normalized["command"]
    assert normalized["description"] == "列出工作区pptx文件"


def test_command_not_swallowed_without_closing_param_tag():
    """测试模型缺失 </parameter> 闭合标签或使用不同空白间隔时，命令也不会渗漏后续标签"""
    pwsh_tools = [{
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    }]
    # 模拟无闭合标签与多空格的 DSML 变体
    body = (
        '< | | DSML | | parameter name="command" string="true">python F:\\projects\\verify9.py\n'
        '< | | DSML | | description" string="true">验证第9页形状布局\n'
        '</ | | DSML | | invoke>'
    )
    parsed = ToolBridge.parse_invoke_body(body, "pwsh", pwsh_tools)
    normalized = ToolBridge._normalize_tool_args("pwsh", parsed, pwsh_tools)

    assert normalized["command"] == "python F:\\projects\\verify9.py"
    assert normalized["description"] == "验证第9页形状布局"
    assert "DSML" not in normalized["command"]
    assert "description" not in normalized["command"]


def test_bare_dsml_parameter_block_without_invoke_opening_tag():
    """复现 20:55 用户截屏故障：DeepSeek 遗漏 <invoke name="..."> 直接输出参数块，正确反向推导工具名并提取"""
    tools = [{
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "workdir": {"type": "string"},
                },
                "required": ["command", "description"],
            },
        },
    }]
    raw_text = (
        '端口 9222 是开着的。\n'
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ parameter name="command" string="true">Get-Process</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="description" string="true">Query processes</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    clean_text, tcs = ToolBridge.extract_tool_calls_from_text(raw_text, tools)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "pwsh"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args["command"] == "Get-Process"
    assert args["description"] == "Query processes"
    assert "Get-Process" not in clean_text
    assert "DSML" not in clean_text


def test_streaming_parser_bare_dsml_pwsh_not_leaking():
    """流式解析测试：缺失 <invoke> 标签时，流式输出不得将脚本代码当文本流出，末尾应发射完整 tool_call"""
    from app.engine.streaming_parser import StreamingToolParser

    tools = [{
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["command", "description"],
            },
        },
    }]
    raw_text = (
        '前置分析说明文本。\n'
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ parameter name="command" string="true">$v = Invoke-RestMethod http://127.0.0.1:9222</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="description" string="true">Check 9222</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    parser = StreamingToolParser(tools=tools)
    events = []
    for i in range(0, len(raw_text), 20):
        events.extend(parser.feed(raw_text[i:i+20]))
    events.extend(parser.flush())

    text_pieces = [d for t, d in events if t == "text"]
    tool_calls = [d for t, d in events if t == "tool_call"]

    full_text = "".join(text_pieces)
    assert "前置分析说明文本" in full_text
    assert "Invoke-RestMethod" not in full_text
    assert "DSML" not in full_text
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "pwsh"
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert "Invoke-RestMethod" in args["command"]



