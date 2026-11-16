"""
tests/test_pure_http_relogin_and_panel.py

验证纯 HTTP + 真实数美指纹重构方案 (Tasks 1-3):
- Task 1: browser_login / trusted_relogin_engine 彻底移除 Playwright/Chromium 与本地节流，
          全部桥接至 http_auth_engine.renew_account_token(..., platform="web")；
          AccountRuntime.try_relogin 透传完整 5 件套与 device_fingerprint，正确处理 ACTIVE / MUTED。
- Task 2: do_activate_cold_account / wake_cold_batch_route 通过纯 HTTP 唤醒 INACTIVE_COLD 账号，
          同步更新 config_manager / SQLite / account_pool。
- Task 3: /api/accounts/health 与 /api/system/relogin_status 彻底移除 "未重登过" 与 IP 过热误报，
          输出三态健康标识 ("已续杯" / "HTTP续杯就绪" / "一次性账号") 与 ip_restricted=False。
"""

import ast
import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.api import admin_routes
from app.config import config_manager
from app.db.repositories import account_repo
from app.engine import browser_login, trusted_relogin_engine, http_auth_engine
from app.engine.account_pool import AccountRuntime, account_pool


def _clear_renewals(*keys: str) -> None:
    for k in keys:
        if k:
            trusted_relogin_engine.clear_http_renewal(k)


def _sample_account(label: str, **extra) -> dict:
    base = {
        "account_label": label,
        "_email": f"{label}@example.com",
        "email": f"{label}@example.com",
        "_password": "secret-password",
        "password": "secret-password",
        "token": "",
        "cookie": "ds_smid=B_REAL_SHUMEI_FP_1234567890",
        "session_id": "",
        "credential_id": "cred_123",
        "mfa_private_key": "priv_pem_or_b64",
        "mfa_public_key": "pub_b64",
        "mfa_key_version": "1",
        "device_id": "dev_123",
        "device_fingerprint": "B_REAL_SHUMEI_FP_1234567890",
        "expire_at": int(time.time()) + 180 * 86400,
        "is_valid": False,
        "status": "INACTIVE_COLD",
        "mute_until": None,
        "ban_kind": None,
        "mute_count": 0,
    }
    base.update(extra)
    return base


# ── Task 1: 无浏览器红线 & 纯 HTTP 桥接 ─────────────────────────────────────


def test_zero_playwright_chromium_launch_in_relogin_engines():
    for rel in ("app/engine/browser_login.py", "app/engine/trusted_relogin_engine.py"):
        src = Path(_REPO_ROOT, rel).read_text(encoding="utf-8")
        assert "p.chromium.launch" not in src
        tree = ast.parse(src)
        imported_modules = set()
        defined_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Name):
                defined_names.add(node.id)
        assert not any("playwright" in m for m in imported_modules)
        assert "_LAST_ATTEMPT" not in defined_names
        assert "_IP_RESTRICT_UNTIL" not in defined_names
        assert "_LAST_FAIL" not in defined_names

    assert not hasattr(trusted_relogin_engine, "_LAST_ATTEMPT")
    assert not hasattr(trusted_relogin_engine, "_IP_RESTRICT_UNTIL")
    assert trusted_relogin_engine.is_ip_restricted() is False
    assert trusted_relogin_engine.ip_restrict_remaining() == 0


def test_quick_trusted_relogin_bridges_to_pure_http_and_persists():
    lbl = "http_bridge_acc"
    acc = _sample_account(lbl, status="INVALID", token="old_tok")
    email = acc["_email"]
    _clear_renewals(lbl, email)

    captured_payloads = []

    def fake_renew(account, platform="web", proxy_url=None, timeout=20):
        captured_payloads.append((dict(account), platform, proxy_url))
        return True, {
            "token": "fresh_http_tok_abc",
            "mode": "signature_challenge",
            "cost": 0.42,
            "is_muted": False,
            "mute_until": None,
        }

    try:
        with patch.object(config_manager, "get_accounts", return_value=[dict(acc)]), \
             patch.object(config_manager, "update_account") as mock_cfg_upd, \
             patch.object(account_pool, "reload"), \
             patch.object(http_auth_engine, "renew_account_token", side_effect=fake_renew):
            ok, res = browser_login.quick_trusted_relogin(email, "secret-password")

        assert ok is True
        assert res["token"] == "fresh_http_tok_abc"
        assert len(captured_payloads) == 1
        sent_acc, sent_platform, _ = captured_payloads[0]
        assert sent_platform == "ios"
        assert sent_acc["credential_id"] == "cred_123"
        assert sent_acc["device_fingerprint"] == "B_REAL_SHUMEI_FP_1234567890"
        mock_cfg_upd.assert_called_once()
        assert mock_cfg_upd.call_args[0][0] == lbl
        assert mock_cfg_upd.call_args[1]["token"] == "fresh_http_tok_abc"
        assert mock_cfg_upd.call_args[1]["status"] == "ACTIVE"
        assert mock_cfg_upd.call_args[1]["is_valid"] is True

        rec = trusted_relogin_engine.get_recent_http_renewal(lbl) or trusted_relogin_engine.get_recent_http_renewal(email)
        assert rec is not None
        assert rec["mode"] == "signature_challenge"
        assert "age_s" in rec

        with patch.object(config_manager, "get_accounts", return_value=[dict(acc)]):
            rh_entry = trusted_relogin_engine.get_relogin_health()["accounts"].get(lbl)
        assert rh_entry is not None
        assert rh_entry["label"] == "已续杯"
        assert rh_entry["level"] == "info"
        assert rh_entry["desc"] == "纯HTTP无感刷新"
    finally:
        _clear_renewals(lbl, email)


