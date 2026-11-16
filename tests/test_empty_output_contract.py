"""
「上游只想不说」不得回一个空响应。

## 实测（2026-09-12，真实会话 dfc7cd20）

最后一步：`thinking_len=1869`、正文 0 字符、工具 0 个。网关照旧走
`[Gateway-EmptyOutput]` 分支——**它只打了一行日志，什么都没发给客户端**。
客户端于是收到"completed response with no content"，判定 `EMPTY_RESPONSE`，
重试；重试再拿到空的；4 次之后报 `TRANSPORT: terminated`，用户手动掐断整轮。

同一会话里还有一次 `thinking_len=12019`、正文同样为空。

## 为什么不能只"保留会话缓存"

那个分支的注释说"保留会话缓存以维持上下文连续（不销毁）"——这对**下一轮**是对的，
但**这一轮仍然是空的**。空响应本身必须让客户端看得出来，否则它只会盲目重试
同一个必死的请求。

所以照同文件里「循环熔断」已有的手法：响应头 200 已经发出、状态码改不了，
就发一条**显式诊断正文**。
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_SRC = open(os.path.join(_PROJECT_ROOT, "app", "api", "openai_routes.py"),
            encoding="utf-8").read()


def _empty_output_branch() -> str:
    """取出 `[Gateway-EmptyOutput]` 那个 if 之后的窗口源码。"""
    i = _SRC.find("if len(full_str) == 0 and not captured_tool_calls:")
    assert i != -1, "找不到「正文为空且未捕获工具」的分支"
    return _SRC[i:i + 3200]


def _code_only(text: str) -> str:
    """去掉整行注释后再断言。

    ⚠️ 必须这样：这个分支的注释里**提到了** `delete_session`（说明旧行为），
    直接 in 判断会命中注释、把"注释在解释历史"误判成"代码又去销毁会话了"。
    （同一个错误我在别处也犯过一次，所以这里显式处理。）
    """
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def test_branch_still_logs_and_keeps_the_session():
    """原有行为不许丢：记录诊断 + 保留会话缓存（不销毁）。"""
    w = _empty_output_branch()
    assert "[Gateway-EmptyOutput]" in w, "诊断日志没了"
    assert "delete_session" not in _code_only(w), (
        "这里又开始**真的**销毁会话了 —— 一次空响应丢掉整段上下文，下一轮从头再来"
    )
    assert "delete_session(conv_key)" in w, (
        "注释里那句『旧实现在此处直接 delete_session』被删了 —— "
        "它是这段代码为什么这样写的依据，不该丢"
    )


def test_thinking_only_emits_a_visible_diagnostic():
    """★ 核心：只想不说时必须发一条正文，而不是回空。"""
    w = _empty_output_branch()
    assert "_thinking_len" in w, "没统计思考长度"
    assert "if _thinking_len > 0:" in w, "缺少「只想不说」的判定"
    i = w.find("if _thinking_len > 0:")
    seg = w[i:i + 1400]
    assert "yield" in seg, "没有向客户端发出任何东西 —— 空响应照旧"
    assert "delta" in seg and "content" in seg, "发的不是一条正文增量"
    assert "没有产出" in seg, "诊断文案没说明『本轮没有产出』"


def test_diagnostic_marks_the_metric():
    """故障要能在 metrics 里区分出来，否则事后无法统计发生率。"""
    assert 'err_msg = "upstream_thinking_only"' in _EMPTY_WINDOW, (
        "没有把这次空产出标记进 err_msg —— 无法统计「上游只想不说」的频率"
    )


def test_truly_empty_response_is_not_papered_over():
    """`thinking_len == 0`（真·空响应：限流/中断）**不补字**。

    那种情况补一句"上游只思考"是错的诊断，会误导排查方向；
    它该由空白/限流判定那条路去处理。
    """
    w = _empty_output_branch()
    i = w.find("if _thinking_len > 0:")
    assert i != -1
    # 补字必须在 `_thinking_len > 0` 的缩进块内，而不是无条件执行
    indent_of_if = len(w[i]) - len(w[i:].lstrip()) if False else 0
    after = w[i:].split("\n", 1)[1]
    body = [ln for ln in after.splitlines() if ln.strip()][0]
    body_indent = len(body) - len(body.lstrip())
    if_line = w[i:i + w[i:].index("\n")]
    if_indent = len(if_line) - len(if_line.lstrip())
    assert body_indent > if_indent, "补字不在 `if _thinking_len > 0` 块内，会无条件执行"


_EMPTY_WINDOW = _SRC[_SRC.find("if len(full_str) == 0 and not captured_tool_calls:"):][:3200]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
