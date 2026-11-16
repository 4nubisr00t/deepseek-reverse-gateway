"""
401 必须能自我解释：告诉你"是哪把 key、为什么被拒"。

## 为什么要专门测这个

2026-09-12 排查"API key 无效"时，成本几乎全花在**猜**上：
access log 只有 IP，没有任何线索说"发的是哪把 key、为什么不认"。
最后是靠时间线（12:58 作废 → 13:08 开始 401）反推出来的。
而真实原因是**客户端揣着旧明文**：明文只在创建时给一次，服务端只存
sha256 与前缀，丢了就找不回来 —— 这种失败重试一万次也一样。

两种失败的性质完全不同：
  · 表里根本没有这把 key → 客户端要换新明文
  · 有这把 key 但已作废/停用 → 去管理页看一眼就行

所以 401 必须把这两者分开说清楚，并且**只打前缀**（15 位，与管理页一致）。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core import security


class FakeUrl:
    def __init__(self, path="/v1/chat/completions"):
        self.path = path


class FakeClient:
    host = "203.0.113.7"


class FakeRequest:
    def __init__(self, token="", path="/v1/chat/completions"):
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.url = FakeUrl(path)
        self.client = FakeClient()
        self.state = type("S", (), {})()


class AuthBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = security.KEYS_FILE
        self._orig_require = security.config_manager.config.require_api_key
        self._orig_keys = security._keys
        self._orig_sig = security._keys_sig
        security.KEYS_FILE = Path(self._tmp.name) / "api_keys.json"
        security._keys = []
        security._keys_sig = None
        security._pending_records = {}
        security._pending_touch = {}
        security._loaded = False
        security._dirty = False
        security.config_manager.config.require_api_key = True

    def tearDown(self):
        security.config_manager.config.require_api_key = self._orig_require
        security.KEYS_FILE = self._orig_file
        security._keys = self._orig_keys
        security._keys_sig = self._orig_sig
        security.load_keys()
        self._tmp.cleanup()

    def make_key(self, name="k"):
        rec = security.generate_key(name)
        return rec


class TestExplainKeyMiss(AuthBase):

    def test_missing_key(self):
        self.assertIn("未携带", security._explain_key_miss(""))

    def test_unknown_key_says_plaintext_is_gone_forever(self):
        msg = security._explain_key_miss("sk-this-key-was-never-created")
        self.assertIn("表里没有这把 key", msg)
        self.assertIn("明文", msg, "必须说清'明文找不回来'，否则用户会一直重试")

    def test_revoked_key(self):
        rec = self.make_key("dead")
        for k in security._keys:
            if k["id"] == rec["id"]:
                k["revoked"] = True
        msg = security._explain_key_miss(rec["key"])
        self.assertIn("已被作废", msg)

    def test_disabled_key(self):
        rec = self.make_key("paused")
        for k in security._keys:
            if k["id"] == rec["id"]:
                k["enabled"] = False
        msg = security._explain_key_miss(rec["key"])
        self.assertIn("已被停用", msg)

    def test_valid_key_is_not_reported_as_a_miss(self):
        rec = self.make_key("good")
        self.assertIn("不该走到这里", security._explain_key_miss(rec["key"]))

    def test_revoked_and_unknown_are_distinguishable(self):
        """这两句必须不同 —— 混在一起就等于没解释。"""
        rec = self.make_key("dead")
        for k in security._keys:
            if k["id"] == rec["id"]:
                k["revoked"] = True
        self.assertNotEqual(security._explain_key_miss(rec["key"]),
                            security._explain_key_miss("sk-nope"))


class TestVerifyLogsTheReason(AuthBase):
    """⚠️ 用 `contextlib.redirect_stdout` 而不是 pytest 的 `capsys`：
    本项目用的是 `unittest.TestCase`，而 `capsys` 只是 pytest 的 fixture，
    在 TestCase 方法上不会注入（实测报 "missing 1 required positional argument: 'capsys'"）。
    """

    def _call(self, token):
        return asyncio.run(security.verify_api_key(FakeRequest(token)))

    def _capture(self, token):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                self._call(token)
                err = None
            except Exception as e:          # noqa: BLE001 —— 这里就是要看它抛什么
                err = e
        return buf.getvalue(), err

    def test_log_line_names_only_the_prefix(self):
        """★ 只打前缀：完整 key 绝不能进日志。"""
        r = self.make_key("good")
        full = r["key"]
        out, err = self._capture("sk-completely-different-key-000000000000")

        from fastapi import HTTPException
        self.assertIsInstance(err, HTTPException)
        self.assertIn("[Gateway-Auth] 401 拒绝", out)
        self.assertIn("表里没有这把 key", out)
        self.assertIn("client=203.0.113.7", out, "要能定位到来源")
        self.assertIn("sk-completely-d…", out, "打的是被拒那把的前缀")
        self.assertNotIn(full, out, "完整 key 泄漏进日志了")

    def test_revoked_key_is_reported_as_revoked(self):
        r = self.make_key("dead")
        for k in security._keys:
            if k["id"] == r["id"]:
                k["revoked"] = True
        out, err = self._capture(r["key"])
        from fastapi import HTTPException
        self.assertIsInstance(err, HTTPException)
        self.assertIn("已被作废", out)
        self.assertNotIn(r["key"], out)

    def test_missing_key_is_reported_as_missing(self):
        out, err = self._capture("")
        from fastapi import HTTPException
        self.assertIsInstance(err, HTTPException)
        self.assertIn("未携带 key", out)

    def test_valid_key_does_not_log_a_reject(self):
        r = self.make_key("good")
        out, err = self._capture(r["key"])
        self.assertIsNone(err)
        self.assertNotIn("401 拒绝", out)

    def test_still_returns_401_status(self):
        """加日志不能改动判定结果。"""
        from fastapi import HTTPException
        _out, err = self._capture("sk-nope")
        self.assertIsInstance(err, HTTPException)
        self.assertEqual(err.status_code, 401)

    def test_logging_failure_does_not_break_auth(self):
        """观测失败绝不影响鉴权本身（`_log_auth_reject` 内部吞异常）。"""
        class Exploding:
            @property
            def url(self):
                raise RuntimeError("boom")

            headers = {"Authorization": "Bearer sk-nope"}
            client = None
            state = type("S", (), {})()

        try:
            asyncio.run(security.verify_api_key(Exploding()))
        except Exception as e:                      # noqa: BLE001
            from fastapi import HTTPException
            self.assertIsInstance(e, HTTPException,
                                  f"日志炸了却把鉴权也带崩了: {e!r}")
        else:
            self.fail("非法 key 被放行了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
