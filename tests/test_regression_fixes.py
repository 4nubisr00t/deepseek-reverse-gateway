"""
工具链修复的回归测试（native pytest 风格）

锁住以下四条「网关不得改写模型意图 / 不得静默丢内容」的行为：
  1. 不伪造 write 的 content（旧行为会补空串 → 客户端可能覆写文件为空）
  2. 不伪造 workdir（旧行为把空值改写成 "." → 改变命令工作目录）
  3. 工具名模糊匹配必须确定性，歧义时保留原名而不是任选其一
  4. 流尾不得因为正文里出现 "name"/"command"/"tool"/"function" 就整体丢弃

设计原则：这些测试断言的是「不做什么」，而不是「做了什么」，
因为原缺陷全部是「过度代劳」而非「能力不足」。
"""
import json
import os
import sys

import pytest

# 相对定位仓库根，避免硬编码绝对路径（旧测试文件的问题）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.streaming_parser import StreamingToolParser  # noqa: E402
from app.engine.tool_bridge import ToolBridge  # noqa: E402


def _write_tools():
    return [{
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write file",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["filePath", "content"],
            },
        },
    }]


def _bash_tools(include_workdir=True):
    props = {"command": {"type": "string"}, "description": {"type": "string"}}
    if include_workdir:
        props["workdir"] = {"type": "string"}
    return [{
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run shell command",
            "parameters": {
                "type": "object",
                "properties": props,
                "required": ["command", "description"],
            },
        },
    }]


# ─────────────────────────────────────────────────────────────
# 1. 不得伪造 content
# ─────────────────────────────────────────────────────────────
def test_write_missing_content_is_not_fabricated_as_empty_string():
    """模型没给 content 时，绝不能补空串——那会让客户端把文件覆写成空。"""
    args = ToolBridge._normalize_tool_args(
        "write", {"filePath": "important.py"}, _write_tools()
    )
    assert "content" not in args, (
        "content 被伪造了：模型漏给 content 时应保持缺失并显式暴露，"
        f"而不是补默认值。实际 args={args!r}"
    )
    # 别名派生仍然应该发生（这是合法的跨 schema 适配）
    assert args["filePath"] == "important.py"


def test_write_with_explicit_content_is_preserved():
    """模型明确给出 content 时必须原样保留。"""
    args = ToolBridge._normalize_tool_args(
        "write", {"filePath": "a.py", "content": "print(1)"}, _write_tools()
    )
    assert args["content"] == "print(1)"


# ─────────────────────────────────────────────────────────────
# 2. 不得伪造 workdir
# ─────────────────────────────────────────────────────────────
def test_bash_empty_workdir_is_not_rewritten_to_dot():
    """空的 workdir 不得被改写成 "."——那等于网关替模型决定了工作目录。"""
    args = ToolBridge._normalize_tool_args(
        "bash", {"command": "ls", "workdir": ""}, _bash_tools()
    )
    assert args["workdir"] != ".", (
        f"workdir 被改写成 '.'，改变了命令执行位置。实际 args={args!r}"
    )


def test_bash_absent_workdir_stays_absent():
    """模型完全没给 workdir 时，不得凭空添加。"""
    args = ToolBridge._normalize_tool_args("bash", {"command": "ls"}, _bash_tools())
    assert "workdir" not in args, (
        f"workdir 被凭空添加。实际 args={args!r}"
    )


def test_bash_description_is_derived_but_not_semantic():
    """description 可以由 command 派生（纯描述字段，不改变执行语义）。"""
    args = ToolBridge._normalize_tool_args("bash", {"command": "ls -la"}, _bash_tools())
    assert "description" in args
    assert "ls -la" in args["description"]


