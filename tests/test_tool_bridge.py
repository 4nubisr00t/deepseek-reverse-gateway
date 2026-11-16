import sys
import os
from pathlib import Path

# 之前这里硬编码 r"E:\OH-WorkSpace\ds-gateway"：只要仓库不在开发机那个目录，
# 这个测试就会 import 到别处的 app（或直接 ImportError）。改为相对本文件定位仓库根。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.tool_bridge import ToolBridge

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run shell command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number"},
                    "workdir": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["command", "description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write file",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "filePath": {"type": "string"}
                },
                "required": ["content", "filePath"]
            }
        }
    }
]

# Test 1: XML bash
clean, tc = ToolBridge.extract_tool_calls_from_text("<bash>python staff.py</bash>", tools_schema)
print("Test 1 (XML bash):", len(tc))
print("  tc[0]:", tc[0]["function"])
assert "description" in tc[0]["function"]["arguments"]

# Test 2: Code block bash
clean, tc = ToolBridge.extract_tool_calls_from_text("```bash\npython staff.py\n```", tools_schema)
print("Test 2 (Code block bash):", len(tc))
print("  tc[0]:", tc[0]["function"])
assert "description" in tc[0]["function"]["arguments"]

# Test 3: JSON write
json_text = """```json
{
  "tool": "write",
  "arguments": {
    "path": "test.txt",
    "content": "hello world"
  }
}
```"""
clean, tc = ToolBridge.extract_tool_calls_from_text(json_text, tools_schema)
print("Test 3 (JSON write with path):", len(tc))
print("  tc[0]:", tc[0]["function"])
assert "filePath" in tc[0]["function"]["arguments"]

# Test 4: Duplicate loop detection
mock_history = [
    {"role": "user", "content": "run script"},
    {"role": "assistant", "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "python test.py"}'}}]},
    {"role": "tool", "content": "error"},
    {"role": "assistant", "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "python test.py"}'}}]},
    {"role": "tool", "content": "error"},
    {"role": "assistant", "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "python test.py"}'}}]},
    {"role": "tool", "content": "error"},
    {"role": "assistant", "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "python test.py"}'}}]},
    {"role": "tool", "content": "error"},
    {"role": "assistant", "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "python test.py"}'}}]},
    {"role": "tool", "content": "error"}
]
candidate_tc = [{"function": {"name": "bash", "arguments": '{"command": "python test.py"}'}}]
is_loop = ToolBridge.check_duplicate_loop(mock_history, candidate_tc)
print("Test 4 (Loop detection):", is_loop)
assert is_loop is True

candidate_diff = [{"function": {"name": "bash", "arguments": '{"command": "python fix.py"}'}}]
assert ToolBridge.check_duplicate_loop(mock_history, candidate_diff) is False

# Test 5: Nested XML child tag read
tools_with_read = tools_schema + [{
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read file",
        "parameters": {
            "type": "object",
            "properties": {"filePath": {"type": "string"}, "path": {"type": "string"}},
            "required": ["filePath"]
        }
    }
}]
sample_read = "<read>\n<path>C:\\Users\\test\\image.jpg</path>\n</read>"
clean, tc = ToolBridge.extract_tool_calls_from_text(sample_read, tools_with_read)
print("Test 5 (Nested XML child tag read):", len(tc))
print("  tc[0]:", tc[0]["function"])
assert len(tc) == 1
assert tc[0]["function"]["name"] == "read"
assert "image.jpg" in tc[0]["function"]["arguments"]

# Test 6: Pulse tag normalization & deduplication
sample_dirty = """我仔细看看你的桌面～
<pulse>
Vibe: 主人发了桌面
</pulse>
我先看一眼。
<pulse>
Vibe: 多余的脉搏
</pulse>
<read><path>img.png</path></read>"""
clean_text, tc = ToolBridge.extract_tool_calls_from_text(sample_dirty, tools_with_read)
print("Test 6 (Tag normalization & deduplication):", clean_text.startswith("<pulse>"))
assert clean_text.startswith("<pulse>")
assert clean_text.count("<pulse>") == 1
assert len(tc) == 1

# Test 7: DSML tool calling (DeepSeek Markup Language with fullwidth pipes)
import json
sample_dsml = """<reflect>
仔细观察用户截图
</reflect>
我先仔细看一下这张截图的细节，确认布局、配色和每个元素。

<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="read">
<｜｜DSML｜｜parameter name="path" string="true">C:\\Users\\LLZY2\\.hanako\\uploads\\粘贴图片_mtnz9r3t_088e92f7.png</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>"""

clean_text, tc = ToolBridge.extract_tool_calls_from_text(sample_dsml, tools_with_read)
print("Test 7 (DSML tool calling):", len(tc))
assert len(tc) == 1
assert tc[0]["function"]["name"] == "read"
args = json.loads(tc[0]["function"]["arguments"])
assert "粘贴图片_mtnz9r3t_088e92f7.png" in args["path"]
assert "粘贴图片_mtnz9r3t_088e92f7.png" in args["filePath"]
assert "DSML" not in clean_text
assert "我先仔细看一下这张截图的细节" in clean_text
print("  clean_text:", repr(clean_text))
print("  tc[0]:", tc[0])

# Test 8: Self-closing XML tool call with attributes (e.g. current_status)
tools_with_status = tools_with_read + [{
    "type": "function",
    "function": {
        "name": "current_status",
        "description": "Check status",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "key": {"type": "string"}},
            "required": ["action"]
        }
    }
}]
sample_self_closing = '我先查一下会话里已生成的文件状态。\n<current_status action="get" key="session_files" />'
clean_text, tc = ToolBridge.extract_tool_calls_from_text(sample_self_closing, tools_with_status)
print("Test 8 (Self-closing XML tool calling):", len(tc))
assert len(tc) == 1
assert tc[0]["function"]["name"] == "current_status"
args = json.loads(tc[0]["function"]["arguments"])
assert args["action"] == "get"
assert args["key"] == "session_files"
assert "current_status" not in clean_text
print("  clean_text:", repr(clean_text))
print("  tc[0]:", tc[0])

print("\nALL UNIT TESTS (INCLUDING LOOP GUARD, DSML, SELF-CLOSING XML & TAG SANITIZATION) PASSED SUCCESSFULLY!")

