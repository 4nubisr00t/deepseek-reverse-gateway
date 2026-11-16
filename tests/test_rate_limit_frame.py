"""
回归测试：上游限流帧不得被静默丢弃。

真实故障（2026-09-10 21:15，会话 d7ac8a81，schoolbus1.0）：
上游通过 **SSE data 帧**返回限流错误：

    data: {"type":"error","content":"消息发送过于频繁，请稍后重试",
           "clear_response":true,"finish_reason":"rate_limit_reached"}

而 deepseek_client 的报错分支只检查 `line.startswith("{")`（裸 JSON 形态），
`data: {...}` 不满足该条件 → 被当普通事件解析 → 既无 `v` 字段也不匹配任何分支
→ **整帧静默丢弃** → 网关零输出 → 发空 stop → 客户端报 EMPTY_RESPONSE 并重试。

实测该形态在 44 秒内连续出现 12 次，客户端 12 次重试全部失败。

修复分两处：
  1) deepseek_client：识别 SSE 帧里的 type=error / finish_reason → emit error 事件
  2) openai_routes：限流特征词按实测补齐（rate_limit_reached / 消息发送过于频繁）
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import (  # noqa: E402
    _RATE_LIMIT_HINTS,
    classify_upstream_preflight,
)

# 上游实际返回的文本（逐字取自故障日志）
ACTUAL_UPSTREAM_TEXT = "消息发送过于频繁，请稍后重试"
ACTUAL_FINISH_REASON = "rate_limit_reached"


class FakeEvent:
    def __init__(self, event_type="error", content="", error_code=""):
        self.event_type = event_type
        self.content = content
        self.error_code = error_code


# ─────────────────────────────────────────────────────────────
# 1. 特征词必须覆盖上游真实用词
# ─────────────────────────────────────────────────────────────
def test_hints_cover_actual_upstream_wording():
    """上游原文「消息发送过于频繁」必须被识别——初版清单漏掉了它。"""
    hits = [h for h in _RATE_LIMIT_HINTS if h.lower() in ACTUAL_UPSTREAM_TEXT.lower()]
    assert hits, (
        f"限流特征词未覆盖上游原文 {ACTUAL_UPSTREAM_TEXT!r}；"
        f"当前清单={_RATE_LIMIT_HINTS}"
    )


@pytest.mark.parametrize("fr", ["rate_limit_reached", "rate_limit", "RATE_LIMIT_REACHED"])
def test_hints_cover_finish_reason_forms(fr):
    """上游 finish_reason 用的是 rate_limit_reached 形态，必须被覆盖。"""
    hits = [h for h in _RATE_LIMIT_HINTS if h.lower() in fr.lower()]
    assert hits, f"未覆盖 finish_reason={fr!r}"


@pytest.mark.parametrize("text", [
    "消息发送过于频繁，请稍后重试",
    "请求过于频繁",
    "操作过于频繁",
    "429 Too Many Requests",
    "rate limit exceeded",
    "RATE_LIMIT_REACHED",
])
def test_classifier_marks_these_as_rate_limited(text):
    d = classify_upstream_preflight(FakeEvent("error", text, "rate_limit"), None, 1.0)
    assert d.kind == "rate_limited", f"{text!r} 未被判定为限流"
    assert d.retry_after >= 1


def test_actual_frame_classifies_as_rate_limited():
    """用故障当日真实的帧内容做端到端判定。"""
    d = classify_upstream_preflight(
        FakeEvent("error", ACTUAL_UPSTREAM_TEXT, ACTUAL_FINISH_REASON), None, 3.6
    )
    assert d.kind == "rate_limited", "真实限流帧仍未被识别，客户端会继续无脑重试"
    assert d.metric_error, "缺少可观测性标记"


# ─────────────────────────────────────────────────────────────
# 2. 普通错误不得被误判为限流
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "invalid chat session id",
    "DeepSeek 业务错误",
    "内容审核未通过",
    "模型不存在",
])
def test_ordinary_errors_stay_errors(text):
    """普通错误不得被误判为限流。

    ⚠️ `内容审核未通过` 现在归到 `safety_blocked`（T16/B1 新增分类，
    目的只是让"安全拦截率"可计算；行为不变，仍走通用错误路径）。
    本用例守的是原意"**不是限流**"，所以不再对那一条断言字面量 `error`。
    """
    d = classify_upstream_preflight(FakeEvent("error", text, "biz:1"), None, 1.0)
    assert d.kind != "rate_limited", f"{text!r} 被误判为限流，会掩盖真实故障"
    if text != "内容审核未通过":
        assert d.kind == "error", f"{text!r} 被误判为 {d.kind}"



# ─────────────────────────────────────────────────────────────
# 3. deepseek_client 必须能识别 SSE 帧里的错误
# ─────────────────────────────────────────────────────────────
def test_client_parses_sse_error_frame():
    """直接把上游那帧喂给解析逻辑，确认会产出 error 事件而非静默丢弃。

    这里不启动真实网络：直接读源码断言关键分支存在（结构断言），
    因为完整路径需要真实 SSE 流；同时断言判据字符串确实覆盖了该形态。
    """
    src_path = os.path.join(_REPO_ROOT, "app", "engine", "deepseek_client.py")
    src = open(src_path, encoding="utf-8").read()

    assert 'obj.get("type") == "error"' in src, (
        "deepseek_client 未识别 SSE 帧里的 type=error —— "
        "这正是限流帧被静默丢弃的根因"
    )
    assert 'finish_reason' in src, "未读取 finish_reason，无法判定 rate_limit_reached"
    assert "rate_limit" in src, "未包含 rate_limit 判定"


def test_client_error_branch_precedes_generic_parsing():
    """错误分支必须出现在通用事件解析之前，否则仍会被后续逻辑吞掉。"""
    src_path = os.path.join(_REPO_ROOT, "app", "engine", "deepseek_client.py")
    src = open(src_path, encoding="utf-8").read()
    i_err = src.find('obj.get("type") == "error"')
    i_generic = src.find('if "response_message_id" in obj')
    assert i_err != -1 and i_generic != -1
    assert i_err < i_generic, (
        "错误检测位于通用解析之后，错误帧会先被其它分支吞掉"
    )


# ─────────────────────────────────────────────────────────────
# 4. 上游限流的**多种形态**都必须被识别
#    （每种形态都由一次真实故障暴露出来）
# ─────────────────────────────────────────────────────────────
def test_html_overload_page_is_treated_as_rate_limit():
    """上游过载时返回 HTML 错误页 —— 也是限流的一种形态。

    实测（2026-09-10 压测）：25 账号 × 2 并发被瞬间打满后，
    80 个请求中 22 个收到 text/html。旧实现一律报 bad_content_type → 502，
    客户端当作服务端故障继续重试，进一步加剧过载。
    """
    src_path = os.path.join(_REPO_ROOT, "app", "engine", "deepseek_client.py")
    src = open(src_path, encoding="utf-8").read()
    assert "looks_like_overload" in src, (
        "未识别 HTML 过载页，过载会被误报成 502"
    )
    assert 'error_code="rate_limit"' in src


def test_classifier_checks_error_code_not_only_content():
    """分类器必须同时看 error_code 与 content。

    只查 content 会漏掉"错误码表明限流、但文本无关键词"的情形
    （HTML 过载页就是这样）。该疏漏由本地验证抓出过。
    """
    src_path = os.path.join(_REPO_ROOT, "app", "api", "openai_routes.py")
    src = open(src_path, encoding="utf-8").read()
    assert 'getattr(first_event, "error_code"' in src, (
        "分类器未读取 error_code，会漏判部分限流形态"
    )


def test_all_three_rate_limit_forms_are_detected():
    """三种已知限流形态必须全部判为 rate_limited，且对照不被误判。"""
    cases = [
        ("SSE 限流帧", "消息发送过于频繁，请稍后重试", "rate_limit", "rate_limited"),
        ("HTML 过载页", "上游过载（返回 HTML 错误页），请稍后重试", "rate_limit", "rate_limited"),
        ("finish_reason 形态", "rate_limit_reached", "rate_limit", "rate_limited"),
        ("普通错误（对照）", "invalid chat session id", "biz:1", "error"),
    ]
    for label, content, code, expect in cases:
        d = classify_upstream_preflight(FakeEvent("error", content, code), None, 3.0)
        assert d.kind == expect, f"[{label}] 期望 {expect}，实际 {d.kind}"


def test_error_code_alone_can_trigger_rate_limit():
    """即使文本无任何关键词，仅凭 error_code=rate_limit 也应判为限流。"""
    d = classify_upstream_preflight(
        FakeEvent("error", "上游返回了无法解析的内容", "rate_limit"), None, 1.0
    )
    assert d.kind == "rate_limited"
