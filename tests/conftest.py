"""
pytest 全局保险：**让测试永远碰不到真实的持久化文件**。

## 为什么需要这个文件

2026-09-11 真实事故：测试用例为了测调度逻辑，把 `config_manager.accounts`
换成假账号（`a@x` / `tok-a@x`）。而 `release_account(is_muted=True)`
会走到 `update_account` → `save()`，把内存里的假账号**写进真实的 config.json**。
tearDown 只还原了内存对象，文件已经被写坏了：

    · 本地 config.json 的 3 个账号变成 a@x / b@x / c@x（token 是 tok-a@x 这种假值）
    · `auto_replenish` 被测试改成 False 并落盘
    · config.json 既没进 git、也没有备份 → 只能从服务器重新取

**如果那台机器是生产，25 个账号会被一次性清空。** 这不是"测试没写好"的小事，
而是一条会毁掉生产数据的路径。

另一起同源头的事故：`api_keys.json` 被 atexit 的全量 flush 从 45 条记录清成 0。

## 修法

本文件在**任何 app 模块被导入之前**，把这个仓库里全部持久化存储的环境变量
指向临时目录（pytest 保证 conftest 先于测试模块导入），于是 `ConfigManager`、
keystore、用量引擎、metrics、bindings **从创建那一刻起**就用临时文件，
即使某条路径调用了 `save()`，写的也是临时文件。

覆盖面必须**等于存储清单**，而不是"我们记得的那几个"：漏掉任何一个，
那条存储就仍然可以写真实文件，契约只实现了一半。末尾的断言就是守这一点。
"""

import os
import tempfile
from pathlib import Path

# ⚠️ 必须在本文件顶部、任何 `app.*` 导入之前完成设置。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAFE_DIR = Path(tempfile.mkdtemp(prefix="dsg-test-isolation-"))

# 环境变量 -> 该存储的默认文件名。
# 这里列出**全部**持久化存储；新增存储时必须同步登记（见文件末尾断言）。
_STORES = {
    "DSG_CONFIG_FILE": "config.json",
    "DSG_KEYS_FILE": "api_keys.json",
    "DSG_USAGE_FILE": "key_usage.json",
    "DSG_METRICS_FILE": "metrics.json",
    "DSG_BINDINGS_PATH": "bindings.json",
    "DSG_DB_FILE": "data/gateway.db",
}

for _env, _name in _STORES.items():
    # setdefault：外层若已显式指定（对着固定 fixture 目录跑某条用例）则尊重它，
    # 但未指定的那些一律落到临时目录 —— 不做"全都已设才生效"的整体判断，
    # 否则只要外设了其中一个，其余几个就会漏回真实路径。
    os.environ.setdefault(_env, str(_SAFE_DIR / _name))

# 节流关掉：测试里不该有"等 30 秒才落盘"的时序依赖
os.environ.setdefault("DSG_BINDINGS_FLUSH_INTERVAL", "0")


def _assert_no_real_store_in_use() -> None:
    """兜底断言：任何存储都不得指向仓库内的真实文件。

    允许指到仓库外的 fixture 目录（外层显式覆盖的场景），但不允许指到
    仓库根下那份真实数据 —— 那正是两起事故的写法。
    """
    for env, name in _STORES.items():
        resolved = Path(os.environ[env]).resolve()
        real = (_REPO_ROOT / name).resolve()
        if resolved == real:
            raise RuntimeError(
                f"{env} 指向真实存储 {real} —— 测试会写坏它。"
                f"请在运行 pytest 前 unset {env}，或指向临时目录。"
            )


_assert_no_real_store_in_use()
