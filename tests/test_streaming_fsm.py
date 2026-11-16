import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.engine.streaming_parser import StreamingToolParser
from app.engine.tool_bridge import ToolBridge

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run shell",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write file",
            "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filePath", "content"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "office_html-to-pdf",
            "description": "Convert HTML to PDF",
            "parameters": {"type": "object", "properties": {"htmlPath": {"type": "string"}, "filename": {"type": "string"}}, "required": ["htmlPath"]}
        }
    }
]

def test_streaming_instant_text_and_tool():
    print("--- Running test_streaming_instant_text_and_tool ---")
    parser = StreamingToolParser(TOOLS)
    
    # 模拟文本 + 工具 + 结尾文本
    chunks = [
        "我先为你",
        "创建目录：\n",
        "<bash>mkdir",
        " -p my_dir</bash>\n",
        "目录创建完毕！"
    ]

    emitted_texts = []
    emitted_tools = []

    for c in chunks:
        events = parser.feed(c)
        for ev, data in events:
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)

    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    full_text = "".join(emitted_texts)
    print(f"Emitted text: {repr(full_text)}")
    print(f"Emitted tools count: {len(emitted_tools)}")
    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "bash"
    assert "mkdir -p my_dir" in emitted_tools[0]["function"]["arguments"]
    assert "<bash>" not in full_text
    assert "我先为你创建目录：" in full_text
    assert "目录创建完毕！" in full_text
    print("✅ test_streaming_instant_text_and_tool PASSED")

def test_code_block_immunity():
    print("\n--- Running test_code_block_immunity ---")
    parser = StreamingToolParser(TOOLS)
    
    # Markdown 代码块里的 <write> 绝对不能被当成工具拦截！
    content = (
        "下面是一个写文件的示例教程，请不要执行它：\n"
        "```xml\n"
        "<write path=\"sample.txt\">示例内容</write>\n"
        "```\n"
        "教程结束。"
    )

    # 碎片化输入 (以 5 个字符为单位打散)
    emitted_texts = []
    emitted_tools = []
    for i in range(0, len(content), 5):
        chunk = content[i:i+5]
        for ev, data in parser.feed(chunk):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)

    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    full_text = "".join(emitted_texts)
    print(f"Emitted text in code block test: {repr(full_text)}")
    print(f"Emitted tools count: {len(emitted_tools)}")
    
    # 核心断言：工具拦截数为 0，且文本里完整保留代码块与 XML
    assert len(emitted_tools) == 0, f"Expected 0 tools inside code block, got {len(emitted_tools)}"
    assert "<write path=\"sample.txt\">" in full_text
    print("✅ test_code_block_immunity PASSED")

def test_self_closing_tag_streaming():
    print("\n--- Running test_self_closing_tag_streaming ---")
    parser = StreamingToolParser(TOOLS)
    
    content = "正在生成 PDF 报表：\n<office_html-to-pdf htmlPath=\"report.html\" filename=\"my_report\" pageSize=\"A4\" />\nPDF 已交付！"
    
    emitted_texts = []
    emitted_tools = []
    # 随机以 2~4 个字符分片
    i = 0
    step = 3
    while i < len(content):
        chunk = content[i:i+step]
        for ev, data in parser.feed(chunk):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
        i += step

    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    print(f"Emitted text: {repr(''.join(emitted_texts))}")
    print(f"Emitted tools: {emitted_tools}")
    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "office_html-to-pdf"
    assert "report.html" in emitted_tools[0]["function"]["arguments"]
    print("✅ test_self_closing_tag_streaming PASSED")

def test_self_closing_with_internal_arrow():
    print("\n--- Running test_self_closing_with_internal_arrow ---")
    parser = StreamingToolParser(TOOLS)

    content = '<write filePath="calc_fib.py" content="def fib(n: int) -> int: return n" />\n<bash>python calc_fib.py</bash>'
    emitted_texts = []
    emitted_tools = []

    for ev, data in parser.feed(content):
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    print(f"Emitted tools count: {len(emitted_tools)}")
    assert len(emitted_tools) == 2
    assert emitted_tools[0]["function"]["name"] == "write"
    assert "-> int:" in emitted_tools[0]["function"]["arguments"]
    assert emitted_tools[1]["function"]["name"] == "bash"
    print("✅ test_self_closing_with_internal_arrow PASSED")