def test_account_runtime_try_relogin_active_and_muted():
    acc_data = _sample_account("rt_relogin_acc", status="INVALID", is_valid=False)
    rt = AccountRuntime(acc_data)
    assert rt.device_fingerprint == "B_REAL_SHUMEI_FP_1234567890"

    seen_dicts = []

    def fake_quick(email, password="", timeout=25.0, account_dict=None, proxy=None):
        seen_dicts.append(account_dict)
        return True, {"token": "rt_new_tok", "is_muted": False, "mute_until": None}

    with patch("app.engine.browser_login.quick_trusted_relogin", side_effect=fake_quick), \
         patch.object(config_manager, "update_account") as mock_upd:
        ok = asyncio.run(rt.try_relogin())

    assert ok is True
    assert rt.token == "rt_new_tok"
    assert rt.status == "ACTIVE"
    assert rt.is_valid is True
    assert seen_dicts[0]["credential_id"] == "cred_123"
    assert seen_dicts[0]["device_fingerprint"] == "B_REAL_SHUMEI_FP_1234567890"
    mock_upd.assert_called_once()

    # Upstream reports muted even after token refresh
    rt_muted = AccountRuntime(_sample_account("rt_muted_acc", status="INVALID", is_valid=False))
    future_ms = int((time.time() + 3600) * 1000)

    def fake_quick_muted(email, password="", timeout=25.0, account_dict=None, proxy=None):
        return True, {"token": "muted_tok", "is_muted": True, "mute_until": future_ms}

    with patch("app.engine.browser_login.quick_trusted_relogin", side_effect=fake_quick_muted), \
         patch.object(config_manager, "update_account") as mock_upd_muted:
        ok_muted = asyncio.run(rt_muted.try_relogin())

    assert ok_muted is False
    assert rt_muted.token == "muted_tok"
    assert rt_muted.status == "MUTED"
    assert rt_muted.is_valid is False
    assert rt_muted.ban_kind == "dated"
    assert rt_muted.mute_until is not None and rt_muted.mute_until < 10_000_000_000
    mock_upd_muted.assert_called_once()


def test_http_auth_engine_extracts_mute_state_and_email_fallback():
    tok, is_muted, mute_until = http_auth_engine._extract_user_chat_state({
        "user": {
            "token": "tok_xyz",
            "chat": {"is_muted": 1, "mute_until": 1800000000},
        }
    })
    assert tok == "tok_xyz"
    assert is_muted is True
    assert mute_until == 1800000000

    assert http_auth_engine.extract_shumei_device_id({"device_fingerprint": "B_PLAIN_FP_001"}) == "B_PLAIN_FP_001"

    with patch("app.engine.http_auth_engine.cffi_requests.Session") as mock_sess_cls:
        sess = mock_sess_cls.return_value
        resp_mock = sess.post.return_value
        resp_mock.status_code = 200
        resp_mock.json.return_value = {
            "code": 0,
            "data": {
                "biz_code": 0,
                "biz_data": {
                    "user": {
                        "token": "tok_via__email",
                        "chat": {"is_muted": 0, "mute_until": None},
                    }
                },
            },
        }
        ok, res = http_auth_engine.renew_account_token(
            {
                "_email": "only_underscore@example.com",
                "_password": "pw",
                "credential_id": "cred_underscore_1",
                "mfa_private_key": "priv_underscore_1",
                "device_fingerprint": "B_FP_99900001",
            },
            platform="web",
        )
        assert ok is True
        assert res["token"] == "tok_via__email"
        assert res["mode"] == "direct_login"


# ── Task 2: 冷库纯 HTTP 唤醒闭环 ─────────────────────────────────────────────


def test_do_activate_cold_account_pure_http_active_and_muted():
    lbl = "cold_acc_1"
    acc = _sample_account(lbl, status="INACTIVE_COLD")
    email = acc["_email"]
    _clear_renewals(lbl, email)

    try:
        with patch.object(config_manager, "get_accounts", return_value=[dict(acc)]), \
             patch.object(config_manager, "update_account") as mock_cfg, \
             patch.object(account_repo, "update_runtime_state") as mock_db, \
             patch.object(account_pool, "reload") as mock_reload, \
             patch("app.engine.http_auth_engine.renew_account_token", return_value=(
                 True,
                 {"token": "cold_tok_live", "mode": "signature_challenge", "cost": 0.35, "is_muted": False, "mute_until": None},
             )), \
             patch("app.engine.deepseek_client.DeepSeekClient.create_chat_session", return_value="sid_001"):
            ok, msg = admin_routes.do_activate_cold_account(email)

        assert ok is True
        assert "纯HTTP指纹协议" in msg
        mock_cfg.assert_called_once()
        assert mock_cfg.call_args[0][0] == lbl
        assert mock_cfg.call_args[1]["status"] == "ACTIVE"
        assert mock_cfg.call_args[1]["is_valid"] is True
        assert mock_cfg.call_args[1]["token"] == "cold_tok_live"
        mock_db.assert_called_once()
        mock_reload.assert_called_once()
    finally:
        _clear_renewals(lbl, email)

    # Muted path
    future_ms = int((time.time() + 7200) * 1000)
    try:
        with patch.object(config_manager, "get_accounts", return_value=[dict(acc)]), \
             patch.object(config_manager, "update_account") as mock_cfg, \
             patch.object(account_repo, "update_runtime_state"), \
             patch.object(account_pool, "reload"), \
             patch("app.engine.http_auth_engine.renew_account_token", return_value=(
                 True,
                 {"token": "cold_tok_muted", "mode": "direct_login", "cost": 0.25, "is_muted": True, "mute_until": future_ms},
             )), \
             patch("app.engine.deepseek_client.DeepSeekClient.create_chat_session", return_value=""):
            ok, msg = admin_routes.do_activate_cold_account(lbl)

        assert ok is True
        assert "MUTED" in msg
        assert mock_cfg.call_args[1]["status"] == "MUTED"
        assert mock_cfg.call_args[1]["is_valid"] is False
        assert mock_cfg.call_args[1]["mute_until"] < 10_000_000_000
    finally:
        _clear_renewals(lbl, email)


