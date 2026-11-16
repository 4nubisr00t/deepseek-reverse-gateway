import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import config_manager
from app.core import security
from app.engine.account_pool import AccountPool, AccountRuntime, PERMANENT_INVALID_SECONDS
from app.engine.deepseek_client import DeepSeekClient
from app.engine.sensitive_filter import SafetyOutputGuard
from app.engine.session_tree import SessionTracker


def test_ios_default_headers_and_web_env_override(monkeypatch):
    monkeypatch.delenv("DSG_CLIENT_PLATFORM", raising=False)
    orig_plat = config_manager.config.client_platform
    try:
        config_manager.config.client_platform = "ios"
        h_ios = DeepSeekClient.build_headers("tok123", "")
        assert h_ios["x-client-platform"] == "ios"
        assert h_ios["x-client-version"] == "2.1.0"
        assert h_ios["x-app-version"] == "20260201.1"
        assert "DeepSeek/2.1.0" in h_ios["user-agent"]
        assert "origin" not in h_ios
        assert "referer" not in h_ios
        assert "cookie" not in h_ios
        assert DeepSeekClient.get_impersonate() == "safari17_0"

        monkeypatch.setenv("DSG_CLIENT_PLATFORM", "web")
        h_web = DeepSeekClient.build_headers("tok123", "c=1", session_id="sid-1")
        assert h_web["x-client-platform"] == "web"
        assert h_web["x-client-bundle-id"] == "com.deepseek.chat"
        assert h_web["x-client-version"] == "2.8.0"
        assert h_web["x-client-timezone-offset"] == "28800"
        assert "x-app-version" not in h_web
        assert "x-device-id" in h_web and len(h_web["x-device-id"]) == 36
        assert h_web["x-device-id"][14] == "4"
        assert h_web["origin"] == "https://chat.deepseek.com"
        assert h_web["referer"].endswith("/sid-1")
        assert h_web["cookie"] == "c=1"
        assert DeepSeekClient.get_impersonate() == "chrome124"
    finally:
        config_manager.config.client_platform = orig_plat


def test_pow_null_json_and_stop_stream_null_mid_guard():
    # PoW 返回非 JSON / None 不再抛 AttributeError
    fake_resp = SimpleNamespace(status_code=200, text="<html>busy</html>", headers={"content-type": "text/html"}, json=lambda: None)
    with patch("app.engine.deepseek_client.cffi_requests.post", return_value=fake_resp):
        assert DeepSeekClient.get_pow_challenge("tok") is None

    # stop_stream 缺少 message_id 时直接跳过请求避免 422
    with patch("app.engine.deepseek_client.cffi_requests.post") as mock_post:
        assert DeepSeekClient.stop_stream("tok", "sid-1", message_id=None) is False
        mock_post.assert_not_called()

    # stop_stream 将字符串数字 message_id 转为 int 发送
    ok_resp = SimpleNamespace(status_code=200, text='{"code":0}', headers={})
    with patch("app.engine.deepseek_client.cffi_requests.post", return_value=ok_resp) as mock_post:
        assert DeepSeekClient.stop_stream("tok", "sid-1", message_id="12") is True
        assert mock_post.call_args.kwargs["json"]["message_id"] == 12


def test_stream_chat_logs_tip_warning_frames(capsys):
    sse_lines = [
        b'data: {"v": {"response": {"message_id": 7, "fragments": [{"type": "TIP", "content": "rate notice"}]}}}\n',
        b'data: {"p": "response/fragments", "v": [{"type": "RESPONSE", "content": "hi"}]}\n',
        b'event: close\n',
        b'data: {"click_behavior": "none"}\n',
    ]

    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}
        text = ""

        def iter_content(self, chunk_size=4096):
            return iter(sse_lines)

        def close(self):
            pass

    async def _collect():
        evs = []
        with patch("app.engine.deepseek_client.DeepSeekClient.get_pow_challenge", return_value=None), \
             patch("app.engine.deepseek_client.cffi_requests.post", return_value=FakeResp()):
            async for ev in DeepSeekClient.stream_chat("tok", "sid-tip", "hello"):
                evs.append(ev)
        return evs

    evs = asyncio.run(_collect())
    out = capsys.readouterr().out
    assert "[Upstream-Notice]" in out
    assert "rate notice" in out
    assert any(ev.event_type == "content" and ev.content == "hi" for ev in evs)


