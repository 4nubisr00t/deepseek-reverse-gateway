"""
ds-gateway 调用监控与统计引擎 (Metrics Engine)
记录请求量、Token 消耗、延迟、成功率及最近调用流水，支持本地 JSON 持久化与历史数据校准
"""

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Dict, Any, List, Optional
import threading

# 仓库根目录（app/engine/metrics.py -> app/engine -> app -> 根）。这里不 import
# app.config，是为了让本模块保持纯标准库依赖、避免 engine -> config 的导入环。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 之前这里硬编码 "/opt/ds-gateway/metrics.json"：线上恰好等于 _PROJECT_ROOT/metrics.json
# 所以一直没暴露，但任何非 /opt/ds-gateway 的运行环境（本地开发、测试）都会写到错误位置。
# 与 config.json / api_keys.json / bindings.json 一致：默认跟随仓库根，可用环境变量覆盖。
METRICS_FILE = Path(os.environ.get("DSG_METRICS_FILE", str(_PROJECT_ROOT / "metrics.json")))


class MetricsEngine:
    #: 每个计数器最多留几条样本（带上下文，用于事后归因）。
    #: 留样本的理由：只知道"发生了 37 次"没法判断是不是同一个会话 / 同一个号出问题。
    MAX_COUNTER_SAMPLES = 20

    def __init__(self, max_history: int = 100, storage_file: Path = METRICS_FILE):
        self._lock = threading.Lock()
        self.max_history = max_history
        self.storage_file = storage_file
        self.start_time = time.time()
        
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_prompt_tokens = 0
        self.total_cached_tokens = 0
        self.total_completion_tokens = 0
        self.total_duration_ms = 0.0

        # ── T16 / B1：被动计数器 ──
        #
        # 为什么不复用 `recent_records`：那是"每个请求一条"的流水（上限 100 条），
        # 而这里要数的是**请求内部发生的异常事件**——一次请求可能同时命中好几个，
        # 而且核心目标不是"某一条请求长什么样"，是**比率**（错位率 / 拦截率 / 解析失败率）。
        #
        # 为什么不做日志抓取：线上 journal 是 3GB，`journalctl` 查询会超时；
        # 而且解析靠文案，文案一改就**静默失效**（表现是"指标一直是 0，看起来一切正常"）。
        # 计数器在事件发生点自增、随 metrics.json 一起落盘，成本是几次字典自增，
        # **不产生任何上游请求**——这正是 T16 需求（发现静默故障）与金丝雀
        # （主动合成流量）的关键区别。
        self.counters: Dict[str, int] = {}
        self.counter_samples: Dict[str, deque] = {}

        self.recent_records: deque = deque(maxlen=max_history)
        self._last_save_time = 0.0
        self._load()

    def bump(self, name: str, /, **fields):
        """给被动计数器 +1，可带少量上下文字段。

        ⚠️ `name` 是**位置限定参数**（`/` 之后的 `**fields` 才收关键字）。
        这不是风格洁癖：调用方要传的上下文字段里**本身就有 `name`**
        （`tool.emitted` 要记工具名）。原先的 `def bump(self, name, **fields)`
        遇到 `bump("tool.emitted", name="read")` 会抛
        `TypeError: got multiple values for argument 'name'`。

        而且它**不会响** —— 下面整段被 `try/except` 包着，异常被吞掉，
        表现是"这个计数器永远是 0，看着一切正常"。这正是本模块要防的那种故障，
        所以修法是让碰撞**不可能发生**（位置限定），而不是在调用点小心绕开。

        **纯观测：任何异常都必须被吞掉。** 计数器失败绝不能影响业务请求——
        这是本方法里唯一重要的性质（对比：金丝雀把"巡检"做成了业务流量，
        结果是 5 个号被禁言，属于同类错误的另一个极端）。
        """
        try:
            with self._lock:
                self.counters[name] = self.counters.get(name, 0) + 1
                if fields:
                    samples = self.counter_samples.setdefault(
                        name, deque(maxlen=self.MAX_COUNTER_SAMPLES))
                    # 只留标量的短字符串：样本要能进 JSON、也要能直接打印，
                    # 不能把会话对象/账号对象这类活数据塞进去
                    safe = {}
                    for k, v in fields.items():
                        if v is None:
                            continue
                        safe[str(k)] = str(v)[:120]
                    if safe:
                        samples.appendleft({"timestamp": int(time.time()), **safe})
                now = time.time()
                if now - self._last_save_time > 5.0:
                    self._last_save_time = now
                    self.save()
        except Exception as e:
            print(f"[MetricsEngine] 记录计数器 {name} 失败（已忽略）: {e}")

    def get_counters(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.counters)

    def get_counter_samples(self, name: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.counter_samples.get(name, ()))


    def _load(self):
        """服务启动时从 metrics.json 恢复持久化用量统计"""
        if not self.storage_file.exists():
            return
        try:
            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.total_requests = data.get("total_requests", 0)
            self.successful_requests = data.get("successful_requests", 0)
            self.failed_requests = data.get("failed_requests", 0)
            self.total_prompt_tokens = data.get("total_prompt_tokens", 0)
            self.total_cached_tokens = data.get("total_cached_tokens", 0)
            self.total_completion_tokens = data.get("total_completion_tokens", 0)
            self.total_duration_ms = data.get("total_duration_ms", 0.0)
            
            recs = data.get("recent_records", [])
            self.recent_records = deque(recs[:self.max_history], maxlen=self.max_history)

            # 计数器是**累计量**：重启后必须接着数，否则"错位率"每次重启都归零，
            # 而恰恰是"重启前后各出一点"这种形态最容易被看漏。
            raw_counters = data.get("counters") or {}
            if isinstance(raw_counters, dict):
                for k, v in raw_counters.items():
                    try:
                        self.counters[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
            raw_samples = data.get("counter_samples") or {}
            if isinstance(raw_samples, dict):
                for k, v in raw_samples.items():
                    if isinstance(v, list):
                        self.counter_samples[str(k)] = deque(
                            (x for x in v if isinstance(x, dict)),
                            maxlen=self.MAX_COUNTER_SAMPLES)

            print(f"[MetricsEngine] 已从 {self.storage_file} 恢复持久化统计: {self.total_requests} 请求, {self.total_prompt_tokens + self.total_completion_tokens} Tokens")
        except Exception as e:
            print(f"[MetricsEngine] 读取历史持久化文件失败: {e}")

    def save(self):
        """原子化刷盘持久化统计数据"""
        try:
            data = {
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_cached_tokens": self.total_cached_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
                "total_duration_ms": self.total_duration_ms,
                "saved_at": int(time.time()),
                "recent_records": list(self.recent_records),
                # T16 / B1：被动计数器随统计一起落盘（不含任何请求内容，
                # 样本里只有计数器名、时间戳与短标量上下文）
                "counters": dict(self.counters),
                "counter_samples": {
                    k: list(v) for k, v in self.counter_samples.items() if v
                },
            }
            tmp_file = self.storage_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_file.replace(self.storage_file)
        except Exception as e:
            print(f"[MetricsEngine] 保存统计数据失败: {e}")

    def record(
        self,
        endpoint: str,
        model: str,
        status_code: int,
        duration_ms: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        account: Optional[str] = None,
        error: Optional[str] = None,
        attribute_to_key: bool = True,
    ):
        with self._lock:
            self.total_requests += 1
            if 200 <= status_code < 400:
                self.successful_requests += 1
            else:
                self.failed_requests += 1

            self.total_prompt_tokens += prompt_tokens
            self.total_cached_tokens += cached_tokens
            self.total_completion_tokens += completion_tokens
            self.total_duration_ms += duration_ms

            record = {
                "id": self.total_requests,
                "timestamp": int(time.time()),
                "time_str": time.strftime("%H:%M:%S", time.localtime()),
                "endpoint": endpoint,
                "model": model,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 1),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "account": account or "-",
                "error": error or "",
            }
            # T25: 流水里也带上"是哪个 key 发的"，否则排障时
            # 只知道是哪个账号承载的，不知道是谁的请求
            try:
                from app.core.request_context import current_key_label
                record["key"] = current_key_label() or "-"
            except Exception:
                record["key"] = "-"
            self.recent_records.appendleft(record)

            now = time.time()
            if now - self._last_save_time > 15.0 or self.total_requests % 5 == 0:
                self._last_save_time = now
                self.save()

        # T25: 顺带记进"按 API Key"的用量账本。
        # 身份从请求上下文取（见 request_context.py），因此**不需要**在
        # 14 个调用点各加一个 key 参数 —— 也就不会出现"某条退出路径漏统"。
        # 放在锁外调用，避免 metrics 锁与 key_usage 锁交叉。
        #
        # attribute_to_key=False 用于**网关自己挡下的**请求（容量不足、配额超限）。
        # 语义：配额统计的是"你实际消耗了多少服务"，被网关挡回说明没被服务过。
        # 这不是细节 —— 若把被拒的请求也计入配额，一旦触顶，
        # 客户端的每次重试都会继续推高计数，用户无论等多久都恢复不了（永久触顶），
        # 表现为"这把钥废了"。同理，容量紧张时段的 429 也不该吃掉用户的日配额。
        if attribute_to_key:
            try:
                from app.core.request_context import current_key_id, current_key_label
                from app.engine.key_usage import key_usage_engine
                kid = current_key_id()
                if kid:
                    key_usage_engine.record(
                        key_id=kid,
                        label=current_key_label(),
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cached_tokens=cached_tokens,
                        duration_ms=duration_ms,
                        ok=(200 <= status_code < 400),
                        error=error or "",
                    )
            except Exception as ex:
                # 观测失败绝不能影响业务请求
                print(f"[KeyUsage] 记录按 key 用量失败（已忽略）: {ex}")

    def set_historical_totals(
        self,
        total_prompt_tokens: int,
        total_completion_tokens: int,
        total_requests: Optional[int] = None,
    ):
        """历史数据重新核算后的校准接口"""
        with self._lock:
            if total_requests is not None and total_requests > self.total_requests:
                self.total_requests = total_requests
            self.total_prompt_tokens = total_prompt_tokens
            self.total_completion_tokens = total_completion_tokens
            self.save()
            print(f"[MetricsEngine] 历史 Token 统计已校正: prompt={total_prompt_tokens}, completion={total_completion_tokens}, total={total_prompt_tokens + total_completion_tokens}")

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            avg_duration = (
                round(self.total_duration_ms / self.total_requests, 1)
                if self.total_requests > 0
                else 0.0
            )
            success_rate = (
                round((self.successful_requests / self.total_requests) * 100, 1)
                if self.total_requests > 0
                else 100.0
            )
            cache_hit_rate = (
                round((self.total_cached_tokens / self.total_prompt_tokens) * 100, 1)
                if self.total_prompt_tokens > 0
                else 0.0
            )
            return {
                "uptime_seconds": int(time.time() - self.start_time),
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "success_rate": success_rate,
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_cached_tokens": self.total_cached_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
                "cache_hit_rate": cache_hit_rate,
                "avg_duration_ms": avg_duration,
                "recent_records": list(self.recent_records),
                # T16 / B1：被动指标。管理页与 tools/passive_report.py 都读这里，
                # 所以排查时不需要再去 grep 3GB 的 journal。
                "counters": dict(self.counters),
            }


metrics_engine = MetricsEngine()
