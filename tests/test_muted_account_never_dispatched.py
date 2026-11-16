"""
"已禁言的号不许发出去" —— 修掉一次实测抓到的**状态自相矛盾**。

────────────────────────────────────────────────────────────────────
实测记录（2026-09-12，线上账号 `4ot3ovrdcr`）
────────────────────────────────────────────────────────────────────
    is_valid=True      ← token 有效（探活拿到的真是它）
    status="ACTIVE"    ← 没人改过它
    mute_until=17:39:39（未来 6 小时后）← 上游明确说"禁言到那时"

三个字段是**分别写入**的，于是留下了这条谁也看不懂的记录。而号池的可用判据
旧版只看前两个字段 → **判定可用 → 把已禁言的号发给客户端 → 上游回 biz_code:5
→ 502**。用户看到的是"这把 key 失效了"，实际是号池在发死号
（他就是因此把 `agent-client` 作废的）。

它是「一键探活」写出来的，两个独立缺陷叠加：

  ① `admin_routes.check_accounts` 只写 `is_valid` 与 `mute_until`，**没写 `status`**；
     而 `probe_single_account` 的 `is_valid` 语义是"token 有效"而非"可用"。
  ② `probe_single_account` 把上游的 `mute_until` **原样**写进 config.json。
     那个字段可能是秒 / 毫秒 / "还要等多少秒"，`_parse_mute_until` 就是为此存在的，
     但这条路径没用它 —— 于是同一个字段在项目里出现两种单位。

修法是两层，缺一不可（本文件两条都钉）：

  · **生产者**：探活把三个字段写成自洽的一组，并把 mute_until 规整成 unix 秒。
  · **判据入口**：`is_usable_state` 把"停用期未到"并进可用判据 ——
    这样**任何**字段组合都不再可能把停用期内的号发出去，而不是只修某一个写入点。
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import config_manager
from app.core import security
from app.engine.account_pool import AccountPool, PERMANENT_INVALID_SECONDS
from app.engine.deepseek_client import _parse_mute_until


def _acct(label, **kw):
    d = {"account_label": label, "token": "tok-" + label, "cookie": "",
         "is_valid": True, "status": "ACTIVE"}
    d.update(kw)
    return d


class PoolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_keys_file = security.KEYS_FILE
        security.KEYS_FILE = Path(self._tmp.name) / "api_keys.json"
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}
        security._pending_touch = {}
        security._loaded = False
        security._dirty = False
        self._orig_accounts = config_manager.accounts
        self._orig_replenish = config_manager.config.auto_replenish
        config_manager.config.auto_replenish = False
        # 单账号，便于断言"这一个号有没有被发出去"
        config_manager.accounts = [_acct("a@x")]

    def tearDown(self):
        config_manager.config.auto_replenish = self._orig_replenish
        config_manager.accounts = self._orig_accounts
        security.KEYS_FILE = self._orig_keys_file
        security.load_keys()
        self._tmp.cleanup()

    def pool_with_inconsistent_record(self, label="a@x"):
        """复刻线上那条记录：valid + ACTIVE + 停用期在未来。"""
        config_manager.accounts = [
            _acct(label, mute_until=int(time.time()) + 3600)
        ]
        return AccountPool()


class TestTheInconsistentRecordIsNeverDispatched(PoolBase):

    def test_predicates_reject_it(self):
        p = self.pool_with_inconsistent_record()
        acc = p._accounts["a@x"]
        self.assertTrue(acc.is_valid, "前置条件：token 是有效的")
        self.assertEqual(acc.status, "ACTIVE", "前置条件：状态没被改过")
        self.assertTrue(acc.is_muted_now(), "停用期在未来 → 判定为'仍在禁言'")
        self.assertFalse(acc.is_usable_state,
                         "三个字段自相矛盾时，必须以'不能用'为准（保守）")
        self.assertFalse(acc.is_available_for_api,
                         "旧判据只看 is_valid + status → 这里会返回 True，进而发出死号")

    def test_acquire_account_refuses_it(self):
        """★ 核心：池子里只有这一个号时，宁可不给，也不能把死号发出去。"""
        p = self.pool_with_inconsistent_record()

        async def run():
            return await p.acquire_account(immediate=True)

        self.assertIsNone(asyncio.run(run()),
                          "已禁言的号被发了出去 —— 客户端会拿到 502")

    def test_sticky_path_refuses_it_too(self):
        """粘性路径也必须拒绝：一个禁言中的粘性号，等多久都拿不到答案。"""
        p = self.pool_with_inconsistent_record()

        async def run():
            return await p.acquire_account(preferred_label="a@x", immediate=True)

        self.assertIsNone(asyncio.run(run()),
                          "粘性路径绕过了停用期检查 —— 会话会一直卡在这个号上")

    def test_usable_again_once_the_mute_expires(self):
        """解禁时间一过，必须恢复正常（不能修成"永久冷藏"）。"""
        config_manager.accounts = [_acct("a@x", mute_until=int(time.time()) - 1)]
        p = AccountPool()
        acc = p._accounts["a@x"]
        self.assertFalse(acc.is_muted_now())
        self.assertTrue(acc.is_usable_state)

        async def run():
            return await p.acquire_account(immediate=True)

        self.assertIsNotNone(asyncio.run(run()))

    def test_expired_stale_mute_until_is_cleared(self):
        """stale 的 mute_until 到期必须被清掉。

        旧 `check_mute_expiry` 的条件是 `status == "MUTED" or (not is_valid and mute_until)`
        —— 对这条 ACTIVE+valid 的记录**恒不成立**，于是那个字段永远不会被清，
        管理页会一直显示"已被官方禁言"，即使解禁时间早就过了。
        """
        config_manager.accounts = [_acct("a@x", mute_until=int(time.time()) - 5)]
        p = AccountPool()
        acc = p._accounts["a@x"]
        acc.check_mute_expiry()
        self.assertIsNone(acc.mute_until, "到期的停用标记没被清掉")
        self.assertTrue(acc.is_valid)
        self.assertEqual(acc.status, "ACTIVE")

    def test_permanent_invalid_still_wakes_after_its_period(self):
        """原行为不能被我改坏：永久失效号在超长停用期后仍会恢复（代码里有明确打印）。"""
        p = AccountPool()
        acc = p._accounts["a@x"]
        p.release_account(acc, permanent=True)
        self.assertEqual(acc.status, "INVALID")
        self.assertIsNotNone(acc.mute_until)
        self.assertGreater(acc.mute_until, int(time.time()) + PERMANENT_INVALID_SECONDS - 60)

    def test_replenish_counts_only_usable_accounts(self):
        """补号水位线必须按真实可用算。"""
        config_manager.config.auto_replenish = True
        config_manager.config.min_active_accounts = 3
        config_manager.accounts = [
            _acct("a@x", mute_until=int(time.time()) + 3600),
            _acct("b@x", mute_until=int(time.time()) + 3600),
            _acct("c@x", mute_until=int(time.time()) + 3600),
        ]
        p = AccountPool()

        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p._check_replenish()

        out_text = buf.getvalue()
        self.assertIn("活跃可用账号 (0) 低于阈值 (3)", out_text, "3 个号全在禁言中，水位线应判定可用数为 0")


class TestHealthReportsTheTruth(PoolBase):
    """`/health` 的 `valid` 必须是"真实可用"，不是"本地字段写着有效"。

    实测偏差（2026-09-12）：`/health` 报 `valid=20 / muted=5`，真相是 `18 / 7` ——
    有 2 个**已被上游禁言**的号因为被某个 key 独占（不进公共池 → 不被探活）
    而一直显示为有效。运维按 20 规划容量，必然不断失败。
    """

    def test_valid_excludes_a_muted_but_locally_active_account(self):
        from app.api import admin_routes

        config_manager.accounts = [
            _acct("good@x"),
            _acct("muted@x", mute_until=int(time.time()) + 3600),
        ]
        h = asyncio.run(admin_routes.health_check())
        self.assertEqual(h["accounts"], 2)
        self.assertEqual(h["valid"], 1,
                         "已禁言的号被算成有效 —— 这就是实测那个 20 vs 18 的偏差")
        self.assertEqual(h["muted"], 1)

    def test_after_the_mute_expires_it_counts_again(self):
        from app.api import admin_routes

        config_manager.accounts = [_acct("a@x", mute_until=int(time.time()) - 1)]
        h = asyncio.run(admin_routes.health_check())
        self.assertEqual(h["valid"], 1, "解禁后必须重新计入可用")
        self.assertEqual(h["muted"], 0)

    def test_per_account_view_exposes_usable(self):
        from app.api import admin_routes

        config_manager.accounts = [_acct("a@x", mute_until=int(time.time()) + 3600)]
        out = asyncio.run(admin_routes.accounts_health())
        row = (out["results"] if isinstance(out, dict) else out)[0]
        self.assertFalse(row["usable"],
                         "逐账号视图必须给出 usable —— 只看 is_valid 会得出相反结论")
        self.assertTrue(row["muted_now"])


class TestTheProducerWritesAConsistentRecord(PoolBase):
    """生产者侧：探活不能再写出自相矛盾的记录。"""

    def _run_check(self, probe_result, account="a@x"):
        from app.api import admin_routes
        config_manager.accounts = [_acct(account)]
        orig = admin_routes.probe_single_account
        admin_routes.probe_single_account = lambda acc, proxy: dict(probe_result)
        try:
            asyncio.run(admin_routes.check_accounts(
                admin_routes.CheckAccountRequest(account=account)))
        finally:
            admin_routes.probe_single_account = orig
        return config_manager.accounts[0]

    def test_muted_probe_records_unusable_state(self):
        """探到一个禁言号 → 必须写成 is_valid=False + status=MUTED（三字段自洽）。"""
        rec = self._run_check({
            "ok": True, "is_valid": True, "is_muted": True,
            "mute_until": int(time.time()) + 3600, "msg": "已被官方禁言",
        })
        self.assertFalse(rec["is_valid"],
                         "探到禁言却仍记 is_valid=True —— 号池会把它发出去")
        self.assertEqual(rec["status"], "MUTED",
                         "没写 status 就是实测那条自相矛盾记录的来源")
        self.assertGreater(rec["mute_until"], time.time())

    def test_healthy_probe_records_active(self):
        rec = self._run_check({
            "ok": True, "is_valid": True, "is_muted": False,
            "mute_until": None, "msg": "正常可用",
        })
        self.assertTrue(rec["is_valid"])
        self.assertEqual(rec["status"], "ACTIVE")
        self.assertIsNone(rec["mute_until"])

    def test_dead_token_records_invalid(self):
        rec = self._run_check({
            "ok": True, "is_valid": False, "is_muted": False,
            "mute_until": None, "msg": "Token 已过期失效",
        })
        self.assertFalse(rec["is_valid"])
        self.assertEqual(rec["status"], "INVALID",
                         "Token 已过期失效时不得再覆写回 ACTIVE")


class TestProbeNormalizesMuteUntil(PoolBase):
    """② 上游的 mute_until 可能是秒/毫秒/相对秒数，必须规整成 unix 秒。

    同一个字段在项目里出现两种单位的后果：
      · 毫秒被当成秒 → ~1.7e12 的"解禁时间" → 号被**永久冷藏**
      · 相对秒数被当成绝对时间 → 64011（远小于 now）→ 被判定"已解禁"
        → **把禁言中的号发出去**（就是本次要修的故障，换一条路又回来了）

    ⚠️ 这里必须连 HTTP 层一起假掉，**不能**替换 `probe_single_account` ——
    规整逻辑就在它内部，替换掉整个函数等于把被测代码换掉了。
    （第一版就是这么写的，两条用例"通过"了但其实什么都没测：
    它验证的只是"check_accounts 会把探针返回值原样写进配置"。）
    """

    def _stored_mute_until(self, raw_upstream_value):
        from app.api import admin_routes

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "code": 0,
                    "data": {"biz_data": {
                        "email": "a@x",
                        "chat": {"is_muted": 1, "mute_until": raw_upstream_value},
                    }},
                }

        class FakeRequests:
            @staticmethod
            def get(*a, **kw):
                return FakeResp()

        config_manager.accounts = [_acct("a@x")]
        orig = admin_routes.cffi_requests
        admin_routes.cffi_requests = FakeRequests
        try:
            asyncio.run(admin_routes.check_accounts(
                admin_routes.CheckAccountRequest(account="a@x")))
        finally:
            admin_routes.cffi_requests = orig
        return config_manager.accounts[0]["mute_until"]

    def test_milliseconds_are_converted_to_seconds(self):
        want = int(time.time()) + 3600
        got = self._stored_mute_until(want * 1000)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, want, delta=5,
                               msg="毫秒没被换算 —— 号会被永久冷藏")

    def test_relative_seconds_are_converted_to_absolute(self):
        got = self._stored_mute_until(3600)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, int(time.time()) + 3600, delta=5,
                               msg="相对秒数被当成绝对时间 —— 号会被判定'已解禁'后发出去")

    def test_seconds_pass_through(self):
        want = int(time.time()) + 1800
        self.assertAlmostEqual(self._stored_mute_until(want), want, delta=5)

    def test_probe_result_also_reports_the_normalized_value(self):
        """探针**返回给管理页**的那份也要是规整过的秒。

        否则接口显示的和落盘的不是同一个数，排查时两边对不上。
        """
        from app.api import admin_routes

        class FakeResp:
            status_code = 200

            def json(self):
                return {"code": 0, "data": {"biz_data": {
                    "email": "a@x",
                    "chat": {"is_muted": 1,
                             "mute_until": (int(time.time()) + 3600) * 1000},
                }}}

        class FakeRequests:
            @staticmethod
            def get(*a, **kw):
                return FakeResp()

        config_manager.accounts = [_acct("a@x")]
        orig = admin_routes.cffi_requests
        admin_routes.cffi_requests = FakeRequests
        try:
            out = asyncio.run(admin_routes.check_accounts(
                admin_routes.CheckAccountRequest(account="a@x")))
        finally:
            admin_routes.cffi_requests = orig
        mu = out["results"][0]["mute_until"]
        self.assertAlmostEqual(mu, int(time.time()) + 3600, delta=5,
                               msg="接口返回的仍是原始毫秒值")

    def test_unparseable_becomes_none_not_a_guess(self):
        """拿不准就返回 None —— 绝不猜一个时间点。

        猜错的代价是把还在禁言的号放回池子（继续失败），
        而 None 会让退避阶梯兜底。
        """
        for raw in (None, 0, -5, "abc", int(time.time()) - 100,
                    int(time.time()) + 30 * 86400):
            with self.subTest(raw=raw):
                self.assertIsNone(_parse_mute_until(raw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