def test_activate_account_route_validates_result():
    with patch("app.api.admin_routes.do_activate_cold_account", return_value=(True, "激活成功")):
        res = asyncio.run(admin_routes.activate_account_route(admin_routes.AccountActionRequest(account="acc_1")))
    assert res["ok"] is True
    assert res["message"] == "激活成功"


# ── Task 3: 看板健康态消除 "未重登过" 与 IP 过热 ────────────────────────────────


def test_accounts_health_and_relogin_status_three_states_and_no_ip_restriction():
    acc_renewed = _sample_account("acc_renewed", status="ACTIVE", is_valid=True, token="t1")
    acc_ready = _sample_account(
        "acc_ready",
        status="INACTIVE_COLD",
        is_valid=False,
        token="",
        device_fingerprint='{"device_id": "B_JSON_SHUMEI_FP_9988"}',
    )
    acc_oneshot = _sample_account(
        "acc_oneshot",
        status="ACTIVE",
        is_valid=True,
        token="t3",
        credential_id="",
        mfa_private_key="",
        device_fingerprint="",
        device_id="uuid-not-shumei",
        cookie="ds_smid=invalid_uuid_not_B",
    )

    _clear_renewals(
        "acc_renewed", acc_renewed["_email"],
        "acc_ready", acc_ready["_email"],
        "acc_oneshot", acc_oneshot["_email"],
    )
    trusted_relogin_engine.record_http_renewal("acc_renewed", mode="signature_challenge", cost=0.31)

    statuses = [
        {
            "account_label": "acc_renewed",
            "email": acc_renewed["_email"],
            "is_valid": True,
            "status": "ACTIVE",
            "usable": True,
            "muted_now": False,
            "mute_until": None,
            "ban_kind": None,
            "is_busy": False,
            "has_trusted_mfa": True,
        },
        {
            "account_label": "acc_ready",
            "email": acc_ready["_email"],
            "is_valid": False,
            "status": "INACTIVE_COLD",
            "usable": False,
            "muted_now": False,
            "mute_until": None,
            "ban_kind": None,
            "is_busy": False,
            "has_trusted_mfa": True,
        },
        {
            "account_label": "acc_oneshot",
            "email": acc_oneshot["_email"],
            "is_valid": True,
            "status": "ACTIVE",
            "usable": True,
            "muted_now": False,
            "mute_until": None,
            "ban_kind": None,
            "is_busy": False,
            "has_trusted_mfa": False,
        },
    ]

    try:
        with patch.object(account_pool, "get_all_status", return_value=statuses), \
             patch.object(config_manager, "get_accounts", return_value=[acc_renewed, acc_ready, acc_oneshot]):
            health = asyncio.run(admin_routes.accounts_health())
            rel_status = asyncio.run(admin_routes.relogin_status())

        assert health["relogin_health"]["ip"]["restricted"] is False
        assert rel_status["ip"]["restricted"] is False

        by_acc = {row["account"]: row for row in health["results"]}
        for row in by_acc.values():
            assert row["ip_restricted"] is False
            assert row["relogin_label"] != "未重登过"

        assert by_acc["acc_renewed"]["relogin_label"] == "已续杯"
        assert by_acc["acc_renewed"]["relogin_level"] == "info"
        assert by_acc["acc_renewed"]["relogin_desc"] == "纯HTTP无感刷新"

        assert by_acc["acc_ready"]["relogin_label"] == "HTTP续杯就绪"
        assert by_acc["acc_ready"]["relogin_level"] == "success"

        assert by_acc["acc_oneshot"]["relogin_label"] == "一次性账号"
        assert by_acc["acc_oneshot"]["relogin_level"] == "idle"

        # Verify demote_cold_route clears recent renewal
        with patch("app.core.security.disown_account"), \
             patch.object(config_manager, "get_accounts", return_value=[acc_renewed]), \
             patch.object(config_manager, "update_account"), \
             patch.object(account_repo, "update_runtime_state"), \
             patch.object(account_pool, "reload"):
            asyncio.run(admin_routes.demote_cold_route(admin_routes.AccountActionRequest(account="acc_renewed")))
        assert trusted_relogin_engine.get_recent_http_renewal("acc_renewed") is None
    finally:
        _clear_renewals(
            "acc_renewed", acc_renewed["_email"],
            "acc_ready", acc_ready["_email"],
            "acc_oneshot", acc_oneshot["_email"],
        )


