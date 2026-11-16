"""
上下文窗口耗尽的识别回归测试（T6 / T19）。

实测上游原文（2026-09-10，E37）：
    {"type":"error","content":"达到对话长度上限，请开启新对话","clear_response":true,...}

实测断点：
    400,000 字符 (~100k tokens)  → 正常，ttfb 2.40s
    800,000 字符 (~200k tokens)  → 正常，ttfb 2.52s
  1,600,000 字符 (~400k tokens)  → 正常，ttfb 6.58s
  3,200,000 字符 (~800k tokens)  → 正常，ttfb 13.46s
  6,400,000 字符 (~1.6M tokens)  → ❌ 达到对话长度上限   ← 断点在此区间

为什么必须单独识别：
  · 该错误是**终态**——同一会话无法通过重试恢复，必须开新会话
  · 旧行为：当普通错误 → 502 → 客户端视作服务端故障**无限重试**，
    而每次重试必然再失败 → 表现为"任务做到一半就再也写不出来了"（T19 实证）
  · 报错形态与限流帧完全一致（同为 type:error + clear_response），
    但语义相反：限流**可**退避重试，长度上限**不可**
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import (  # noqa: E402
    _CONTEXT_EXHAUSTED_HINTS,
    classify_upstream_preflight,
)

ACTUAL_UPSTREAM_TEXT = "达到对话长度上限，请开启新对话"


class FakeEvent:
    def __init__(self, event_type="error", content="", error_code=""):
        self.event_type = event_type
        self.content = content
        self.error_code = error_code


# ─────────────────────────────────────────────────────────────
# 1. 必须被识别为 context_exhausted（而非限流、也非普通错误）
# ─────────────────────────────────────────────────────────────
def test_actual_upstream_text_is_recognized():
    d = classify_upstream_preflight(
        FakeEvent("error", ACTUAL_UPSTREAM_TEXT, "length_limit"), None, 5.0
    )
    assert d.kind == "context_exhausted", (
        f"未识别为上下文耗尽，实际 {d.kind}——会被当普通错误重试，永远失败"
    )
    assert d.metric_error, "缺少可观测性标记"


def test_hints_cover_actual_wording():
    hits = [h for h in _CONTEXT_EXHAUSTED_HINTS
            if h in ACTUAL_UPSTREAM_TEXT]
    assert hits, (
        f"特征词未覆盖上游原文 {ACTUAL_UPSTREAM_TEXT!r}；"
        f"当前清单={_CONTEXT_EXHAUSTED_HINTS}"
    )


@pytest.mark.parametrize("text", [
    "达到对话长度上限，请开启新对话",
    "对话长度上限",
    "context length exceeded",
    "maximum context length is 128000 tokens",
    "input is too long",
])
def test_various_wording_forms(text):
    d = classify_upstream_preflight(FakeEvent("error", text, ""), None, 5.0)
    assert d.kind == "context_exhausted", f"{text!r} 未被识别"


def test_context_exhausted_is_not_rate_limited():
    """两者形态相同但语义相反，绝不能混淆。"""
    ctx = classify_upstream_preflight(
        FakeEvent("error", ACTUAL_UPSTREAM_TEXT, "length_limit"), None, 5.0)
    rl = classify_upstream_preflight(
        FakeEvent("error", "消息发送过于频繁，请稍后重试", "rate_limit"), None, 5.0)
    assert ctx.kind == "context_exhausted"
    assert rl.kind == "rate_limited"
    assert ctx.kind != rl.kind


def test_context_exhausted_has_no_retry_after():
    """终态错误不应给出 Retry-After（那会暗示"等一会儿再来"）。"""
    d = classify_upstream_preflight(
        FakeEvent("error", ACTUAL_UPSTREAM_TEXT, "length_limit"), None, 5.0)
    assert not d.retry_after, "终态错误不该带退避提示"


def test_ordinary_errors_still_errors():
    """普通错误不得被误判为上下文耗尽。

    ⚠️ `内容审核未通过` 现在会被判成 `safety_blocked`（T16/B1 新增的分类，
    为了能算出"安全拦截率"）。这条用例的**原意**是"别把它当上下文耗尽"，
    所以断言改成守原意，而不是守那个字面量 —— 守字面量会让"多分一类"
    看起来像回归，而它其实只是把一个比率变成可计算的。
    """
    for text in ("invalid chat session id", "内容审核未通过", "模型不存在"):
        d = classify_upstream_preflight(FakeEvent("error", text, "biz:1"), None, 1.0)
        assert d.kind != "context_exhausted", f"{text!r} 被误判为 {d.kind}"
    # 另外两条仍然必须是通用错误（不是安全拦截）
    for text in ("invalid chat session id", "模型不存在"):
        d = classify_upstream_preflight(FakeEvent("error", text, "biz:1"), None, 1.0)
        assert d.kind == "error", f"{text!r} 被误判为 {d.kind}"



# ─────────────────────────────────────────────────────────────
# 2. 会话必须可自愈（这是"写不出来"的真正解药）
# ─────────────────────────────────────────────────────────────
def test_session_is_cleared_on_context_exhausted():
    """结构断言：命中上下文耗尽时必须清除会话绑定。

    否则下一轮会继续复用那个已满的会话 → 再次失败 → 永远卡住。
    清除绑定后，网关的复用闸门会因查不到缓存会话而自动开新会话。
    """
    src_path = os.path.join(_REPO_ROOT, "app", "api", "openai_routes.py")
    src = open(src_path, encoding="utf-8").read()
    i = src.find('decision.kind == "context_exhausted"')
    assert i != -1, "未处理 context_exhausted 分支"
    window = src[i:i + 1600]
    assert "session_tracker.delete_session" in window, (
        "命中上下文耗尽却未清除会话绑定——下一轮仍会复用已满会话，永远失败"
    )
    assert "HTTPException" in window, "未向客户端返回明确错误"


def test_client_gets_4xx_not_5xx_for_context_exhausted():
    """必须返回 4xx：5xx 会让客户端当作服务端故障而无限重试。"""
    src_path = os.path.join(_REPO_ROOT, "app", "api", "openai_routes.py")
    src = open(src_path, encoding="utf-8").read()
    i = src.find('decision.kind == "context_exhausted"')
    window = src[i:i + 1600]
    assert "status_code=400" in window, (
        "上下文耗尽应返回 4xx（客户端不应重试同一会话）；5xx 会触发无限重试"
    )
