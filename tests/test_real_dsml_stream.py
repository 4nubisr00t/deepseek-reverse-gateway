"""
真实失败样本的回归测试（2026-09-12，会话 dfc7cd20）。

## 样本来源

`tests/fixtures/real_dsml_tool_call.txt` 是从网关日志里**逐帧重建**的模型原文
（不是手写的）：正文末尾 + 一个 DSML 工具调用。它的开头是

    ...I'll query host settings service.<U+FF5C><U+FF5C>DSML<U+FF5C><U+FF5C> calls>
    <...invoke name="cordis_inspect_query">
    <...parameter name="input" ...>{"service":"settings"}</...parameter>
    ...（共 5 个参数）
    </...invoke>
    </...calls>FINISHED

⚠️ 本文件**不把那个标记写成字面量**，一律用 `\uff5c` 或从夹具文件读。
原因：任何输出里出现该标记，读取方的"文本→工具调用"切分逻辑就会把后面全吞掉。
（实测：我的消息与思考流都因此在同一位置被截断过。）

## 要钉住的两件事

1. **容器标签叫 `calls`，不是 `tool_calls`。** 状态机的容器正则原先只认后者，
   于是这个顶层容器没被消费掉 —— 真实会话里 34 步、每步都靠整文安全网兜底。
2. **没被解析成工具调用的文本必须还给正文。** 状态机看到"疑似标记"会扣住后续内容，
   扣住又解析不出来时**不能永久丢掉** —— 否则客户端看到的正文就断在标记处。
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.streaming_parser import StreamingToolParser  # noqa: E402
from app.engine.tool_bridge import ToolBridge  # noqa: E402

FIXTURE = os.path.join(_REPO_ROOT, "tests", "fixtures", "real_dsml_tool_call.txt")

#: 与真实会话一致：被调用的那个工具必须在注册表里
TOOLS = [{
    "type": "function",
    "function": {
        "name": "cordis_inspect_query",
        "description": "read-only inspect",
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "provider": {"type": "string"},
                "method": {"type": "string"},
                "input": {"type": "object"},
            },
            "required": ["platform", "provider", "method"],
        },
    },
}]


def _fixture_text() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


#: 前置正文：真实会话里模型是先写一句说明、再吐工具调用的。
#: 夹具本身只含"工具调用"那一段（因为重建时按路径只收了内容增量，
#: 正文与工具调用在不同的帧序列里），所以这里在测试侧补上，专门验证"前置正文不丢"。
PROSE = "I'll query host settings service.\n"


def _with_prose() -> str:
    return PROSE + _fixture_text()


def _run_char_by_char(text: str):
    """按真实流式行为逐字符喂进去（上游是逐 token 推的）。"""
    p = StreamingToolParser(TOOLS)
    texts, tools = [], []
    for ch in text:
        for ev, d in p.feed(ch):
            if ev == "tool_call":
                tools.append(d)
            elif ev == "text":
                texts.append(d)
    for ev, d in p.flush():
        if ev == "tool_call":
            tools.append(d)
        elif ev == "text":
            texts.append(d)
    return "".join(texts), tools


def test_fixture_is_the_real_thing():
    """夹具必须真的是那段原文，否则下面所有断言都是自娱自乐。"""
    t = _fixture_text()
    assert "\uff5c" in t, "夹具里没有全角竖线 —— 那就不是真实样本"
    assert "cordis_inspect_query" in t
    # 4 个参数：input / method / platform / provider（上一版我写了 5，数错了）
    assert t.count("parameter name=") == 4, f"参数个数不对: {t.count('parameter name=')}"
    # ★ 状态帧不许混进来：`FINISHED` 来自 {"p":"response/status",...,"v":"FINISHED"}，
    #   它不是正文。第一版夹具就是被它污染的（只判了"v 是字符串"，没判路径）。
    assert "FINISHED" not in t, "夹具混入了状态帧的值 —— 重建时没收按路径过滤"


class TestStreamingFsmParsesTheContainer:
    """★ 主路径必须自己认出这个容器，而不是靠整文兜底。"""

    def test_fsm_emits_exactly_one_tool_call(self):
        text, tools = _run_char_by_char(_fixture_text())
        assert len(tools) == 1, (
            f"状态机没认出容器标签，发出 {len(tools)} 个工具调用。"
            f"真实会话里这意味着每一步都要靠 [Gateway-Safety-Net] 兜底。"
        )
        fn = tools[0]["function"]
        assert fn["name"] == "cordis_inspect_query"

    def test_arguments_are_complete_and_typed(self):
        _text, tools = _run_char_by_char(_fixture_text())
        assert tools, "没有解析出工具调用"
        import json
        args = json.loads(tools[0]["function"]["arguments"])
        assert args.get("method") == "listService"
        assert args.get("platform") == "host"
        assert args.get("provider") == "Service"
        assert args.get("input") == {"service": "settings"}, (
            "input 参数（一段 JSON）没被正确还原"
        )

    def test_whole_text_extractor_agrees(self):
        """整文提取器与状态机对同一段文本的结论必须一致。

        两者不一致就是本次故障的形态：一个抓得到、一个抓不到。
        """
        text = _fixture_text()
        _clean, tc = ToolBridge.extract_tool_calls_from_text(text, TOOLS)
        assert len(tc) == 1, "整文提取器也没认出 —— 那问题比状态机更深"
        _stream_text, stream_tc = _run_char_by_char(text)
        assert len(stream_tc) == len(tc) == 1, (
            f"两条路径不一致：状态机 {len(stream_tc)} 个 / 整文 {len(tc)} 个"
        )
        assert (stream_tc[0]["function"]["name"]
                == tc[0]["function"]["name"])


class TestNoTextIsSwallowed:
    """扣住的正文必须还回来，且标记碎片绝不许漏给客户端。

    用户看到的就是这两条：
      · 正文在标记处**凭空截断**（前置正文被吞）
      · 或者标记碎片（全角竖线 / 容器标签尾）**泄漏进正文**——
        而客户端一看到那个碎片，就会把后面的内容当成工具调用载荷吞掉，
        于是**答案在客户端那一侧再次被吃掉**。

    ⚠️ 下面两条目前是 `xfail`：**已复现、未修好**。
    交接说明（供接手者）：
      · 现状：状态机能解析出工具调用（前两条断言是绿的），但**容器开标签的碎片
        会漏进正文** —— 实测输出 = 前置正文 + 标记碎片 + 「calls>」。
      · 已定位：`app/engine/streaming_parser.py` 里消费顶层容器的那条正则
        （搜 `dsml_container`）只列了 `tool_calls` 候选，**不认上游实际写的 `calls`**。
      · 我试过补上 `calls` 候选，但**打断了 8 条既有测试**（`test_streaming_fsm.py`
        与 `test_tool_fabrication.py` 里各有若干），所以**已回退**，没有把它留在树里。
      · 也就是说：这个改动不是"加个候选词"那么简单，需要真正读过状态机的
        候选前缀判定与 `flush()` 残余清洗两条路径之后再动。
    """

    def test_prose_before_the_tool_call_survives(self):
        text, tools = _run_char_by_char(_with_prose())
        assert tools, "前置条件：应解析出工具调用"
        assert PROSE.strip() in text, (
            "工具调用之前的正文被吞掉了 —— 客户端会看到答案在标记处凭空截断"
        )

    def test_no_marker_fragments_leak_into_the_text(self):
        """★ 最关键的不变量。

        实测：标记字符一旦出现在给客户端看的正文里，客户端的
        "文本→工具调用"切分逻辑就会把**后面的一切**当成工具调用载荷、
        停止输出（我自己的消息与思考流都因此在同一位置被截断过）。
        所以网关有责任保证它**一个都不发**。
        """
        text, _tools = _run_char_by_char(_with_prose())
        assert "\uff5c" not in text, (
            f"正文里漏出了全角竖线（标记碎片）—— 客户端会因此吞掉后文。"
            f"泄漏位置: {text[max(0, text.find(chr(0xFF5C)) - 40):][:80]!r}"
        )
        for bad in ("calls>", "invoke", "parameter"):
            assert bad not in text.lower(), f"正文里泄漏了结构标签碎片 {bad!r}: {text[-80:]!r}"

    def test_container_tail_is_consumed_not_emitted(self):
        """容器收尾必须被消费掉，而不是当正文发出去。"""
        text, _tools = _run_char_by_char(_with_prose())
        assert "calls" not in text.lower(), (
            f"容器收尾当正文发了出去: {text[-80:]!r}"
        )


class TestHeldTextIsReleasedWhenItIsNotAToolCall:
    """不是工具调用时，标记后面的内容**不能永久丢失**。

    这是"看到疑似标记就扣住"的另一面：判错了不要紧，
    但判错之后必须把正文还回去，而不是静默丢弃。
    """

    def test_unresolvable_tool_name_releases_the_body(self):
        # 把工具表清空 → 名字无法收敛到注册表 → 状态机应把这段当正文吐出
        text = _fixture_text()
        p = StreamingToolParser([])
        texts = []
        for ch in text:
            for ev, d in p.feed(ch):
                if ev == "text":
                    texts.append(d)
        for ev, d in p.flush():
            if ev == "text":
                texts.append(d)
        out = "".join(texts)
        assert out.strip(), (
            "名字解析不出来时把整段内容静默吞掉了 —— "
            "客户端会看到空白；正确方向是把原文留在正文里"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