def test_disown_last_account_strict_isolation_and_favor_compat():
    tmp = tempfile.TemporaryDirectory()
    orig_file = security.KEYS_FILE
    try:
        security.KEYS_FILE = Path(tmp.name) / "keys.json"
        security._keys = []
        security._keys_sig = None

        k_dedicated = security.generate_key(
            name="ybr",
            owner="ybr",
            account_labels=["owned@x"],
            sensitive_filter=False,
        )
        k_favor = security.generate_key(
            name="人情",
            owner="张三",
            account_labels=[],
        )
        assert security.get_identity_snapshot({"key_id": k_dedicated["id"]})["pool_fallback"] is False

        # 未剥离前：两者 snapshot 检查
        snap_ded = security.get_identity_snapshot({"key_id": k_dedicated["id"]})
        snap_fav = security.get_identity_snapshot({"key_id": k_favor["id"]})
        assert snap_ded["strict_owned"] is True
        assert snap_ded["prefer_disposable"] is False
        assert snap_fav["strict_owned"] is False

        # 剥离 dedicated 的最后一个账号后，strict_owned 仍为 True，绝不偷 public 池
        security.disown_account("owned@x")
        snap_after = security.get_identity_snapshot({"key_id": k_dedicated["id"]})
        assert snap_after["account_labels"] == []
        assert snap_after["strict_owned"] is True

        pool = AccountPool.__new__(AccountPool)
        AccountPool.__init__(pool)
        pool._accounts = {
            "pub@x": AccountRuntime({"account_label": "pub@x", "status": "ACTIVE", "is_valid": True, "token": "t1", "pool_tag": "public"}),
            "res@x": AccountRuntime({"account_label": "res@x", "status": "ACTIVE", "is_valid": True, "token": "t2", "pool_tag": "reserve"}),
        }

        async def _run():
            with patch.object(pool, "reload"):
                got_ded = await pool.acquire_for_identity(snap_after, immediate=True)
                assert got_ded is None
                assert pool.last_deny_reason == "no_account_assigned"

                # 无主纯人情 Key 仍可使用 public（绝不打破现有兼容契约）
                got_fav = await pool.acquire_for_identity(snap_fav, immediate=True)
                assert got_fav is not None
                assert got_fav.account_label == "pub@x"
                pool.release_account(got_fav)

                # 当免过滤 Key 允许兜底(pool_fallback=True)时，可使用 public 池，但严禁越权使用 reserve 储备池
                snap_bypass_fb = dict(snap_after, pool_fallback=True, strict_owned=False)
                got_pub = await pool.acquire_for_identity(snap_bypass_fb, immediate=True)
                assert got_pub is not None
                assert got_pub.account_label == "pub@x"
                pool.release_account(got_pub)

        asyncio.run(_run())
    finally:
        security.KEYS_FILE = orig_file
        tmp.cleanup()


def test_permanent_dead_account_not_misclassified_as_cooling():
    pool = AccountPool.__new__(AccountPool)
    AccountPool.__init__(pool)
    pool._accounts = {
        "dead_30d@x": AccountRuntime({
            "account_label": "dead_30d@x",
            "status": "INVALID",
            "is_valid": False,
            "ban_kind": "permanent",
            "mute_until": int(time.time()) + PERMANENT_INVALID_SECONDS,
            "token": "t1",
        }),
        "short_backoff@x": AccountRuntime({
            "account_label": "short_backoff@x",
            "status": "MUTED",
            "is_valid": False,
            "ban_kind": "backoff",
            "mute_until": int(time.time()) + 120,
            "token": "t2",
        }),
    }

    async def _run():
        with patch.object(pool, "reload"), patch("app.core.security.sanitize_key_bindings"):
            got1 = await pool.acquire_account(owned_labels=["dead_30d@x"], pool_fallback=False, immediate=True)
            assert got1 is None
            assert pool.last_deny_reason == "all_assigned_accounts_invalid"

            got2 = await pool.acquire_account(owned_labels=["short_backoff@x"], pool_fallback=False, immediate=True)
            assert got2 is None
            assert pool.last_deny_reason == "own_account_at_capacity"

    asyncio.run(_run())


def test_safety_output_guard_strip_and_circuit_breaker_cooldown():
    # 1. 上行历史清洗 <ds_safety>
    raw_hist = "前置文本\n<ds_safety>反复安全提示\n<ds_safety>第二行</ds_safety>\n后置结论"
    cleaned = SafetyOutputGuard.strip_safety_tags(raw_hist)
    assert "ds_safety" not in cleaned.lower()
    assert "前置文本" in cleaned and "后置结论" in cleaned

    # 2. 单行正文偶发提及不误杀，连续多行/多标签立即熔断
    g_single = SafetyOutputGuard(safety_repeat_limit=3)
    assert g_single.feed("这是关于 <ds_safety> 标签的科普说明。\n") is False
    assert g_single.tripped is False

    g_loop = SafetyOutputGuard(safety_repeat_limit=3)
    assert g_loop.feed("<ds_safety>blocked 1\n") is False
    assert g_loop.feed("<ds_safety>blocked 2\n") is False
    assert g_loop.feed("<ds_safety>blocked 3\n") is True
    assert g_loop.tripped is True

    # 3. 安全冷却仅施加 backoff，绝不剥离专属归属
    pool = AccountPool.__new__(AccountPool)
    AccountPool.__init__(pool)
    acc = AccountRuntime({"account_label": "acc@x", "status": "ACTIVE", "is_valid": True, "token": "t1"})
    pool._accounts = {"acc@x": acc}
    with patch("app.core.security.disown_account") as mock_disown, \
         patch("app.config.config_manager.update_account"), \
         patch("app.api.admin_routes.record_system_alert"):
        pool.release_with_safety_cooldown(acc, reason="ds_safety_lines=3")
        mock_disown.assert_not_called()
        assert acc.status == "MUTED"
        assert acc.ban_kind == "backoff"
        assert acc.mute_until is not None and acc.mute_until > time.time()


def test_conv_key_isolated_across_api_keys():
    tracker = SessionTracker()
    msgs = [{"role": "user", "content": "same prompt"}]
    k1, _ = tracker.resolve_conversation(msgs, model="deepseek-v4.1-flash", custom_id="shared-cid", scope_key="key_A")
    k2, _ = tracker.resolve_conversation(msgs, model="deepseek-v4.1-flash", custom_id="shared-cid", scope_key="key_B")
    assert k1 != k2

    b1, _ = tracker.resolve_conversation(msgs, model="deepseek-v4.1-flash", custom_id=None, allow_reuse=True, scope_key="key_A")
    b2, _ = tracker.resolve_conversation(msgs, model="deepseek-v4.1-flash", custom_id=None, allow_reuse=True, scope_key="key_B")
    assert b1 != b2