def test_tool_call_json_block():
    print("\n--- Running test_tool_call_json_block ---")
    parser = StreamingToolParser(TOOLS)
    chunks = [
        "我来帮你写入脚本：\n",
        "<tool_call>\n",
        '{"name": "write", "arguments": ',
        '{"filePath": "run.py", "content": "print(\\"ok\\")\\n"}}\n',
        "</tool_call>\n",
        "已经完成写入。"
    ]
    emitted_texts = []
    emitted_tools = []
    for c in chunks:
        for ev, data in parser.feed(c):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "write"
    import json
    parsed_args = json.loads(emitted_tools[0]["function"]["arguments"])
    assert parsed_args["filePath"] == "run.py"
    assert "print(\"ok\")" in parsed_args["content"]
    assert "<tool_call>" not in "".join(emitted_texts)
    print("✅ test_tool_call_json_block PASSED")

def test_bracket_call():
    print("\n--- Running test_bracket_call ---")
    parser = StreamingToolParser(TOOLS)
    chunk = '执行命令：[call:bash]{"command": "ls -la"}[/call]执行完毕'
    emitted_texts = []
    emitted_tools = []
    for ev, data in parser.feed(chunk):
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "bash"
    import json
    parsed_args = json.loads(emitted_tools[0]["function"]["arguments"])
    assert parsed_args["command"] == "ls -la"
    print("✅ test_bracket_call PASSED")

def test_char_by_char_tool_call_streaming():
    print("\n--- Running test_char_by_char_tool_call_streaming ---")
    parser = StreamingToolParser(TOOLS)
    full_text = '我先看看：\n<tool_call>\n{"name": "write", "arguments": {"filePath": "app.py", "content": "print(1)"}}\n</tool_call>'
    emitted_texts = []
    emitted_tools = []
    for ch in full_text:
        for ev, data in parser.feed(ch):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "write"
    assert "<tool_call>" not in "".join(emitted_texts)
    print("✅ test_char_by_char_tool_call_streaming PASSED")

def test_stuttering_and_windows_backslash_streaming():
    print("\n--- Running test_stuttering_and_windows_backslash_streaming ---")
    parser = StreamingToolParser(TOOLS + [{
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "workdir": {"type": "string"}}}
        }
    }])
    raw_output = """<tool_call>
<tool_call>
{"name": "pwsh", "arguments": {"command": "Get-ChildItem -Force | Select-Object Mode,Length,Name | Format-Table -AutoSize", "description": "List top-level workspace entries", "workdir": "E:\\OH-WorkSpace\\ds-free-api-project"}}
</tool_call>"""
    emitted_texts = []
    emitted_tools = []
    for i in range(0, len(raw_output), 3):
        chunk = raw_output[i:i+3]
        for ev, data in parser.feed(chunk):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "pwsh"
    import json
    args = json.loads(emitted_tools[0]["function"]["arguments"])
    assert "Get-ChildItem" in args["command"]
    assert "ds-free-api-project" in args["workdir"]
    full_text = "".join(emitted_texts)
    assert "<" not in full_text
    assert "tool_call" not in full_text
    print("✅ test_stuttering_and_windows_backslash_streaming PASSED")

def test_read_image_tool_and_normalization():
    print("\n--- Running test_read_image_tool_and_normalization ---")
    tools = TOOLS + [
        {
            "type": "function",
            "function": {
                "name": "read_image",
                "description": "Read image file",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"]
                }
            }
        }
    ]
    parser = StreamingToolParser(tools)
    raw_output = '<tool_call>\n{"name": "read_image", "arguments": {"path": "C:\\images\\chart.png"}}\n</tool_call>'
    emitted_tools = []
    for ev, data in parser.feed(raw_output):
        if ev == "tool_call":
            emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "tool_call":
            emitted_tools.append(data)

    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "read_image"
    import json
    args = json.loads(emitted_tools[0]["function"]["arguments"])
    # 验证 file_path, path, filePath 三向对齐
    assert args["file_path"] == "C:\\images\\chart.png"
    assert args["path"] == "C:\\images\\chart.png"
    assert args["filePath"] == "C:\\images\\chart.png"
    print("✅ test_read_image_tool_and_normalization PASSED")

