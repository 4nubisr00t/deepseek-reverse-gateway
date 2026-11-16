"""
原生工具调用格式的解析回归测试（T7.9 下半场）

背景：去掉"教模型用文本协议"的提示词后，模型恢复使用网页端**原生**工具调用块：
    <|tool_calls>
    <|invoke name="read_file">
    ...
    </|invoke>
    </|tool_calls>

但解析侧两条路径能力不对等：
  · streaming_parser._build_tool_call  —— 三种 body 形态都能解析
  · ToolBridge.extract_tool_calls_from_text (Safety-Net) —— 原先**只**认
    <|parameter name=...> 一种，遇到 XML 子标签或裸 JSON 就退化成 args={}
    → 客户端收到空参数工具调用（线上实测 T4：read_file{}）

而 Safety-Net 并不是冗余代码：首工具硬刹车会在闭合标签到齐前截断流，
状态机此时仍在"等待闭合"，只能靠 Safety-Net 事后从正文提取。
所以 Safety-Net 的能力必须与主路径对齐。

本测试锁死四种 body 形态在**两条路径**上都得到正确参数。
"""
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.streaming_parser import StreamingToolParser  # noqa: E402
from app.engine.tool_bridge import ToolBridge  # noqa: E402

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}]

WIN_PATH = r"C:\code\main.py"

# 四种模型实际会写出的 body 形态
BODIES = {
    "DSML parameter 标签": f'<|parameter name="path" string="true">{WIN_PATH}</|parameter>',
    "XML 子标签":          f"  <path>{WIN_PATH}</path>",
    "裸 JSON":             '{"path": "%s"}' % WIN_PATH.replace("\\", "\\\\"),
    "裸值（仅路径）":       WIN_PATH,
}

FULL_BLOCKS = {
    "A 文本协议": f'<tool_call>\n{{"name": "read_file", "arguments": {{"path": "{WIN_PATH.replace(chr(92), chr(92)*2)}"}}}}\n</tool_call>',
    "B 原生 parameter": (
        "<|tool_calls>\n<|invoke name=\"read_file\">\n"
        f'<|parameter name="path" string="true">{WIN_PATH}</|parameter>\n'
        "</|invoke>\n</|tool_calls>"
    ),
    "C 原生 子标签": (
        "<|tool_calls>\n<|invoke name=\"read_file\">\n"
        f"  <path>{WIN_PATH}</path>\n"
        "</|invoke>\n</|tool_calls>"
    ),
    "D 原生 裸JSON": (
        "<|tool_calls>\n<|invoke name=\"read_file\">\n"
        f'{{"path": "{WIN_PATH.replace(chr(92), chr(92)*2)}"}}\n'
        "</|invoke>\n</|tool_calls>"
    ),
    "E 原生 DeepSeek V3 特殊 Token": (
        f'<｜tool call begin｜>read_file<｜tool sep｜>{{"path": "{WIN_PATH.replace(chr(92), chr(92)*2)}"}}\n<｜tool call end｜>'
    ),
    "F 原生 DeepSeek V3 连字符与SentencePiece空白": (
        f'<｜｜tool\u2581call\u2581begin｜｜>read-file<｜｜tool\u2581sep｜｜>{{"path": "{WIN_PATH.replace(chr(92), chr(92)*2)}"}}<｜｜tool\u2581call\u2581end｜｜>'
    ),
}


# ─────────────────────────────────────────────────────────────
# parse_invoke_body：四种 body 形态
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("label,body", list(BODIES.items()))
def test_parse_invoke_body_extracts_path(label, body):
    """四种 body 形态都必须解析出 path，而不是退化成 input 或空参数。"""
    args = ToolBridge.parse_invoke_body(body, "read_file", TOOLS)
    assert "path" in args, f"[{label}] 未解析出 path，实际得到 {args!r}"
    assert args["path"] == WIN_PATH, f"[{label}] path 值不对：{args['path']!r}"


def test_parse_invoke_body_handles_windows_escape():
    """Windows 路径里 \\c 是非法 JSON 转义，裸 json.loads 会失败——必须走修复。"""
    body = '{"path": "C:\\code\\x.py"}'
    args = ToolBridge.parse_invoke_body(body, "read_file", TOOLS)
    assert args.get("path") == "C:\\code\\x.py", f"Windows 转义未修复: {args!r}"


def test_parse_invoke_body_does_not_mistake_content_for_params():
    """XML 子标签只取 schema 声明过的属性名，避免把正文里的 HTML 当参数。"""
    tools = [{
        "type": "function",
        "function": {
            "name": "write",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}, "filePath": {"type": "string"}},
                "required": ["content", "filePath"],
            },
        },
    }]
    body = "<content><div class='x'>hi</div></content>"
    args = ToolBridge.parse_invoke_body(body, "write", tools)
    assert "content" in args
    assert "<div" not in args.get("div", "")  # 不得把 div 当成参数


def test_parse_invoke_body_empty_returns_empty():
    assert ToolBridge.parse_invoke_body("", "read_file", TOOLS) == {}
    assert ToolBridge.parse_invoke_body("   ", "read_file", TOOLS) == {}


