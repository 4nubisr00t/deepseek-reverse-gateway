"""
tests/test_ban_lifecycle.py
测试封号隔离库细分、封禁类型生命周期回流闭环与溯源取证 (Feature A & Feature C)
"""

import os
import sys
import time
from unittest.mock import patch, MagicMock
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.config import config_manager
from app.db.repositories import AccountRepo
from app.engine.account_pool import AccountPool, AccountRuntime


def _make_account(label: str, **kwargs) -> dict:
    base = {
        "account_label": label,
        "token": f"tok_{label}",
        "cookie": f"cookie_{label}",
        "is_valid": True,
        "status": "ACTIVE",
        "ban_kind": None,
        "mute_until": None,
        "mute_count": 0,
    }
    base.update(kwargs)
    return base


# ── 1. release_account 封禁类别细分测试 ──


def test_release_account_permanent_ban():
    pool = AccountPool()
    acc_data = _make_account("acc_perm@test.com")
    runtime = AccountRuntime(acc_data)
    pool.accounts = {runtime.account_label: runtime}

    with patch("app.api.admin_routes.record_system_alert") as mock_alert, \
         patch.object(config_manager, "update_account"):
        pool.release_account(
            runtime,
            permanent=True,
            caller_info={"key_name": "worker_key", "owner": "Alice", "ip": "1.2.3.4"},
        )

    assert runtime.ban_kind == "permanent"
    assert runtime.status == "INVALID"
    assert runtime.is_valid is False
    assert runtime.mute_count == 1
    # 验证溯源信息传递给系统告警
    mock_alert.assert_called_once()
    args, kwargs = mock_alert.call_args
    assert "Alice" in args[2]
    assert "worker_key" in args[2]
    assert "1.2.3.4" in args[2]
    assert "permanent" in args[2]
    assert kwargs.get("caller_info") == {"key_name": "worker_key", "owner": "Alice", "ip": "1.2.3.4"}


def test_release_account_dated_ban():
    pool = AccountPool()
    acc_data = _make_account("acc_dated@test.com")
    runtime = AccountRuntime(acc_data)
    pool.accounts = {runtime.account_label: runtime}

    future_mute = time.time() + 3600  # 1小时后解禁

    with patch("app.api.admin_routes.record_system_alert") as mock_alert, \
         patch.object(config_manager, "update_account"):
        pool.release_account(
            runtime,
            is_muted=True,
            mute_until=future_mute,
            caller_info={"key_name": "bot_key", "owner": "Bob", "ip": "5.6.7.8"},
        )

    assert runtime.ban_kind == "dated"
    assert runtime.status == "MUTED"
    assert runtime.is_valid is False
    mock_alert.assert_called_once()
    args, _ = mock_alert.call_args
    assert "dated" in args[2]
    assert "Bob" in args[2]


def test_release_account_backoff_ban():
    pool = AccountPool()
    acc_data = _make_account("acc_backoff@test.com")
    runtime = AccountRuntime(acc_data)
    pool.accounts = {runtime.account_label: runtime}

    with patch("app.api.admin_routes.record_system_alert") as mock_alert, \
         patch.object(config_manager, "update_account"):
        pool.release_account(
            runtime,
            is_muted=True,
            mute_until=None,  # 未提供明确未来时间点，走本地阶梯退避
        )

    assert runtime.ban_kind == "backoff"
    assert runtime.status == "MUTED"
    assert runtime.is_valid is False
    mock_alert.assert_called_once()
    args, _ = mock_alert.call_args
    assert "backoff" in args[2]


def test_release_account_success_clears_ban_kind_and_mute_count():
    pool = AccountPool()
    acc_data = _make_account("acc_cleared@test.com", mute_count=3, ban_kind="backoff")
    runtime = AccountRuntime(acc_data)
    pool.accounts = {runtime.account_label: runtime}

    with patch.object(config_manager, "update_account") as mock_update:
        pool.release_account(runtime, is_muted=False, permanent=False)

    assert runtime.mute_count == 0
    assert runtime.ban_kind is None
    mock_update.assert_called_once_with(runtime.account_label, mute_count=0, ban_kind=None)


# ── 2. check_mute_expiry 双轨康复生命周期测试 ──


