# -*- coding: utf-8 -*-
"""
drop_probe.py — 账号全链路请求探针与掉号特征抓包器
================================================================================
用途：
  在高风险账号（如刚注册/刚激活且易触发 INVALID_TOKEN / biz_code=14 MFA_REQUIRED 的账号）
  跑压测或日常流量时，记录每一步对 DeepSeek 上游发起的请求特征，自动比对：
    - Token 年龄 (token_age_s)、单 Token 累计请求序号 (req_seq)、距上一次请求间隔 (dt_since_last_req_s)
    - 实际命中出口代理 (proxy_used) 与是否发生“单次内故障切换 (proxy_switched)”
    - 同账号/同 Token 前后两次请求之间的“跨请求代理出口漂移 (proxy_changed_from_prev_req)”
    - 会话复用状态 (reuse_session / session_id)、并发数 (in_flight)、平台伪装 (platform / impersonate)
    - 掉号瞬间及前 N 条请求上下文快照 (drop_snapshot)

存储：
  - 滚动追加写入 data/drop_probe.jsonl（超 20MB 自动轮转 drop_probe.jsonl.1）
  - 内存保留每账号最近 40 条事件 + 最近 10 次掉号快照，供 /api/system/drop_probe 实时查看
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

_BASE_DIR = Path(__file__).resolve().parents[2]
_PROBE_PATH = _BASE_DIR / "data" / "drop_probe.jsonl"
_MAX_FILE_BYTES = 20 * 1024 * 1024

_LOCK = threading.Lock()
_CTX: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar("_DROP_PROBE_CTX", default=None)

_ACCOUNT_STATE: Dict[str, Dict[str, Any]] = {}
_ACCOUNT_HISTORY: Dict[str, Deque[Dict[str, Any]]] = {}
_DROP_SNAPSHOTS: Deque[Dict[str, Any]] = deque(maxlen=30)
_TOKEN_OWNER_MAP: Dict[str, Tuple[str, float]] = {}

_DROP_ERROR_KINDS = frozenset({
    "auth_expired",
    "mfa_required",
    "invalid",
    "permanent_banned",
    "relogin_fail",
    "mark_mfa_required",
})


def _hash8(val: str) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:8]


def register_account_token(
    account_label: str,
    token: str,
    token_updated_at: Optional[float] = None,
) -> None:
    """建立 token_sha8 -> (account_label, token_updated_at) 映射，保证异步流/后台任务自动归因账号。"""
    lbl = str(account_label or "").strip()
    t8 = _hash8(token)
    if not lbl or not t8:
        return
    with _LOCK:
        _TOKEN_OWNER_MAP[t8] = (lbl, float(token_updated_at or 0.0))
        if len(_TOKEN_OWNER_MAP) > 512:
            # 清理最旧一半条目
            for old_k in list(_TOKEN_OWNER_MAP.keys())[:256]:
                _TOKEN_OWNER_MAP.pop(old_k, None)


def _norm_proxy(proxy_url: Optional[str]) -> str:
    return str(proxy_url).strip() if proxy_url else "direct"


@contextlib.contextmanager
def bind_request_context(
    account: Optional[Any] = None,
    *,
    account_label: str = "",
    token: str = "",
    token_updated_at: Optional[float] = None,
    caller_info: Optional[Dict[str, Any]] = None,
    model: str = "",
    session_id: str = "",
    reuse_session: Optional[bool] = None,
    in_flight: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """在请求生命周期内绑定当前账号上下文（支持 asyncio.to_thread 自动透传 ContextVar）。"""
    lbl = str(account_label or getattr(account, "account_label", "") or getattr(account, "email", "") or "").strip()
    tok = str(token or getattr(account, "token", "") or "").strip()
    tok_upd = token_updated_at if token_updated_at is not None else getattr(account, "token_updated_at", None)
    cur_in_flight = in_flight if in_flight is not None else getattr(account, "in_flight", None)
    cinfo = caller_info or {}

    ctx_dict: Dict[str, Any] = {
        "account": lbl,
        "token_sha8": _hash8(tok),
        "token_updated_at": float(tok_upd or 0.0),
        "caller_owner": str(cinfo.get("owner") or ""),
        "caller_key": str(cinfo.get("key_name") or ""),
        "model": str(model or ""),
        "session_id": str(session_id or "")[:16],
        "reuse_session": reuse_session,
        "in_flight": cur_in_flight,
        "proxy_primary": "",
        "proxy_used": "",
        "proxy_switched": False,
        "failover_attempts": 0,
        "failover_errors": [],
    }
    tok_reset = _CTX.set(ctx_dict)
    try:
        yield ctx_dict
    finally:
        _CTX.reset(tok_reset)


def _ensure_ctx() -> Dict[str, Any]:
    ctx = _CTX.get()
    if ctx is None:
        ctx = {
            "account": "",
            "token_sha8": "",
            "token_updated_at": 0.0,
            "caller_owner": "",
            "caller_key": "",
            "model": "",
            "session_id": "",
            "reuse_session": None,
            "in_flight": None,
            "proxy_primary": "",
            "proxy_used": "",
            "proxy_switched": False,
            "failover_attempts": 0,
            "failover_errors": [],
        }
        _CTX.set(ctx)
    return ctx


def update_context(**fields: Any) -> None:
    ctx = _ensure_ctx()
    for k, v in fields.items():
        if k == "session_id" and v:
            ctx[k] = str(v)[:16]
        elif k == "token" and v:
            ctx["token_sha8"] = _hash8(str(v))
        else:
            ctx[k] = v


def note_proxy_outcome(
    *,
    primary_proxy: Optional[str],
    used_proxy: Optional[str],
    attempts: int,
    switched: bool,
    errors: Optional[List[str]] = None,
    account_label: str = "",
) -> None:
    ctx = _ensure_ctx()
    ctx["proxy_primary"] = _norm_proxy(primary_proxy)
    ctx["proxy_used"] = _norm_proxy(used_proxy)
    ctx["failover_attempts"] = int(attempts or 1)
    ctx["proxy_switched"] = bool(switched)
    if errors:
        ctx["failover_errors"] = list(errors)[-3:]
    if account_label and not ctx.get("account"):
        ctx["account"] = str(account_label).strip()


def _write_jsonl_locked(ev: Dict[str, Any]) -> None:
    try:
        _PROBE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _PROBE_PATH.exists() and _PROBE_PATH.stat().st_size > _MAX_FILE_BYTES:
            bak = _PROBE_PATH.with_suffix(".jsonl.1")
            if bak.exists():
                bak.unlink()
            _PROBE_PATH.replace(bak)
        with _PROBE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def record_event(
    stage: str,
    *,
    account: str = "",
    token: str = "",
    token_updated_at: Optional[float] = None,
    platform: str = "",
    impersonate: str = "",
    has_cookie: Optional[bool] = None,
    proxy_primary: Optional[str] = None,
    proxy_used: Optional[str] = None,
    http_status: Optional[int] = None,
    code: Optional[Any] = None,
    biz_code: Optional[Any] = None,
    msg: str = "",
    error_kind: str = "",
    latency_ms: Optional[float] = None,
    snippet: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """记录单条账号请求或状态跃迁特征；遇掉号关键字自动冻结近期序列快照。"""
    now = time.time()
    ctx = _CTX.get() or {}

    tok_sha = _hash8(token) if token else str(ctx.get("token_sha8") or "")
    ctx_same_tok = (not tok_sha) or (not ctx.get("token_sha8")) or (tok_sha == ctx.get("token_sha8"))
    acc = str(account or (ctx.get("account") if ctx_same_tok else "") or "").strip()
    tok_upd = float(
        token_updated_at
        if token_updated_at is not None
        else ((ctx.get("token_updated_at") if ctx_same_tok else 0.0) or 0.0)
    )
    if (not acc or tok_upd <= 0.0) and tok_sha:
        mapped = _TOKEN_OWNER_MAP.get(tok_sha)
        if mapped:
            if not acc:
                acc = mapped[0]
            if tok_upd <= 0.0 and mapped[1] > 0.0:
                tok_upd = mapped[1]
    acc = acc or "_unbound_"
    eff_proxy_used = (
        _norm_proxy(proxy_used)
        if proxy_used is not None
        else str(ctx.get("proxy_used") or _norm_proxy(proxy_primary))
    )
    eff_proxy_primary = (
        _norm_proxy(proxy_primary)
        if proxy_primary is not None
        else str(ctx.get("proxy_primary") or eff_proxy_used)
    )

    with _LOCK:
        st = _ACCOUNT_STATE.setdefault(
            acc,
            {
                "last_token_sha8": "",
                "req_seq": 0,
                "last_req_ts": 0.0,
                "last_proxy_used": "",
                "proxy_exits": {},
                "proxy_drift_count": 0,
                "last_error": None,
            },
        )
        if tok_sha and tok_sha != st["last_token_sha8"]:
            st["last_token_sha8"] = tok_sha
            st["req_seq"] = 0
            st["last_proxy_used"] = ""
        st["req_seq"] += 1

        dt_prev = round(now - st["last_req_ts"], 3) if st["last_req_ts"] > 0 else None
        st["last_req_ts"] = now

        prev_proxy = st["last_proxy_used"]
        proxy_drifted = bool(prev_proxy and eff_proxy_used and prev_proxy != eff_proxy_used)
        if proxy_drifted:
            st["proxy_drift_count"] += 1
        if eff_proxy_used:
            st["last_proxy_used"] = eff_proxy_used
            exits = st["proxy_exits"]
            exits[eff_proxy_used] = int(exits.get(eff_proxy_used, 0)) + 1

        token_age_s = round(now - tok_upd, 1) if tok_upd > 0 else None
        ev: Dict[str, Any] = {
            "ts": round(now, 3),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "account": acc,
            "stage": str(stage),
            "req_seq": st["req_seq"],
            "dt_prev_s": dt_prev,
            "token_sha8": tok_sha,
            "token_age_s": token_age_s,
            "platform": platform,
            "impersonate": impersonate,
            "has_cookie": has_cookie,
            "caller_owner": ctx.get("caller_owner") or "",
            "caller_key": ctx.get("caller_key") or "",
            "model": ctx.get("model") or "",
            "session_id": ctx.get("session_id") or "",
            "reuse_session": ctx.get("reuse_session"),
            "in_flight": ctx.get("in_flight"),
            "proxy_primary": eff_proxy_primary,
            "proxy_used": eff_proxy_used,
            "proxy_switched": bool(ctx.get("proxy_switched")),
            "proxy_changed_from_prev_req": proxy_drifted,
            "failover_attempts": int(ctx.get("failover_attempts") or 0),
            "http_status": http_status,
            "code": code,
            "biz_code": biz_code,
            "msg": str(msg or "")[:160],
            "error_kind": str(error_kind or ""),
            "latency_ms": round(float(latency_ms), 1) if latency_ms is not None else None,
        }
        if ctx.get("failover_errors"):
            ev["failover_errors"] = ctx["failover_errors"]
        if snippet:
            ev["snippet"] = str(snippet)[:280]
        if extra:
            ev["extra"] = extra

        hist = _ACCOUNT_HISTORY.setdefault(acc, deque(maxlen=40))
        hist.append(ev)

        if ev["error_kind"] in _DROP_ERROR_KINDS or stage in _DROP_ERROR_KINDS or ev["biz_code"] == 14:
            st["last_error"] = {
                "iso": ev["iso"],
                "stage": ev["stage"],
                "error_kind": ev["error_kind"],
                "biz_code": ev["biz_code"],
                "code": ev["code"],
                "http_status": ev["http_status"],
                "proxy_used": ev["proxy_used"],
                "token_age_s": ev["token_age_s"],
                "req_seq": ev["req_seq"],
            }
            snap = {
                "iso": ev["iso"],
                "account": acc,
                "trigger_stage": stage,
                "error_kind": ev["error_kind"],
                "biz_code": ev["biz_code"],
                "token_age_s": token_age_s,
                "req_seq": st["req_seq"],
                "proxy_drift_count": st["proxy_drift_count"],
                "proxy_exits": dict(st["proxy_exits"]),
                "trail": list(hist)[-15:],
            }
            _DROP_SNAPSHOTS.append(snap)
            _write_jsonl_locked({"type": "drop_snapshot", **snap})

        _write_jsonl_locked(ev)
        return ev


def get_probe_report(account: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
    """返回每账号抓包汇总与最近掉号轨迹快照。"""
    lim = max(1, min(200, int(limit or 40)))
    with _LOCK:
        targets = [account] if (account and account in _ACCOUNT_STATE) else list(_ACCOUNT_STATE.keys())
        accounts_out: Dict[str, Any] = {}
        for acc in targets:
            st = _ACCOUNT_STATE[acc]
            hist = list(_ACCOUNT_HISTORY.get(acc) or ())
            accounts_out[acc] = {
                "req_seq": st["req_seq"],
                "last_token_sha8": st["last_token_sha8"],
                "last_proxy_used": st["last_proxy_used"],
                "proxy_exits": dict(st["proxy_exits"]),
                "proxy_drift_count": st["proxy_drift_count"],
                "last_error": st["last_error"],
                "recent_events": hist[-lim:],
            }
        snaps = [s for s in _DROP_SNAPSHOTS if (not account or s.get("account") == account)][-20:]
        return {
            "log_file": str(_PROBE_PATH),
            "account_count": len(accounts_out),
            "accounts": accounts_out,
            "drop_snapshots": snaps,
        }
