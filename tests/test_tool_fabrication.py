"""
网关不得**编造**工具调用 —— 这是"API 正常返回正确结果"的直接守卫。

## 缺陷（离线可复现，不需要联网、不需要真实上游）

正文里一段**未闭合**的 JSON，会被网关当成一次工具调用发给客户端：

    配置文件示例如下：{"name": "my-service", "port": 8080

而这次请求注册的工具只有 `read` / `write`。后果是**双向的**：

  · 客户端收到一个它**不认识**的工具名 → DSH 侧直接报错或空转
  · **模型真正说的那句话被吞掉** —— `openai_routes.py` 把 flush 出来的 tool_call
    并入 `captured_tool_calls` 后就直接发射并 return，那段正文再也不会发给客户端

成因：`_match_tool_name` 对**未注册**的名字会**原样返回**（它的 `return name` 兜底），
而宽松路径的条件写的是 `matched in self.tool_names or (not require_tool_match and bool(self.tools))`
—— "匹配成功"被当成了"可以发射"。

修法：新增 `_resolve_emittable_name()`，只有两种情况才允许发射
① 名字确实在注册表里 ② 允许宽松匹配**且只注册了一个工具**（此时无歧义，
但必须改写成那个工具的名字，**不能把模型编出来的名字透出去**）。
否则**不发射**，让残余文本走"当正文处理"分支 ——
把模型的原文留在正文里，是明确更优的失败方向。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import unittest

from app.engine.streaming_parser import StreamingToolParser


def _tool(name):
    return {"type": "function",
            "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


MULTI_TOOLS = [_tool("read"), _tool("write")]

#: 子代理离线复现时用的**原始输入**，逐字保留
PROSE_WITH_UNCLOSED_JSON = '配置文件示例如下：{"name": "my-service", "port": 8080'


def _flush(chunks, tools):
    p = StreamingToolParser(tools)
    events = []
    for c in chunks:
        events.extend(p.feed(c))
    events.extend(p.flush())
    return events


def _tool_calls(events):
    return [v for t, v in events if t == "tool_call"]


def _text(events):
    return "".join(v for t, v in events if t == "text")


class NoFabricatedToolCallTest(unittest.TestCase):

    def test_unclosed_json_in_prose_is_not_turned_into_a_tool_call(self):
        """★ 核心回归：正文里的 JSON 示例不得变成工具调用。

        用 `tools=[read, write]`（多于一个），所以宽松路径**没有**放行理由 ——
        `my-service` 既不是注册名、也无法归一到任何一个注册名。
        """
        events = _flush([PROSE_WITH_UNCLOSED_JSON], MULTI_TOOLS)
        calls = _tool_calls(events)
        self.assertEqual(
            calls, [],
            f"★ 网关编造了工具调用：{[c['function']['name'] for c in calls]} —— "
            f"客户端会收到一个不存在的工具名",
        )

    def test_the_prose_survives(self):
        """修法的关键收益：模型真正说的话必须还在。

        旧行为下这段正文被整段吞掉，客户端看到答案凭空截断。
        """
        events = _flush([PROSE_WITH_UNCLOSED_JSON], MULTI_TOOLS)
        text = _text(events)
        self.assertIn("配置文件示例如下", text, "正文被吞掉了 —— 客户端会看到答案凭空截断")
        self.assertIn("my-service", text)

    def test_registered_tool_still_emits_normally(self):
        """不能因为堵住编造就把正常工具调用也堵掉"""
        events = _flush(['<tool_call>\n{"name": "read", "arguments": {"path": "a.py"}}\n</tool_call>'],
                        MULTI_TOOLS)
        calls = _tool_calls(events)
        self.assertEqual(len(calls), 1, f"正常工具调用被误伤：{events}")
        self.assertEqual(calls[0]["function"]["name"], "read")

    def test_single_tool_relaxation_still_works_but_rewrites_the_name(self):
        """宽松路径**保留**（它当初是修"流式状态机遗漏"加的），但必须改名。

        只注册一个工具时，模型指哪个没有歧义 —— 采用它，但发给客户端的名字
        必须是那个**已注册**的工具名，绝不是模型编出来的名字。
        """
        events = _flush(['<tool_call>\n{"name": "my-service", "arguments": {"port": 8080}}\n</tool_call>'],
                        [_tool("read")])
        calls = _tool_calls(events)
        self.assertEqual(len(calls), 1, "单工具场景的宽松匹配被误伤了")
        self.assertEqual(
            calls[0]["function"]["name"], "read",
            f"★ 把模型编出来的名字透给了客户端：{calls[0]['function']['name']!r}",
        )

    def test_alias_resolution_is_preserved(self):
        """别名归一（`read_file` → `read`）必须照旧工作 —— 这是合法的宽松匹配"""
        events = _flush(['<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'],
                        MULTI_TOOLS)
        calls = _tool_calls(events)
        self.assertEqual(len(calls), 1, "别名归一被误伤了")
        self.assertEqual(calls[0]["function"]["name"], "read")

    def test_resolver_contract(self):
        """直接钉住 `_resolve_emittable_name` 的契约（守卫不能空转）"""
        p = StreamingToolParser(MULTI_TOOLS)
        # 注册名 → 原样返回
        self.assertEqual(p._resolve_emittable_name("read", require_tool_match=False), "read")
        self.assertEqual(p._resolve_emittable_name("read", require_tool_match=True), "read")
        # 未注册名 + 多工具 → 拒绝
        self.assertIsNone(p._resolve_emittable_name("my-service", require_tool_match=False))
        # 未注册名 + 严格模式即使单工具也拒绝
        single = StreamingToolParser([_tool("read")])
        self.assertIsNone(single._resolve_emittable_name("my-service", require_tool_match=True))
        # 未注册名 + 宽松 + 单工具 → 收敛成那个唯一工具
        self.assertEqual(
            single._resolve_emittable_name("my-service", require_tool_match=False), "read")

    def test_guard_is_not_vacuous(self):
        """守卫不空转：确认缺陷的成因（`_match_tool_name` 原样返回未注册名）仍然存在。

        如果哪天有人把 `_match_tool_name` 改成"匹配不上就返回 None"，
        这条测试会失败 —— 那不是坏事，说明可以简化守卫，但必须**有人知道**。
        """
        p = StreamingToolParser(MULTI_TOOLS)
        self.assertEqual(p._match_tool_name("my-service"), "my-service",
                         "`_match_tool_name` 的行为变了 —— 请重新评估这组守卫是否还需要")

    def test_multi_tool_case_really_has_more_than_one_tool(self):
        """确认"多工具"这个前提成立，否则上面那条核心回归就是在单工具场景下通过的假测试"""
        self.assertGreater(len(StreamingToolParser(MULTI_TOOLS).tool_names), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
