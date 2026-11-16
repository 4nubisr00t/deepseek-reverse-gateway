"""
tests/test_disconnect_guard.py
测试客户端主动断流保护与短时频控防御 (DisconnectGuard)
"""

import os
import sys
import time
import threading
from unittest.mock import patch
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.disconnect_guard import DisconnectGuard


def test_disconnect_guard_below_threshold():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=2, cooldown_seconds=15.0)
    client_id = "test-client-1"

    # 单次断连不应触发冷却
    guard.record_disconnect(client_id)
    assert guard.check_cooldown(client_id) is None


def test_disconnect_guard_triggers_cooldown():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=2, cooldown_seconds=15.0)
    client_id = "test-client-2"

    guard.record_disconnect(client_id)
    guard.record_disconnect(client_id)

    # 达到 2 次，应该触发 15 秒冷却
    cd = guard.check_cooldown(client_id)
    assert cd is not None
    assert 0.0 < cd <= 15.0


def test_disconnect_guard_cooldown_expiration():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=2, cooldown_seconds=15.0)
    client_id = "test-client-3"

    base_time = 1000.0
    with patch("time.time", return_value=base_time):
        guard.record_disconnect(client_id)
        guard.record_disconnect(client_id)
        assert guard.check_cooldown(client_id) == pytest.approx(15.0)

    # 经过 16 秒，冷却期结束
    with patch("time.time", return_value=base_time + 16.0):
        assert guard.check_cooldown(client_id) is None
        # 冷却映射表中应该已被清理
        assert client_id not in guard._cooldown_until


def test_disconnect_guard_sliding_window_pruning():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=2, cooldown_seconds=15.0)
    client_id = "test-client-4"

    base_time = 1000.0
    # 第 1 次断连发生在 base_time
    with patch("time.time", return_value=base_time):
        guard.record_disconnect(client_id)

    # 第 2 次断连发生在 base_time + 35 秒（超出 30 秒窗口）
    with patch("time.time", return_value=base_time + 35.0):
        guard.record_disconnect(client_id)
        # 第一次记录被修剪，窗口内仅 1 次，不应触发冷却
        assert guard.check_cooldown(client_id) is None


def test_disconnect_guard_client_isolation():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=2, cooldown_seconds=15.0)
    guard.record_disconnect("client-a")
    guard.record_disconnect("client-a")

    # client-a 冷却，client-b 不应受影响
    assert guard.check_cooldown("client-a") is not None
    assert guard.check_cooldown("client-b") is None


def test_disconnect_guard_reset():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=2, cooldown_seconds=15.0)
    guard.record_disconnect("client-1")
    guard.record_disconnect("client-1")
    guard.record_disconnect("client-2")
    guard.record_disconnect("client-2")

    assert guard.check_cooldown("client-1") is not None
    assert guard.check_cooldown("client-2") is not None

    # 重置单个 client
    guard.reset("client-1")
    assert guard.check_cooldown("client-1") is None
    assert guard.check_cooldown("client-2") is not None

    # 全局重置
    guard.reset()
    assert guard.check_cooldown("client-2") is None


def test_disconnect_guard_empty_client_id():
    guard = DisconnectGuard()
    guard.record_disconnect("")
    guard.record_disconnect(None)
    assert guard.check_cooldown("") is None
    assert guard.check_cooldown(None) is None


def test_disconnect_guard_concurrency():
    guard = DisconnectGuard(window_seconds=30.0, threshold_count=10, cooldown_seconds=15.0)
    client_id = "concurrent-client"

    def worker():
        for _ in range(5):
            guard.record_disconnect(client_id)
            guard.check_cooldown(client_id)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 10 线程 * 5 = 50 次断连，肯定触发冷却
    assert guard.check_cooldown(client_id) is not None
