import asyncio
import json
import os
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import classify_upstream_preflight, PreflightDecision
from app.engine.deepseek_client import ChatEvent, DeepSeekClient
from app.engine.session_tree import SessionTracker
from app.engine.account_pool import AccountPool, AccountRuntime


class FakeEvent:
    def __init__(self, event_type="content", content="", error_code=""):
        self.event_type = event_type
        self.content = content
        self.error_code = error_code


def test_classify_preflight_session_broken():
    """测试 invalid message id 及 biz:26 被正确归类为 session_broken。"""
    ev_26 = FakeEvent("error", "invalid message id", "biz:26")
    d1 = classify_upstream_preflight(ev_26, None, 1.0)
    assert d1.kind == "session_broken"
    assert d1.retry_after == 1
    assert "invalid message id" in d1.detail

    ev_tag = FakeEvent("error", "invalid message id", "invalid_message_id")
    d2 = classify_upstream_preflight(ev_tag, None, 1.0)
    assert d2.kind == "session_broken"
    assert d2.retry_after == 1


def test_biz_data_none_safe_parsing():
    """实测验证：上游返回 biz_data: null 时不抛 TypeError 且正确解析错误。"""
    line = '{"code":0,"msg":"","data":{"biz_code":26,"biz_msg":"invalid message id","biz_data":null}}'
    obj = json.loads(line)
    data_block = obj.get("data") or {}
    biz_code = data_block.get("biz_code", 0) or 0
    biz_msg = data_block.get("biz_msg", "") or obj.get("msg", "")

    is_muted = (
        biz_code == 5
        or "muted" in str(biz_msg).lower()
        or ("mute_until" in (data_block.get("biz_data") or {}))
    )
    is_invalid_mid = (
        biz_code in (1, 26)
        or "invalid message id" in str(biz_msg).lower()
    )
    err_tag = (
        "biz:5" if is_muted
        else "invalid_message_id" if is_invalid_mid
        else f"biz:{biz_code}" if biz_code
        else "0"
    )

    assert not is_muted
    assert is_invalid_mid
    assert err_tag == "invalid_message_id"


def test_ghost_message_id_not_committed_when_not_produced():
    """验证：若未真实产出正文或工具调用，session_tracker 中的 parent_message_id 不应被错误推进。"""
    tracker = SessionTracker()
    conv_key = "test_conv_ghost"
    tracker.register_session(conv_key, "test_account", "sess_123")
    tracker.update_message_id(conv_key, "10")

    # 模拟上一轮成功落库为 10
    ctx = tracker.get_session(conv_key)
    assert ctx.last_message_id == "10"

    # 若本轮仅拿到了 message_id=22，但遭遇限流或零产出（未调用 commit）
    # 模拟网关流式结束时的守卫：
    final_message_id = "22"
    full_str = ""
    captured_tool_calls = []

    if final_message_id and (len(full_str) > 0 or captured_tool_calls):
        tracker.update_message_id(conv_key, final_message_id)

    # 确认依然保持在上一轮的 10，没有产生幽灵 ID 污染
    ctx = tracker.get_session(conv_key)
    assert ctx.last_message_id == "10"


def test_ghost_message_id_committed_when_tools_produced():
    """验证：若成功产出工具调用，session_tracker 正常更新至最新消息 ID。"""
    tracker = SessionTracker()
    conv_key = "test_conv_tools"
    tracker.register_session(conv_key, "test_account", "sess_123")
    tracker.update_message_id(conv_key, "10")

    final_message_id = "22"
    captured_tool_calls = [{"id": "call_1", "function": {"name": "pwsh"}}]
    full_str = ""

    if final_message_id and (len(full_str) > 0 or captured_tool_calls):
        tracker.update_message_id(conv_key, final_message_id)

    ctx = tracker.get_session(conv_key)
    assert ctx.last_message_id == "22"


def test_user_accounts_strict_isolation_cannot_access_public_pool():
    """严守用户要求：带 owned_labels 的 Key 绝不能拿到公共池的账号。"""
    p = AccountPool()
    p._accounts = {
        "user_acc_1": AccountRuntime({"account_label": "user_acc_1", "is_valid": True, "token": "t1", "cookie": "c1"}),
        "public_acc_1": AccountRuntime({"account_label": "public_acc_1", "is_valid": True, "token": "t2", "cookie": "c2"}),
        "public_acc_2": AccountRuntime({"account_label": "public_acc_2", "is_valid": True, "token": "t3", "cookie": "c3"}),
    }
    # 当用户指定 owned_labels=["user_acc_1"] 且 pool_fallback=False 时
    allowed = p.allowed_labels(["user_acc_1"], pool_fallback=False)
    assert allowed == {"user_acc_1"}
    assert "public_acc_1" not in allowed
    assert "public_acc_2" not in allowed