# ─────────────────────────────────────────────────────────────
# Safety-Net 整条路径：与主路径能力对齐
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("label,block", list(FULL_BLOCKS.items()))
def test_safety_net_extracts_args_for_all_formats(label, block):
    """Safety-Net 必须与流式状态机能力对齐，否则空参数会漏给客户端。"""
    _clean, tcs = ToolBridge.extract_tool_calls_from_text(block, TOOLS)
    assert len(tcs) == 1, f"[{label}] 提取数量异常: {len(tcs)}"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args.get("path") == WIN_PATH, f"[{label}] Safety-Net 参数错误: {args!r}"


@pytest.mark.parametrize("label,block", list(FULL_BLOCKS.items()))
def test_streaming_parser_extracts_args_for_all_formats(label, block):
    """流式状态机路径同样必须四种形态全通。"""
    p = StreamingToolParser(TOOLS)
    got = []
    for ch in block:                      # 逐字符模拟真实流式
        for ev, d in p.feed(ch):
            if ev == "tool_call":
                got.append(d)
    for ev, d in p.flush():
        if ev == "tool_call":
            got.append(d)
    assert len(got) == 1, f"[{label}] 状态机未捕获"
    args = json.loads(got[0]["function"]["arguments"])
    assert args.get("path") == WIN_PATH, f"[{label}] 状态机参数错误: {args!r}"


def test_two_paths_agree_on_arguments():
    """两条路径对同一输入必须给出同样的参数（消除能力不对等）。"""
    for label, block in FULL_BLOCKS.items():
        p = StreamingToolParser(TOOLS)
        got = []
        for ch in block:
            for ev, d in p.feed(ch):
                if ev == "tool_call":
                    got.append(d)
        for ev, d in p.flush():
            if ev == "tool_call":
                got.append(d)
        _clean, tcs = ToolBridge.extract_tool_calls_from_text(block, TOOLS)
        assert got and tcs, f"[{label}] 某条路径未捕获"
        a = json.loads(got[0]["function"]["arguments"])
        b = json.loads(tcs[0]["function"]["arguments"])
        assert a.get("path") == b.get("path"), (
            f"[{label}] 两条路径参数不一致：状态机={a!r} Safety-Net={b!r}"
        )


def test_incident_native_token_leakage_fixed():
    """线上实测缺陷复现：模型输出原生双管道与 SentencePiece 空白标记工具调用块，
    网关必须完整提取并归一化，严禁泄漏原生标签或工具名到客户端正文。
    """
    raw_leak = (
        "我先尝试读取你这个文件。\n\n"
        "<｜｜tool\u2581call\u2581begin｜｜>read-file<｜｜tool\u2581sep｜｜>"
        '{"path": "F:\\\\丰链答辩与交付成果\\\\01_答辩汇报PPT\\\\丰链答辩汇报_v13(真实小程序与图谱) - agent用副本.pdf"}'
        "<｜｜tool\u2581call\u2581end｜｜>"
    )

    # 1. 挂载标准 read 工具
    std_tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    clean, tcs = ToolBridge.extract_tool_calls_from_text(raw_leak, std_tools)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "read"
    assert "丰链答辩汇报" in tcs[0]["function"]["arguments"]
    assert "<｜" not in clean
    assert "read-file" not in clean

    # 2. 挂载文档阅读工具 office_read-document (线上环境 64 工具场景)
    office_tools = [
        {"type": "function", "function": {"name": "office_read-document", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    ]
    clean_off, tcs_off = ToolBridge.extract_tool_calls_from_text(raw_leak, office_tools)
    assert len(tcs_off) == 1
    assert tcs_off[0]["function"]["name"] == "office_read-document"
    args_off = json.loads(tcs_off[0]["function"]["arguments"])
    assert "filePath" in args_off
    assert "丰链答辩汇报" in args_off["filePath"]
    assert "<｜" not in clean_off
    assert "read-file" not in clean_off

    # 3. 流式状态机逐字符解析测试
    p = StreamingToolParser(office_tools)
    emitted_texts = []
    emitted_tcs = []
    for ch in raw_leak:
        for ev, d in p.feed(ch):
            if ev == "text":
                emitted_texts.append(d)
            elif ev == "tool_call":
                emitted_tcs.append(d)
    for ev, d in p.flush():
        if ev == "text":
            emitted_texts.append(d)
        elif ev == "tool_call":
            emitted_tcs.append(d)

    assert len(emitted_tcs) == 1
    assert emitted_tcs[0]["function"]["name"] == "office_read-document"
    full_text = "".join(emitted_texts)
    assert "<｜" not in full_text
    assert "read-file" not in full_text
    assert "我先尝试读取你这个文件" in full_text


def test_chinese_smart_quotes_repaired():
    """模型在工具 JSON 中写中文引号（“path”）时能容错自愈解析。"""
    raw_leak_smart_quotes = (
        "<｜｜tool\u2581call\u2581begin｜｜>read-file<｜｜tool\u2581sep｜｜>"
        '{“path”: “F:\\\\test.pdf”}'
        "<｜｜tool\u2581call\u2581end｜｜>"
    )
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    clean, tcs = ToolBridge.extract_tool_calls_from_text(raw_leak_smart_quotes, tools)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "read"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args.get("path") == "F:\\test.pdf"


def test_suspicion_detects_unclosed_native_token():
    """未闭合的原生特殊 Token 必须被判定为可疑异常触发自愈循环。"""
    broken_token = "我先读取一下：<｜｜tool\u2581call\u2581begin｜｜>read-file"
    assert ToolBridge.check_zero_tolerance_suspicion(broken_token, TOOLS) is True

