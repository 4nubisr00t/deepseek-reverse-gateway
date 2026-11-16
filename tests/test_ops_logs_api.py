"""
运维监控与日志诊断 API 自动化单元测试
测试端点:
  - GET /api/system/logs
  - GET /api/system/forensics
  - GET /api/system/proxies/status
"""

import asyncio
import time
import unittest
from unittest.mock import patch, MagicMock

from app.api.admin_routes import (
    get_system_logs,
    export_system_logs,
    get_system_forensics,
    get_proxies_status,
)
from app.config import config_manager
from app.engine.account_pool import account_pool


class TestOpsLogsApi(unittest.TestCase):

    def test_get_system_logs_default(self):
        """测试获取系统日志默认参数与结构"""
        res = asyncio.run(get_system_logs(service="ds-gateway", lines=50))
        self.assertTrue(res["ok"])
        self.assertEqual(res["service"], "ds-gateway")
        self.assertIsInstance(res["lines"], list)
        self.assertIn("total_lines", res)
        self.assertIn("timestamp", res)

    def test_get_system_logs_clamping_and_filter(self):
        """测试日志行数区间钳位与关键字过滤"""
        # 测试 lines 钳位
        res_small = asyncio.run(get_system_logs(lines=1))
        # 实际最低钳位为 10
        self.assertLessEqual(len(res_small["lines"]), 2000)

        # 测试关键字过滤
        res_filter = asyncio.run(get_system_logs(filter="NON_EXISTENT_KEYWORD_XYZ_999"))
        self.assertTrue(res_filter["ok"])
        self.assertEqual(len(res_filter["lines"]), 0)

    def test_get_system_forensics_clean_and_muted(self):
        """测试账号风控溯源诊断输出"""
        # 1. 干净状态
        with patch.object(account_pool, "get_all_status", return_value=[
            {
                "account_label": "healthy@ds.com",
                "is_valid": True,
                "status": "ACTIVE",
                "usable": True,
                "mute_until": 0,
                "mute_count": 0,
                "owner": None,
            }
        ]):
            res = asyncio.run(get_system_forensics())
            self.assertTrue(res["ok"])
            self.assertEqual(res["muted_count"], 0)
            self.assertEqual(len(res["muted_accounts"]), 0)

        # 2. 模拟被封禁/禁言账号
        future_time = time.time() + 1800
        with patch.object(account_pool, "get_all_status", return_value=[
            {
                "account_label": "healthy@ds.com",
                "is_valid": True,
                "status": "ACTIVE",
                "usable": True,
                "mute_until": 0,
                "mute_count": 0,
                "owner": None,
            },
            {
                "account_label": "banned_acc@ds.com",
                "is_valid": True,
                "status": "MUTED",
                "usable": False,
                "mute_until": future_time,
                "mute_count": 2,
                "owner": "fanwang",
            }
        ]):
            res2 = asyncio.run(get_system_forensics())
            self.assertEqual(res2["muted_count"], 1)
            self.assertEqual(len(res2["muted_accounts"]), 1)
            muted = res2["muted_accounts"][0]
            self.assertEqual(muted["account"], "banned_acc@ds.com")
            self.assertEqual(muted["status"], "MUTED")
            self.assertEqual(muted["owner"], "fanwang")
            self.assertGreater(muted["remaining_seconds"], 1700)
            self.assertEqual(muted["mute_count"], 2)

        # 3. 验证零IP冷库账号绝对不会被误判为封禁/禁言
        with patch.object(account_pool, "get_all_status", return_value=[
            {
                "account_label": "healthy@ds.com",
                "is_valid": True,
                "status": "ACTIVE",
                "usable": True,
                "mute_until": 0,
                "mute_count": 0,
                "owner": None,
            },
            {
                "account_label": "cold_reserve@ds.com",
                "is_valid": False,
                "status": "INACTIVE_COLD",
                "usable": False,
                "mute_until": None,
                "mute_count": 0,
                "owner": None,
            },
            {
                "account_label": "warming_acc@ds.com",
                "is_valid": False,
                "status": "WARMING",
                "usable": False,
                "mute_until": None,
                "mute_count": 0,
                "owner": None,
            },
            {
                "account_label": "banned_acc@ds.com",
                "is_valid": False,
                "status": "MUTED",
                "usable": False,
                "mute_until": future_time,
                "mute_count": 2,
                "owner": "fanwang",
            },
        ]):
            res3 = asyncio.run(get_system_forensics())
            self.assertEqual(res3["muted_count"], 1)
            self.assertEqual(res3["cold_count"], 1)
            self.assertEqual(res3["warming_count"], 1)
            self.assertEqual(res3["active_count"], 1)
            self.assertEqual(len(res3["muted_accounts"]), 1)
            self.assertEqual(res3["muted_accounts"][0]["account"], "banned_acc@ds.com")

    def test_get_proxies_status_mocked(self):
        """测试多出口代理测速与 IP 嗅探 API"""
        mock_response_probe = MagicMock()
        mock_response_probe.status_code = 202

        mock_response_ip = MagicMock()
        mock_response_ip.text = "103.21.244.5"

        with patch("app.api.admin_routes.cffi_requests.get") as mock_get:
            def side_effect(url, **kwargs):
                if "chat.deepseek.com" in url:
                    return mock_response_probe
                elif "api.ipify.org" in url:
                    return mock_response_ip
                return mock_response_probe

            mock_get.side_effect = side_effect

            # 配置模拟代理
            with patch.object(config_manager.config, "proxies", ["http://127.0.0.1:7891", "http://127.0.0.1:7892"]):
                with patch.object(config_manager.config, "proxy", "http://127.0.0.1:7890"):
                    res = asyncio.run(get_proxies_status())
                    self.assertTrue(res["ok"])
                    self.assertEqual(len(res["proxies"]), 3)
                    p0 = res["proxies"][0]
                    self.assertTrue(p0["online"])
                    self.assertEqual(p0["status_code"], 202)
                    self.assertEqual(p0["egress_ip"], "103.21.244.5")
                    self.assertIn("latency_ms", p0)

    def test_get_system_logs_with_date_and_level(self):
        """测试按指定日期与错误级别查询日志"""
        res = asyncio.run(get_system_logs(service="ds-gateway", date="2026-10-10", level="error", lines=100))
        self.assertTrue(res["ok"])
        self.assertEqual(res["date"], "2026-10-10")
        self.assertEqual(res["level"], "error")
        self.assertIn("since", res)
        self.assertIn("until", res)
        self.assertIsInstance(res["lines"], list)

    def test_export_system_logs_endpoint(self):
        """测试按日期与服务一键导出日志下载"""
        resp = asyncio.run(export_system_logs(service="ds-gateway", date="2026-10-10", filter="error"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Content-Disposition", resp.headers)
        self.assertIn("ds-gateway-2026-10-10.log", resp.headers["Content-Disposition"])
        self.assertEqual(resp.media_type, "text/plain; charset=utf-8")
        self.assertIn("X-Total-Lines", resp.headers)
        self.assertIsInstance(resp.body, bytes)
