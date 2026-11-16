"""
T12 账号池调度层测试：熔断/解禁、退避阶梯、并发调度、冷却估算。

本文件的核心是**一个曾经真实存在的缺陷**：

    旧实现里路由调用 `release_account(account, is_muted=True)`，
    `mute_until` 走默认值 `None`；而 `check_mute_expiry` 的唤醒条件是
    `if self.mute_until and now >= self.mute_until` —— 要求它**非空**。
    两者对不上，于是账号一旦被官方禁言（biz_code:5）就**永久退出池子**，
    而 `AccountPool` 的 docstring 却写着"到期自动静默唤醒"。

多用户长期运行下，这表现为"号池悄悄缩水直到某天没人可用"，且全程无告警。
所以这里既测"能解禁"，也测"退避是有限的、会上限、会清零"。
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

from app.config import config_manager
from app.core import security
from app.engine.account_pool import (
    AccountPool,
    AccountRuntime,
    MAX_CONCURRENT_PER_ACCOUNT,
    MUTE_BACKOFF_LADDER,
    PERMANENT_INVALID_SECONDS,
)
from app.engine.deepseek_client import ChatEvent, _parse_mute_until


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
        config_manager.accounts = [_acct("a@x"), _acct("b@x"), _acct("c@x")]
        # ⚠️ 测试里必须关掉弹性补位：`_check_replenish` 在有效账号低于
        # min_active_accounts 时会**真的去批量注册新账号**（走 mail.cx 网络调用）。
        # 每次 release_account 都可能触发它 —— 这会让单测跑 60 秒并产生外部副作用。
        self._orig_replenish = config_manager.config.auto_replenish
        config_manager.config.auto_replenish = False

    def tearDown(self):
        config_manager.config.auto_replenish = self._orig_replenish
        security.KEYS_FILE = self._orig_keys_file
        security.load_keys()
        config_manager.accounts = self._orig_accounts
        self._tmp.cleanup()

    def pool(self):
        return AccountPool()


class TestMuteHasRealExpiry(PoolBase):
    """★ 核心：熔断必须有真实的解禁时间，否则账号永远回不来"""

    def test_muting_sets_a_real_mute_until(self):
        p = self.pool()
        acc = p._accounts["a@x"]
        before = int(time.time())
        p.release_account(acc, is_muted=True, mute_until=None)
        self.assertFalse(acc.is_valid)
        self.assertEqual(acc.status, "MUTED")
        self.assertIsNotNone(acc.mute_until, "熔断后必须有一个真实的解禁时间")
        self.assertGreater(acc.mute_until, before)

    def test_muted_account_actually_wakes_up(self):
        """把解禁时间拨到过去，必须真的自动恢复 ACTIVE"""
        p = self.pool()
        acc = p._accounts["a@x"]
        p.release_account(acc, is_muted=True)
        self.assertFalse(acc.is_valid)

        acc.mute_until = int(time.time()) - 1     # 时间到
        acc.check_mute_expiry()
        self.assertTrue(acc.is_valid, "到期后必须自动唤醒（旧实现这里恒为 False）")
        self.assertEqual(acc.status, "ACTIVE")
        self.assertIsNone(acc.mute_until)

        # 唤醒后必须能重新被调度
        async def run():
            got = await p.acquire_account(immediate=True)
            self.assertIsNotNone(got)
            p.release_account(got)
        asyncio.run(run())

    def test_backoff_ladder_is_bounded_and_escalates(self):
        p = self.pool()
        acc = p._accounts["a@x"]
        seen = []
        for _ in range(len(MUTE_BACKOFF_LADDER) + 3):
            p.release_account(acc, is_muted=True)
            seen.append(acc.mute_until - int(time.time()))
        self.assertEqual(seen[:len(MUTE_BACKOFF_LADDER)], list(MUTE_BACKOFF_LADDER))
        self.assertEqual(seen[-1], MUTE_BACKOFF_LADDER[-1], "退避必须有上限，不能无限增长")

    def test_success_resets_backoff(self):
        """一次成功的使用就把退避阶梯清零 —— 否则偶发禁言会让阶梯只升不降"""
        p = self.pool()
        acc = p._accounts["a@x"]
        p.release_account(acc, is_muted=True)
        p.release_account(acc, is_muted=True)
        self.assertEqual(acc.mute_count, 2)
        p.release_account(acc, is_muted=False)
        self.assertEqual(acc.mute_count, 0)

    def test_upstream_mute_until_wins_over_ladder(self):
        """上游自己说了禁言到几点，就没有理由去猜"""
        p = self.pool()
        acc = p._accounts["a@x"]
        upstream = int(time.time()) + 7200      # 2 小时后
        p.release_account(acc, is_muted=True, mute_until=upstream)
        self.assertAlmostEqual(acc.mute_until, upstream, delta=5)

    def test_permanent_invalid_gets_long_but_finite_window(self):
        """永久失效给一个很长的停用期，而不是 None。

        None 会让它永远不参与唤醒检查，管理页上也看不出"它还会不会回来"。
        """
        p = self.pool()
        acc = p._accounts["a@x"]
        p.release_account(acc, is_muted=True, permanent=True)
        self.assertEqual(acc.status, "INVALID")
        self.assertIsNotNone(acc.mute_until)
        left = acc.mute_until - int(time.time())
        self.assertAlmostEqual(left, PERMANENT_INVALID_SECONDS, delta=10)
        self.assertLess(left, 400 * 86400, "再长也该有个头")

    def test_mute_count_persists(self):
        p = self.pool()
        acc = p._accounts["a@x"]
        p.release_account(acc, is_muted=True)
        stored = [a for a in config_manager.accounts if a["account_label"] == "a@x"][0]
        self.assertEqual(int(stored.get("mute_count") or 0), 1,
                         "熔断次数必须落盘，否则重启后退避阶梯会重置")
        # 新池子要能读回来
        self.assertEqual(self.pool()._accounts["a@x"].mute_count, 1)

    def test_muted_account_is_not_dispatched(self):
        p = self.pool()
        acc = p._accounts["a@x"]
        p.release_account(acc, is_muted=True)

        async def run():
            got = set()
            for _ in range(6):
                a = await p.acquire_account(immediate=True)
                if a:
                    got.add(a.account_label)
                    p.release_account(a)
            return got

        got = asyncio.run(run())
        self.assertNotIn("a@x", got, "已熔断的账号绝不能被调度")
        self.assertTrue(got.issubset({"b@x", "c@x"}))


class TestMuteUntilParsing(unittest.TestCase):
    """上游给的禁言时间格式不确定，必须稳健解析；拿不准就返回 None 交给退避兜底"""

    def test_seconds_timestamp(self):
        now = time.time()
        target = int(now + 3600)
        self.assertAlmostEqual(_parse_mute_until(target), target, delta=2)

    def test_milliseconds_timestamp(self):
        target = int((time.time() + 3600) * 1000)
        self.assertAlmostEqual(_parse_mute_until(target), target / 1000, delta=2)

    def test_relative_seconds(self):
        got = _parse_mute_until(600)     # 还要等 600 秒
        self.assertAlmostEqual(got, time.time() + 600, delta=5)

    def test_rejects_past_and_absurd(self):
        self.assertIsNone(_parse_mute_until(int(time.time()) - 3600), "过去的时间无意义")
        self.assertIsNone(_parse_mute_until(int(time.time() + 365 * 86400)), "超过 7 天视为不可信")
        self.assertIsNone(_parse_mute_until(0))
        self.assertIsNone(_parse_mute_until(-1))
        self.assertIsNone(_parse_mute_until(None))
        self.assertIsNone(_parse_mute_until("not a number"))

    def test_chat_event_carries_mute_until(self):
        e = ChatEvent("error", content="muted", error_code="biz:5", mute_until=12345)
        self.assertEqual(e.mute_until, 12345)
        self.assertIsNone(ChatEvent("content", content="x").mute_until)


class TestDispatchUnderConcurrency(PoolBase):
    """压测：并发取号的行为必须是可预测的"""

    def test_total_capacity_is_accounts_times_two(self):
        p = self.pool()

        async def run():
            held = []
            for _ in range(len(config_manager.accounts) * MAX_CONCURRENT_PER_ACCOUNT):
                a = await p.acquire_account(immediate=True)
                self.assertIsNotNone(a, "容量内的请求不该被拒")
                held.append(a)
            denied = await p.acquire_account(immediate=True)
            self.assertIsNone(denied, "超出容量的请求必须快速失败（而不是排队）")
            self.assertEqual(p.last_deny_reason, "own_account_at_capacity")
            for a in held:
                p.release_account(a)

        asyncio.run(run())

    def test_no_starvation_under_sequential_pressure(self):
        """反复取放，每个账号都应被用到 —— 不能总是挑同一个"""
        p = self.pool()

        async def run():
            used = {}
            for _ in range(30):
                a = await p.acquire_account(immediate=True)
                used[a.account_label] = used.get(a.account_label, 0) + 1
                p.release_account(a)
            return used

        used = asyncio.run(run())
        self.assertEqual(len(used), 3, f"3 个账号都该被用到，实际 {used}")
        # 释放会更新 last_used，所以轮转应当相当均匀
        self.assertLess(max(used.values()) - min(used.values()), 10, f"分布过于不均: {used}")

    def test_fills_one_account_before_moving_on(self):
        """先填满一个号再换下一个：这样其它号能留给粘性会话"""
        p = self.pool()

        async def run():
            first = []
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                a = await p.acquire_account(immediate=True)
                first.append(a.account_label)
            return first

        first = asyncio.run(run())
        self.assertEqual(len(set(first)), 1, f"应先把同一个号填满，实际 {first}")

    def test_sticky_account_full_does_not_drift(self):
        """粘性会话的账号满了必须快速失败，绝不能漂到别的号（那是串线）"""
        p = self.pool()

        async def run():
            held = []
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                held.append(await p.acquire_account(preferred_label="a@x", immediate=True))
            denied = await p.acquire_account(preferred_label="a@x", immediate=True)
            self.assertIsNone(denied, "粘性账号满了就必须失败，不能改用别的号")
            self.assertEqual(p.last_deny_reason, "own_account_at_capacity")
            for a in held:
                p.release_account(a)

        asyncio.run(run())

    def test_deny_detail_names_the_real_bottleneck(self):
        """★ 429 的文案必须指出真正的瓶颈。

        实测踩到的坑：粘性会话的号满载时，上层的模板会说
        「公共池 25 个可用账号均已满载」—— 而事实可能只是**这一个**会话绑定的号满了、
        池里另外几十个号都空着。这种误导会让人往完全错误的方向排查。
        """
        p = self.pool()

        async def run():
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                await p.acquire_account(preferred_label="a@x", immediate=True)
            denied = await p.acquire_account(preferred_label="a@x", immediate=True)
            return denied

        self.assertIsNone(asyncio.run(run()))
        self.assertIn("a@x", p.last_deny_detail, "必须点名是哪个号满了")
        self.assertIn("空闲", p.last_deny_detail, "必须说明池里还有没有空闲账号")
        self.assertIn("2", p.last_deny_detail, "b@x 与 c@x 都空着，应报 2 个空闲")

    def test_deny_detail_reports_no_idle_when_pool_exhausted(self):
        p = self.pool()

        async def run():
            held = []
            for _ in range(len(config_manager.accounts) * MAX_CONCURRENT_PER_ACCOUNT):
                held.append(await p.acquire_account(immediate=True))
            # 整个池子满了：粘性路径应当报"池里也没有其它空闲账号"
            denied = await p.acquire_account(preferred_label="a@x", immediate=True)
            return denied, held

        denied, held = asyncio.run(run())
        self.assertIsNone(denied)
        self.assertIn("也没有其它空闲账号", p.last_deny_detail)

    def test_fast_fail_is_actually_fast(self):
        """★ immediate=True 必须**立刻**返回，不能等。

        这是单测抓到的一个真实生产缺陷：旧实现粘性路径写的是
        `wait_for = remaining if immediate else min(3.0, remaining)`，
        于是 immediate=True 用的是**剩余总预算（默认 60 秒）**——
        "快速失败"实际阻塞整整一分钟才返回 429，而客户端看门狗约 50 秒，
        **客户端永远先超时**，表现为"卡住几十秒然后报错"（TRANSPORT）。
        正是 T22 想消灭的症状，却在这条分支上一直没被修掉。

        断言耗时而不只是断言返回值 —— 只看返回 None 是抓不到这个 bug 的。
        """
        p = self.pool()

        async def run():
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                await p.acquire_account(preferred_label="a@x", immediate=True)
            t0 = time.monotonic()
            denied = await p.acquire_account(preferred_label="a@x", immediate=True)
            elapsed = time.monotonic() - t0
            self.assertIsNone(denied)
            self.assertLess(elapsed, 0.5,
                            f"粘性账号满载时的快速失败耗时 {elapsed:.2f}s —— "
                            f"它必须立刻返回，否则会撞上客户端看门狗")

        asyncio.run(run())

    def test_fast_fail_is_fast_for_pool_capacity_too(self):
        """整个池子被打满时的快速失败同样必须是瞬时的"""
        p = self.pool()

        async def run():
            held = []
            for _ in range(len(config_manager.accounts) * MAX_CONCURRENT_PER_ACCOUNT):
                held.append(await p.acquire_account(immediate=True))
            t0 = time.monotonic()
            denied = await p.acquire_account(immediate=True)
            elapsed = time.monotonic() - t0
            self.assertIsNone(denied)
            self.assertLess(elapsed, 0.5, f"池子满载时的快速失败耗时 {elapsed:.2f}s")
            for a in held:
                p.release_account(a)

        asyncio.run(run())

    def test_sticky_to_missing_account_falls_back(self):
        """粘性的号已经不在池里 → 正常走候选（会话本来就不能跨号，新建即可）"""
        p = self.pool()

        async def run():
            a = await p.acquire_account(preferred_label="已删除@x", immediate=True)
            self.assertIsNotNone(a)
            p.release_account(a)

        asyncio.run(run())

    def test_release_is_idempotent_safe(self):
        """多释放一次不能让信号量凭空多出额度（容量悄悄变大会更难查）"""
        p = self.pool()
        acc = p._accounts["a@x"]

        async def run():
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                await p.acquire_account(preferred_label="a@x", immediate=True)
            self.assertEqual(acc.in_flight, MAX_CONCURRENT_PER_ACCOUNT)

        asyncio.run(run())
        p.release_account(acc)
        self.assertEqual(acc.in_flight, MAX_CONCURRENT_PER_ACCOUNT - 1)


class TestCooldownEstimate(PoolBase):

    def test_within_documented_bounds(self):
        acc = AccountRuntime(_acct("a@x"))
        acc.last_released_at = 0
        self.assertGreaterEqual(acc.cooldown_remaining(), 2.0)
        self.assertLessEqual(acc.cooldown_remaining(), 15.0)

    def test_just_released_is_longest(self):
        acc = AccountRuntime(_acct("a@x"))
        acc.last_released_at = time.time()
        self.assertAlmostEqual(acc.cooldown_remaining(), 6.0, delta=0.5)

    def test_old_release_yields_minimum(self):
        acc = AccountRuntime(_acct("a@x"))
        acc.last_released_at = time.time() - 60
        self.assertEqual(acc.cooldown_remaining(), 2.0)


class TestPoolHealthReporting(PoolBase):
    """号池状态必须能看出"它在缩水"，否则只能靠人去数"""

    def test_status_exposes_mute_info(self):
        p = self.pool()
        acc = p._accounts["a@x"]
        p.release_account(acc, is_muted=True)
        rows = {r["account_label"]: r for r in p.get_all_status()}
        self.assertEqual(rows["a@x"]["mute_count"], 1)
        self.assertIsNotNone(rows["a@x"]["mute_until"])
        self.assertIsNotNone(rows["a@x"]["wake_at"], "必须给出人类可读的解禁时刻")
        self.assertFalse(rows["a@x"]["is_valid"])
        self.assertTrue(rows["b@x"]["is_valid"])

    def test_reload_keeps_runtime_semaphore(self):
        """reload 不能把正在使用的并发额度重置掉（否则容量会瞬间翻倍）"""
        p = self.pool()
        acc = p._accounts["a@x"]

        async def run():
            await p.acquire_account(preferred_label="a@x", immediate=True)

        asyncio.run(run())
        self.assertEqual(acc.in_flight, 1)
        p.reload()
        self.assertEqual(p._accounts["a@x"].in_flight, 1,
                         "reload 必须复用同一个 AccountRuntime，不能重建信号量")


if __name__ == "__main__":
    unittest.main(verbosity=2)
