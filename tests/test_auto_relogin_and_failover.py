"""
测试：
1. AccountRuntime.try_relogin 自动重新登录与持久化更新
2. 建会话阶段捕获 DeepSeekAuthError 时的自动重登自愈
3. 自动重登失败时无缝故障转移至替补账号 (Auto-Failover)
"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import config_manager
from app.engine.account_pool import AccountPool, AccountRuntime
from app.engine.deepseek_client import DeepSeekAuthError, DeepSeekClient
from app.api import openai_routes


def _make_account(label, **kw):
    d = {
        "account_label": label,
        "token": f"token-{label}",
        "cookie": f"cookie-{label}",
        "_email": f"{label}@test.top",
        "_password": f"pwd-{label}",
        "is_valid": True,
        "status": "ACTIVE",
    }
    d.update(kw)
    return d


class TestAutoReloginAndFailover(unittest.TestCase):
    def setUp(self):
        self._orig_accounts = config_manager.accounts
        self._orig_replenish = config_manager.config.auto_replenish
        config_manager.config.auto_replenish = False

    def tearDown(self):
        config_manager.config.auto_replenish = self._orig_replenish
        config_manager.accounts = self._orig_accounts

    def test_account_runtime_try_relogin_success(self):
        """测试 AccountRuntime 在具备真实 5 件套与 B... 指纹时能够调用 pure-HTTP 重登刷新 Token"""
        raw = _make_account(
            "acc-relogin",
            _password="valid-password",
            credential_id="cred-relogin-1",
            mfa_private_key="priv-relogin-1",
            mfa_public_key="pub-relogin-1",
            device_id="BRealTryReloginFp0001",
            expire_at=int(__import__("time").time()) + 86400 * 20,
        )
        config_manager.accounts = [raw]
        runtime = AccountRuntime(raw)

        with patch("app.engine.browser_login.quick_trusted_relogin") as mock_login:
            mock_login.return_value = (True, {"token": "new-refreshed-token-xyz", "is_muted": False})

            ok = asyncio.run(runtime.try_relogin(proxy=None))
            self.assertTrue(ok)
            self.assertEqual(runtime.token, "new-refreshed-token-xyz")
            self.assertTrue(runtime.is_valid)
            self.assertEqual(runtime.status, "ACTIVE")
            self.assertIsNone(runtime.mute_until)

            # 验证 config_manager 中的持久化更新
            saved = [a for a in config_manager.accounts if a.get("account_label") == "acc-relogin"][0]
            self.assertEqual(saved.get("token"), "new-refreshed-token-xyz")
            self.assertTrue(saved.get("is_valid"))

    def test_account_runtime_try_relogin_no_password(self):
        """测试无密码账号无法执行 try_relogin，安全返回 False"""
        raw = _make_account("acc-nopwd", _password="")
        raw["_password"] = ""
        runtime = AccountRuntime(raw)

        ok = asyncio.run(runtime.try_relogin(proxy=None))
        self.assertFalse(ok)

    def test_session_creation_auto_relogin_heals_in_place(self):
        """测试在 openai_routes 建会话阶段，Token 过期触发 try_relogin 原地重登自愈"""
        raw_a = _make_account("acc-a", _password="pwd-a")
        config_manager.accounts = [raw_a]
        runtime_a = AccountRuntime(raw_a)

        # 模拟第 1 次 create_chat_session 抛出 DeepSeekAuthError，重登成功后第 2 次成功返回 sid
        calls = []

        def mock_create(tok, cookie, proxy):
            calls.append(tok)
            if len(calls) == 1:
                raise DeepSeekAuthError("invalid token 40003")
            return "session-healed-123"

        with patch.object(DeepSeekClient, "create_chat_session", side_effect=mock_create):
            with patch.object(runtime_a, "try_relogin", new_callable=AsyncMock) as mock_relogin:
                async def do_relogin(proxy=None):
                    runtime_a.token = "refreshed-token-a"
                    return True
                mock_relogin.side_effect = do_relogin

                # 执行类似于 routes 里的自愈逻辑
                async def run_flow():
                    account = runtime_a
                    deepseek_session_id = None
                    for attempt in range(3):
                        try:
                            deepseek_session_id = await asyncio.to_thread(
                                DeepSeekClient.create_chat_session,
                                account.token, account.cookie, None
                            )
                        except DeepSeekAuthError:
                            if getattr(account, "password", None) and await account.try_relogin():
                                deepseek_session_id = await asyncio.to_thread(
                                    DeepSeekClient.create_chat_session,
                                    account.token, account.cookie, None
                                )
                        if deepseek_session_id:
                            break
                    return deepseek_session_id, account

                sid, final_acc = asyncio.run(run_flow())
                self.assertEqual(sid, "session-healed-123")
                self.assertEqual(final_acc.account_label, "acc-a")
                self.assertEqual(final_acc.token, "refreshed-token-a")
                self.assertEqual(len(calls), 2)

    def test_session_creation_failover_when_relogin_fails(self):
        """测试当账号 A 凭证失效且重登失败时，自动摘除 A 并调度替补账号 B 建会话"""
        raw_a = _make_account("acc-dead", _password="pwd-dead")
        raw_b = _make_account("acc-backup", _password="pwd-backup")
        config_manager.accounts = [raw_a, raw_b]
        pool = AccountPool()

        runtime_a = pool._accounts["acc-dead"]
        runtime_b = pool._accounts["acc-backup"]

        def mock_create(tok, cookie, proxy):
            if "dead" in tok:
                raise DeepSeekAuthError("account permanent dead")
            return "session-from-backup"

        with patch.object(DeepSeekClient, "create_chat_session", side_effect=mock_create):
            with patch.object(runtime_a, "try_relogin", return_value=False):
                async def run_flow():
                    account = runtime_a
                    deepseek_session_id = None
                    for attempt in range(3):
                        try:
                            deepseek_session_id = await asyncio.to_thread(
                                DeepSeekClient.create_chat_session,
                                account.token, account.cookie, None
                            )
                        except DeepSeekAuthError:
                            relogin_ok = await account.try_relogin()
                            if not relogin_ok:
                                pool.release_account(account, is_muted=True, permanent=True)
                                failover_acc = await pool.acquire_account(avoid_label=account.account_label, immediate=True)
                                if failover_acc:
                                    account = failover_acc
                                    continue
                        if deepseek_session_id:
                            break
                    return deepseek_session_id, account

                sid, final_acc = asyncio.run(run_flow())
                self.assertEqual(sid, "session-from-backup")
                self.assertEqual(final_acc.account_label, "acc-backup")
                self.assertEqual(runtime_a.status, "INVALID")
                self.assertFalse(runtime_a.is_valid)

    def test_recover_or_release_on_auth_error_branches(self):
        config_manager.accounts = [_make_account("acc-upload")]
        pool = AccountPool()
        rt = pool._accounts["acc-upload"]

        with patch.object(rt, "try_relogin", new=AsyncMock(return_value=True)):
            self.assertTrue(asyncio.run(pool.recover_or_release_on_auth_error(rt, stage="图片上传")))
            self.assertEqual(rt.status, "ACTIVE")

        rt.last_relogin_error = {"biz_code": 14, "error": "MFA_VERIFICATION_CODE_REQUIRED"}
        with patch.object(rt, "try_relogin", new=AsyncMock(return_value=False)), \
             patch.object(pool, "mark_mfa_required") as mock_mfa:
            self.assertFalse(asyncio.run(pool.recover_or_release_on_auth_error(rt, stage="图片上传")))
            mock_mfa.assert_called_once()

        rt.last_relogin_error = {"biz_code": 40029, "kind": "network_error", "error": "ip_restricted"}
        with patch.object(rt, "try_relogin", new=AsyncMock(return_value=False)), \
             patch.object(pool, "release_account") as mock_rel:
            self.assertFalse(asyncio.run(pool.recover_or_release_on_auth_error(rt, stage="图片上传")))
            self.assertFalse(mock_rel.call_args.kwargs["permanent"])

        rt.last_relogin_error = {"biz_code": 10, "kind": "challenge_rejected", "error": "bad"}
        with patch.object(rt, "try_relogin", new=AsyncMock(return_value=False)), \
             patch.object(pool, "release_account") as mock_rel:
            self.assertFalse(asyncio.run(pool.recover_or_release_on_auth_error(rt, stage="图片上传")))
            self.assertTrue(mock_rel.call_args.kwargs["permanent"])


if __name__ == "__main__":
    unittest.main()
