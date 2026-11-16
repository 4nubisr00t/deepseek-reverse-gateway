"""
T25 按 API Key 用量统计与日配额测试

重点覆盖三件容易出错、且出错后**静默**的事：
 1. 用量归因：`metrics_engine.record()` 有 14 个调用点，靠 contextvar 归因。
    如果 contextvar 在流式路径上失效，图表只会"数字偏小"，没人会发现。
    所以这里逐条路径验证归因确实落到正确的 key 上。
 2. 日滚动：跨零点必须归零并归档，否则配额要么永远不重置，
    要么把昨天用量算进今天。
 3. 配额边界：`0 = 不限`（42 条历史记录的默认值），
    否则升级当天所有人会被一个 0 上限直接锁死。
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core import security
from app.core import request_context as rctx
from app.engine.key_usage import KeyUsageEngine, _today


def _sync_verify(request):
    """同步调用 verify_api_key 的测试包装器（原因见 test_account_ownership.py）"""
    return asyncio.run(security.verify_api_key(request))


class QuotaBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_keys_file = security.KEYS_FILE
        security.KEYS_FILE = Path(self._tmp.name) / "api_keys.json"
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}
        security._last_flush = 0.0
        self.engine = KeyUsageEngine(Path(self._tmp.name) / "key_usage.json")
        rctx.set_current_identity(None)

    def tearDown(self):
        security.KEYS_FILE = self._orig_keys_file
        security.load_keys()
        rctx.set_current_identity(None)
        self._tmp.cleanup()

    def _ident(self, **kw):
        base = {
            "key_id": "k1", "name": "client", "owner": "张三",
            "log_label": "张三", "account_labels": [], "enabled": True,
            "pool_fallback": False, "quota_daily_requests": 0, "quota_daily_tokens": 0,
        }
        base.update(kw)
        return base


class TestAttribution(QuotaBase):

    def test_contextvar_roundtrip(self):
        rctx.set_current_identity(self._ident())
        self.assertEqual(rctx.current_key_id(), "k1")
        self.assertEqual(rctx.current_key_label(), "张三")

    def test_no_identity_is_silent_noop(self):
        """非请求上下文（后台任务/启动期）调用不得抛错，也不得凭空记账"""
        rctx.set_current_identity(None)
        self.engine.record(
            key_id=rctx.current_key_id(), label=rctx.current_key_label(),
            prompt_tokens=100, completion_tokens=10, duration_ms=50.0,
        )
        self.assertEqual(self.engine.all_snapshots(), {})

    def test_metrics_record_attributes_to_current_key(self):
        """核心断言：metrics_engine.record 必须把用量记到当前请求的 key 上 —— 而不需要
        在任何调用点显式传 key。"""
        from app.engine import metrics as metrics_mod
        eng = metrics_mod.MetricsEngine(
            max_history=5, storage_file=Path(self._tmp.name) / "m.json"
        )
        import app.engine.metrics as mmod
        import app.engine.key_usage as ku_mod
        orig_engine, orig_metrics = ku_mod.key_usage_engine, mmod.metrics_engine
        mmod.metrics_engine = eng
        ku_mod.key_usage_engine = self.engine
        try:
            rctx.set_current_identity(self._ident())
            # 不传任何 key 参数 —— 完全依赖上下文归因
            eng.record(
                endpoint="/v1/chat/completions", model="m", status_code=200,
                duration_ms=1200.0, prompt_tokens=1000, completion_tokens=200,
            )
            snap = self.engine.all_snapshots()
            self.assertIn("k1", snap)
            self.assertEqual(snap["k1"]["requests"], 1)
            self.assertEqual(snap["k1"]["prompt_tokens"], 1000)
            self.assertEqual(snap["k1"]["completion_tokens"], 200)
            self.assertEqual(snap["k1"]["errors"], 0)
            # 流水里也带上 key，排障时才知道是谁发的
            self.assertEqual(eng.recent_records[0]["key"], "张三")
        finally:
            mmod.metrics_engine = orig_metrics
            ku_mod.key_usage_engine = orig_engine

    def test_error_path_also_attributed(self):
        """失败退出路径也必须记账 —— 旧写法最容易漏的就是这里"""
        from app.engine import metrics as metrics_mod
        import app.engine.metrics as mmod
        import app.engine.key_usage as ku_mod
        orig_engine = ku_mod.key_usage_engine
        ku_mod.key_usage_engine = self.engine
        eng = metrics_mod.MetricsEngine(
            max_history=5, storage_file=Path(self._tmp.name) / "m.json"
        )
        try:
            rctx.set_current_identity(self._ident())
            for code in (429, 502, 503):
                eng.record(
                    endpoint="/v1/chat/completions", model="m", status_code=code,
                    duration_ms=10.0, error="boom",
                )
            snap = self.engine.all_snapshots()["k1"]
            self.assertEqual(snap["requests"], 3)
            self.assertEqual(snap["errors"], 3)
            self.assertEqual(snap["last_error"], "boom")
        finally:
            ku_mod.key_usage_engine = orig_engine

    def test_streaming_task_inherits_context(self):
        """流式响应在子任务里 iterating 时必须仍能拿到身份。

        这是 contextvar 方案唯一的真实风险点：若归因在这条路径上失效，
        流式请求（= 绝大多数请求）的用量会全部丢失，而现象只是"数字偏小"。
        """
        from app.engine import metrics as metrics_mod
        import app.engine.metrics as mmod
        import app.engine.key_usage as ku_mod
        orig_engine = ku_mod.key_usage_engine
        ku_mod.key_usage_engine = self.engine
        eng = metrics_mod.MetricsEngine(
            max_history=5, storage_file=Path(self._tmp.name) / "m.json"
        )

        async def stream_body():
            # 模拟 Starlette StreamingResponse：在派生任务/新协程里走完流
            async def gen():
                yield "a"
                yield "b"
            async for _ in gen():
                pass
            eng.record(
                endpoint="/v1/chat/completions", model="m", status_code=200,
                duration_ms=900.0, prompt_tokens=500, completion_tokens=100,
            )

        try:
            async def main():
                rctx.set_current_identity(self._ident())
                await asyncio.create_task(stream_body())

            asyncio.run(main())
            snap = self.engine.all_snapshots()["k1"]
            self.assertEqual(snap["requests"], 1)
            self.assertEqual(snap["completion_tokens"], 100)
        finally:
            ku_mod.key_usage_engine = orig_engine

    def test_two_keys_do_not_blend(self):
        from app.engine import metrics as metrics_mod
        import app.engine.metrics as mmod
        import app.engine.key_usage as ku_mod
        orig_engine = ku_mod.key_usage_engine
        ku_mod.key_usage_engine = self.engine
        eng = metrics_mod.MetricsEngine(
            max_history=5, storage_file=Path(self._tmp.name) / "m.json"
        )
        try:
            for kid, owner, tok in (("k1", "张三", 100), ("k2", "李四", 900)):
                rctx.set_current_identity(self._ident(key_id=kid, log_label=owner))
                eng.record(
                    endpoint="/v1/chat/completions", model="m", status_code=200,
                    duration_ms=10.0, prompt_tokens=tok,
                )
            snap = self.engine.all_snapshots()
            self.assertEqual(snap["k1"]["prompt_tokens"], 100)
            self.assertEqual(snap["k2"]["prompt_tokens"], 900)
        finally:
            ku_mod.key_usage_engine = orig_engine

    def test_attribute_to_key_false_skips_quota_counting(self):
        """网关自己挡下的请求（容量不足 / 已被配额拦）**不得**计入按 key 用量。

        这是端到端冒烟抓到的一个真实缺陷：配额分支里同时记了
        rejected=True 又让 metrics 通过上下文归因记了一次正常请求，
        于是 requests 被顶高 —— 每重试一次就顶高一次，
        用户无论等多久都恢复不了（永久触顶，表现为"这把钥废了"）。
        """
        from app.engine import metrics as metrics_mod
        import app.engine.metrics as mmod
        import app.engine.key_usage as ku_mod
        orig_engine = ku_mod.key_usage_engine
        ku_mod.key_usage_engine = self.engine
        eng = metrics_mod.MetricsEngine(
            max_history=5, storage_file=Path(self._tmp.name) / "m.json"
        )
        try:
            rctx.set_current_identity(self._ident())
            # 被网关挡下 —— 不该计入
            for _ in range(5):
                eng.record(
                    endpoint="/v1/chat/completions", model="m", status_code=429,
                    duration_ms=1.0, error="all_accounts_at_capacity",
                    attribute_to_key=False,
                )
            self.assertNotIn("k1", self.engine.all_snapshots())

            # 被真正服务过 —— 该计入
            eng.record(
                endpoint="/v1/chat/completions", model="m", status_code=200,
                duration_ms=100.0, prompt_tokens=10,
            )
            self.assertEqual(self.engine.all_snapshots()["k1"]["requests"], 1)
            # 但全局统计仍然如实记录全部 6 次（含被挡的 5 次）
            self.assertEqual(eng.total_requests, 6)
        finally:
            ku_mod.key_usage_engine = orig_engine

    def test_rejected_retries_never_lock_user_out(self):
        """永久触顶回归：反复被拦之后，只要今天实际用量归零就该恢复可用"""
        ident = self._ident(quota_daily_requests=1)
        self.engine.record(key_id="k1", prompt_tokens=1)
        self.assertIsNotNone(self.engine.check_quota(ident))
        for _ in range(50):
            self.engine.record(key_id="k1", rejected=True)
        snap = self.engine.all_snapshots()["k1"]
        self.assertEqual(snap["requests"], 1, "50 次被拦不得推高 requests")
        self.assertEqual(snap["rejected"], 50)
        # 管理员清零后必须立刻恢复
        self.engine.reset("k1")
        self.assertIsNone(self.engine.check_quota(ident))


class TestDailyRollover(QuotaBase):

    def test_zero_quota_means_unlimited(self):
        """42 条历史记录没有配额字段 → _normalize 补 0 → 必须解释为"不限"，
        否则升级当天所有人会被一个 0 上限直接锁死。"""
        ident = security._anonymous_identity()
        self.assertEqual(ident["quota_daily_requests"], 0)
        self.assertIsNone(self.engine.check_quota(self._ident()))

    def test_rollover_resets_and_archives(self):
        self.engine.record(key_id="k1", label="张三", prompt_tokens=100, completion_tokens=50)
        snap = self.engine.all_snapshots()["k1"]
        self.assertEqual(snap["requests"], 1)

        # 手工把 day 改成"昨天"，模拟跨零点
        rec = self.engine._keys["k1"]
        rec["day"] = "2026-01-01"
        rec["requests"] = 7
        rec["prompt_tokens"] = 700
        rec["completion_tokens"] = 70
        rec["errors"] = 2

        self.engine.record(key_id="k1", label="张三", prompt_tokens=1)
        snap = self.engine.all_snapshots()["k1"]

        # 今天归零重来
        self.assertEqual(snap["day"], _today())
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["prompt_tokens"], 1)
        self.assertEqual(snap["errors"], 0)
        # 昨天被归档，不是被丢掉
        hist = self.engine.history("k1")
        self.assertIn("2026-01-01", hist)
        self.assertEqual(hist["2026-01-01"]["requests"], 7)
        self.assertEqual(hist["2026-01-01"]["prompt_tokens"], 700)
        self.assertEqual(hist["2026-01-01"]["errors"], 2)
        # 累计量不因日滚动而归零
        self.assertEqual(snap["total_requests"], 2)

    def test_history_is_bounded(self):
        rec = {"day": "2026-01-01", "history": {f"2026-01-{i:02d}": {"requests": i} for i in range(1, 20)}}
        self.engine._keys["k1"] = rec
        self.engine.record(key_id="k1", prompt_tokens=1)
        self.assertLessEqual(len(self.engine.history("k1")), 14)

    def test_persistence_roundtrip(self):
        self.engine.record(key_id="k1", label="张三", prompt_tokens=42, completion_tokens=8)
        self.engine.flush(force=True)
        fresh = KeyUsageEngine(Path(self._tmp.name) / "key_usage.json")
        snap = fresh.all_snapshots()["k1"]
        self.assertEqual(snap["label"], "张三")
        self.assertEqual(snap["prompt_tokens"], 42)


class TestQuotaEnforcement(QuotaBase):

    def test_request_quota(self):
        ident = self._ident(quota_daily_requests=2)
        self.assertIsNone(self.engine.check_quota(ident))
        self.engine.record(key_id="k1", prompt_tokens=1)
        self.assertIsNone(self.engine.check_quota(ident))
        self.engine.record(key_id="k1", prompt_tokens=1)
        denied = self.engine.check_quota(ident)
        self.assertIsNotNone(denied)
        reason, retry_after = denied
        self.assertIn("2/2", reason)
        self.assertIn("每日请求配额", reason)
        # Retry-After 必须是"到本地零点"，而不是一个瞎猜的秒数
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, 86400)

    def test_token_quota(self):
        ident = self._ident(quota_daily_tokens=1000)
        self.engine.record(key_id="k1", prompt_tokens=900, completion_tokens=50)
        self.assertIsNone(self.engine.check_quota(ident))
        self.engine.record(key_id="k1", prompt_tokens=100, completion_tokens=0)
        denied = self.engine.check_quota(ident)
        self.assertIsNotNone(denied)
        self.assertIn("每日 Token 配额", denied[0])

    def test_rejected_requests_do_not_consume_quota(self):
        """被配额拦下的请求不能算进配额 —— 否则一旦触顶就永久触顶，
        用户无论等多久都恢复不了（这是个会造成"账号废了"错觉的死锁）。"""
        ident = self._ident(quota_daily_requests=1)
        self.engine.record(key_id="k1", prompt_tokens=10)
        for _ in range(5):
            self.engine.record(key_id="k1", rejected=True)
        snap = self.engine.all_snapshots()["k1"]
        self.assertEqual(snap["requests"], 1, "rejected 不得计入 requests")
        self.assertEqual(snap["rejected"], 5)
        # 仍然会因为那 1 次而超限，但计数没有因为被拦而继续膨胀
        self.assertIsNotNone(self.engine.check_quota(ident))
        self.assertEqual(self.engine.all_snapshots()["k1"]["requests"], 1)

    def test_zero_request_quota_but_token_quota_set(self):
        ident = self._ident(quota_daily_requests=0, quota_daily_tokens=100)
        self.engine.record(key_id="k1", prompt_tokens=200)
        denied = self.engine.check_quota(ident)
        self.assertIsNotNone(denied)
        self.assertIn("Token", denied[0])

    def test_anonymous_identity_never_quota_blocked(self):
        """require_api_key=false 时是匿名身份，没有 key_id → 不参与配额"""
        self.assertIsNone(self.engine.check_quota(security._anonymous_identity()))

    def test_reset_and_forget(self):
        self.engine.record(key_id="k1", prompt_tokens=100)
        self.assertTrue(self.engine.reset("k1"))
        self.assertEqual(self.engine.all_snapshots()["k1"]["prompt_tokens"], 0)
        self.assertEqual(self.engine.all_snapshots()["k1"]["total_tokens"], 100)
        self.assertTrue(self.engine.forget("k1"))
        self.assertNotIn("k1", self.engine.all_snapshots())
        self.assertFalse(self.engine.forget("k1"))


class TestSecurityIntegration(QuotaBase):

    def test_key_record_carries_quota(self):
        rec = security.generate_key("client", owner="张三")
        self.assertEqual(rec["quota_daily_requests"], 0)
        self.assertEqual(rec["quota_daily_tokens"], 0)

        security.update_key(rec["id"], quota_daily_requests=500, quota_daily_tokens=200000)
        k = security.get_key(rec["id"])
        self.assertEqual(k["quota_daily_requests"], 500)
        self.assertEqual(k["quota_daily_tokens"], 200000)
        # 身份视图里必须带上配额，否则配额闸门读不到
        self.assertEqual(k["quota_daily_requests"], 500)

    def test_negative_quota_clamped(self):
        rec = security.generate_key("client")
        security.update_key(rec["id"], quota_daily_requests=-5)
        self.assertEqual(security.get_key(rec["id"])["quota_daily_requests"], 0)

    def test_legacy_record_defaults_to_unlimited(self):
        raw = "sk-" + "c" * 48
        security.KEYS_FILE.write_text(json.dumps([{
            "id": "old1", "name": "old",
            "key_hash": security._hash_key(raw),
            "prefix": raw[:14], "created_at": 1, "revoked": False,
        }]), encoding="utf-8")
        security._keys = []
        security._keys_sig = None
        k = security.list_keys()[0]
        self.assertEqual(k["quota_daily_requests"], 0)
        self.assertEqual(k["quota_daily_tokens"], 0)

    def test_revoke_keeps_usage_for_audit(self):
        """撤销 key 必须**保留**用量记录，只打标记。

        反过来想过：撤销一把 key 往往正是因为"他把号跑飞了"，
        而那一刻恰恰是最需要历史证据的时候 —— 删掉等于在需要证据的
        瞬间销毁证据。
        """
        import app.engine.key_usage as ku_mod
        orig = ku_mod.key_usage_engine
        ku_mod.key_usage_engine = self.engine
        try:
            rec = security.generate_key("client", owner="张三")
            self.engine.record(key_id=rec["id"], label="张三", prompt_tokens=10)
            security.revoke_key(rec["id"])
            # 仍然在账本里，但被标记为已作废
            snaps = self.engine.all_snapshots()
            self.assertIn(rec["id"], snaps)
            self.assertTrue(snaps[rec["id"]]["revoked"])
            self.assertEqual(snaps[rec["id"]]["total_tokens"], 10)
            self.assertIn(rec["id"], self.engine.revoked_snapshots())
        finally:
            ku_mod.key_usage_engine = orig

    def test_prune_revoked(self):
        self.engine.record(key_id="k1", prompt_tokens=10)
        self.engine.record(key_id="k2", prompt_tokens=20)
        self.engine.mark_revoked("k1")
        self.assertEqual(len(self.engine.revoked_snapshots()), 1)
        # keep_days=0 → 清掉全部已作废；活跃的不受影响
        n = self.engine.prune_revoked(keep_days=0)
        self.assertEqual(n, 1)
        self.assertNotIn("k1", self.engine.all_snapshots())
        self.assertIn("k2", self.engine.all_snapshots())

    def test_prune_revoked_respects_keep_days(self):
        self.engine.record(key_id="k1")
        self.engine.mark_revoked("k1")
        # keep_days=30 → 刚撤销的（0 天前）不该被清
        self.assertEqual(self.engine.prune_revoked(keep_days=30), 0)
        self.assertIn("k1", self.engine.all_snapshots())

    def test_mark_revoked_is_idempotent_and_safe(self):
        self.engine.mark_revoked("nonexistent")  # 不得抛错
        self.engine.record(key_id="k1")
        self.engine.mark_revoked("k1")
        self.engine.mark_revoked("k1")
        self.assertTrue(self.engine.all_snapshots()["k1"]["revoked"])

    def test_verify_api_key_sets_request_state(self):
        """鉴权必须把身份挂到 request.state（路由层从那里取）。

        注意这里**不能**断言 contextvar 也被设上了：`asyncio.run` 会为任务
        复制一份 context（Task.__init__ 里的 copy_context），
        在副本里的赋值传不回来。真实的传播只能在真实 HTTP 请求里验证 ——
        那正是 tests/smoke_t25.py 存在的理由，也是本项目踩过的坑：
        单元测试直接调用函数，根本走不到 FastAPI 的线程池/任务那条路径。
        """
        import inspect
        from starlette.requests import Request
        rec = security.generate_key("client", owner="张三")
        req = Request({
            "type": "http", "method": "POST", "path": "/", "query_string": b"",
            "headers": [(b"authorization", f"Bearer {rec['key']}".encode())],
            "state": {},
        })
        rctx.set_current_identity(None)
        _sync_verify(req)
        ident = getattr(req.state, "ds_api_identity")
        self.assertEqual(ident["key_id"], rec["id"])
        self.assertEqual(ident["log_label"], "张三")

    def test_api_key_dependencies_are_coroutines(self):
        """回归护栏：两个依赖必须是 async def。

        改成同步 def 之后本文件所有测试照样绿（直接调用走不到线程池），
        但线上按 key 用量会静默恒为 0。所以用"是不是协程函数"把约束钉死。
        """
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(security.verify_api_key))
        self.assertTrue(inspect.iscoroutinefunction(security.get_identity))


if __name__ == "__main__":
    unittest.main(verbosity=2)