# ─────────────────────────────────────────────────────────────
# 3. 工具名模糊匹配必须确定性
# ─────────────────────────────────────────────────────────────
def test_ambiguous_tool_name_is_not_silently_mismapped():
    """同时匹配多个已注册工具时，必须保留原名而不是任选其一。

    旧实现遍历 set 取首个命中者，返回哪个取决于 set 迭代顺序，
    失败形态静默：工具被映射成另一个合法工具，客户端照常执行但做错事。
    """
    tools = [
        {"type": "function", "function": {"name": "office_html-to-pdf", "parameters": {}}},
        {"type": "function", "function": {"name": "web_html-to-pdf", "parameters": {}}},
    ]
    parser = StreamingToolParser(tools)
    # "html-to-pdf" 同时是上面两个工具的后缀 → 歧义
    resolved = parser._match_tool_name("html-to-pdf")
    assert resolved == "html-to-pdf", (
        f"歧义工具名被静默解析为 {resolved!r}；应保留原名交由上层校验"
    )


def test_unambiguous_suffix_match_still_works():
    """唯一命中时，后缀模糊匹配仍应生效（保留原有容错能力）。"""
    tools = [
        {"type": "function", "function": {"name": "office_html-to-pdf", "parameters": {}}},
        {"type": "function", "function": {"name": "write", "parameters": {}}},
    ]
    parser = StreamingToolParser(tools)
    assert parser._match_tool_name("html-to-pdf") == "office_html-to-pdf"


def test_alias_map_still_applies():
    """既有别名映射能力不得被破坏。"""
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    parser = StreamingToolParser(tools)
    assert parser._match_tool_name("read_file") == "read"


# ─────────────────────────────────────────────────────────────
# 4. 流尾不得静默吞掉正文
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("keyword", ['"name"', '"command"', '"tool"', '"function"'])
def test_flush_does_not_swallow_text_containing_json_keywords(keyword):
    """流尾残余文本含这四个关键词时，旧实现整段丢弃 → 客户端看到答案凭空截断。"""
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    parser = StreamingToolParser(tools)
    # 构造一段「以 < 开头、会被挂起、且含关键词」的残余文本：
    # 先喂一个未闭合的 <bash，使 buffer 挂起，再直接 flush
    parser.buffer = f'<bash 说明：字段 {keyword} 的含义是这样。'
    events = parser.flush()
    text = "".join(v for t, v in events if t == "text")
    assert keyword in text, (
        f"流尾正文被静默丢弃了。keyword={keyword!r}, events={events!r}"
    )


def test_flush_still_strips_tool_call_wrappers():
    """剥离工具标签的能力必须保留（不能把标签泄漏给客户端 UI）。"""
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    parser = StreamingToolParser(tools)
    parser.buffer = "<tool_call>半截内容"
    events = parser.flush()
    text = "".join(v for t, v in events if t == "text")
    assert "<tool_call>" not in text
    assert "半截内容" in text


# ─────────────────────────────────────────────────────────────
# 5. 循环阈值必须与 docstring 一致（≥2 次重复即熔断）
# ─────────────────────────────────────────────────────────────
def _history_with_n_duplicates(n):
    msgs = [{"role": "user", "content": "run"}]
    for _ in range(n):
        msgs.append({
            "role": "assistant",
            "tool_calls": [{"function": {"name": "bash", "arguments": '{"command": "python t.py"}'}}],
        })
        msgs.append({"role": "tool", "content": "error"})
    return msgs


def test_duplicate_loop_fires_at_two_repeats():
    """连续 2 次相同调用即应熔断（docstring 写的是 >= 2，旧代码是 >= 5）。"""
    hist = _history_with_n_duplicates(2)
    candidate = [{"function": {"name": "bash", "arguments": '{"command": "python t.py"}'}}]
    assert ToolBridge.check_duplicate_loop(hist, candidate) is True


def test_duplicate_loop_does_not_fire_for_different_args():
    hist = _history_with_n_duplicates(3)
    candidate = [{"function": {"name": "bash", "arguments": '{"command": "python other.py"}'}}]
    assert ToolBridge.check_duplicate_loop(hist, candidate) is False


def test_duplicate_loop_no_history_is_safe():
    candidate = [{"function": {"name": "bash", "arguments": '{"command": "ls"}'}}]
    assert ToolBridge.check_duplicate_loop([], candidate) is False
    assert ToolBridge.check_duplicate_loop([{"role": "user", "content": "hi"}], candidate) is False
