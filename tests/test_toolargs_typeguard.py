"""
_normalize_tool_args 的类型防护回归测试。

实测崩溃（修复前）：
  _normalize_tool_args 无条件执行 `args.items()`，因此
    str / None / list / int  → AttributeError: '...' object has no attribute 'items'
  而调用方普遍写成 `args = self._repair_and_loads(args) or args`
  —— **修复失败时 args 仍是 str** —— 所以这条链路是真实可达的，
  会导致整轮请求 500，且错误信息完全指不到根因。

本测试锁住"任何输入都不得崩"这一契约。
"""
import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.tool_bridge import ToolBridge  # noqa: E402

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]},
    },
}]


# ─────────────────────────────────────────────────────────────
# 1. 非 dict 输入不得崩溃
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("label,bad", [
    ("纯字符串", "C:\\code\\a.py"),
    ("JSON 字符串", '{"path": "a.py"}'),
    ("空字符串", ""),
    ("None", None),
    ("list", ["a", "b"]),
    ("int", 123),
    ("bool", True),
    ("嵌套 list", [{"path": "a.py"}]),
])
def test_non_dict_input_never_crashes(label, bad):
    """任何非 dict 输入都必须被安全收敛，而不是抛 AttributeError。"""
    out = ToolBridge._normalize_tool_args("read", bad, TOOLS)
    assert isinstance(out, dict), f"[{label}] 返回值不是 dict：{type(out)}"


def test_plain_string_is_packed_as_input():
    """无法解析的字符串应降级为 {"input": 原值}，保住信息。"""
    out = ToolBridge._normalize_tool_args("read", "C:\\code\\a.py", TOOLS)
    assert out.get("input") == "C:\\code\\a.py"


def test_json_string_is_parsed_not_packed():
    """能解析的 JSON 字符串应还原成参数，而不是塞进 input。"""
    out = ToolBridge._normalize_tool_args("read", '{"path": "a.py"}', TOOLS)
    assert out.get("path") == "a.py", f"JSON 字符串未还原：{out!r}"


def test_empty_and_none_become_empty_dict():
    assert ToolBridge._normalize_tool_args("read", None, TOOLS) == {}
    assert ToolBridge._normalize_tool_args("read", "", TOOLS) == {}


def test_windows_path_json_string_handles_bad_escape():
    """Windows 路径含非法 JSON 转义（\\c）时不得崩溃，且应尽量还原。"""
    out = ToolBridge._normalize_tool_args("read", '{"path": "C:\\code\\x.py"}', TOOLS)
    assert isinstance(out, dict)
    # 修复后应能拿到路径（值里可能带转义，断言关键片段即可）
    assert "x.py" in json.dumps(out, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────
# 2. 正常 dict 路径不受影响
# ─────────────────────────────────────────────────────────────
def test_normal_dict_still_works():
    out = ToolBridge._normalize_tool_args("read", {"path": "a.py"}, TOOLS)
    assert out["path"] == "a.py"
    assert out["filePath"] == "a.py"
    assert out["file_path"] == "a.py"


def test_runtime_guard_exists_in_source():
    """结构断言：必须存在显式的 isinstance(args, dict) 防护。

    防止后人把这段防护删掉——它的缺失正是崩溃的根因。
    """
    src_path = os.path.join(_REPO_ROOT, "app", "engine", "tool_bridge.py")
    src = open(src_path, encoding="utf-8").read()
    assert "isinstance(args, dict)" in src, (
        "缺少 dict 类型防护，str/None 输入会再次导致 AttributeError"
    )


# ─────────────────────────────────────────────────────────────
# 3. 端到端：畸形 invoke body 经完整解析链不得崩溃
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("body", [
    "C:\\code\\a.py",                    # 裸值
    '{"path": "a.py"}',                  # 裸 JSON
    '"{\\"path\\": \\"a.py\\"}"',        # 值本身是 JSON 字符串
    "   ",                               # 空白
    "!!!invalid!!!",                     # 垃圾内容
])
def test_malformed_invoke_body_does_not_crash(body):
    """畸形 body 走完整解析链（状态机 + Safety-Net）都不得抛异常。"""
    from app.engine.streaming_parser import StreamingToolParser

    text = f'<|tool_calls>\n<|invoke name="read">\n{body}\n</|invoke>\n</|tool_calls>'

    p = StreamingToolParser(TOOLS)
    for ch in text:
        for ev, d in p.feed(ch):
            if ev == "tool_call":
                json.loads(d["function"]["arguments"])   # 必须仍是合法 JSON
    for ev, d in p.flush():
        if ev == "tool_call":
            json.loads(d["function"]["arguments"])

    # Safety-Net 路径同样不得崩
    _clean, tcs = ToolBridge.extract_tool_calls_from_text(text, TOOLS)
    for tc in tcs:
        json.loads(tc["function"]["arguments"])