def test_no_tag_leakage_on_malformed_tool():
    print("\n--- Running test_no_tag_leakage_on_malformed_tool ---")
    parser = StreamingToolParser(TOOLS)
    # 模拟模型输出了未知工具或损坏的 JSON
    raw_output = '<tool_call>\n{"name": "unknown_tool", "arg": 123}\n</tool_call>'
    emitted_texts = []
    emitted_tools = []
    for ev, data in parser.feed(raw_output):
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)

    full_text = "".join(emitted_texts)
    # 确保 <tool_call> 和 </tool_call> 绝不会原样泄漏给客户端
    assert "<tool_call>" not in full_text
    assert "</tool_call>" not in full_text
    print(f"Fallback cleaned text: {repr(full_text)}")
    print("✅ test_no_tag_leakage_on_malformed_tool PASSED")

def test_dsml_tool_calls_streaming():
    print("\n--- Running test_dsml_tool_calls_streaming ---")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "description": "Execute shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["command"]
                }
            }
        }
    ]
    parser = StreamingToolParser(tools)
    raw_output = (
        "先探测端口状态：\n"
        "<｜｜DSML｜｜tool_calls>\n"
        '<｜｜DSML｜｜invoke name="execute_command">\n'
        '<｜｜DSML｜｜parameter name="command" string="true">netstat -ano | findstr :9222</｜｜DSML｜｜parameter>\n'
        '<｜｜DSML｜｜parameter name="description" string="true">检查端口</｜｜DSML｜｜parameter>\n'
        "</｜｜DSML｜｜invoke>\n"
        "</｜｜DSML｜｜tool_calls>\n"
        "探测完毕。"
    )
    emitted_texts = []
    emitted_tools = []
    for chunk in [raw_output[:25], raw_output[25:70], raw_output[70:120], raw_output[120:]]:
        for ev, data in parser.feed(chunk):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    full_text = "".join(emitted_texts)
    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "exec_command"
    import json
    args = json.loads(emitted_tools[0]["function"]["arguments"])
    assert "netstat -ano" in args["command"]
    assert "DSML" not in full_text
    assert "tool_calls" not in full_text
    assert "先探测端口状态：" in full_text
    print("✅ test_dsml_tool_calls_streaming PASSED")

def test_raw_json_tool_streaming():
    print("\n--- Running test_raw_json_tool_streaming ---")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "exec_command",
                "description": "Execute shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"]
                }
            }
        }
    ]
    parser = StreamingToolParser(tools)
    raw_output = '{"tool": "execute_command", "args": {"command": "start chrome --remote-debugging-port=9222"}}'
    emitted_texts = []
    emitted_tools = []
    for chunk in [raw_output[:15], raw_output[15:45], raw_output[45:]]:
        for ev, data in parser.feed(chunk):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "exec_command"
    import json
    args = json.loads(emitted_tools[0]["function"]["arguments"])
    assert "start chrome" in args["command"]
    assert "".join(emitted_texts) == ""
    print("✅ test_raw_json_tool_streaming PASSED")

def test_chinese_tool_call_streaming():
    print("\n--- Running test_chinese_tool_call_streaming ---")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "pwsh",
                "description": "Execute PowerShell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
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
                        "file_path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        }
    ]
    parser = StreamingToolParser(tools)
    raw_output = '[工具调用]: run_command\n{"command": "netstat -ano | findstr :9222 && tasklist | findstr /i chrome"}'

    emitted_texts = []
    emitted_tools = []
    for chunk in [raw_output[:12], raw_output[12:35], raw_output[35:]]:
        for ev, data in parser.feed(chunk):
            if ev == "text":
                emitted_texts.append(data)
            elif ev == "tool_call":
                emitted_tools.append(data)
    for ev, data in parser.flush():
        if ev == "text":
            emitted_texts.append(data)
        elif ev == "tool_call":
            emitted_tools.append(data)

    print("Emitted text:", repr("".join(emitted_texts)))
    print("Emitted tools:", emitted_tools)
    assert len(emitted_tools) == 1
    assert emitted_tools[0]["function"]["name"] == "pwsh"
    import json
    args = json.loads(emitted_tools[0]["function"]["arguments"])
    assert "netstat" in args["command"]
    assert "description" in args
    assert "[工具调用]" not in "".join(emitted_texts)
    print("✅ test_chinese_tool_call_streaming PASSED")

