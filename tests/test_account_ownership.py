"""
T2 账号归属隔离与身份传递测试

覆盖三件事：
 1. 数据模型向后兼容 —— 42 条历史 key 记录没有 owner/account_labels 字段，
    升级后必须继续可用，且行为与升级前一致（走公共池）。
 2. 归属隔离不变量 —— 别人名下的号任何时候都拿不到；有主之号的粘性会话
    也不能变成越权通道。
 3. 鉴权热路径不再写盘 —— 旧实现每请求 read+write 12KB JSON（且在全局锁内），
    这里用计数断言退化为"每次请求 0 次磁盘 I/O"。
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from starlette.requests import Request
from fastapi import HTTPException

from app.config import config_manager
from app.core import security
from app.engine.account_pool import (
    AccountPool,
    AccountRuntime,
    MAX_CONCURRENT_PER_ACCOUNT,
)


def _make_request(headers=None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": raw,
        "state": {},
    })


def _sync_verify(request):
    """同步调用 verify_api_key 的测试包装器。

    verify_api_key / get_identity 都是 async def —— 这不是风格选择，
    而是**功能约束**：FastAPI 对同步依赖会 run_in_threadpool，
    而线程池拿到的是 contextvars 的副本，在里面 set_current_identity
    传不回请求上下文，按 key 的用量会静默恒为 0（详见 security.py）。
    测试里直接调用得自己把协程跑起来。
    """
    return asyncio.run(security.verify_api_key(request))


class TestDependencyContract(unittest.TestCase):
    """把"必须是 async def"这条约束钉死，防止将来有人"顺手"改成同步 def。

    这类回归是**静默**的：改成同步 def 之后所有测试照样绿
    （直接调用函数根本走不到线程池那条路径），
    只有真实 HTTP 请求才会暴露 —— 由 tests/smoke_t25.py 负责那一层。
    """

    def test_api_key_dependencies_are_coroutines(self):
        import inspect
        self.assertTrue(
            inspect.iscoroutinefunction(security.verify_api_key),
            "verify_api_key 必须是 async def：同步 def 会被 FastAPI 丢进线程池，"
            "contextvar 写在副本里传不回来，按 key 用量会静默漏统",
        )
        self.assertTrue(
            inspect.iscoroutinefunction(security.get_identity),
            "get_identity 必须是 async def，原因同上",
        )


class OwnershipTestBase(unittest.TestCase):
    """把 KEYS_FILE 指向临时文件，并重置 security 的内存缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_keys_file = security.KEYS_FILE
        self._orig_accounts = config_manager.accounts

        security.KEYS_FILE = Path(self._tmp.name) / "api_keys.json"
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}
        security._last_flush = 0.0

        self.pool_labels = ["pool-a@x", "pool-b@x", "pool-c@x"]
        config_manager.accounts = [
            {
                "account_label": lbl,
                "token": "tok-" + lbl,
                "cookie": "",
                "is_valid": True,
                "status": "ACTIVE",
            }
            for lbl in self.pool_labels
        ]

    def tearDown(self):
        security.KEYS_FILE = self._orig_keys_file
        security.load_keys()
        config_manager.accounts = self._orig_accounts
        self._tmp.cleanup()

    # ── helpers ──

    def _seed_legacy_key(self, name="legacy"):
        """写一条升级前格式的记录（没有 owner / account_labels / enabled）"""
        raw = "sk-" + "a" * 48
        self._write_raw_keys([{
            "id": "legacy01",
            "name": name,
            "key_hash": security._hash_key(raw),
            "prefix": raw[:15] + "…",
            "created_at": 1,
            "revoked": False,
            "revoked_at": None,
            "last_used_at": None,
        }])
        return raw

    def _write_raw_keys(self, records):
        security.KEYS_FILE.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}

    def _new_pool(self) -> AccountPool:
        return AccountPool()


