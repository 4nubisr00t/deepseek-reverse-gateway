import pytest
import json
from app.engine.dsml_stack_parser import DSMLStackParser

TOOLS = [
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
            "name": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
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


def test_standard_dsml_parameters():
    body = (
        '<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="description" string="true">List items</｜｜DSML｜｜ parameter>'
    )
    res = DSMLStackParser.parse_invoke_body(body, "pwsh", TOOLS)
    assert res["command"] == "Get-ChildItem"
    assert res["description"] == "List items"


def test_session_dump_missing_trailing_gt():
    """复现 session dump 中 900000</｜｜DSML｜｜ 与 true</｜｜DSML｜｜ 缺失 > 的畸变"""
    body = (
        '<｜｜DSML｜｜ parameter name="command" string="true">dir C:\\</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="description" string="true">Check C drive</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="run_in_background" string="true">true</｜｜DSML｜｜\n'
        '<｜｜DSML｜｜ parameter name="timeoutMs" string="true">900000</｜｜DSML｜｜\n'
        '</｜｜DSML｜｜ invoke>'
    )
    res = DSMLStackParser.parse_invoke_body(body, "pwsh", TOOLS)
    assert res["command"] == "dir C:\\"
    assert res["description"] == "Check C drive"
    assert res["run_in_background"] is True
    assert res["timeoutMs"] == 900000
    assert "DSML" not in str(res)


def test_session_dump_job_output_headless():
    body = (
        '<｜｜DSML｜｜ parameter name="job_id" string="true">pwsh-224</｜｜DSML｜｜\n'
        '<｜｜DSML｜｜ parameter name="wait" string="true">true</｜｜DSML｜｜\n'
    )
    res = DSMLStackParser.parse_invoke_body(body, "job_output", TOOLS)
    assert res["job_id"] == "pwsh-224"
    assert res["wait"] is True
    assert "DSML" not in str(res)


def test_session_dump_job_list_empty():
    body = '</｜｜DSML｜｜'
    res = DSMLStackParser.parse_invoke_body(body, "job_list", TOOLS)
    assert res == {}

    body2 = '</｜｜DSML｜｜>'
    res2 = DSMLStackParser.parse_invoke_body(body2, "job_list", TOOLS)
    assert res2 == {}


def test_code_with_math_and_operators_in_param():
    """参数值内部包含 <, >, &&, 换行与引号时，符号栈绝不发生误闭合"""
    code_text = (
        'def check_range(a, b):\n'
        '    if a < b and b > 10:\n'
        '        return "<tag>inside</tag>"\n'
        '    return None\n'
    )
    body = f'<｜｜DSML｜｜ parameter name="content" string="true">{code_text}</｜｜DSML｜｜ parameter>'
    res = DSMLStackParser.parse_invoke_body(body, "write", TOOLS)
    assert res["content"] == code_text.strip()


def test_implicit_auto_pop_when_close_omitted():
    """模型漏写闭合标签直接输出下一个参数，符号栈自动隐式归约"""
    body = (
        '<｜｜DSML｜｜ parameter name="path" string="true">C:\\work\\file.txt\n'
        '<｜｜DSML｜｜ parameter name="content" string="true">Hello World\n'
    )
    res = DSMLStackParser.parse_invoke_body(body, "write", TOOLS)
    assert res["path"] == "C:\\work\\file.txt"
    assert res["content"] == "Hello World"


def test_xml_child_tags_support():
    """支持 Schema 声明过的 XML 子标签 <path>...</path>"""
    body = (
        '<path>C:\\test.py</path>\n'
        '<content>print("hi")</content>'
    )
    res = DSMLStackParser.parse_invoke_body(body, "write", TOOLS)
    assert res["path"] == "C:\\test.py"
    assert res["content"] == 'print("hi")'


def test_raw_json_inside_invoke():
    """支持 Invoke 体内是裸 JSON 的情况"""
    body = '{"path": "C:\\\\foo.txt", "content": "hello"}'
    res = DSMLStackParser.parse_invoke_body(body, "write", TOOLS)
    assert res["path"] == "C:\\foo.txt"
    assert res["content"] == "hello"


def test_full_document_extraction_parallel_tools():
    """测试包含前言、多个并发 Invoke、后记文本的完整提取"""
    raw_doc = (
        '我来为你执行以下操作：\n\n'
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="pwsh">\n'
        '<｜｜DSML｜｜ parameter name="command" string="true">ls -la</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '<｜｜DSML｜｜ invoke name="job_output">\n'
        '<｜｜DSML｜｜ parameter name="job_id" string="true">pwsh-999</｜｜DSML｜｜\n'
        '<｜｜DSML｜｜ parameter name="wait" string="true">true</｜｜DSML｜｜\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>\n\n'
        '执行结果汇总如下。'
    )
    clean_text, tcs = DSMLStackParser.extract_tool_calls_from_text(raw_doc, TOOLS)
    assert clean_text == '我来为你执行以下操作：\n\n执行结果汇总如下。'
    assert len(tcs) == 2
    assert tcs[0]["function"]["name"] == "pwsh"
    args0 = json.loads(tcs[0]["function"]["arguments"])
    assert args0["command"] == "ls -la"

    assert tcs[1]["function"]["name"] == "job_output"
    args1 = json.loads(tcs[1]["function"]["arguments"])
    assert args1["job_id"] == "pwsh-999"
    assert args1["wait"] is True


def test_bare_dsml_parameter_block_without_invoke():
    """测试模型缺失 <invoke> 开标签时，符号栈自动推断工具名并归约参数"""
    raw_doc = (
        '正在为你扫描：\n'
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ parameter name="command" string="true">Get-Process</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="description" string="true">Query processes</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    clean_text, tcs = DSMLStackParser.extract_tool_calls_from_text(raw_doc, TOOLS)
    assert clean_text == '正在为你扫描：'
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "pwsh"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args["command"] == "Get-Process"
    assert args["description"] == "Query processes"


def test_markup_and_c_headers_preserved_in_parameter():
    """测试 HTML、XML、以及 C 语言头文件等标签语言在参数内部 100% 完整保留不被吞"""
    html_snippet = '<!DOCTYPE html><html><head><title>Test</title></head><body><div class="box">Hello</div></body></html>'
    c_snippet = '#include <stdio.h>\n#include <vector>\nif (a < b && b > 0) return true;'

    # 1. HTML in write content
    raw_doc_html = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="write">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">index.html</｜｜DSML｜｜ parameter>\n'
        f'<｜｜DSML｜｜ parameter name="content" string="true">{html_snippet}</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    clean_text, tcs = DSMLStackParser.extract_tool_calls_from_text(raw_doc_html, TOOLS)
    assert len(tcs) == 1
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args["content"] == html_snippet

    # 2. C headers in write content
    raw_doc_c = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="write">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">main.c</｜｜DSML｜｜ parameter>\n'
        f'<｜｜DSML｜｜ parameter name="content" string="true">{c_snippet}</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    clean_text_c, tcs_c = DSMLStackParser.extract_tool_calls_from_text(raw_doc_c, TOOLS)
    assert len(tcs_c) == 1
    args_c = json.loads(tcs_c[0]["function"]["arguments"])
    assert args_c["content"] == c_snippet


def test_headless_close_does_not_truncate_subsequent_parameters():
    """测试漏写 parameter 关键字的 </|DSML|> 闭标签不会导致后续参数丢失或分裂"""
    raw_doc = (
        '<|DSML| invoke name="write">\n'
        '<|DSML| parameter name="path">scratch/t2.txt</|DSML|>\n'
        '</|DSML|>\n'
        '<|DSML| parameter name="content">hello world</|DSML| parameter>\n'
        '</|DSML| invoke>'
    )
    clean_text, tcs = DSMLStackParser.extract_tool_calls_from_text(raw_doc, TOOLS)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "write"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert "t2.txt" in args["path"]
    assert args["content"] == "hello world"


def test_bare_parameter_with_headless_close():
    """测试无 invoke 外壳的裸参数结构在遇到 </|DSML|> 时不丢失多参数"""
    raw_doc = (
        '<|DSML| parameter name="file_path">scratch/t2.txt</|DSML|>\n'
        '</|DSML|>\n'
        '<|DSML| parameter name="content">hello world</|DSML| parameter>\n'
        '</|DSML| invoke>'
    )
    clean_text, tcs = DSMLStackParser.extract_tool_calls_from_text(raw_doc, TOOLS)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "write"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert "t2.txt" in args["path"]
    assert args["content"] == "hello world"



