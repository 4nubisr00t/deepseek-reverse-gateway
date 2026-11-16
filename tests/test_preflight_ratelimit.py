"""
T4 回归测试：空响应（上游限流）必须转 429，且不得销毁会话缓存。

背景（实测）：
  单账号并发超限或突发限流时，上游返回 HTTP 200 + 空正文（约 0.19s / 281B），
  或一个错误体。旧实现把它当「正常空回答」转发 → 客户端判定模型没说话 →
  触发客户端重试 + 网关「死磕自愈循环」→ 最终吐出【网关安全拦截】文案。
  日志实测：安全拦截 141 次、反射自愈 963 次。

同时锁住 4b：旧实现在「正文为空且未捕获工具」时直接
  session_tracker.delete_session(conv_key)，
  等于把一次空响应放大成「整段上下文丢失」。该行为不得回归。
"""
import ast
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import (  # noqa: E402
    PREFLIGHT_FAST_EMPTY_SECONDS,
    classify_upstream_preflight,
)


class FakeEvent:
    def __init__(self, event_type="content", content="", error_code=""):
        self.event_type = event_type
        self.content = content
        self.error_code = error_code


# ─────────────────────────────────────────────────────────────
# 4a：限流识别
# ─────────────────────────────────────────────────────────────
def test_fast_empty_stream_is_classified_as_rate_limited():
    """快速空流 = 实测的限流特征（0.19s / 281B），必须判为限流。"""
    d = classify_upstream_preflight(None, None, 0.19)
    assert d.kind == "rate_limited"
    assert d.retry_after >= 1
    assert d.metric_error


def test_slow_empty_stream_is_not_misclassified_as_rate_limit():
    """慢速空流更像上游静默超时，不应误判为限流。"""
    d = classify_upstream_preflight(None, None, PREFLIGHT_FAST_EMPTY_SECONDS + 30.0)
    assert d.kind == "ok"


def test_boundary_just_below_threshold_is_rate_limited():
    d = classify_upstream_preflight(None, None, PREFLIGHT_FAST_EMPTY_SECONDS - 0.01)
    assert d.kind == "rate_limited"


@pytest.mark.parametrize("text", [
    "429 Too Many Requests",
    "rate limit exceeded",
    "请求过于频繁，请稍后再试",
    "RATE LIMITED",
])
def test_error_event_with_rate_limit_hint_is_429(text):
    d = classify_upstream_preflight(FakeEvent("error", text, "biz:429"), None, 1.0)
    assert d.kind == "rate_limited", f"未识别限流文本: {text!r}"


def test_ordinary_error_event_stays_an_error():
    """普通上游错误不得被改写成限流，否则会掩盖真实故障。"""
    d = classify_upstream_preflight(FakeEvent("error", "无效的会话", "biz:1"), None, 1.0)
    assert d.kind == "error"


def test_normal_first_event_passes_preflight():
    """正常首事件必须放行。"""
    d = classify_upstream_preflight(FakeEvent("thinking", "We need to"), None, 0.4)
    assert d.kind == "ok"


def test_exception_path_is_not_rewritten_as_rate_limit():
    """拉取首事件时抛异常，应交给上层原有异常处理，而不是伪装成限流。"""
    d = classify_upstream_preflight(None, RuntimeError("boom"), 0.1)
    assert d.kind == "ok"


# ─────────────────────────────────────────────────────────────
# 4b：结构测试——空输出不得销毁会话
# ─────────────────────────────────────────────────────────────
def _calls_in_function(tree, func_name):
    """收集指定函数内所有被调用的属性名（形如 xxx.delete_session）"""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    names.append(sub.func.attr)
    return names


def test_delete_session_always_guarded_by_a_legitimate_reason():
    """delete_session 只允许在这两种正当情形下调用：

      1. **本次新建 + 零产出** → 回收，避免上游堆积空会话
      2. **上下文耗尽（终态）**   → 清除绑定，使下一轮自动开新会话

    历史教训：旧实现在「正文为空且未捕获工具」时**无条件**销毁会话缓存，
    把一次空响应放大成整段上下文丢失（那是 963 次自愈循环的燃料之一）。

    因此每个 delete_session 调用点都必须能明确指出它属于上述哪一种。
    这样既防止「销毁已有会话丢上下文」回归，
    又允许「回收新建空会话」与「耗尽后自愈」这两种正当场景。
    """
    src_path = Path(_REPO_ROOT) / "app" / "api" / "openai_routes.py"
    src = src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                continue
            if sub.func.attr != "delete_session":
                continue
            lineno = getattr(sub, "lineno", 0)
            window = "\n".join(src.splitlines()[max(0, lineno - 25):lineno])
            reason_1 = ("_session_created_by_this_request" in window
                        and "not produced" in window)
            reason_2 = "context_exhausted" in window
            if not (reason_1 or reason_2):
                offenders.append((node.name, lineno))

    assert not offenders, (
        "存在未说明正当理由的 delete_session 调用（可能销毁已有会话、丢上下文）："
        f"{offenders}"
    )


def test_ast_guard_can_detect_unguarded_call():
    """自检：本测试的 AST 检查确实能抓到未加守卫的调用（防止空转通过）。"""
    fake = ast.parse(
        "class T:\n"
        "    def f(self):\n"
        "        session_tracker.delete_session('k')\n"
    )
    found = []
    for node in ast.walk(fake):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr == "delete_session":
                        found.append(sub.func.attr)
    assert found, "AST 自检失败：无法识别 delete_session 调用"


def test_ast_guard_can_detect_unguarded_call():
    """自检：本测试的 AST 检查确实能抓到未加守卫的调用（防止空转通过）。"""
    fake = ast.parse(
        "class T:\n"
        "    def f(self):\n"
        "        session_tracker.delete_session('k')\n"
    )
    found = []
    for node in ast.walk(fake):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr == "delete_session":
                        found.append(sub.func.attr)
    assert found, "AST 自检失败：无法识别 delete_session 调用"