class TestLegacyCompatibility(OwnershipTestBase):

    def test_legacy_record_gets_defaults(self):
        """旧记录被读入后自动补齐新字段，且不改变原有行为"""
        raw = self._seed_legacy_key()
        keys = security.list_keys()
        self.assertEqual(len(keys), 1)
        k = keys[0]
        self.assertEqual(k["owner"], "")
        self.assertEqual(k["account_labels"], [])
        self.assertTrue(k["enabled"])
        self.assertFalse(k["pool_fallback"])
        self.assertEqual(k["log_label"], "legacy")

        # admin 视图里也不能出现 key_hash
        self.assertNotIn("key_hash", k)

        req = _make_request({"Authorization": f"Bearer {raw}"})
        self.assertTrue(_sync_verify(req))
        ident = asyncio.run(security.get_identity(req))
        self.assertEqual(ident["key_id"], "legacy01")
        self.assertEqual(ident["account_labels"], [])
        self.assertTrue(ident["authenticated"])

    def test_legacy_key_uses_public_pool(self):
        """名下没号的 key → 走公共池（升级前的行为，保证平滑）"""
        raw = self._seed_legacy_key()
        req = _make_request({"Authorization": f"Bearer {raw}"})
        _sync_verify(req)
        ident = asyncio.run(security.get_identity(req))

        pool = self._new_pool()
        allowed = pool.allowed_labels(ident["account_labels"], ident["pool_fallback"])
        self.assertEqual(allowed, set(self.pool_labels))

    def test_revoked_and_disabled_keys_rejected(self):
        raw = self._seed_legacy_key()
        rec = security.list_keys()[0]
        security.update_key(rec["id"], enabled=False)
        with self.assertRaises(HTTPException) as ctx:
            _sync_verify(_make_request({"Authorization": f"Bearer {raw}"}))
        self.assertEqual(ctx.exception.status_code, 401)

        security.update_key(rec["id"], enabled=True)
        security.revoke_key(rec["id"])
        with self.assertRaises(HTTPException):
            _sync_verify(_make_request({"Authorization": f"Bearer {raw}"}))

    def test_missing_and_invalid_key(self):
        with self.assertRaises(HTTPException) as ctx:
            _sync_verify(_make_request())
        self.assertEqual(ctx.exception.status_code, 401)

        with self.assertRaises(HTTPException):
            _sync_verify(_make_request({"x-api-key": "sk-nope"}))


