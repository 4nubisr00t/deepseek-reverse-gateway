"""
架构审计与生产级重构专项契约测试套件 (test_refactored_architecture.py)
覆盖：
1. /v1/responses 三大 P0 缺陷修复验证（会话复用属性名、错误分类属性名、消除双倍扣费）
2. C 端邮箱验证码自注册全链路（HMAC 加密存储、60s 冷却、5 次防爆破熔断、临时邮箱拦截、单 IP 滑动窗口防刷）
3. 专属账号软挂起租约（Soft-Suspension Lease）与并发安全三阶段热替换（Hot Swap）
4. 粘性会话微等待（Micro-Wait）+ 无图跨号平滑漂移降级，以及确定性 CRC32 代理健康状态机
"""

import asyncio
import tempfile
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from main import app
from app.config import config_manager
from app.core import security
from app.core.auth import get_jwt_secret
from app.db.repositories import email_verification_repo, user_repo
from app.db.database import db
from app.engine.account_pool import AccountPool, AccountRuntime, MAX_CONCURRENT_PER_ACCOUNT
from app.engine.deepseek_client import ChatEvent
from app.engine.key_usage import key_usage_engine
from app.engine.proxy_manager import ProxyHealthRegistry
from app.engine.session_tree import session_tracker
from app.api.auth_routes import ip_send_limiter