def test_strict_trusted_device_requires_expire_at_and_valid_b_fp():
    acc_no_exp = _sample_account("acc_no_exp", status="ACTIVE", is_valid=True, token="t_no_exp")
    acc_no_exp.pop("expire_at", None)

    acc_bad_d_fp = _sample_account(
        "acc_bad_d_fp",
        status="ACTIVE",
        is_valid=True,
        token="t_bad_fp",
        cookie="",
        device_id="4b993a84-958d-4ae8-86ab-6b23aeb43061",
        device_fingerprint='{"device_id": "DeyJhcHBJZCI6ImRlZmF1bHQifQ=="}',
    )

    acc_expired = _sample_account(
        "acc_expired",
        status="ACTIVE",
        is_valid=True,
        token="t_exp",
        expire_at=int(time.time()) - 10,
    )

    statuses = [
        {
            "account_label": acc["account_label"],
            "email": acc["_email"],
            "is_valid": True,
            "status": "ACTIVE",
            "usable": True,
            "muted_now": False,
            "mute_until": None,
            "ban_kind": None,
            "is_busy": False,
            "has_trusted_mfa": False,
        }
        for acc in (acc_no_exp, acc_bad_d_fp, acc_expired)
    ]

    with patch.object(account_pool, "get_all_status", return_value=statuses), \
         patch.object(config_manager, "get_accounts", return_value=[acc_no_exp, acc_bad_d_fp, acc_expired]):
        health = asyncio.run(admin_routes.accounts_health())

    by_acc = {row["account"]: row for row in health["results"]}
    for lbl, expected_reason in (
        ("acc_no_exp", "missing_expire_at"),
        ("acc_bad_d_fp", "invalid_fingerprint"),
        ("acc_expired", "expired_trusted_device"),
    ):
        row = by_acc[lbl]
        assert row["device_trusted"] is False
        assert row["has_trusted_mfa"] is False
        assert row["is_renewable"] is False
        assert row["trusted_reason"] == expected_reason
        assert row["relogin_label"] == "一次性账号"
        assert AccountRuntime(
            next(a for a in (acc_no_exp, acc_bad_d_fp, acc_expired) if a["account_label"] == lbl)
        ).has_trusted_mfa is False


def test_keepalive_skips_disposable_and_missing_expire_at():
    from app.engine import keepalive

    old_ts = int(time.time()) - 10 * 3600
    acc_disposable = _sample_account("ka_disp", status="ACTIVE", is_valid=True, token="tok1", pool_tag="disposable", updated_at=old_ts)
    acc_no_exp = _sample_account("ka_noexp", status="ACTIVE", is_valid=True, token="tok2", pool_tag="public", updated_at=old_ts)
    acc_no_exp.pop("expire_at", None)
    acc_valid = _sample_account("ka_valid", status="ACTIVE", is_valid=True, token="tok3", pool_tag="public", updated_at=old_ts)

    renew_calls = []

    def fake_renew(acc, platform="ios", proxy_url=None, timeout=20):
        renew_calls.append(acc.get("account_label"))
        return True, {"token": "new_tok_ka", "mode": "signature_challenge", "cost": 0.4}

    with patch.object(keepalive, "list_targets", return_value=[acc_disposable, acc_no_exp, acc_valid]), \
         patch.object(keepalive, "_probe_with_retry", new_callable=AsyncMock, return_value={"alive": True, "code": 0, "err": None}), \
         patch.object(keepalive, "renew_account_token", side_effect=fake_renew), \
         patch.object(keepalive, "_persist_token"), \
         patch.object(keepalive, "_RENEW_THRESHOLD_SEC", 7 * 3600), \
         patch.object(keepalive, "_STAGGER", 0):
        stats = asyncio.run(keepalive.run_once())

    assert renew_calls == ["ka_valid"]
    assert stats["last_renewed"] == 1


def test_batch_import_preserves_expire_at_and_pool_tag():
    future_exp = int(time.time()) + 180 * 86400
    item = _sample_account("import_trusted_acc", status="ACTIVE", is_valid=True, token="tok_import_123456789012345", expire_at=future_exp, pool_tag="active")
    added = []

    with patch.object(account_repo, "list_all", return_value=[]), \
         patch.object(config_manager, "accounts", []), \
         patch.object(config_manager, "add_account", side_effect=lambda d: added.append(dict(d))), \
         patch.object(account_pool, "reload"):
        res = asyncio.run(admin_routes.batch_import_accounts(
            admin_routes.BatchImportAccountsRequest(
                accounts_list=[item],
                target_status="ACTIVE",
                check_liveness=False,
                deduplicate=True,
                overwrite_existing=True,
            )
        ))

    assert res["ok"] is True
    assert len(added) == 1
    assert added[0]["expire_at"] == future_exp
    assert added[0]["pool_tag"] == "public"
    assert res["imported"][0]["has_trusted_mfa"] is True