class TestOwnershipRules(OwnershipTestBase):

    def setUp(self):
        super().setUp()
        self.a = security.generate_key("keyA", owner="张三")
        self.b = security.generate_key("keyB", owner="李四")

    def test_assign_and_release(self):
        r = security.set_key_accounts(self.a["id"], ["pool-a@x"])
        self.assertEqual(r["assigned"], ["pool-a@x"])
        self.assertEqual(security.get_key(self.a["id"])["account_labels"], ["pool-a@x"])

        # 覆盖式：改成另一个号，旧的自动回池
        r = security.set_key_accounts(self.a["id"], ["pool-b@x"])
        self.assertEqual(r["assigned"], ["pool-b@x"])
        self.assertEqual(r["released"], ["pool-a@x"])

        r = security.set_key_accounts(self.a["id"], [])
        self.assertEqual(r["released"], ["pool-b@x"])
        self.assertEqual(security.account_owner_map(), {})

    def test_conflict_is_rejected_not_silently_stolen(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        with self.assertRaises(ValueError) as ctx:
            security.set_key_accounts(self.b["id"], ["pool-a@x"])
        self.assertIn("pool-a@x", str(ctx.exception))
        self.assertIn("张三", str(ctx.exception))
        # 断言没被抢走
        self.assertEqual(security.get_key(self.a["id"])["account_labels"], ["pool-a@x"])
        self.assertEqual(security.get_key(self.b["id"])["account_labels"], [])

    def test_assign_dedupes_and_skips_blank(self):
        r = security.set_key_accounts(self.a["id"], ["pool-a@x", "pool-a@x", "", "  "])
        self.assertEqual(r["assigned"], ["pool-a@x"])

    def test_assign_to_unknown_key(self):
        with self.assertRaises(KeyError):
            security.set_key_accounts("nope", ["pool-a@x"])

    def test_revoke_releases_accounts_back_to_pool(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        self.assertIn("pool-a@x", security.account_owner_map())
        security.revoke_key(self.a["id"])
        self.assertEqual(security.account_owner_map(), {})

    def test_disabled_key_releases_accounts(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        security.update_key(self.a["id"], enabled=False)
        self.assertEqual(security.account_owner_map(), {})

    def test_owner_map_excludes_other_keys_accounts(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        owners = security.account_owner_map()
        self.assertEqual(owners["pool-a@x"]["key_id"], self.a["id"])
        self.assertEqual(owners["pool-a@x"]["owner"], "张三")
        self.assertNotIn("pool-b@x", owners)


class TestAllowedLabels(OwnershipTestBase):

    def setUp(self):
        super().setUp()
        self.a = security.generate_key("keyA", owner="张三")
        self.b = security.generate_key("keyB", owner="李四")
        self.pool = self._new_pool()

    def test_no_ownership_gets_whole_pool(self):
        allowed = self.pool.allowed_labels([], False)
        self.assertEqual(allowed, set(self.pool_labels))

    def test_owner_gets_only_own_accounts(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        allowed = self.pool.allowed_labels(["pool-a@x"], False)
        self.assertEqual(allowed, {"pool-a@x"})
        # 关键：另外两个号虽然在"公共池"，但这是 张三 自己的视角，
        # 它名下已经有号了 → 不能再蹭池子里的号（否则分配就没意义）
        self.assertNotIn("pool-b@x", allowed)

    def test_other_keys_accounts_are_never_allowed(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        # 李四 名下没号 → 走公共池；张三 的号必须被排除
        allowed = self.pool.allowed_labels([], False)
        self.assertEqual(allowed, {"pool-b@x", "pool-c@x"})
        self.assertNotIn("pool-a@x", allowed)

    def test_pool_fallback_is_explicit_opt_in(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        allowed = self.pool.allowed_labels(["pool-a@x"], pool_fallback=True)
        self.assertEqual(allowed, set(self.pool_labels))

    def test_stale_ownership_does_not_silently_grant_pool(self):
        """名下号被删掉后，不能悄悄退化成"整个池子都是我的"""
        allowed = self.pool.allowed_labels(["已删除的号@x"], False)
        self.assertEqual(allowed, set())

    def test_pool_fallback_still_excludes_taken(self):
        security.set_key_accounts(self.b["id"], ["pool-b@x"])
        allowed = self.pool.allowed_labels(["pool-a@x"], pool_fallback=True)
        self.assertEqual(allowed, {"pool-a@x", "pool-c@x"})


class TestPoolDispatch(OwnershipTestBase):

    def setUp(self):
        super().setUp()
        self.a = security.generate_key("keyA", owner="张三")
        self.b = security.generate_key("keyB", owner="李四")

    def test_acquire_respects_ownership(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        pool = self._new_pool()

        async def run():
            acc = await pool.acquire_account(owned_labels=["pool-a@x"], immediate=True)
            self.assertIsNotNone(acc)
            self.assertEqual(acc.account_label, "pool-a@x")

        asyncio.run(run())

    def test_acquire_never_hands_out_foreign_account(self):
        """李四 名下无号，反复抢号也绝不能抢到 张三 的号"""
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        pool = self._new_pool()

        async def run():
            got = set()
            for _ in range(5):
                acc = await pool.acquire_account(owned_labels=[], immediate=True)
                self.assertIsNotNone(acc)
                got.add(acc.account_label)
                pool.release_account(acc)
            self.assertNotIn("pool-a@x", got)
            self.assertTrue(got.issubset({"pool-b@x", "pool-c@x"}))

        asyncio.run(run())

    def test_own_account_saturated_returns_none_not_pool(self):
        """自有号满载 → 诚实 429，而不是静默去占公共池的空号"""
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        pool = self._new_pool()

        async def run():
            held = []
            for _ in range(MAX_CONCURRENT_PER_ACCOUNT):
                acc = await pool.acquire_account(owned_labels=["pool-a@x"], immediate=True)
                self.assertIsNotNone(acc)
                held.append(acc)

            denied = await pool.acquire_account(owned_labels=["pool-a@x"], immediate=True)
            self.assertIsNone(denied)
            self.assertEqual(pool.last_deny_reason, "own_account_at_capacity")
            # 池里明明还有 2 个空闲号，也不能给
            free = [a for a in pool._accounts.values() if a.is_available_for_api]
            self.assertEqual(
                sorted(a.account_label for a in free), ["pool-b@x", "pool-c@x"]
            )
            for a in held:
                pool.release_account(a)

        asyncio.run(run())

    def test_no_account_assigned_reason(self):
        pool = self._new_pool()

        async def run():
            acc = await pool.acquire_account(
                owned_labels=["已删除的号@x"], immediate=True
            )
            self.assertIsNone(acc)
            self.assertEqual(pool.last_deny_reason, "no_account_assigned")

        asyncio.run(run())

    def test_sticky_session_cannot_cross_keys(self):
        """粘性会话记的是 account_label；换 key 重放/号被改派时必须失效，
        否则 preferred_label 会变成越权通道。"""
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        pool = self._new_pool()

        async def run():
            # 李四（无号，公共池）拿着 张三 的号做粘性请求
            acc = await pool.acquire_account(
                preferred_label="pool-a@x", owned_labels=[], immediate=True
            )
            self.assertIsNotNone(acc)
            self.assertNotEqual(acc.account_label, "pool-a@x")
            pool.release_account(acc)

        asyncio.run(run())

    def test_anonymous_identity_uses_pool(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        pool = self._new_pool()
        ident = security._anonymous_identity()
        allowed = pool.allowed_labels(ident["account_labels"], ident["pool_fallback"])
        self.assertEqual(allowed, {"pool-b@x", "pool-c@x"})

    def test_status_exposes_ownership(self):
        security.set_key_accounts(self.a["id"], ["pool-a@x"])
        pool = self._new_pool()
        rows = {r["account_label"]: r for r in pool.get_all_status()}
        self.assertEqual(rows["pool-a@x"]["owner"], "张三")
        self.assertFalse(rows["pool-a@x"]["in_pool"])
        self.assertTrue(rows["pool-b@x"]["in_pool"])


class TestKeyStoreIO(OwnershipTestBase):
    """T2 顺带修掉的 I/O 热点：鉴权热路径不得读写磁盘。"""

    def test_verify_does_not_read_or_write_per_request(self):
        raw = self._seed_legacy_key()
        # 先热一次缓存（首次读取是必要的；我们要断言的是稳态下的每请求 I/O）
        _sync_verify(_make_request({"Authorization": f"Bearer {raw}"}))

        reads, writes = [], []
        orig_read, orig_write = security._read_keys_locked, security._write_keys_locked

        def counting_read():
            reads.append(1)
            return orig_read()

        def counting_write():
            writes.append(1)
            return orig_write()

        security._read_keys_locked = counting_read
        security._write_keys_locked = counting_write
        try:
            for _ in range(20):
                req = _make_request({"Authorization": f"Bearer {raw}"})
                self.assertTrue(_sync_verify(req))
        finally:
            security._read_keys_locked = orig_read
            security._write_keys_locked = orig_write

        # 旧实现：20 次请求 = 20 读 + 20 写（每次 12KB，且都在全局锁内）
        self.assertLessEqual(len(writes), 1, f"应节流落盘，实际写了 {len(writes)} 次")
        self.assertEqual(len(reads), 0, "文件未变不应重读")

    def test_last_used_visible_in_memory_before_flush(self):
        raw = self._seed_legacy_key()
        security._last_flush = security.time.time()  # 压住首次落盘
        req = _make_request({"Authorization": f"Bearer {raw}"})
        _sync_verify(req)
        self.assertIsNotNone(security.list_keys()[0]["last_used_at"])

    def test_external_file_change_is_picked_up(self):
        raw = self._seed_legacy_key()
        req = _make_request({"Authorization": f"Bearer {raw}"})
        self.assertTrue(_sync_verify(req))

        # 外部（运维脚本/手工编辑）改文件 → 必须被发现
        newraw = "sk-" + "b" * 48
        self._write_raw_keys([{
            "id": "legacy02",
            "name": "manual",
            "key_hash": security._hash_key(newraw),
            "prefix": newraw[:15] + "…",
            "created_at": 2,
            "revoked": False,
            "revoked_at": None,
            "last_used_at": None,
        }])
        req2 = _make_request({"Authorization": f"Bearer {newraw}"})
        self.assertTrue(_sync_verify(req2))
        self.assertEqual(asyncio.run(security.get_identity(req2))["key_id"], "legacy02")

    def test_revoke_flushes_immediately(self):
        """增删改必须立刻落盘，不能等 30s 节流窗口"""
        rec = security.generate_key("x")
        security._last_flush = security.time.time()
        security.revoke_key(rec["id"])
        on_disk = json.loads(security.KEYS_FILE.read_text("utf-8"))
        self.assertTrue([r for r in on_disk if r["id"] == rec["id"]][0]["revoked"])


class TestWriteBeforeReadNeverClobbers(OwnershipTestBase):
    """回归：**写之前必须先读**。

    真实事故（2026-09-10 深夜）：T2 为了让增删改立即落盘，把 _maybe_flush(force=True)
    改成跳过 _dirty 判断无条件写盘。于是"只 import、从没读过文件"的进程
    （部署脚本里的导入自检）在 atexit 触发 flush_pending() 时，
    用内存里的空列表覆盖了整个 api_keys.json —— 45 条记录瞬间归零，
    其中包括一把正在服役的 key。

    而**明文本就只有哈希存着，一旦丢失无法恢复**：那把 key 会直接废掉，
    所有下游客户端被切断且找不回来。当时仅因为还有一个旧进程内存里留着数据、
    重启前的 shutdown flush 又写了回去，才侥幸没出事。

    这组测试把"未读先写"这条路彻底堵死。
    """

    def _simulate_fresh_process(self):
        """模拟刚 import、尚未读过任何 key 的进程状态"""
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}
        security._pending_touch = {}
        security._loaded = False

    def test_flush_pending_does_not_clobber(self):
        raw = self._seed_legacy_key()
        self._simulate_fresh_process()
        security.flush_pending()
        on_disk = json.loads(security.KEYS_FILE.read_text("utf-8"))
        self.assertEqual(len(on_disk), 1, "未读先写的进程不得清空 api_keys.json")
        self.assertEqual(on_disk[0]["id"], "legacy01")

    def test_maybe_flush_force_reads_first(self):
        """即使绕过 flush_pending 直接 force flush，也必须先读后写"""
        self._seed_legacy_key()
        self._simulate_fresh_process()
        security._maybe_flush(force=True)
        on_disk = json.loads(security.KEYS_FILE.read_text("utf-8"))
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]["id"], "legacy01")

    def test_save_keys_does_not_clobber(self):
        self._seed_legacy_key()
        self._simulate_fresh_process()
        security.save_keys()
        on_disk = json.loads(security.KEYS_FILE.read_text("utf-8"))
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]["id"], "legacy01")

    def test_explicit_load_then_write_still_works(self):
        """正常路径不能被这道保险弄坏：读了之后再改，必须真的落盘"""
        rec = security.generate_key("fresh")
        security._last_flush = security.time.time()
        security.update_key(rec["id"], owner="张三")
        on_disk = json.loads(security.KEYS_FILE.read_text("utf-8"))
        self.assertEqual([r for r in on_disk if r["id"] == rec["id"]][0]["owner"], "张三")

    def test_missing_file_is_genuinely_empty(self):
        """文件确实不存在时，"空"才是真的空 —— 不能被保险挡成"永远不落盘\""""
        security.KEYS_FILE.unlink(missing_ok=True)
        self._simulate_fresh_process()
        rec = security.generate_key("first-ever")
        on_disk = json.loads(security.KEYS_FILE.read_text("utf-8"))
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]["id"], rec["id"])

    def test_key_usage_store_same_guard(self):
        """同一个缺陷不该在 key_usage 里留第二份"""
        from app.engine.key_usage import KeyUsageEngine
        p = Path(self._tmp.name) / "ku.json"
        p.write_text(json.dumps({"k1": {"requests": 5, "day": "2026-01-01"}}), encoding="utf-8")
        fresh = KeyUsageEngine(p)
        fresh._keys = {}
        fresh._loaded = False
        fresh._dirty = False
        fresh._flush_locked()
        on_disk = json.loads(p.read_text("utf-8"))
        self.assertEqual(on_disk["k1"]["requests"], 5, "未读先写不得清空 key_usage.json")


class TestExternalEditsSurvive(OwnershipTestBase):
    """服务运行期间的外部改动**不得被内存快照静默覆盖**。

    实测事故（2026-09-10 深夜）：在服务运行时执行 tools/prune_keys.py 删掉 46 条
    已作废记录，随后金丝雀发了一次请求（触发 last_used_at 落盘），
    那 46 条**全部复活** —— 因为落盘时直接拿内存里的旧快照覆盖了整个文件。

    反方向的危害更严重：有人手工在文件里撤销一把**泄露的 key**，
    会被正在运行的服务悄悄"复活"—— 你以为已经切断了，实际上那把钥照样能用。

    修复后落盘 = 重读磁盘基线 + 合并我们的增量。这组测试把这个语义钉死。
    """

    def _read_disk(self):
        return json.loads(security.KEYS_FILE.read_text("utf-8"))

    def _write_disk(self, records):
        security.KEYS_FILE.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def test_external_delete_is_not_resurrected(self):
        a = security.generate_key("keep-me")
        b = security.generate_key("delete-me")
        # 让服务再有几次 last_used 类的增量（模拟"运行中"）
        req = _make_request({"Authorization": f"Bearer {a['key']}"})
        _sync_verify(req)

        # 外部脚本删掉 b（模拟 tools/prune_keys.py 或手工编辑）
        on_disk = [r for r in self._read_disk() if r["id"] != b["id"]]
        self.assertEqual(len(on_disk), 1)
        self._write_disk(on_disk)

        # 服务再落盘一次（triggered by another request's last_used_at）
        req2 = _make_request({"Authorization": f"Bearer {a['key']}"})
        _sync_verify(req2)
        security.save_keys()

        after = self._read_disk()
        ids = [r["id"] for r in after]
        self.assertNotIn(b["id"], ids, "外部删除的记录被内存快照复活了")
        self.assertIn(a["id"], ids)

    def test_external_revoke_is_not_undone(self):
        """最危险的一种：外部把一把泄露的 key 标为 revoked，服务不能把它改回可用"""
        rec = security.generate_key("leaked")
        req = _make_request({"Authorization": f"Bearer {rec['key']}"})
        _sync_verify(req)

        # 运维直接在文件里撤销（紧急切断泄露的钥）
        on_disk = self._read_disk()
        for r in on_disk:
            if r["id"] == rec["id"]:
                r["revoked"] = True
                r["revoked_at"] = 1
        self._write_disk(on_disk)

        # 服务继续运行并落盘
        security.save_keys()
        after = self._read_disk()
        target = [r for r in after if r["id"] == rec["id"]][0]
        self.assertTrue(target["revoked"], "外部撤销被服务复活了 —— 泄露的 key 会继续可用")

        # 而且鉴权必须立刻拒绝它
        with self.assertRaises(HTTPException):
            _sync_verify(_make_request({"Authorization": f"Bearer {rec['key']}"}))

    def test_external_add_is_visible_immediately(self):
        """外部新增的 key（如自动化签发）必须立刻可鉴权，不需要重启"""
        live = security.generate_key("live")
        _sync_verify(_make_request({"Authorization": f"Bearer {live['key']}"}))

        extra_raw = "sk-" + "9" * 48
        on_disk = self._read_disk()
        on_disk.append({
            "id": "ext001", "name": "external-added",
            "key_hash": security._hash_key(extra_raw),
            "prefix": extra_raw[:14], "created_at": 1, "revoked": False,
        })
        self._write_disk(on_disk)

        req = _make_request({"Authorization": f"Bearer {extra_raw}"})
        self.assertTrue(_sync_verify(req))
        self.assertEqual(req.state.ds_api_identity["key_id"], "ext001")

    def test_our_own_structural_change_still_wins(self):
        """合并不能反过来：我们自己的改动必须生效"""
        rec = security.generate_key("mine")
        other = security.generate_key("other")
        # 外部改一个无关字段（触发签名变化）
        on_disk = self._read_disk()
        for r in on_disk:
            if r["id"] == other["id"]:
                r["owner"] = "外部改的"
        self._write_disk(on_disk)

        security.update_key(rec["id"], owner="我改的")
        after = self._read_disk()
        by_id = {r["id"]: r for r in after}
        self.assertEqual(by_id[rec["id"]]["owner"], "我改的")
        # 外部改动也要保住
        self.assertEqual(by_id[other["id"]]["owner"], "外部改的")

    def test_last_used_merges_without_resurrecting(self):
        """touch 增量与外部删除同时发生时，删除仍然生效"""
        a = security.generate_key("a")
        b = security.generate_key("b")
        # 让 b 产生一条待落盘的 touch
        security._mark_touched(b["id"], 123456)
        self.assertTrue(security.has_pending())

        # 外部把 b 删了
        self._write_disk([r for r in self._read_disk() if r["id"] != b["id"]])
        security.save_keys()

        after = self._read_disk()
        self.assertEqual([r["id"] for r in after], [a["id"]],
                         "已删除的 key 不该因为一条 last_used 增量被重新写回")


class TestPersonalCapability(OwnershipTestBase):
    """一人一号后的并发上限：账号数 × 2"""

    def test_single_account_capacity_is_two(self):
        security.generate_key("keyA", owner="张三")
        a = security.list_keys()[0]
        security.set_key_accounts(a["id"], ["pool-a@x"])
        pool = self._new_pool()
        allowed = pool.allowed_labels(["pool-a@x"], False)
        self.assertEqual(len(allowed) * MAX_CONCURRENT_PER_ACCOUNT, 2)

    def test_two_accounts_capacity_is_four(self):
        security.generate_key("keyA", owner="张三")
        a = security.list_keys()[0]
        security.set_key_accounts(a["id"], ["pool-a@x", "pool-b@x"])
        pool = self._new_pool()
        allowed = pool.allowed_labels(["pool-a@x", "pool-b@x"], False)
        self.assertEqual(len(allowed) * MAX_CONCURRENT_PER_ACCOUNT, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