class TestResponsesRoutesP0Fixes(unittest.TestCase):
    """验证 /v1/responses 三个 P0 级缺陷修复"""

    def setUp(self):
        from app.core.security import verify_api_key, get_identity
        self.key_id = f"k_resp_{int(time.time() * 1000)}"
        app.dependency_overrides[verify_api_key] = lambda: True
        app.dependency_overrides[get_identity] = lambda: {
            "key_id": self.key_id,
            "name": "resp_test_key",
            "owner": "tester",
            "log_label": "tester",
            "sensitive_filter": False,
            "pool_fallback": True,
            "account_labels": [],
            "quota_daily_requests": 100,
            "quota_daily_tokens": 100000,
        }
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("app.api.responses_routes.DeepSeekClient.create_chat_session", return_value="ds_sid_reuse_1")
    @patch("app.api.responses_routes.DeepSeekClient.stream_chat")
    @patch("app.api.responses_routes.account_pool.acquire_account")
    def test_responses_session_reuse_and_single_billing(self, mock_acquire, mock_stream, mock_create):
        """验证：
        1. 同会话第 2 轮复用 cached_session.deepseek_session_id 不再抛 AttributeError(upstream_session_id)
        2. 单次请求在 key_usage_engine 中仅记账 1 次（修复双倍重复扣费 Bug）
        """
        mock_acc = MagicMock()
        mock_acc.token = "tok_reuse"
        mock_acc.cookie = ""
        mock_acc.account_label = "acc_reuse@test.com"
        mock_acquire.return_value = mock_acc

        async def fake_stream(*args, **kwargs):
            yield ChatEvent("content", content="First turn answer")
            yield ChatEvent("message_id", message_id="mid_101")

        mock_stream.side_effect = fake_stream

        # 第 1 轮：新建会话
        r1 = self.client.post(
            "/v1/responses",
            json={"model": "deepseek-v4.1-flash", "input": "Hello turn 1", "conversation_id": "conv_reuse_test"},
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        snap1 = key_usage_engine.snapshot(self.key_id)
        self.assertEqual(snap1.get("requests"), 1, "单次调用只应计费 1 次，绝不能双倍计费")

        # 第 2 轮：携带相同 conversation_id 触发同账号会话复用
        r2 = self.client.post(
            "/v1/responses",
            json={"model": "deepseek-v4.1-flash", "input": [{"role": "user", "content": "Hello turn 1"}, {"role": "assistant", "content": "First turn answer"}, {"role": "user", "content": "Hello turn 2"}], "conversation_id": "conv_reuse_test"},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        # 复用已有会话时 create_chat_session 只在第 1 轮被调用过 1 次
        self.assertEqual(mock_create.call_count, 1)
        snap2 = key_usage_engine.snapshot(self.key_id)
        self.assertEqual(snap2.get("requests"), 2)

    @patch("app.api.responses_routes.DeepSeekClient.create_chat_session", return_value="ds_sid_err")
    @patch("app.api.responses_routes.DeepSeekClient.stream_chat")
    @patch("app.api.responses_routes.account_pool.acquire_account")
    def test_responses_error_event_does_not_crash_on_preflight_decision(self, mock_acquire, mock_stream, mock_create):
        """验证 /v1/responses 遇到上游 error 事件时正确映射状态码，不再抛 AttributeError(http_status)"""
        mock_acc = MagicMock()
        mock_acc.token = "tok_err"
        mock_acc.cookie = ""
        mock_acc.account_label = "acc_err@test.com"
        mock_acquire.return_value = mock_acc

        async def fake_err_stream(*args, **kwargs):
            yield ChatEvent("error", content="消息发送过于频繁，请稍后重试", error_code="rate_limit")

        mock_stream.side_effect = fake_err_stream

        r = self.client.post(
            "/v1/responses",
            json={"model": "deepseek-v4.1-flash", "input": "Trigger rate limit", "stream": False},
        )
        self.assertEqual(r.status_code, 429)


class TestEmailVerificationAndAntiAbuse(unittest.TestCase):
    """验证问题三：用户自助邮箱验证、HMAC 存储、60s 冷却、5 次防爆破与 IP 滑动窗口防刷"""

    def setUp(self):
        self.client = TestClient(app)
        ip_send_limiter.reset()
        self._orig_req_verif = config_manager.config.require_email_verification
        config_manager.config.require_email_verification = True

    def tearDown(self):
        config_manager.config.require_email_verification = self._orig_req_verif
        ip_send_limiter.reset()

    def test_send_code_and_verified_register_full_lifecycle(self):
        email = f"verified_user_{int(time.time()*1000)}@example.com"
        captured_codes = []

        async def fake_send_email(to_addr: str, code: str):
            captured_codes.append((to_addr, code))

        with patch("app.api.auth_routes.send_verification_email_async", side_effect=fake_send_email):
            resp = self.client.post("/api/auth/send-code", json={"email": email})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(len(captured_codes), 1)
            sent_code = captured_codes[0][1]
            self.assertEqual(len(sent_code), 6)
            self.assertTrue(sent_code.isdigit())

            # 验证数据库中绝不明文存储验证码
            row = db.fetchone("SELECT code_hash FROM email_verifications WHERE email = ?", (email,))
            self.assertIsNotNone(row)
            self.assertNotEqual(row["code_hash"], sent_code)
            self.assertEqual(len(row["code_hash"]), 64)  # HMAC-SHA256 hex

            # 60 秒内重复请求发码必须被 429 冷却拦截
            resp_cooldown = self.client.post("/api/auth/send-code", json={"email": email})
            self.assertEqual(resp_cooldown.status_code, 429)

        # 未携带验证码注册应被 400 拦截
        no_code_resp = self.client.post("/api/auth/register", json={"email": email, "password": "Password123!"})
        self.assertEqual(no_code_resp.status_code, 400)

        # 携带正确验证码注册应成功
        reg_resp = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "Password123!", "verification_code": sent_code},
        )
        self.assertEqual(reg_resp.status_code, 200, reg_resp.text)
        self.assertIn("initial_key", reg_resp.json())

    def test_verification_code_brute_force_lockout_after_5_attempts(self):
        """验证连续输错 5 次后验证码立即作废，第 6 次即使输入正确验证码也被拒绝"""
        email = f"bruteforce_{int(time.time()*1000)}@example.com"
        real_code = "839201"
        secret = get_jwt_secret()
        err = email_verification_repo.check_and_prepare_send(
            email=email, code=real_code, client_ip="10.0.0.1", secret=secret, cooldown_seconds=0
        )
        self.assertIsNone(err)

        for i in range(5):
            ok, msg = email_verification_repo.verify_and_consume(email, "000000", secret)
            self.assertFalse(ok)

        # 第 6 次哪怕输入真实的 real_code，也必须因达到 5 次防爆破上限而被拒绝！
        ok_after, msg_after = email_verification_repo.verify_and_consume(email, real_code, secret)
        self.assertFalse(ok_after)
        self.assertIn("失效", msg_after)

    def test_disposable_email_and_ip_rate_limiter(self):
        """验证一次性临时邮箱拦截与单 IP 滑动窗口防邮件炸弹"""
        bad_resp = self.client.post("/api/auth/send-code", json={"email": "bot@mailinator.com"})
        self.assertEqual(bad_resp.status_code, 400)
        self.assertIn("临时邮箱", bad_resp.text)

        # 测试单 IP 1 分钟内超过 3 次触发 429
        ip_send_limiter.reset()
        for i in range(3):
            self.assertIsNone(ip_send_limiter.check_and_record("203.0.113.99"))
        self.assertIsNotNone(ip_send_limiter.check_and_record("203.0.113.99"))