def test_renew_account_token_rejects_fake_uuid_sample_or_missing_mfa():
    for bad_acc, expect_kind in [
        (
            {
                "email": "bad1@example.com",
                "password": "pw",
                "credential_id": "c1",
                "mfa_private_key": "k1",
                "device_id": "550e8400-e29b-41d4-a716-446655440000",
            },
            "missing_device_id",
        ),
        (
            {
                "email": "bad2@example.com",
                "password": "pw",
                "credential_id": "c1",
                "mfa_private_key": "k1",
                "device_id": "BaaYe63E2H6zwF71pgMQ+QekZ32zgskYgSh.thumbcache_sample",
            },
            "missing_device_id",
        ),
        (
            {
                "email": "bad3@example.com",
                "password": "pw",
                "device_id": "BRealShumeiFingerprint123456",
            },
            "missing_mfa",
        ),
    ]:
        ok, res = http_auth_engine.renew_account_token(bad_acc, platform="ios")
        assert ok is False
        assert res["kind"] == expect_kind


def test_batch_import_rejects_missing_or_duplicate_shumei_fingerprint():
    future_exp = int(time.time()) + 180 * 86400
    fp_shared = "BSharedFingerprint00000001"
    existing = _sample_account("existing_fp", device_id=fp_shared, device_fingerprint=fp_shared, cookie="", expire_at=future_exp)
    item_no_fp = _sample_account("nofp", device_id="", device_fingerprint="", cookie="", expire_at=future_exp)
    item_dup_fp = _sample_account("dupfp", device_id=fp_shared, device_fingerprint=fp_shared, cookie="", expire_at=future_exp)
    item_ok = _sample_account("unique_fp", device_id="BUniqueFingerprint00000002", device_fingerprint="BUniqueFingerprint00000002", cookie="", expire_at=future_exp, token="tok_unique_" + "a" * 20)
    added = []

    with patch.object(account_repo, "list_all", return_value=[existing]), \
         patch.object(config_manager, "accounts", []), \
         patch.object(config_manager, "add_account", side_effect=lambda d: added.append(dict(d))), \
         patch.object(account_pool, "reload"):
        res = asyncio.run(admin_routes.batch_import_accounts(
            admin_routes.BatchImportAccountsRequest(
                accounts_list=[item_no_fp, item_dup_fp, item_ok],
                target_status="INACTIVE_COLD",
                check_liveness=False,
                deduplicate=True,
                overwrite_existing=False,
            )
        ))

    assert res["ok"] is True
    assert len(added) == 1
    assert added[0]["email"] == "unique_fp@example.com"
    cats = {s["email"]: s["category"] for s in res["skipped"]}
    assert cats["nofp@example.com"] == "INVALID_FINGERPRINT"
    assert cats["dupfp@example.com"] == "DUPLICATE_FINGERPRINT"


def test_relogin_accounts_biz14_marks_mfa_required_and_disowns():
    future_exp = int(time.time()) + 180 * 86400
    acc = _sample_account("mfa14@example.com", device_id="BRealFpForBiz14Test0001", expire_at=future_exp, status="ACTIVE", is_valid=False)
    config_manager.add_account(dict(acc))
    try:
        with patch("app.engine.browser_login.quick_trusted_relogin", return_value=(False, {"biz_code": 14, "error": "MFA_VERIFICATION_CODE_REQUIRED"})), \
             patch("app.core.security.disown_account") as mock_disown, \
             patch.object(account_repo, "update_runtime_state") as mock_db_upd:
            res = asyncio.run(admin_routes.relogin_accounts(admin_routes.ReloginAccountRequest(account="mfa14@example.com")))
        assert len(res["results"]) == 1
        assert res["results"][0]["ok"] is False
        assert "biz_code=14" in res["results"][0]["msg"]
        mock_disown.assert_called_once_with("mfa14@example.com")
        _, kw = mock_db_upd.call_args
        assert kw["status"] == "MFA_REQUIRED"
        assert kw["ban_kind"] == "mfa_required"
        assert kw["is_valid"] is False
    finally:
        config_manager.remove_account("mfa14@example.com")


def test_token_updated_at_preserved_across_non_token_runtime_updates():
    lbl = "tok_clock_iso@example.com"
    old_tok_ts = int(time.time()) - 8 * 3600
    acc = _sample_account(
        lbl,
        status="ACTIVE",
        is_valid=True,
        token="tok_original_clock_123456",
        updated_at=old_tok_ts,
        token_updated_at=old_tok_ts,
    )
    account_repo.save(acc)
    config_manager.load()
    try:
        # 1. 非换签更新 (如状态/禁言计数/探活) 只能改 updated_at，绝不能清零 token_updated_at
        account_repo.update_runtime_state(lbl, status="ACTIVE", mute_count=0, ban_kind=None)
        config_manager.update_account(lbl, is_valid=True, mute_count=0)
        row = account_repo.get(lbl)
        assert row is not None
        assert int(row["token_updated_at"]) == old_tok_ts
        assert int(row["updated_at"]) >= int(time.time()) - 10

        # 2. 传相同 token 时同样保留原 token_updated_at
        account_repo.update_runtime_state(lbl, token="tok_original_clock_123456")
        row_same = account_repo.get(lbl)
        assert int(row_same["token_updated_at"]) == old_tok_ts

        # 3. 真正换签写入新 token 时才刷新 token_updated_at
        config_manager.update_account(lbl, token="tok_newly_minted_789012")
        row_new = account_repo.get(lbl)
        assert int(row_new["token_updated_at"]) >= int(time.time()) - 10
    finally:
        config_manager.remove_account(lbl)