def test_dsml_tool_call_tag_no_leak():
    tools = [{'type': 'function', 'function': {'name': 'read_image', 'parameters': {}}}]
    parser = StreamingToolParser(tools)
    raw_tool_stream = '<｜｜DSML｜｜tool_call>\n{"name": "read_image", "arguments": {"file_path": "C:\\\\foo.png"}}\n</｜｜DSML｜｜tool_call>'

    events = []
    for chunk in [raw_tool_stream[:25], raw_tool_stream[25:60], raw_tool_stream[60:]]:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())

    text_events = [val for t, val in events if t == 'text']
    assert not text_events, f"LEAK DETECTED: {text_events}"
    tool_events = [val for t, val in events if t == 'tool_call']
    assert len(tool_events) == 1
    assert tool_events[0]["function"]["name"] == "read_image"
    print("✅ test_dsml_tool_call_tag_no_leak PASSED")

def test_markdown_newline_preservation():
    parser = StreamingToolParser([{'type': 'function', 'function': {'name': 'bash', 'parameters': {}}}])
    input_text = '''### 1. 安装插件
```powershell
dsh plugin add dshmarket
```

---

### 2. 确认插件是否已被添加
```powershell
dsh plugin list --profile web
```'''

    out = []
    for char in input_text:
        for t, val in parser.feed(char):
            if t == 'text':
                out.append(val)
    for t, val in parser.flush():
        if t == 'text':
            out.append(val)
    res = ''.join(out)
    assert res == input_text, f"Mismatch:\n{res!r}\n!=\n{input_text!r}"
    print("✅ test_markdown_newline_preservation PASSED")

def test_thinking_cot_tool_salvage():
    print("\n--- Running test_thinking_cot_tool_salvage ---")
    thinking_text = (
        "我需要检查DSH版本。\n"
        "我决定分步：先查看DSH_CHECKOUT环境变量。使用pwsh命令：$env:DSH_CHECKOUT。\n\n"
        "<tool_call>\n"
        '{"name": "pwsh", "arguments": {"command": "$env:DSH_CHECKOUT", "description": "Check DSH_CHECKOUT"}}\n'
        "</tool_call>"
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "pwsh",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"]
                }
            }
        }
    ]
    _, tool_calls = ToolBridge.extract_tool_calls_from_text(thinking_text, tools)
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "pwsh"
    import json
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["command"] == "$env:DSH_CHECKOUT"
    print("✅ test_thinking_cot_tool_salvage PASSED")

def test_thinking_cot_suspicion():
    print("\n--- Running test_thinking_cot_suspicion ---")
    broken_thinking = "我将调用工具查看环境变量：<tool_call>{\"name\": \"pwsh\""
    tools = [{"type": "function", "function": {"name": "pwsh"}}]
    assert ToolBridge.check_zero_tolerance_suspicion(broken_thinking, tools) is True
    print("✅ test_thinking_cot_suspicion PASSED")

def test_normal_code_blocks_and_conversation_not_suspicious():
    print("\n--- Running test_normal_code_blocks_and_conversation_not_suspicious ---")
    tools = [{"type": "function", "function": {"name": "pwsh"}}, {"type": "function", "function": {"name": "read"}}]
    # 1. 普通 Python / Bash 代码块
    python_code = "下面是斐波那契数列函数：\n```python\ndef fib(n):\n    return n if n <= 1 else fib(n-1) + fib(n-2)\n```\n请直接使用。"
    assert ToolBridge.check_zero_tolerance_suspicion(python_code, tools) is False

    bash_code = "请在终端执行：\n```bash\nls -lh /var/log\n```\n查看日志文件。"
    assert ToolBridge.check_zero_tolerance_suspicion(bash_code, tools) is False

    # 2. 普通 JSON 说明
    json_doc = 'API 返回结果示例：\n```json\n{"name": "Alice", "command": "status"}\n```'
    assert ToolBridge.check_zero_tolerance_suspicion(json_doc, tools) is False

    # 3. 对话中提及工具名称
    chat_text = "你可以用 `pwsh` 或 `read` 查看当前目录和文件配置，无需担心权限问题。"
    assert ToolBridge.check_zero_tolerance_suspicion(chat_text, tools) is False

    # 4. 思考链中权衡工具
    thinking_text = "We need to consider whether to call pwsh or read. Here the user just asked for an explanation, so no tool call is needed."
    assert ToolBridge.check_zero_tolerance_suspicion(thinking_text, tools) is False
    print("✅ test_normal_code_blocks_and_conversation_not_suspicious PASSED")

