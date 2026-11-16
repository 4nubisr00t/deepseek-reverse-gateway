import asyncio
import json

from app.config import config_manager
from app.engine import quick_register as qr_mod
from app.engine.account_pool import _stable_proxy_index
import app.api.admin_routes as admin_routes


def test_quick_register_three_step_flow_and_p256_mint(monkeypatch):
    eng = qr_mod.QuickRegisterEngine()
    added = []
    monkeypatch.setattr(config_manager, "add_account", lambda rec: added.append(rec))
    monkeypatch.setattr(qr_mod.DeepSeekPOW, "solve", lambda ch: "pow_stub")
    monkeypatch.setattr(qr_mod, "release_captcha_seed", lambda *a, **kw: None)

    def _fake_farm(email, scenario, account_seed, farm_url="", timeout=240):
        return f"P1_{scenario}_" + "x" * 20, "B" + "d" * 24

    monkeypatch.setattr(qr_mod, "request_captcha_from_farm", _fake_farm)

    def _fake_post(sess, url, retries=2, **kwargs):
        if "create_guest_challenge" in url:
            return {
                "code": 0,
                "data": {
                    "biz_code": 0,
                    "biz_data": {
                        "guest_challenge": {
                            "algorithm": "DeepSeekHashV1",
                            "challenge": "c",
                            "salt": "s",
                            "difficulty": 100,
                            "expire_at": 1999999999,
                            "signature": "sig",
                        }
                    },
                },
            }
        if "create_verification_code" in url:
            return {"code": 0, "data": {"biz_code": 0}}
        if url.endswith("/register") and "trusted_device" not in url:
            pl = (kwargs.get("json") or {}).get("payload") or {}
            if pl.get("email") == "preflight_probe@example.com":
                return {"code": 0, "data": {"biz_code": 1}}
            return {"code": 0, "data": {"biz_code": 0, "biz_data": {"token": "reg_tok"}}}
        if url.endswith("/login"):
            return {"code": 0, "data": {"biz_code": 0, "biz_data": {"mfa_session_id": "mfa_sess_1"}}}
        if "check_verification_code" in url:
            return {"code": 0, "data": {"biz_code": 0, "biz_data": {"mfa_ticket": "otp_tkt", "user": {"token": "u_tok_1"}}}}
        if "trusted_device/register" in url:
            return {"code": 0, "data": {"biz_code": 0, "biz_data": {"credential_id": "cred_1", "mfa_challenge": "ch_1", "mfa_ticket": "reg_tkt"}}}
        if "trusted_device/verify" in url:
            return {"code": 0, "data": {"biz_code": 0, "biz_data": {"expire_at": 1999999999}}}
        return {"code": 0, "data": {"biz_code": 0}}

    monkeypatch.setattr(qr_mod, "_post_json", _fake_post)
    monkeypatch.setattr(qr_mod, "renew_account_token", lambda rec, **kw: (True, {"token": "verified_tok_final"}))

    j1 = eng.start(email="learner@example.com")
    assert j1["stage"] == "awaiting_register_otp"

    j2 = eng.verify_register(job_id=j1["job_id"], register_otp="111222")
    assert j2["stage"] == "awaiting_mfa_otp"

    j3 = eng.complete(job_id=j1["job_id"], mfa_otp="333444")
    assert j3["stage"] == "completed"
    assert len(added) == 1
    rec = added[0]
    assert rec["email"] == "learner@example.com"
    assert rec["credential_id"] == "cred_1"
    assert rec["token"] == "verified_tok_final"
    assert rec["mfa_private_key"] and rec["mfa_public_key"]


def test_deterministic_proxy_index_and_openai_proxy_consistency():
    assert _stable_proxy_index("acc_a@example.com", 3) == _stable_proxy_index("acc_a@example.com", 3)
    with open(admin_routes.__file__, "r", encoding="utf-8") as f:
        admin_src = f.read()
    assert "anymessage" not in admin_src.lower()
    assert "subscription_repo" not in admin_src
    assert "/api/register/quick/start" in admin_src