def test_keepalive_uses_token_updated_at_and_handles_missing_timestamps():
    from app.engine import keepalive

    now_ts = int(time.time())
    # updated_at 刚被刷新成 30 秒前，但 token_updated_at 已经是 8 小时前 -> 仍应触发续杯
    acc_clock_immune = _sample_account(
        "ka_tok_ts",
        status="ACTIVE",
        is_valid=True,
        token="tok_a",
        pool_tag="public",
        updated_at=now_ts - 30,
        token_updated_at=now_ts - 8 * 3600,
    )
    # updated_at 与 token_updated_at 都为 0 的受信任长期号 -> 不应卡在 0 小时永不续杯
    acc_zero_ts = _sample_account(
        "ka_zero_ts",
        status="ACTIVE",
        is_valid=True,
        token="tok_b",
        pool_tag="public",
        updated_at=0,
        token_updated_at=0,
    )
    # token_updated_at 只有 2 小时前 -> 不应提前续杯
    acc_fresh = _sample_account(
        "ka_fresh_ts",
        status="ACTIVE",
        is_valid=True,
        token="tok_c",
        pool_tag="public",
        updated_at=now_ts - 10 * 3600,
        token_updated_at=now_ts - 2 * 3600,
    )

    renew_calls = []

    def fake_renew(acc, platform="ios", proxy_url=None, timeout=20):
        renew_calls.append(acc.get("account_label"))
        return True, {"token": f"new_{acc.get('account_label')}", "mode": "signature_challenge", "cost": 0.3}

    with patch.object(keepalive, "list_targets", return_value=[acc_clock_immune, acc_zero_ts, acc_fresh]), \
         patch.object(keepalive, "_probe_with_retry", new_callable=AsyncMock, return_value={"alive": True, "code": 0, "err": None}), \
         patch.object(keepalive, "renew_account_token", side_effect=fake_renew), \
         patch.object(keepalive, "_persist_token"), \
         patch.object(keepalive, "_RENEW_THRESHOLD_SEC", 7 * 3600), \
         patch.object(keepalive, "_STAGGER", 0):
        stats = asyncio.run(keepalive.run_once())

    assert renew_calls == ["ka_tok_ts", "ka_zero_ts"]
    assert stats["last_renewed"] == 2


def test_batch_import_refreshes_active_5piece_token_and_preserves_jwt_iat():
    import base64
    import json

    now_ts = int(time.time())
    future_exp = now_ts + 180 * 86400
    iat_ts = now_ts - 5 * 3600
    b64_hdr = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    b64_pay = base64.urlsafe_b64encode(json.dumps({"sub": "u1", "iat": iat_ts}).encode()).decode().rstrip("=")
    jwt_tok = f"{b64_hdr}.{b64_pay}.sigpart_1234567890"

    item_refresh = _sample_account(
        "imp_refresh",
        device_id="BImpRefreshFp000000000001",
        device_fingerprint="BImpRefreshFp000000000001",
        token="old_valid_tok_123456789012345",
        expire_at=future_exp,
        pool_tag="public",
    )
    item_jwt = _sample_account(
        "imp_jwt_only",
        device_id="BImpJwtFp0000000000000002",
        device_fingerprint="BImpJwtFp0000000000000002",
        token=jwt_tok,
        expire_at=future_exp,
        pool_tag="public",
    )

    added_refresh = []
    with patch.object(account_repo, "list_all", return_value=[]), \
         patch.object(config_manager, "accounts", []), \
         patch.object(admin_routes, "passive_verify_single_token", return_value={"is_valid": True, "is_muted": False, "msg": "ok"}), \
         patch("app.engine.browser_login.quick_trusted_relogin", return_value=(True, {"token": "fresh_imported_tok_999", "is_muted": False})), \
         patch.object(config_manager, "add_account", side_effect=lambda d: added_refresh.append(dict(d))), \
         patch.object(account_pool, "reload"):
        res = asyncio.run(admin_routes.batch_import_accounts(
            admin_routes.BatchImportAccountsRequest(
                accounts_list=[item_refresh],
                target_status="ACTIVE",
                check_liveness=True,
                refresh_trusted_token=True,
                deduplicate=True,
                overwrite_existing=True,
            )
        ))

    assert res["ok"] is True
    assert len(added_refresh) == 1
    assert added_refresh[0]["token"] == "fresh_imported_tok_999"
    assert int(added_refresh[0]["token_updated_at"]) >= now_ts - 5

    added_jwt = []
    with patch.object(account_repo, "list_all", return_value=[]), \
         patch.object(config_manager, "accounts", []), \
         patch.object(config_manager, "add_account", side_effect=lambda d: added_jwt.append(dict(d))), \
         patch.object(account_pool, "reload"):
        res2 = asyncio.run(admin_routes.batch_import_accounts(
            admin_routes.BatchImportAccountsRequest(
                accounts_list=[item_jwt],
                target_status="ACTIVE",
                check_liveness=False,
                deduplicate=True,
                overwrite_existing=True,
            )
        ))

    assert res2["ok"] is True
    assert len(added_jwt) == 1
    assert int(added_jwt[0]["token_updated_at"]) == iat_ts


