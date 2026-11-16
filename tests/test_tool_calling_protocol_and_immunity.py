import sys
import os
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.tool_bridge import ToolBridge
from app.engine.session_tree import SessionTracker


def test_format_tools_system_prompt_includes_canonical_wire_protocol():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "office_read-document",
                "description": "Read office documents (PDF, Word, PPTX)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "pageRange": {"type": "string", "description": "Page range (e.g. 1-10)"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "pwsh",
                "description": "Execute PowerShell command",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command string"},
                    },
                    "required": ["command"],
                },
            },
        },
    ]

    prompt = ToolBridge.format_tools_system_prompt(tools)
    # 1. 包含标准 XML <tool_call> 范例
    assert "<tool_call>" in prompt
    assert "</tool_call>" in prompt
    assert '{"name": "工具名", "arguments": {"参数名": "参数值"}}' in prompt
    # 2. 包含工具名称与参数说明
    assert "office_read-document" in prompt
    assert "pwsh" in prompt
    # 3. 包含直接自然语言回复说明
    assert "请直接用清晰自然的语言回复用户" in prompt


def test_check_zero_tolerance_suspicion_precise():
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    # 明确损坏或未闭合的工具调用标签 -> 应报可疑
    assert ToolBridge.check_zero_tolerance_suspicion("<tool_call>\n{\"name\": \"read_file\"", tools) is True
    assert ToolBridge.check_zero_tolerance_suspicion("<invoke name=\"read_file\">", tools) is True
    assert ToolBridge.check_zero_tolerance_suspicion("<|tool_calls>", tools) is True
    assert ToolBridge.check_zero_tolerance_suspicion("<｜tool call begin｜>", tools) is True
    assert ToolBridge.check_zero_tolerance_suspicion("[工具调用: read_file", tools) is True

    # 正常的自然语言、HTML标签或变量名 -> 绝对不应误伤
    assert ToolBridge.check_zero_tolerance_suspicion("Hello, this is a normal text response.", tools) is False
    assert ToolBridge.check_zero_tolerance_suspicion("Here is a <callback> in javascript and <action_list> in html.", tools) is False
    assert ToolBridge.check_zero_tolerance_suspicion("I suggest you call a function or run the tool read_file.", tools) is False
    assert ToolBridge.check_zero_tolerance_suspicion("```json\n{\"status\": \"ok\"}\n```", tools) is False