class TestSoftSuspensionAndStickyDegradation(unittest.TestCase):
    """验证问题一与问题二：软挂起原主归还、粘性微等待与无图会话平滑漂移、CRC32 代理状态机"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_keys_file = security.KEYS_FILE
        security.KEYS_FILE = Path(self._tmp.name) / "api_keys.json"
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}
        security._pending_touch = {}
        security._loaded = False
        self._orig_accounts = config_manager.accounts
        self._orig_replenish = config_manager.config.auto_replenish
        config_manager.config.auto_replenish = False

    def tearDown(self):
        config_manager.config.auto_replenish = self._orig_replenish
        security.KEYS_FILE = self._orig_keys_file
        security.load_keys()
        config_manager.accounts = self._orig_accounts
        self._tmp.cleanup()

    def test_soft_suspension_rebinds_rehabilitated_account_to_original_owner(self):
        """当专属账号因长效封禁被 disown 后，康复重激活时可精准溯源并归还原主 Key"""
        rec = security.generate_key(name="vip_user", owner="VIP_Alice", account_labels=["vip_acc@test.com"])
        kid = rec["id"]

        # 触发剥离
        changed = security.disown_account("vip_acc@test.com")
        self.assertTrue(changed)
        self.assertEqual(security.get_key(kid)["account_labels"], [])

        # 验证软挂起快照存在
        susp = security.get_suspended_owner("vip_acc@test.com")
        self.assertIsNotNone(susp)
        self.assertEqual(susp["key_id"], kid)

        # 模拟解封康复后归还原主
        rebound_kid = security.rebind_rehabilitated_account("vip_acc@test.com")
        self.assertEqual(rebound_kid, kid)
        self.assertIn("vip_acc@test.com", security.get_key(kid)["account_labels"])

    def test_sticky_micro_wait_and_graceful_drift(self):
        """验证粘性账号满载时的微窗口吸收 (sticky_micro_wait) 与无图平滑漂移 (allow_sticky_drift)"""
        config_manager.accounts = [
            {"account_label": "sticky@x", "token": "t1", "cookie": "", "is_valid": True, "status": "ACTIVE"},
            {"account_label": "idle@x", "token": "t2", "cookie": "", "is_valid": True, "status": "ACTIVE"},
        ]
        pool = AccountPool()

        async def _run():
            # 占满 sticky@x 的 2 个并发槽位
            a1 = await pool.acquire_account(preferred_label="sticky@x", immediate=True)
            a2 = await pool.acquire_account(preferred_label="sticky@x", immediate=True)
            self.assertEqual(a1.account_label, "sticky@x")
            self.assertEqual(a2.account_label, "sticky@x")

            # 1. 开启 allow_sticky_drift=True 时，满载不报 429，平滑漂移至空闲号 idle@x
            drifted = await pool.acquire_account(
                preferred_label="sticky@x",
                immediate=True,
                allow_sticky_drift=True,
            )
            self.assertIsNotNone(drifted)
            self.assertEqual(drifted.account_label, "idle@x")
            pool.release_account(drifted)

            # 2. 开启 sticky_micro_wait=0.5s，且前序流在 0.1s 后释放槽位 → 微等待成功拿到原粘性号 sticky@x
            async def _delayed_release():
                await asyncio.sleep(0.1)
                pool.release_account(a1)

            asyncio.create_task(_delayed_release())
            waited = await pool.acquire_account(
                preferred_label="sticky@x",
                immediate=True,
                sticky_micro_wait=0.5,
            )
            self.assertIsNotNone(waited)
            self.assertEqual(waited.account_label, "sticky@x")
            pool.release_account(waited)
            pool.release_account(a2)

        asyncio.run(_run())

    def test_proxy_health_registry_deterministic_and_quarantine(self):
        """验证代理健康状态机的 CRC32 确定性分配与故障节点冷却隔离"""
        reg = ProxyHealthRegistry()
        proxies = ["http://127.0.0.1:7891", "http://127.0.0.1:7892", "http://127.0.0.1:7893"]

        p1 = reg.select_healthy_proxy("acc_01@test.com", proxies)
        p2 = reg.select_healthy_proxy("acc_01@test.com", proxies)
        self.assertEqual(p1, p2)

        # 将 p1 标记为故障冷却后，再次分配必须自动避开 p1
        reg.mark_proxy_suspect(p1, cooldown_sec=60.0, reason="timeout")
        self.assertFalse(reg.is_healthy(p1))
        p_failover = reg.select_healthy_proxy("acc_01@test.com", proxies)
        self.assertNotEqual(p_failover, p1)


if __name__ == "__main__":
    unittest.main()