def test_single_flight_try_relogin_concurrent_calls_only_login_once():
    lbl = "sf_concurrent@example.com"
    acc = _sample_account(
        lbl,
        status="ACTIVE",
        is_valid=False,
        token="stale_expired_tok_001",
        updated_at=int(time.time()) - 3600,
        token_updated_at=int(time.time()) - 3600,
    )
    config_manager.add_account(dict(acc))
    rt = AccountRuntime(acc)

    calls = []

    def slow_renew(payload, platform="ios", proxy_url=None, timeout=20):
        calls.append(payload.get("email"))
        time.sleep(0.15)
        return True, {"token": "sf_fresh_token_888", "mode": "signature_challenge", "cost": 0.2, "is_muted": False}

    try:
        with patch.object(http_auth_engine, "renew_account_token", side_effect=slow_renew):
            async def _both():
                return await asyncio.gather(rt.try_relogin(), rt.try_relogin())

            ok1, ok2 = asyncio.run(_both())

        assert ok1 is True and ok2 is True
        assert len(calls) == 1
        assert rt.token == "sf_fresh_token_888"
        assert rt.is_valid is True
    finally:
        config_manager.remove_account(lbl)


def test_keepalive_skips_when_already_refreshed_before_lock(tmp_path):
    import sqlite3
    from app.engine import keepalive

    db_file = tmp_path / "ka_sf.db"
    now_ts = int(time.time())
    c = sqlite3.connect(str(db_file))
    c.execute(
        "CREATE TABLE accounts (account_label TEXT PRIMARY KEY, email TEXT, status TEXT, is_valid INTEGER, token TEXT, token_updated_at INTEGER)"
    )
    c.execute(
        "INSERT INTO accounts VALUES (?, ?, 'ACTIVE', 1, ?, ?)",
        ("ka_sf_acc", "ka_sf_acc@example.com", "fresh_tok_just_minted", now_ts - 5),
    )
    c.commit()
    c.close()

    stale_acc = _sample_account(
        "ka_sf_acc",
        email="ka_sf_acc@example.com",
        _email="ka_sf_acc@example.com",
        status="ACTIVE",
        is_valid=True,
        token="old_tok_8h",
        pool_tag="public",
        updated_at=now_ts - 8 * 3600,
        token_updated_at=now_ts - 8 * 3600,
    )
    renew_calls = []

    with patch.object(keepalive, "_DB_PATH", str(db_file)), \
         patch.object(keepalive, "list_targets", return_value=[stale_acc]), \
         patch.object(keepalive, "_probe_with_retry", new_callable=AsyncMock, return_value={"alive": True, "code": 0, "err": None}), \
         patch.object(keepalive, "renew_account_token", side_effect=lambda *a, **kw: renew_calls.append(1) or (True, {"token": "x"})), \
         patch.object(keepalive, "_RENEW_THRESHOLD_SEC", 7 * 3600), \
         patch.object(keepalive, "_STAGGER", 0):
        stats = asyncio.run(keepalive.run_once())

    assert renew_calls == []
    assert stats["last_renewed"] == 0
    assert stats["last_detail"][0]["action"] == "skipped_already_fresh"


def test_batch_import_cold_requires_password_and_skips_token_dedup():
    future_exp = int(time.time()) + 180 * 86400
    no_pwd = _sample_account(
        "cold_nopwd",
        password="",
        _password="",
        device_id="BColdNoPwdFp000000000001",
        device_fingerprint="BColdNoPwdFp000000000001",
        expire_at=future_exp,
    )
    cold_1 = _sample_account(
        "cold_ok_1",
        device_id="BColdOkFp000000000000002",
        device_fingerprint="BColdOkFp000000000000002",
        token="shared_old_token_placeholder_12345",
        expire_at=future_exp,
    )
    cold_2 = _sample_account(
        "cold_ok_2",
        device_id="BColdOkFp000000000000003",
        device_fingerprint="BColdOkFp000000000000003",
        token="shared_old_token_placeholder_12345",
        expire_at=future_exp,
    )

    added = []
    with patch.object(account_repo, "list_all", return_value=[]), \
         patch.object(config_manager, "accounts", []), \
         patch.object(config_manager, "add_account", side_effect=lambda d: added.append(dict(d))), \
         patch.object(account_pool, "reload"):
        res = asyncio.run(admin_routes.batch_import_accounts(
            admin_routes.BatchImportAccountsRequest(
                accounts_list=[no_pwd, cold_1, cold_2],
                target_status="INACTIVE_COLD",
                check_liveness=True,
                deduplicate=True,
                overwrite_existing=False,
            )
        ))

    assert res["ok"] is True
    assert len(added) == 2
    assert all(a["status"] == "INACTIVE_COLD" and a["token"] == "" and a["token_updated_at"] == 0 for a in added)
    assert any(s["category"] == "NO_PASSWORD" for s in res["skipped"])