def test_tail_tool_results_includes_available_tool_names():
    tools = [
        {"type": "function", "function": {"name": "office_read-document"}},
        {"type": "function", "function": {"name": "bash"}},
    ]

    messages = [
        {"role": "user", "content": "请分析报告.pdf的内容并制作PPT"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "office_read-document", "arguments": '{"path": "报告.pdf"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "[PDF read truncated at page 25; continue with pageRange=\"26-27\"]"},
    ]

    # 直接传 tools
    prompt, _ = SessionTracker.prepare_prompt(messages, has_existing_session=True, tools=tools)
    assert "【工具执行输出】" in prompt
    assert "office_read-document" in prompt
    assert "bash" in prompt
    assert "（当前可用工具：office_read-document, bash）" in prompt

    # 不传 tools 但 system 消息中注册了工具
    system_prompt = ToolBridge.format_tools_system_prompt(tools)
    messages_with_system = [{"role": "system", "content": system_prompt}] + messages
    prompt2, _ = SessionTracker.prepare_prompt(messages_with_system, has_existing_session=True, tools=None)
    assert "（当前可用工具：office_read-document, bash）" in prompt2


def test_thinking_chain_does_not_condemn_normal_text():
    """
    模型在思考链中权衡工具调用（包含 <tool_call> 等），
    但在正文中决定不调用工具，直接给出高质量文本回复。
    验证：check_zero_tolerance_suspicion 绝不误伤正文！
    """
    tools = [{"type": "function", "function": {"name": "office_read-document"}}]

    # 思考链内容（包含工具标签推演）
    thinking_content = (
        "这里系统说'输出规范合法的工具调用与完整JSON参数'，可能格式是 <tool_call>... "
        "但我不能确定工具名。如果工具名错误，又会失败。我决定：不调用工具，直接继续输出完整修改清单和算法展示页草稿。"
    )

    # 正文内容（纯文本自然语言）
    final_text = (
        "根据前25页读取的内容，我已经为你整理出完整的修改清单与算法展示页草稿：\n"
        "1. 首页版式调整：采用深色科技背景；\n"
        "2. 算法核心架构：在第12页增加流程拓扑图；\n"
        "3. 答辩总结与预期成果展示。"
    )

    # 网关只检查正文 final_text，绝不检查 thinking_content
    is_suspicious = ToolBridge.check_zero_tolerance_suspicion(final_text, tools)
    assert is_suspicious is False


def test_reflection_retry_natural_text_passthrough_logic():
    """
    模拟打回重试后，模型输出自然语言回答：
    网关应识别其为合法自然语言，清空 is_suspicious，放行输出，杜绝【网关安全拦截】。
    """
    tools = [{"type": "function", "function": {"name": "office_read-document"}}]

    # 重试轮次返回的内容：纯文字回答
    retry_reply = "我已了解您的需求，当前文档前序内容已足够，无需重复调用工具读取。为您提供以下优化方案..."

    clean_text, tool_calls = ToolBridge.extract_tool_calls_from_text(retry_reply, tools)
    assert len(tool_calls) == 0

    # 检查是否可疑
    is_suspicious = ToolBridge.check_zero_tolerance_suspicion(retry_reply, tools)
    assert is_suspicious is False

    # 模拟网关逻辑判定
    if not tool_calls:
        if not ToolBridge.check_zero_tolerance_suspicion(retry_reply, tools):
            is_suspicious = False
            final_content = retry_reply
        else:
            final_content = "【网关安全拦截】"

    assert is_suspicious is False
    assert final_content == retry_reply
    assert "【网关安全拦截】" not in final_content


def test_incident_1718_dsml_invoke_without_params():
    """
    真实复现 17:18 事故：
    模型输出 DeepSeek 原生 DSML，invoke 标签带双全角竖线且反斜杠在后，无参数。
    验证：
    1. ToolBridge extract 成功捕获工具，clean_text 彻底剥离 DSML 标签，绝不把工具调用当文本漏出；
    2. StreamingToolParser 同样成功捕获工具！
    """
    incident_text = (
        "我先查一下当前可用的 beautify 系列能力，看看有没有你说的“起 web 服务改半成品”的那个。\n"
        "<｜｜DSML｜｜ calls>\n"
        '<｜｜DSML｜｜ invoke name="beautify_list-capabilities">\n'
        "<｜｜DSML｜｜ /invoke>\n"
        "</｜｜DSML｜｜ calls>"
    )
    tools = [{"type": "function", "function": {"name": "beautify_list-capabilities", "parameters": {"type": "object", "properties": {}}}}]

    # 1. 验证 ToolBridge 提取
    clean_text, fallback_tcs = ToolBridge.extract_tool_calls_from_text(incident_text, tools)
    assert len(fallback_tcs) == 1
    assert fallback_tcs[0]["function"]["name"] == "beautify_list-capabilities"
    assert fallback_tcs[0]["function"]["arguments"] == "{}"
    assert "<｜｜DSML｜｜" not in clean_text
    assert "beautify_list-capabilities" not in clean_text
    assert "我先查一下当前可用的 beautify 系列能力" in clean_text

    # 2. 验证 StreamingToolParser 提取
    from app.engine.streaming_parser import StreamingToolParser
    parser = StreamingToolParser(tools)
    evs = []
    for ch in incident_text:
        evs.extend(parser.feed(ch))
    evs.extend(parser.flush())
    stream_tcs = [d for t, d in evs if t == "tool_call"]
    assert len(stream_tcs) == 1
    assert stream_tcs[0]["function"]["name"] == "beautify_list-capabilities"
    assert stream_tcs[0]["function"]["arguments"] == "{}"


if __name__ == "__main__":
    test_format_tools_system_prompt_includes_canonical_wire_protocol()
    test_check_zero_tolerance_suspicion_precise()
    test_tail_tool_results_includes_available_tool_names()
    test_thinking_chain_does_not_condemn_normal_text()
    test_reflection_retry_natural_text_passthrough_logic()
    test_incident_1718_dsml_invoke_without_params()
    print("ALL 6 PROTOCOL & IMMUNITY TESTS (INC. 17:18 INCIDENT) PASSED SUCCESSFULLY!")