if __name__ == "__main__":
    test_streaming_instant_text_and_tool()
    test_code_block_immunity()
    test_self_closing_tag_streaming()
    test_self_closing_with_internal_arrow()
    test_tool_call_json_block()
    test_bracket_call()
    test_char_by_char_tool_call_streaming()
    test_stuttering_and_windows_backslash_streaming()
    test_read_image_tool_and_normalization()
    test_no_tag_leakage_on_malformed_tool()
    test_dsml_tool_calls_streaming()
    test_raw_json_tool_streaming()
    test_chinese_tool_call_streaming()
    test_dsml_tool_call_tag_no_leak()
    test_markdown_newline_preservation()
    test_thinking_cot_tool_salvage()
    test_thinking_cot_suspicion()
    test_normal_code_blocks_and_conversation_not_suspicious()
    print("\n🎉 ALL STREAMING FSM TESTS PASSED!")


# ══════════════════════════════════════════════════════════════════════
# 免责声明：两份实现不许漂移
# ══════════════════════════════════════════════════════════════════════
#
# 上游网页端会在回答开头塞提示横幅（"本回答由 AI 生成…"、"专家模式暂不支持
# 阅读链接…"）。网关**有两处**各自动手清理它：
#
#   1. `StreamingToolParser.DISCLAIMERS` + `_clean_disclaimers()` —— 精确串匹配，
#      并且参与"流首前缀吸收"（需要字面量才能做 startswith 的逐片等待）
#   2. `ToolBridge.sanitize_internal_tags()` —— 正则匹配，容忍空格/标点变体
#
# 两者**行为不同是有意的**，所以不该硬合成一个。但**两份文案清单会悄悄漂移**：
# 上游改了措辞、你只改了其中一处，另一处就永远漏掉 —— 而这类错误的症状是
# "客户端回答开头莫名多了一句免责声明"，很难联想到是这里。
#
# 下面这条守卫把"两份覆盖同一组文案"变成可执行断言：
# DISCLAIMERS 里**每一条**都必须也能被 tool_bridge 的正则清掉。
# 以后往 DISCLAIMERS 加新文案时，这条会立刻失败并指出是哪一条。
#
# 诚实说明局限：反方向无法检查 —— 正则不可枚举，所以"正则能清但字面量表里没有"
# 的情况拦不住（那种情况不会造成漏清，只会多清，风险低）。

def test_every_streaming_disclaimer_is_also_removed_by_tool_bridge():
    for phrase in StreamingToolParser.DISCLAIMERS:
        cleaned = ToolBridge.sanitize_internal_tags(phrase).strip()
        assert cleaned == "", (
            f"免责声明 {phrase!r} 能被 StreamingToolParser 清掉，但 tool_bridge 的正则清不掉 —— "
            f"两份实现已经漂移，实际残留：{cleaned!r}"
        )


def test_disclaimer_guard_is_not_vacuous():
    """守卫不空转：确认真的取到了文案、而且两份实现都真的在做清理。"""
    assert StreamingToolParser.DISCLAIMERS, "DISCLAIMERS 是空的 —— 上一条守卫等于没跑"
    sample = StreamingToolParser.DISCLAIMERS[0]
    assert StreamingToolParser._clean_disclaimers(sample) == "", "流式侧没清掉"
    assert ToolBridge.sanitize_internal_tags(sample).strip() == "", "tool_bridge 侧没清掉"
    # 反例：正常文本不该被清空（防止"什么都删"式的假通过）
    keep = "这是一段正常回答，请查收。"
    assert ToolBridge.sanitize_internal_tags(keep).strip() == keep


def test_disclaimers_are_absorbed_at_stream_start():
    """流首的免责声明必须被吸收，且其后的正文完整保留（不能连正文一起吃掉）。"""
    parser = StreamingToolParser(TOOLS)
    events = []
    for chunk in ["本回答由 AI 生成，内容仅供参考，请仔细甄别", "\n\n", "正文开始"]:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    text = "".join(v for t, v in events if t == "text")
    assert "本回答由 AI 生成" not in text, f"免责声明泄漏给了客户端：{text!r}"
    assert "正文开始" in text, f"正文被误删：{text!r}"