def test_do_activate_cold_account_single_flight():
    from concurrent.futures import ThreadPoolExecutor

    lbl = "cold_sf_wake@example.com"
    acc = _sample_account(
        lbl,
        status="INACTIVE_COLD",
        is_valid=False,
        token="",
        device_id="BColdSfWakeFp00000000001",
        device_fingerprint="BColdSfWakeFp00000000001",
    )
    config_manager.add_account(dict(acc))

    calls = []

    def slow_renew(payload, platform="ios", proxy_url=None, timeout=20):
        calls.append(payload.get("email"))
        time.sleep(0.15)
        return True, {"token": "cold_woken_tok_777", "mode": "signature_challenge", "cost": 0.3, "is_muted": False}

    try:
        with patch.object(http_auth_engine, "renew_account_token", side_effect=slow_renew), \
             patch.object(admin_routes.DeepSeekClient, "create_chat_session", return_value="sid_cold"), \
             patch.object(account_pool, "reload"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(admin_routes.do_activate_cold_account, lbl)
                f2 = pool.submit(admin_routes.do_activate_cold_account, lbl)
                r1 = f1.result()
                r2 = f2.result()

        assert r1[0] is True and r2[0] is True
        assert len(calls) == 1
    finally:
        config_manager.remove_account(lbl)


def test_keepalive_recovers_40003_for_trusted_account_and_protects_network_errors(tmp_path):
    import sqlite3
    from app.engine import keepalive

    db_file = tmp_path / "gw_ka_40003.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE accounts ("
        "account_label TEXT, email TEXT, status TEXT, is_valid INTEGER, "
        "token TEXT, updated_at INTEGER, token_updated_at INTEGER, ban_kind TEXT)"
    )
    now_ts = int(time.time())
    conn.execute(
        "INSERT INTO accounts VALUES (?, ?, 'ACTIVE', 1, 'stale_tok_ka', ?, ?, NULL)",
        ("ka_40003@example.com", "ka_40003@example.com", now_ts - 600, now_ts - 600),
    )
    conn.execute(
        "INSERT INTO accounts VALUES (?, ?, 'ACTIVE', 1, 'net_err_tok', ?, ?, NULL)",
        ("ka_net@example.com", "ka_net@example.com", now_ts - 600, now_ts - 600),
    )
    conn.commit()
    conn.close()

    acc_40003 = _sample_account("ka_40003", status="ACTIVE", is_valid=True, token="stale_tok_ka", pool_tag="public")
    acc_net = _sample_account("ka_net", status="ACTIVE", is_valid=True, token="net_err_tok", pool_tag="public")

    async def fake_probe(email, token, cookie, retries=1):
        if email == "ka_40003@example.com":
            return {"alive": False, "code": 40003, "err": None}
        return {"alive": False, "code": None, "err": "Timeout"}

    persisted = []
    renew_calls = []

    def fake_renew(acc, platform="ios", proxy_url=None, timeout=20):
        renew_calls.append((acc.get("email"), platform))
        return True, {"token": "recovered_tok_40003", "mode": "signature_challenge", "cost": 0.42}

    with patch.object(keepalive, "_DB_PATH", str(db_file)), \
         patch.object(keepalive, "list_targets", return_value=[acc_40003, acc_net]), \
         patch.object(keepalive, "_probe_with_retry", side_effect=fake_probe), \
         patch.object(keepalive, "renew_account_token", side_effect=fake_renew), \
         patch.object(keepalive, "_persist_token", side_effect=lambda em, tok: persisted.append((em, tok))), \
         patch.object(keepalive, "_STAGGER", 0):
        stats = asyncio.run(keepalive.run_once())

    assert renew_calls == [("ka_40003@example.com", "ios")]
    assert persisted == [("ka_40003@example.com", "recovered_tok_40003")]
    assert stats["last_renewed"] == 1
    assert stats["last_ok"] == 1
    assert stats["last_fail"] == 1

    conn = sqlite3.connect(str(db_file))
    row_net = conn.execute("SELECT status, is_valid FROM accounts WHERE email='ka_net@example.com'").fetchone()
    conn.close()
    assert row_net == ("ACTIVE", 1)


def test_http_auth_engine_matches_platform_tls_and_web_280_headers():
    acc = _sample_account("sync280@example.com")
    seen_sessions = []

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self, impersonate=None):
            self.impersonate = impersonate
            self.proxies = {}
            self.posts = []
            seen_sessions.append(self)

        def post(self, url, data=None, json=None, headers=None, timeout=20):
            self.posts.append({"url": url, "headers": dict(headers or {})})
            return FakeResp({"code": 0, "data": {"biz_code": 0, "biz_data": {"user": {"token": "tok_ok"}}}})

    with patch.object(http_auth_engine.cffi_requests, "Session", FakeSession):
        ok_ios, _ = http_auth_engine.renew_account_token(acc, platform="ios")
        ok_web, _ = http_auth_engine.renew_account_token(acc, platform="web")

    assert ok_ios is True and ok_web is True
    assert seen_sessions[0].impersonate == "safari17_0"
    assert seen_sessions[1].impersonate == "chrome124"
    web_hdrs = seen_sessions[1].posts[0]["headers"]
    assert web_hdrs["x-client-platform"] == "web"
    assert web_hdrs["x-client-version"] == "2.8.0"
    assert web_hdrs["x-client-bundle-id"] == "com.deepseek.chat"
    assert web_hdrs["x-client-timezone-offset"] == "28800"
    assert "x-app-version" not in web_hdrs
    assert len(web_hdrs["x-device-id"]) == 36 and web_hdrs["x-device-id"][14] == "4"
