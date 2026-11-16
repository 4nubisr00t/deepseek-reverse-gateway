#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proxy_manager.py - 代理健康检测与被动 Mute 切换引擎
1. 监控本地 Mihomo (127.0.0.1:9090) 节点延迟
2. 维护健康的可用代理节点列表 (healthy_nodes)
3. 当被动检测到账号禁言 (is_muted=True) 时，原子切换到下一个优质独立 IP 节点，阻断连坐
4. 常规业务报错 (429/502) 保持当前 IP，不触发切换，保证毫秒级丝滑换号
"""
import asyncio
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional, List, Dict, Any, TypeVar

T = TypeVar("T")

MIHOMO_CONTROLLER = os.environ.get("DSG_MIHOMO_CONTROLLER", "http://127.0.0.1:9090")
PROXY_SELECTOR = os.environ.get("DSG_MIHOMO_SELECTOR", "节点选择")
_DEFAULT_SELECTOR_CANDIDATES = (
    PROXY_SELECTOR,
    "网关故障转移",
    "节点选择",
    "🚀 节点选择",
    "注册专用海外",
    "GLOBAL",
)
_PSEUDO_NODE_PREFIXES = ("剩余流量", "套餐到期", "过期时间", "官网", "网址")
_PSEUDO_NODE_EXACT = frozenset({"DIRECT", "REJECT", "GLOBAL", "自动选择", "网关自动选择"})


def _is_real_proxy_node(name: str) -> bool:
    s = (name or "").strip()
    if not s or s in _PSEUDO_NODE_EXACT:
        return False
    return not any(s.startswith(pfx) for pfx in _PSEUDO_NODE_PREFIXES)


def _selector_for_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """根据代理端口推断最匹配的 Mihomo Selector 组名。仅对明确绑定 Selector 的本机 Mihomo 端口返回组名。"""
    if not proxy_url:
        return None
    try:
        parsed = urllib.parse.urlparse(proxy_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return None
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    if port == 7892:
        return "注册专用海外"
    if port == 7890:
        return "节点选择"
    # 7891 绑定 Mihomo fallback/url-test 自动故障转移组，无需也不可通过 Selector PUT 切换
    return None


def is_proxy_transport_error(exc: Exception) -> bool:
    """判断异常是否属于代理链路/网络传输级故障（非业务逻辑或账号鉴权失败）。"""
    if exc is None:
        return False
    cls_name = type(exc).__name__.lower()
    if any(k in cls_name for k in (
        "proxy", "connection", "connect", "timeout", "timedout",
        "ssl", "tls", "socket", "network", "dns", "brokenpipe", "eof",
    )):
        return True
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return True
    msg = str(exc).lower()
    transport_hints = (
        "proxy",
        "tunnel",
        "timed out",
        "timeout",
        "failed to connect",
        "could not connect",
        "connection refused",
        "connection reset",
        "reset by peer",
        "recv failure",
        "empty reply from server",
        "couldn't connect to server",
        "could not resolve host",
        "name resolution",
        "eof occurred",
        "ssl connect error",
        "bad gateway",
        "socks",
        "curl: (5)",
        "curl: (6)",
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (52)",
        "curl: (56)",
        "curl: (97)",
    )
    return any(h in msg for h in transport_hints)


class ProxyManager:
    def __init__(self, controller_url: str = MIHOMO_CONTROLLER, selector_name: str = PROXY_SELECTOR):
        self.controller_url = controller_url.rstrip("/")
        self.selector_name = selector_name
        self.healthy_nodes: List[Dict[str, Any]] = []
        self._last_test_time = 0.0
        self._last_switch_time = 0.0
        self._current_node = ""
        self._lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    def _fetch_all_proxies(self) -> Dict[str, Any]:
        url = f"{self.controller_url}/proxies"
        with urllib.request.urlopen(url, timeout=3) as r:
            return (json.loads(r.read()) or {}).get("proxies") or {}

    def _resolve_selector_name(self, preferred: Optional[str] = None, all_proxies: Optional[Dict[str, Any]] = None) -> str:
        try:
            proxies_map = all_proxies if all_proxies is not None else self._fetch_all_proxies()
        except Exception:
            return preferred or self.selector_name
        candidates: List[str] = []
        for item in (preferred, self.selector_name, *_DEFAULT_SELECTOR_CANDIDATES):
            if item and item not in candidates:
                candidates.append(item)
        for cand in candidates:
            info = proxies_map.get(cand)
            if isinstance(info, dict) and info.get("type") in ("Selector", "Fallback", "URLTest"):
                if info.get("type") == "Selector":
                    return cand
        for cand in candidates:
            if cand in proxies_map:
                return cand
        return preferred or self.selector_name

    def get_current_node(self, selector: Optional[str] = None) -> str:
        try:
            target = self._resolve_selector_name(preferred=selector)
            url = f"{self.controller_url}/proxies/{urllib.parse.quote(target)}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
            self._current_node = data.get("now", "")
            return self._current_node
        except Exception:
            return self._current_node

    def _probe_healthy_nodes_sync(self, selector: Optional[str] = None, max_timeout: int = 3000) -> List[Dict[str, Any]]:
        now = time.time()
        if not selector and self.healthy_nodes and (now - self._last_test_time) < 45.0:
            return self.healthy_nodes
        try:
            proxies_map = self._fetch_all_proxies()
            target = self._resolve_selector_name(preferred=selector, all_proxies=proxies_map)
            data = proxies_map.get(target) or {}
            all_nodes = [x for x in data.get("all", []) if _is_real_proxy_node(x)]
        except Exception:
            return []

        results = []
        for node in all_nodes[:20]:
            try:
                t_url = (
                    f"{self.controller_url}/proxies/{urllib.parse.quote(node)}"
                    f"/delay?timeout={max_timeout}&url=http://www.gstatic.com/generate_204"
                )
                with urllib.request.urlopen(t_url, timeout=max_timeout / 1000 + 1) as tr:
                    d = json.loads(tr.read()).get("delay")
                    if d and d > 0:
                        results.append({"name": node, "delay": d})
            except Exception:
                pass
        results.sort(key=lambda x: x["delay"])
        if results and not selector:
            self.healthy_nodes = results
            self._last_test_time = now
        return results

    def switch_next_healthy_node_sync(
        self,
        selector: Optional[str] = None,
        min_interval_sec: float = 15.0,
    ) -> Optional[str]:
        """同步节流切换 Mihomo Selector 至下一个健康节点（供代理传输失败时自愈使用）。"""
        if not self._sync_lock.acquire(blocking=False):
            return None
        try:
            now = time.time()
            if min_interval_sec > 0 and (now - self._last_switch_time) < min_interval_sec:
                return None
            proxies_map = self._fetch_all_proxies()
            target = self._resolve_selector_name(preferred=selector, all_proxies=proxies_map)
            info = proxies_map.get(target) or {}
            if info.get("type") != "Selector":
                return None
            cur = info.get("now", "")
            # 先尝试已测延迟缓存或快速探测前几个真实节点
            all_nodes = [x for x in info.get("all", []) if _is_real_proxy_node(x) and x != cur]
            if not all_nodes:
                return None
            chosen = None
            chosen_delay = None
            for node in all_nodes[:10]:
                try:
                    t_url = (
                        f"{self.controller_url}/proxies/{urllib.parse.quote(node)}"
                        f"/delay?timeout=2000&url=http://www.gstatic.com/generate_204"
                    )
                    with urllib.request.urlopen(t_url, timeout=2.5) as tr:
                        d = json.loads(tr.read()).get("delay")
                        if d and d > 0:
                            chosen = node
                            chosen_delay = d
                            break
                except Exception:
                    continue
            if not chosen:
                chosen = all_nodes[0]
            url = f"{self.controller_url}/proxies/{urllib.parse.quote(target)}"
            payload = json.dumps({"name": chosen}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status in (200, 204):
                    self._last_switch_time = time.time()
                    self._current_node = chosen
                    print(
                        f"[ProxyManager] 🔄 代理异常自愈：组 {target!r} 从 {cur!r} 切换至 {chosen!r}"
                        + (f" (延迟: {chosen_delay}ms)" if chosen_delay else ""),
                        flush=True,
                    )
                    return chosen
        except Exception:
            return None
        finally:
            self._sync_lock.release()
        return None

    async def switch_next_healthy_node(self, selector: Optional[str] = None) -> Optional[str]:
        """被动监听触发：切换至下一个健康的独立 IP 节点"""
        async with self._lock:
            target = self._resolve_selector_name(preferred=selector)
            cur = self.get_current_node(selector=target)
            candidates = await self.probe_healthy_nodes(selector=target)
            avail = [n for n in candidates if n["name"] != cur]
            if not avail:
                avail = candidates

            if not avail:
                print("[ProxyManager] ⚠️ 无可用备选节点，维持当前出口", flush=True)
                return None

            chosen = avail[0]["name"]
            try:
                url = f"{self.controller_url}/proxies/{urllib.parse.quote(target)}"
                payload = json.dumps({"name": chosen}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="PUT"
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    if r.status in (200, 204):
                        print(
                            f"[ProxyManager] 🛡️ 【被动 Mute 触发】已切断原出口 {cur!r} ({target})，"
                            f"无缝切换至健康节点: {chosen!r} (延迟: {avail[0]['delay']}ms)",
                            flush=True
                        )
                        self._current_node = chosen
                        self._last_switch_time = time.time()
                        return chosen
            except Exception as e:
                print(f"[ProxyManager] 切换节点失败: {e}", flush=True)
                return None

    async def probe_healthy_nodes(self, max_timeout: int = 3000, selector: Optional[str] = None) -> List[Dict[str, Any]]:
        """并发测真延迟，返回可用节点列表（按延迟升序）"""
        return await asyncio.to_thread(self._probe_healthy_nodes_sync, selector, max_timeout)


proxy_manager = ProxyManager()


import zlib


class ProxyHealthRegistry:
    """多代理出口健康状态机：追踪代理出口健康度、提供确定性 CRC32 哈希分配与故障转移"""

    def __init__(self):
        self._quarantined_until: Dict[str, float] = {}
        self._fail_counts: Dict[str, int] = {}

    def mark_proxy_suspect(self, proxy_url: Optional[str], cooldown_sec: float = 120.0, reason: str = "error"):
        if not proxy_url:
            return
        cnt = self._fail_counts.get(proxy_url, 0) + 1
        self._fail_counts[proxy_url] = cnt
        until = time.time() + cooldown_sec * min(cnt, 5)
        self._quarantined_until[proxy_url] = max(self._quarantined_until.get(proxy_url, 0.0), until)
        print(f"[ProxyRegistry] 🛡️ 代理出口 {proxy_url} 标记冷却 {until - time.time():.0f}s (原因: {reason}, 累计异常: {cnt})", flush=True)

    def mark_proxy_healthy(self, proxy_url: Optional[str]):
        if not proxy_url:
            return
        self._fail_counts.pop(proxy_url, None)
        self._quarantined_until.pop(proxy_url, None)

    def is_healthy(self, proxy_url: str) -> bool:
        until = self._quarantined_until.get(proxy_url, 0.0)
        if until and time.time() < until:
            return False
        if until:
            self._quarantined_until.pop(proxy_url, None)
        return True

    def select_healthy_proxy(
        self,
        account_label: str,
        proxies_pool: List[str],
        default_proxy: Optional[str] = None,
        avoid_proxy: Optional[str] = None,
    ) -> Optional[str]:
        """确定性 CRC32 代理分配（跨重启稳定），自动剔除处于隔离期或需规避的代理节点"""
        if proxies_pool:
            candidates = [
                px for px in proxies_pool
                if px != avoid_proxy and self.is_healthy(px)
            ]
            if not candidates:
                candidates = [px for px in proxies_pool if px != avoid_proxy] or list(proxies_pool)
            idx = zlib.crc32(account_label.encode("utf-8")) % len(candidates)
            return candidates[idx]
        if default_proxy and default_proxy != avoid_proxy and self.is_healthy(default_proxy):
            return default_proxy
        return default_proxy


proxy_health_registry = ProxyHealthRegistry()


def _load_proxy_settings(for_relogin: bool = False) -> Dict[str, Any]:
    try:
        from app.config import config_manager
        cfg = config_manager.config
        proxies = list(getattr(cfg, "proxies", None) or [])
        relogin_proxies = list(getattr(cfg, "relogin_proxies", None) or [])
        default_proxy = getattr(cfg, "proxy", None) or None
        allow_direct = bool(getattr(cfg, "allow_direct_fallback_on_proxy_error", False))
        pool = relogin_proxies + [p for p in proxies if p not in relogin_proxies] if for_relogin else proxies
        return {
            "pool": [p for p in pool if p],
            "default_proxy": default_proxy,
            "allow_direct": allow_direct,
        }
    except Exception:
        return {"pool": [], "default_proxy": None, "allow_direct": False}


def resolve_egress_candidates(
    primary_proxy: Optional[str] = None,
    account_label: str = "",
    include_direct_fallback: Optional[bool] = None,
    for_relogin: bool = False,
) -> List[Optional[str]]:
    """
    构造有序代理故障转移候选列表：
      1. 优先尝试健康的 primary_proxy / 确定性选中的健康池代理
      2. 其次尝试池内其余健康备用代理
      3. 若健康池全部异常，回退尝试已冷却代理（优先不走国内直连）
      4. 仅当配置开启 `allow_direct_fallback_on_proxy_error` 时，在所有代理均失败后追加 `None` (本机直连终极兜底)
      5. 若系统未配置任何代理，直接返回 `[None]`
    """
    settings = _load_proxy_settings(for_relogin=for_relogin)
    pool: List[str] = settings["pool"]
    default_proxy: Optional[str] = settings["default_proxy"]
    allow_direct = settings["allow_direct"] if include_direct_fallback is None else bool(include_direct_fallback)

    ordered: List[str] = []
    if primary_proxy:
        ordered.append(primary_proxy)

    if pool:
        first_pool = proxy_health_registry.select_healthy_proxy(
            account_label or "default",
            pool,
            default_proxy=default_proxy,
            avoid_proxy=primary_proxy if (primary_proxy and not proxy_health_registry.is_healthy(primary_proxy)) else None,
        )
        if first_pool and first_pool not in ordered:
            ordered.append(first_pool)
        # 为剩余池内代理做确定性旋转，避免所有账号在同一时刻同时挤向同一个备胎端口
        start_idx = zlib.crc32((account_label or "default").encode("utf-8")) % len(pool)
        rotated = pool[start_idx:] + pool[:start_idx]
        for px in rotated:
            if px and px not in ordered:
                ordered.append(px)

    if default_proxy and default_proxy not in ordered:
        ordered.append(default_proxy)

    if not ordered:
        return [None]

    healthy = [px for px in ordered if proxy_health_registry.is_healthy(px)]
    unhealthy = [px for px in ordered if not proxy_health_registry.is_healthy(px)]
    candidates: List[Optional[str]] = healthy + unhealthy

    if allow_direct:
        candidates.append(None)

    return candidates


def execute_with_proxy_failover(
    fn_for_proxy: Callable[[Optional[str]], T],
    primary_proxy: Optional[str] = None,
    account_label: str = "",
    context: str = "上游请求",
    include_direct_fallback: Optional[bool] = None,
    for_relogin: bool = False,
) -> T:
    """
    使用多级代理故障转移执行同步上游请求：
    仅对代理/网络传输级异常触发隔离冷却、Mihomo 自动切节点与备胎重试；
    业务/鉴权/参数错误直接原样抛出，绝不重复重试。
    """
    candidates = resolve_egress_candidates(
        primary_proxy=primary_proxy,
        account_label=account_label,
        include_direct_fallback=include_direct_fallback,
        for_relogin=for_relogin,
    )
    last_exc: Optional[Exception] = None
    failover_errors: List[str] = []
    total = len(candidates)

    for idx, cand_proxy in enumerate(candidates):
        try:
            result = fn_for_proxy(cand_proxy)
            if cand_proxy:
                proxy_health_registry.mark_proxy_healthy(cand_proxy)
            try:
                from app.engine.drop_probe import note_proxy_outcome
                note_proxy_outcome(
                    primary_proxy=primary_proxy,
                    used_proxy=cand_proxy,
                    attempts=idx + 1,
                    switched=idx > 0,
                    errors=failover_errors,
                    account_label=account_label,
                )
            except Exception:
                pass
            return result
        except Exception as exc:
            last_exc = exc
            failover_errors.append(f"{cand_proxy or 'direct'}:{type(exc).__name__}")
            if not is_proxy_transport_error(exc) or idx >= total - 1:
                try:
                    from app.engine.drop_probe import note_proxy_outcome
                    note_proxy_outcome(
                        primary_proxy=primary_proxy,
                        used_proxy=cand_proxy,
                        attempts=idx + 1,
                        switched=idx > 0,
                        errors=failover_errors,
                        account_label=account_label,
                    )
                except Exception:
                    pass
                raise
            if cand_proxy:
                proxy_health_registry.mark_proxy_suspect(
                    cand_proxy,
                    cooldown_sec=120.0,
                    reason=f"{context}:{type(exc).__name__}",
                )
                sel = _selector_for_proxy_url(cand_proxy)
                if sel:
                    try:
                        proxy_manager.switch_next_healthy_node_sync(selector=sel)
                    except Exception:
                        pass
            nxt = candidates[idx + 1]
            nxt_label = nxt if nxt else "本机直连(终极兜底)"
            cur_label = cand_proxy if cand_proxy else "本机直连"
            print(
                f"[ProxyFailover] ⚠️ {context} 出口 {cur_label} 传输异常 ({type(exc).__name__}: {exc})，"
                f"正在自动切换备选出口 -> {nxt_label} ({idx + 2}/{total})",
                flush=True,
            )

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{context} 无可用出口候选")
