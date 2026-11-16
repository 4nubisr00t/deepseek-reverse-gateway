"""回归：`<tool_call>` 开标签被静默吞掉 → JSON 体逐字符泄漏进正文（2026-10-23）。

根因：dd13e44 把 invoke|tool_call|call 并进了 `_probe_and_consume_tool` 的
"开闭皆可"容器分支，`<tool_call>` 被当成无意义容器吃掉。
特征：整块一次性 feed 能侥幸走通（裸 JSON 路径兜住），逐字符/小分片 feed 则漏字。
真实上游就是小分片，所以必须用逐字符 + 随机分片双重验证。
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.streaming_parser import StreamingToolParser  # noqa: E402

TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}, "workdir": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "write",
        "parameters": {"type": "object",
                       "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["file_path", "content"]}}},
]

CMD = "ls -1 /d/agent"

BLOCKS = {
    "tool_call-json": '<tool_call>\n{"name": "bash", "arguments": {"command": "%s", "workdir": "D:/agent"}}\n</tool_call>' % CMD,
    "tool_call-inline": '<tool_call>{"name": "bash", "arguments": {"command": "%s"}}</tool_call>' % CMD,
    "dsml-invoke": (
        '<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="bash">\n'
        '<｜｜DSML｜｜ parameter name="command" string="true">%s</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>' % CMD
    ),
    "raw-json": '{"name": "bash", "arguments": {"command": "%s"}}' % CMD,
}

LEAK_MARKERS = ("<tool_call", "</tool_call", "DSML", '{"name"', '"arguments"')


def _run(block, chunker):
    p = StreamingToolParser(TOOLS)
    texts, calls = [], []
    for piece in chunker(block):
        for ev, d in p.feed(piece):
            (texts if ev == "text" else calls).append(d)
    for ev, d in p.flush():
        (texts if ev == "text" else calls).append(d)
    return "".join(texts), calls


def _by_char(s):
    return list(s)


def _by_random(seed):
    def _chunker(s):
        rnd = random.Random(seed)
        i, out = 0, []
        while i < len(s):
            n = rnd.randint(1, 7)
            out.append(s[i:i + n])
            i += n
        return out
    return _chunker


def _check(label, block, chunker, tag):
    text, calls = _run(block, chunker)
    assert len(calls) == 1, f"[{label}/{tag}] 期望 1 个工具调用，实际 {len(calls)}；正文={text!r}"
    assert calls[0]["function"]["name"] == "bash", f"[{label}/{tag}] 工具名错误: {calls[0]}"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args.get("command") == CMD, f"[{label}/{tag}] 参数错误: {args}"
    for m in LEAK_MARKERS:
        assert m not in text, f"[{label}/{tag}] 工具调用标记 {m!r} 泄漏进正文: {text!r}"
    assert not text.strip(), f"[{label}/{tag}] 正文应为空，实际 {text!r}"


def test_every_format_char_by_char_has_no_leak():
    for label, block in BLOCKS.items():
        _check(label, block, _by_char, "char")


def test_every_format_random_chunks_has_no_leak():
    for label, block in BLOCKS.items():
        for seed in range(40):
            _check(label, block, _by_random(seed), f"rand{seed}")


def test_text_before_tool_call_is_kept_and_not_duplicated():
    block = "我先看下目录。\n" + BLOCKS["tool_call-json"]
    for chunker, tag in ((_by_char, "char"), (_by_random(7), "rand")):
        text, calls = _run(block, chunker)
        assert len(calls) == 1, tag
        assert "我先看下目录" in text, (tag, text)
        for m in LEAK_MARKERS:
            assert m not in text, (tag, m, text)


def test_open_tool_call_tag_is_not_consumed_as_container():
    """直接断言根因：`<tool_call>` 开标签不能被 container 分支吞掉。"""
    p = StreamingToolParser(TOOLS)
    p.feed("<tool_call>")
    assert p.buffer.startswith("<tool_call>"), f"开标签被吞: buffer={p.buffer!r}"


def test_stray_closing_tags_still_swallowed():
    p = StreamingToolParser(TOOLS)
    evs = p.feed("</tool_call>")
    evs += p.flush()
    assert not any(ev == "text" and d.strip() for ev, d in evs), evs
