"""
客户端主动断流保护与短时频控防御 (Client Disconnect Guard)
- 捕获客户端主动断开连接 (request.is_disconnected())
- 优雅释放占用账号与上游会话
- 监控短时间内连续断流轰炸，实施自适应冷却保护 (429 Too Many Requests)，阻断大模型网关账号全员被封连坐风险
"""

import time
import threading
from typing import Dict, List, Optional
from collections import defaultdict


class DisconnectGuard:
    def __init__(
        self,
        window_seconds: float = 30.0,
        threshold_count: int = 2,
        cooldown_seconds: float = 15.0,
    ):
        self.window_seconds = window_seconds
        self.threshold_count = threshold_count
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._disconnects: Dict[str, List[float]] = defaultdict(list)
        self._cooldown_until: Dict[str, float] = {}

    def record_disconnect(self, client_id: str):
        """记录一次客户端主动断开"""
        if not client_id:
            return
        now = time.time()
        with self._lock:
            # 清理过期时间戳
            cutoff = now - self.window_seconds
            history = [t for t in self._disconnects[client_id] if t >= cutoff]
            history.append(now)
            self._disconnects[client_id] = history

            # 若 30 秒内连续断开达到阈值 (>= 2 次)，强制施加冷却
            if len(history) >= self.threshold_count:
                cd_until = now + self.cooldown_seconds
                self._cooldown_until[client_id] = max(
                    self._cooldown_until.get(client_id, 0.0), cd_until
                )
                print(
                    f"[DisconnectGuard] ⚠️ 客户端 {client_id} 在 {self.window_seconds:.0f}s 内连续断开 {len(history)} 次，"
                    f"施加防风控保护性冷却 {self.cooldown_seconds:.0f}s",
                    flush=True,
                )

    def check_cooldown(self, client_id: str) -> Optional[float]:
        """检查客户端是否处于主动断连冷却期。若在冷却期内返回剩余等待秒数，否则返回 None。"""
        if not client_id:
            return None
        now = time.time()
        with self._lock:
            until = self._cooldown_until.get(client_id, 0.0)
            if until > now:
                return float(until - now)
            # 已过冷却期，清理标记
            if client_id in self._cooldown_until:
                del self._cooldown_until[client_id]
            return None

    def reset(self, client_id: Optional[str] = None):
        """重置状态（供单测使用）"""
        with self._lock:
            if client_id:
                self._disconnects.pop(client_id, None)
                self._cooldown_until.pop(client_id, None)
            else:
                self._disconnects.clear()
                self._cooldown_until.clear()


disconnect_guard = DisconnectGuard()