def test_check_mute_expiry_dated_ban_rehabilitates_to_cold_pool():
    """有期禁言到期后，双轨康复：回流到零IP冷库 (INACTIVE_COLD)，抹除凭证，等待重新探活/登录"""
    acc_data = _make_account(
        "acc_dated_expire@test.com",
        status="MUTED",
        ban_kind="dated",
        mute_until=time.time() - 10,  # 已过期
        token="old_tok",
        cookie="old_cookie",
    )
    runtime = AccountRuntime(acc_data)

    with patch.object(config_manager, "update_account") as mock_update:
        runtime.check_mute_expiry()

    assert runtime.status == "INACTIVE_COLD"
    assert runtime.is_valid is False
    assert runtime.token == ""
    assert runtime.cookie == ""
    assert runtime.session_id is None
    assert runtime.ban_kind is None
    assert runtime.mute_until is None
    assert runtime.mute_count == 0
    mock_update.assert_called_once_with(
        "acc_dated_expire@test.com",
        is_valid=False,
        status="INACTIVE_COLD",
        mute_until=None,
        ban_kind=None,
        mute_count=0,
        token="",
        cookie="",
        session_id="",
    )


def test_check_mute_expiry_normal_mute_recovers_to_active():
    """普通退避禁言到期后，直接恢复 ACTIVE 可用状态"""
    acc_data = _make_account(
        "acc_normal_expire@test.com",
        status="MUTED",
        ban_kind="backoff",
        mute_until=time.time() - 10,  # 已过期
        token="valid_tok",
    )
    runtime = AccountRuntime(acc_data)

    with patch.object(config_manager, "update_account") as mock_update:
        runtime.check_mute_expiry()

    assert runtime.status == "ACTIVE"
    assert runtime.is_valid is True
    assert runtime.token == "valid_tok"
    assert runtime.ban_kind is None
    assert runtime.mute_until is None
    mock_update.assert_called_once_with(
        "acc_normal_expire@test.com",
        is_valid=True,
        status="ACTIVE",
        mute_until=None,
        ban_kind=None,
    )


# ── 3. quarantine_clear 保护有期禁言测试 ──


@pytest.mark.anyio
async def test_quarantine_clear_protects_dated_bans():
    from app.api.admin_routes import quarantine_clear_route

    now = time.time()
    mock_accounts = [
        {
            "account_label": "acc_perm@test.com",
            "status": "QUARANTINE_INVALID",
            "ban_kind": "permanent",
            "mute_until": now + 10000,
        },
        {
            "account_label": "acc_dated@test.com",
            "status": "MUTED",
            "ban_kind": "dated",
            "mute_until": now + 3600,
        },
        {
            "account_label": "acc_garbage@test.com",
            "status": "QUARANTINE_MUTED",
            "ban_kind": None,
            "mute_until": None,
        },
    ]

    with patch.object(config_manager, "get_accounts", return_value=mock_accounts), \
         patch("app.core.security.disown_account") as mock_disown, \
         patch.object(config_manager, "remove_account") as mock_remove, \
         patch("app.engine.account_pool.account_pool.reload"):

        res = await quarantine_clear_route()

    assert res["ok"] is True
    assert res["count"] == 2
    # 必须删除了 perm 和 garbage，但保留了 dated！
    disowned_labels = [call.args[0] for call in mock_disown.call_args_list]
    assert "acc_perm@test.com" in disowned_labels
    assert "acc_garbage@test.com" in disowned_labels
    assert "acc_dated@test.com" not in disowned_labels

    removed_labels = [call.args[0] for call in mock_remove.call_args_list]
    assert "acc_perm@test.com" in removed_labels
    assert "acc_garbage@test.com" in removed_labels
    assert "acc_dated@test.com" not in removed_labels


# ── 4. AccountRepo 对 ban_kind 与 None 清空的持久化测试 ──


def test_account_repo_ban_kind_persistence(tmp_path):
    from app.db.database import DatabaseManager
    from app.db.schema import init_schema

    db_path = tmp_path / "test_ban_kind.db"
    mgr = DatabaseManager(db_path)
    init_schema(mgr)

    with patch("app.db.repositories.db", mgr):
        repo = AccountRepo()
        repo.save({
            "account_label": "repo_acc@test.com",
            "token": "tok123",
            "is_valid": True,
            "status": "ACTIVE",
            "ban_kind": None,
        })

        # 更新为 dated
        repo.update_runtime_state("repo_acc@test.com", ban_kind="dated", mute_until=1900000000)
        row = repo.get("repo_acc@test.com")
        assert row["ban_kind"] == "dated"
        assert row["mute_until"] == 1900000000

        # 更新为 None (使用 _UNSET 机制明确置空)
        repo.update_runtime_state("repo_acc@test.com", ban_kind=None, mute_until=None)
        row = repo.get("repo_acc@test.com")
        assert row["ban_kind"] is None
        assert row["mute_until"] is None

    mgr.close()
