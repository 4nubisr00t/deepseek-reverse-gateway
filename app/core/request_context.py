"""
请求级身份上下文 (Request-scoped identity)

**为什么需要它**：网关里 `metrics_engine.record(...)` 有 14 个调用点，
散布在正常返回、上游报错、容量不足、工具循环、流式中断等各条退出路径上。
要按 API Key 归因用量，最直接的做法是给这 14 处都加一个 `key=` 参数
—— 但那意味着 14 次手工修改，且**任何一处漏掉都会让该路径的用量永远统计不到**，
而这种漏统是静默的：图表上看起来只是"数字偏小"，没人会发现。

改用 contextvar：请求进入时由鉴权层写入一次，`record()` 自己读取。
调用点一处都不用改，也不存在"漏掉某条路径"的可能。

**安全性说明**：uvicorn 下每个请求在自己的 asyncio context 里跑，
contextvar 不会跨请求泄漏；`asyncio.create_task` 派生的任务在创建时
复制当前上下文 —— 网关里只有会话回收这类后台任务，
它们不调用 `record()`，所以不存在归因错位。
"""

import contextvars
from typing import Any, Dict, Optional

# 当前请求的身份（= security 层解析出的 key 视图）。
# default=None 表示"还没鉴权 / 非请求上下文"，此时用量只进全局统计、不进按 key 统计。
_current_identity: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "ds_current_identity", default=None
)


def set_current_identity(ident: Optional[Dict[str, Any]]) -> None:
    """由鉴权层调用。传 None 可显式清空。"""
    _current_identity.set(ident)


def get_current_identity() -> Optional[Dict[str, Any]]:
    ident = _current_identity.get()
    return ident if isinstance(ident, dict) else None


def current_key_id() -> str:
    ident = get_current_identity()
    return (ident or {}).get("key_id", "") or ""


def current_key_label() -> str:
    ident = get_current_identity()
    return (ident or {}).get("log_label", "") or ""
