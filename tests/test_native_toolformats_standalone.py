import sys
import os
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.streaming_parser import StreamingToolParser
from app.engine.tool_bridge import ToolBridge

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

# 1. Test parse_invoke_body
for label, body in BODIES.items():
    args = ToolBridge.parse_invoke_body(body, "read_file", TOOLS)
    assert args.get("path") == WIN_PATH, f"[{label}] failed: {args}"
print("Phase 1 (BODIES) passed!")

# 2. Test Safety-Net on FULL_BLOCKS
for label, block in FULL_BLOCKS.items():
    _clean, tcs = ToolBridge.extract_tool_calls_from_text(block, TOOLS)
    assert len(tcs) == 1, f"[{label}] extraction count error: {len(tcs)}"
    args = json.loads(tcs[0]["function"]["arguments"])
    assert args.get("path") == WIN_PATH, f"[{label}] Safety-Net args error: {args}"
print("Phase 2 (Safety-Net on FULL_BLOCKS) passed!")

# 3. Test StreamingToolParser on FULL_BLOCKS
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
    assert len(got) == 1, f"[{label}] parser failed"
    args = json.loads(got[0]["function"]["arguments"])
    assert args.get("path") == WIN_PATH, f"[{label}] parser args error: {args}"
print("Phase 3 (StreamingToolParser on FULL_BLOCKS) passed!")

# 4. Test Incident native token leakage
raw_leak = (
    "我先尝试读取你这个文件。\n\n"
    "<｜｜tool\u2581call\u2581begin｜｜>read-file<｜｜tool\u2581sep｜｜>"
    '{"path": "F:\\\\丰链答辩与交付成果\\\\01_答辩汇报PPT\\\\丰链答辩汇报_v13(真实小程序与图谱) - agent用副本.pdf"}'
    "<｜｜tool\u2581call\u2581end｜｜>"
)

office_tools = [
    {"type": "function", "function": {"name": "office_read-document", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
]
clean_off, tcs_off = ToolBridge.extract_tool_calls_from_text(raw_leak, office_tools)
assert len(tcs_off) == 1
assert tcs_off[0]["function"]["name"] == "office_read-document"
assert "<｜" not in clean_off
assert "read-file" not in clean_off

p_stream = StreamingToolParser(office_tools)
stream_texts = []
stream_tcs = []
for ch in raw_leak:
    for ev, d in p_stream.feed(ch):
        if ev == "text":
            stream_texts.append(d)
        elif ev == "tool_call":
            stream_tcs.append(d)
for ev, d in p_stream.flush():
    if ev == "text":
        stream_texts.append(d)
    elif ev == "tool_call":
        stream_tcs.append(d)

assert len(stream_tcs) == 1
assert stream_tcs[0]["function"]["name"] == "office_read-document"
full_text = "".join(stream_texts)
assert "<｜" not in full_text
assert "read-file" not in full_text
print("Phase 4 (Incident Native Token Leakage Fixed) passed!")

# 5. Test Chinese smart quotes
smart_quotes = (
    "<｜｜tool\u2581call\u2581begin｜｜>read-file<｜｜tool\u2581sep｜｜>"
    '{“path”: “F:\\\\test.pdf”}'
    "<｜｜tool\u2581call\u2581end｜｜>"
)
tools_read = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
clean_sq, tcs_sq = ToolBridge.extract_tool_calls_from_text(smart_quotes, tools_read)
assert len(tcs_sq) == 1
assert tcs_sq[0]["function"]["name"] == "read"
assert json.loads(tcs_sq[0]["function"]["arguments"]).get("path") == "F:\\test.pdf"
print("Phase 5 (Chinese smart quotes) passed!")

# 6. Test Suspicion detection
assert ToolBridge.check_zero_tolerance_suspicion("我先读取一下：<｜｜tool\u2581call\u2581begin｜｜>read-file", TOOLS) is True
print("Phase 6 (Suspicion detection) passed!")

print("\n=======================================================")
print("  ALL NATIVE TOOL FORMAT & LEAK REGRESSION TESTS PASSED! ")
print("=======================================================")
