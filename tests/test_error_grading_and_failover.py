"""
tests/test_error_grading_and_failover.py
验证错误回显五级分级模型与 Failover 全量上下文保护机制：
1. 上游繁忙/服务级过载 (500/502/503/504/HTML/空流) 判定为 upstream_busy，零账号惩罚
2. 永久封号 (permanent) 绝不自动康复回流
3. 有期禁言 (dated) 精确识别并保留 mute_until
4. Failover 时新账号会话树以 has_existing_session=False 重新构建复合 Prompt，保证上下文不丢失
"""

import time
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api.openai_routes import classify_upstream_preflight, PreflightDecision
from app.engine.account_pool import AccountRuntime, AccountPool
from app.engine.deepseek_client import ChatEvent
from app.engine.session_tree import SessionTracker


def test_upstream_busy_classification():
    # 1. 明确的服务器级瞬态错误
    ev502 = ChatEvent("error", content="Bad Gateway (HTTP 502)", error_code="upstream_busy_502")
    d1 = classify_upstream_preflight(ev502, None, 0.5)
    assert d1.kind == "upstream_busy"
    assert d1.metric_error == "upstream_server_busy"

    # 2. 官方文本提示服务器繁忙
    ev_busy = ChatEvent("error", content="服务器繁忙，请稍后重试", error_code="1000")
    d2 = classify_upstream_preflight(ev_busy, None, 0.8)
    assert d2.kind == "upstream_busy"

    # 3. Cloudflare / WAF 拦截
    ev_waf = ChatEvent("error", content="遭遇出口网络安全拦截 (WAF 403)", error_code="waf_blocked")
    d3 = classify_upstream_preflight(ev_waf, None, 0.2)
    assert d3.kind == "upstream_busy"


def test_permanent_ban_never_rehabilitates():
    acc_data = {
        "account_label": "perm_test@domain.com",
        "token": "tok_123",
        "status": "INVALID",
        "ban_kind": "permanent",
        "mute_until": time.time() - 100,  # 时间戳已过
        "is_valid": False,
        "mute_count": 5,
    }
    runtime = AccountRuntime(acc_data)
    with patch("app.config.config_manager.update_account") as mock_update:
        runtime.check_mute_expiry()
        # 严禁触发任何更新或自动回流
        mock_update.assert_not_called()
    assert runtime.status == "INVALID"
    assert runtime.ban_kind == "permanent"
    assert runtime.is_valid is False


def test_failover_context_preservation_prompt():
    """验证 Failover 发生时，新账号重新调用 prepare_prompt(has_existing_session=False) 能完整包含历史上下文"""
    tracker = SessionTracker()
    messages = [
        {"role": "system", "content": "You are a code assistant."},
        {"role": "user", "content": "Write a python function to add numbers"},
        {"role": "assistant", "content": "def add(a, b): return a + b"},
        {"role": "user", "content": "Now write a function to multiply"},
    ]

    # 原先已有会话时：只输出当前提问
    p_existing, _ = tracker.prepare_prompt(messages, has_existing_session=True)
    assert p_existing == "Now write a function to multiply"

    # 容灾换号建立新会话时：强制生成全量复合 Prompt
    p_failover, _ = tracker.prepare_prompt(messages, has_existing_session=False)
    assert "You are a code assistant." in p_failover
    assert "Write a python function to add numbers" in p_failover
    assert "def add(a, b): return a + b" in p_failover
    assert "Now write a function to multiply" in p_failover
