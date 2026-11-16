import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import GatewayConfig, config_manager
from app.engine import keepalive
from app.engine.account_pool import AccountRuntime, get_effective_proxy
from app.engine.deepseek_client import DeepSeekAuthError, DeepSeekClient
from app.engine.proxy_manager import (
    ProxyHealthRegistry,
    ProxyManager,
    _is_real_proxy_node,
    execute_with_proxy_failover,
    is_proxy_transport_error,
    proxy_health_registry,
    proxy_manager,
    resolve_egress_candidates,
)


class TestProxyFailoverAndKeepAlive(unittest.TestCase):
    def setUp(self):
        self.orig_config = config_manager.config
        proxy_health_registry._quarantined_until.clear()
        proxy_health_registry._fail_counts.clear()
        proxy_manager._last_switch_time = 0.0
        config_manager.config = GatewayConfig(
            admin_password="test",
            proxies=["http://127.0.0.1:7891", "http://127.0.0.1:7892"],
            relogin_proxies=["http://127.0.0.1:7892", "http://127.0.0.1:7891"],
            proxy=None,
            allow_direct_fallback_on_proxy_error=False,
        )

    def tearDown(self):
        config_manager.config = self.orig_config
        proxy_health_registry._quarantined_until.clear()
        proxy_health_registry._fail_counts.clear()

    def test_pseudo_node_filter_and_selector_resolution(self):
        self.assertFalse(_is_real_proxy_node("剩余流量：170.41 GB"))
        self.assertFalse(_is_real_proxy_node("套餐到期：长期有效"))
        self.assertFalse(_is_real_proxy_node("DIRECT"))
        self.assertFalse(_is_real_proxy_node("自动选择"))
        self.assertTrue(_is_real_proxy_node("🇸🇬|新加坡-IEPL 02"))

        pm = ProxyManager(selector_name="🚀 节点选择")
        mock_map = {
            "节点选择": {"type": "Selector", "now": "🇯🇵|日本-中转 02", "all": ["🇸🇬|新加坡-IEPL 02"]},
            "自动选择": {"type": "URLTest", "now": "🇯🇵|日本-中转 02", "all": ["🇯🇵|日本-中转 02"]},
        }
        self.assertEqual(pm._resolve_selector_name(all_proxies=mock_map), "节点选择")

    def test_resolve_candidates_respects_health_and_direct_flag(self):
        cands = resolve_egress_candidates(primary_proxy="http://127.0.0.1:7891", account_label="u1")
        self.assertEqual(cands[0], "http://127.0.0.1:7891")
        self.assertIn("http://127.0.0.1:7892", cands)
        self.assertNotIn(None, cands)

        proxy_health_registry.mark_proxy_suspect("http://127.0.0.1:7891", cooldown_sec=60.0)
        cands_q = resolve_egress_candidates(primary_proxy="http://127.0.0.1:7891", account_label="u1")
        self.assertEqual(cands_q[0], "http://127.0.0.1:7892")

        config_manager.config.allow_direct_fallback_on_proxy_error = True
        cands_direct = resolve_egress_candidates(primary_proxy="http://127.0.0.1:7891", account_label="u1")
        self.assertIsNone(cands_direct[-1])

        config_manager.config.proxies = []
        config_manager.config.proxy = None
        self.assertEqual(resolve_egress_candidates(primary_proxy=None), [None])

    def test_execute_failover_backup_and_gated_direct(self):
        seen = []

        def fake_call(proxy_url):
            seen.append(proxy_url)
            if proxy_url == "http://127.0.0.1:7892":
                raise ConnectionError("Proxy CONNECT aborted (502)")
            return {"ok": True, "via": proxy_url}

        with patch.object(proxy_manager, "switch_next_healthy_node_sync", return_value=None) as mock_sw:
            res = execute_with_proxy_failover(fake_call, primary_proxy="http://127.0.0.1:7892", context="单测")
        self.assertEqual(res, {"ok": True, "via": "http://127.0.0.1:7891"})
        self.assertEqual(seen, ["http://127.0.0.1:7892", "http://127.0.0.1:7891"])
        self.assertFalse(proxy_health_registry.is_healthy("http://127.0.0.1:7892"))
        self.assertTrue(proxy_health_registry.is_healthy("http://127.0.0.1:7891"))
        mock_sw.assert_called_once_with(selector="注册专用海外")

        # allow_direct_fallback_on_proxy_error=False 时全代理失败不得泄露直连
        seen_no_direct = []

        def all_fail(proxy_url):
            seen_no_direct.append(proxy_url)
            raise TimeoutError("proxy timed out")

        with patch.object(proxy_manager, "switch_next_healthy_node_sync", return_value=None):
            with self.assertRaises(TimeoutError):
                execute_with_proxy_failover(all_fail, primary_proxy="http://127.0.0.1:7891")
        self.assertNotIn(None, seen_no_direct)

        # allow_direct_fallback_on_proxy_error=True 时作为终极兜底允许直连
        config_manager.config.allow_direct_fallback_on_proxy_error = True
        seen_with_direct = []

        def fail_proxies_allow_direct(proxy_url):
            seen_with_direct.append(proxy_url)
            if proxy_url is not None:
                raise ConnectionError("proxy dead")
            return "DIRECT_OK"

        with patch.object(proxy_manager, "switch_next_healthy_node_sync", return_value=None):
            res2 = execute_with_proxy_failover(fail_proxies_allow_direct, primary_proxy="http://127.0.0.1:7891")
        self.assertEqual(res2, "DIRECT_OK")
        self.assertIsNone(seen_with_direct[-1])

    def test_auth_error_never_fails_over(self):
        calls = []

        def raise_auth(proxy_url):
            calls.append(proxy_url)
            raise DeepSeekAuthError("invalid token")

        with self.assertRaises(DeepSeekAuthError):
            execute_with_proxy_failover(raise_auth, primary_proxy="http://127.0.0.1:7891")
        self.assertEqual(calls, ["http://127.0.0.1:7891"])
        self.assertFalse(is_proxy_transport_error(DeepSeekAuthError("invalid token")))

    def test_deepseek_client_uses_backup_proxy_not_direct(self):
        used_proxies = []

        def fake_post(url, **kwargs):
            px = (kwargs.get("proxies") or {}).get("http")
            used_proxies.append(px)
            if px == "http://127.0.0.1:7891":
                raise ConnectionError("curl: (56) Proxy CONNECT aborted")
            resp = MagicMock()
            resp.status_code = 200
            resp.text = json.dumps({"code": 0, "data": {"biz_data": {"challenge": {"algorithm": "v1"}}}})
            resp.json.return_value = json.loads(resp.text)
            return resp

        with patch("app.engine.deepseek_client.cffi_requests.post", side_effect=fake_post), \
             patch.object(proxy_manager, "switch_next_healthy_node_sync", return_value=None):
            ch = DeepSeekClient.get_pow_challenge("tok-test", proxy="http://127.0.0.1:7891")
        self.assertEqual(ch, {"algorithm": "v1"})
        self.assertEqual(used_proxies, ["http://127.0.0.1:7891", "http://127.0.0.1:7892"])

    def test_account_runtime_bypasses_quarantined_proxy(self):
        acc = AccountRuntime({"account_label": "acc-1", "token": "tok-1", "proxy": "http://127.0.0.1:7891"})
        self.assertEqual(acc.effective_proxy, "http://127.0.0.1:7891")
        proxy_health_registry.mark_proxy_suspect("http://127.0.0.1:7891", cooldown_sec=60.0)
        self.assertEqual(acc.effective_proxy, "http://127.0.0.1:7892")
        self.assertEqual(get_effective_proxy({"account_label": "acc-dict"}), "http://127.0.0.1:7892")

    def test_keepalive_forwards_proxy_and_skips_blind_7h_renewal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "gw.db")
            c = sqlite3.connect(db_path)
            c.execute(
                """
                CREATE TABLE accounts (
                    account_label TEXT, email TEXT, password TEXT, token TEXT, cookie TEXT,
                    proxy TEXT, pool_tag TEXT, expire_at INTEGER, is_valid INTEGER,
                    status TEXT, ban_kind TEXT, mute_until INTEGER,
                    credential_id TEXT, mfa_private_key TEXT, mfa_public_key TEXT,
                    device_id TEXT, device_fingerprint TEXT, profile_path TEXT,
                    updated_at INTEGER, token_updated_at INTEGER
                )
                """
            )
            import time
            now_ts = int(time.time())
            c.execute(
                """
                INSERT INTO accounts VALUES (
                    'u@gmx.com','u@gmx.com','pw','tok_old_12345678901234567890','',
                    'http://127.0.0.1:7892','standard',?,?,
                    'ACTIVE',NULL,NULL,
                    'cred1','priv1','pub1',
                    'B12345678901234567890123456789012345678901234567890123456789012','', '',
                    ?, ?
                )
                """,
                (now_ts + 86400 * 30, 1, now_ts - 8 * 3600, now_ts - 8 * 3600),
            )
            c.commit()
            c.close()

            probe_proxies = []
            status_proxies = []

            async def fake_probe(email, token, cookie, retries=1, proxy=None):
                probe_proxies.append(proxy)
                return {"alive": True, "code": 0, "err": None}

            def fake_status(token, cookie="", proxy=None):
                status_proxies.append(proxy)
                return {"ok": True, "is_muted": False, "quota_exhausted": False}

            with patch.object(keepalive, "_DB_PATH", db_path), \
                 patch.object(keepalive, "_LOG_DIR", tmpdir), \
                 patch.object(keepalive, "_RENEW_THRESHOLD_SEC", 0.0), \
                 patch.object(keepalive, "_probe_with_retry", side_effect=fake_probe), \
                 patch("app.engine.deepseek_client.DeepSeekClient.fetch_chat_status", side_effect=fake_status), \
                 patch.object(keepalive, "_run_guarded_renew") as mock_renew:
                res = asyncio.run(keepalive.run_once())

            self.assertEqual(probe_proxies, ["http://127.0.0.1:7892"])
            self.assertEqual(status_proxies, ["http://127.0.0.1:7892"])
            mock_renew.assert_not_called()
            self.assertEqual(res["last_renewed"], 0)


if __name__ == "__main__":
    unittest.main()
