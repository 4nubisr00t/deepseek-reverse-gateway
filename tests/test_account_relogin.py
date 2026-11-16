"""
tests/test_account_relogin.py
测试账号一键重新登录 (POST /api/accounts/relogin) 接口契约
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi.testclient import TestClient
from main import app
from app.config import config_manager


class TestAccountReloginEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.cookies.set("ds_admin_session", "fake.session")
        # Mock admin auth
        self.cookie_patcher = patch("app.core.security.verify_admin_session", return_value=True)
        self.cookie_patcher.start()

    def tearDown(self):
        self.cookie_patcher.stop()

    @patch("app.engine.browser_login.quick_trusted_relogin")
    def test_relogin_single_account_success(self, mock_login):
        mock_login.return_value = (True, {"token": "new_fresh_token_1234567890", "is_muted": False})

        # 插入一个具备真实 5 件套与 B... 数美指纹的待重登测试号
        config_manager.add_account({
            "account_label": "relogin_test@example.com",
            "token": "old_expired_token",
            "password": "TestPassword123!",
            "credential_id": "cred_relogin_1",
            "mfa_private_key": "priv_relogin_1",
            "mfa_public_key": "pub_relogin_1",
            "device_id": "BRealShumeiFingerprint0001",
            "expire_at": int(__import__("time").time()) + 86400 * 20,
            "is_valid": False,
            "status": "ACTIVE",
        })

        try:
            resp = self.client.post(
                "/api/accounts/relogin",
                json={"account": "relogin_test@example.com"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            results = data.get("results", [])
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["ok"])

            # 验证内存与配置中的 token 是否刷新且传入了绑定指纹 account_dict
            _, call_kw = mock_login.call_args
            self.assertEqual(call_kw["account_dict"]["device_id"], "BRealShumeiFingerprint0001")
            acc = [a for a in config_manager.get_accounts() if a.get("account_label") == "relogin_test@example.com"][0]
            self.assertEqual(acc["token"], "new_fresh_token_1234567890")
            self.assertTrue(acc["is_valid"])
        finally:
            config_manager.remove_account("relogin_test@example.com")

    def test_relogin_account_missing_password(self):
        config_manager.add_account({
            "account_label": "no_pwd@example.com",
            "token": "old_token",
            "password": "",
            "_password": "",
            "credential_id": "cred_nopwd_1",
            "mfa_private_key": "priv_nopwd_1",
            "mfa_public_key": "pub_nopwd_1",
            "device_id": "BRealShumeiFingerprint0002",
            "expire_at": int(__import__("time").time()) + 86400 * 20,
            "is_valid": False,
            "status": "ACTIVE",
        })

        try:
            resp = self.client.post(
                "/api/accounts/relogin",
                json={"account": "no_pwd@example.com"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            results = data.get("results", [])
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0]["ok"])
            self.assertIn("未保存密码", results[0]["msg"])
        finally:
            config_manager.remove_account("no_pwd@example.com")

    def test_relogin_account_untrusted_refused(self):
        config_manager.add_account({
            "account_label": "untrusted_relogin@example.com",
            "token": "old_token",
            "password": "TestPassword123!",
            "is_valid": False,
            "status": "ACTIVE",
        })

        try:
            resp = self.client.post(
                "/api/accounts/relogin",
                json={"account": "untrusted_relogin@example.com"},
            )
            self.assertEqual(resp.status_code, 200)
            results = resp.json().get("results", [])
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0]["ok"])
            self.assertIn("拒绝兜底登录", results[0]["msg"])
        finally:
            config_manager.remove_account("untrusted_relogin@example.com")
