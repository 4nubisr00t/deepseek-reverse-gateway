"""
测试卫生回归：**测试绝不能写真实的 config.json**。

2026-09-11 真实事故：测调度逻辑的用例把 `config_manager.accounts` 换成假账号，
而 `release_account(is_muted=True)` → `update_account` → `save()` 把它写进了
**真实的 config.json**。tearDown 只还原内存，文件已经坏了：

    · 本地 config.json 的 3 个账号变成 a@x / b@x / c@x（token = tok-a@x 这种假值）
    · `auto_replenish` 被测试改成 False 并落盘
    · config.json 没进 git、没备份 → 只能从服务器重新取

**如果那台机器是生产，25 个账号会被一次性清空。**

这个文件把这个教训变成两条可执行的断言：
1. `ConfigManager` 实例化时用的路径必须是测试临时目录，不是仓库里的 config.json
2. 真的走一遍"改账号 → 落盘"，断言仓库里的 config.json **内容纹丝不动**
"""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import unittest

from app.config import CONFIG_FILE, PROJECT_ROOT as APP_ROOT, config_manager
from app.engine.account_pool import AccountPool


class ConfigIsolationTest(unittest.TestCase):

    def test_config_file_is_redirected(self):
        """conftest 必须把配置路径指到临时目录"""
        real = (APP_ROOT / "config.json").resolve()
        self.assertNotEqual(CONFIG_FILE.resolve(), real,
                            "测试用的 CONFIG_FILE 指向了真实的 config.json —— "
                            "conftest.py 没生效，测试会写坏真实配置")
        # 断言"在系统临时目录下"而不是匹配某个前缀字符串：
        # conftest 的目录名改过一次（dsg-test-config- → dsg-test-isolation-），
        # 依赖前缀的断言会跟着一起坏，而"必须在临时目录里"才是真正要守的性质。
        self.assertTrue(
            str(CONFIG_FILE.resolve()).startswith(str(Path(tempfile.gettempdir()).resolve())),
            f"期望 CONFIG_FILE 落在系统临时目录下，实际 {CONFIG_FILE}")

    def test_mutating_accounts_does_not_touch_real_config(self):
        """走一遍真实的落盘路径，确认真实 config.json 一个字节都没变"""
        real = APP_ROOT / "config.json"
        before = real.read_bytes() if real.exists() else None
        before_mtime = real.stat().st_mtime if real.exists() else None

        # 模拟测试里最危险的那条路径：换掉账号 → 触发熔断 → 落盘
        orig = config_manager.accounts
        try:
            config_manager.accounts = [
                {"account_label": "clobber-probe@x", "token": "tok-clobber",
                 "cookie": "", "is_valid": True, "status": "ACTIVE"},
            ]
            pool = AccountPool()
            acc = list(pool._accounts.values())[0]
            pool.release_account(acc, is_muted=True)   # → update_account → save()
        finally:
            config_manager.accounts = orig

        if before is None:
            self.assertFalse(real.exists(),
                             "真实 config.json 本来不存在，却被测试创建了")
        else:
            self.assertEqual(real.read_bytes(), before,
                             "★ 真实 config.json 被测试改写了 —— 这正是 2026-09-11 那次事故")
            self.assertEqual(real.stat().st_mtime, before_mtime)

    def test_pool_test_fixtures_never_reach_the_real_config(self):
        """探针账号绝不能出现在**真实**的 config.json 里。

        注意要断言的是真实路径，不是 CONFIG_FILE —— 后者本来就被重定向到了临时目录，
        在它里面找到探针是正常的（上一个测试刚写进去的）。
        早期版本就是查错了对象，导致这条测试毫无价值。
        """
        real = APP_ROOT / "config.json"
        text = real.read_text("utf-8") if real.exists() else ""
        self.assertNotIn("clobber-probe@x", text,
                         "★ 测试夹具账号泄漏进了真实 config.json")
        self.assertNotIn("tok-clobber", text)

    def test_tmp_config_is_where_writes_land(self):
        """写入必须落在临时文件上（证明隔离真的生效，而不是碰巧没触发 save）"""
        orig = list(config_manager.accounts)
        try:
            config_manager.accounts = [{"account_label": "tmp-only@x", "token": "t",
                                        "cookie": "", "is_valid": True, "status": "ACTIVE"}]
            config_manager.save()
            self.assertTrue(CONFIG_FILE.exists(), "临时配置文件应当被创建")
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
            self.assertEqual(data["accounts"][0]["account_label"], "tmp-only@x")
        finally:
            config_manager.accounts = orig
            config_manager.save()
            from app.engine.account_pool import account_pool
            account_pool.reload()


if __name__ == "__main__":
    unittest.main(verbosity=2)
