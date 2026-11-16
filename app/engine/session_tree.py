"""
拟人会话树状态机 (Session Tree State Machine)
- 基于上下文指纹识别客户端会话
- 真实维护 parent_message_id 树状递增链条与多分支 DAG 状态图 (Multi-Branch Session Tree)
- 支持 Agent 检查点回滚 (Checkpoint Rollback)、单轮重试生成 (Regenerate) 与多平行分支来回切换
- 杜绝覆盖根节点 (parent_message_id: None) 与特殊 Token 注入
- 会话亲和（T1）：会话身份按"分桶 + 消息前缀"解析，且**绑定与分支树节点跨重启持久化**

────────────────────────────────────────────────────────────────────
T1：为什么不能只用"首条用户消息的哈希"当会话身份
────────────────────────────────────────────────────────────────────
旧实现把 `hash(第一条用户消息)` 直接当会话标识，注册时 `self._sessions[key] = ctx`
是**覆盖**语义。于是只要两个无关会话的首条用户消息相同（例如都从「继续」开始）：

    会话 A 第 1 轮 → 建上游会话 S_A，绑定 K → S_A
    会话 B 第 1 轮 → 建上游会话 S_B，绑定 K → S_B   ← S_A 的绑定被覆盖
    会话 A 第 2 轮 → 命中 K → 拿到 S_B
                     → 按"已有会话"只发送 A 的最新一句
                     → **模型在 B 的上下文里回答 A 的问题**

修法（宁可多建一次会话，也绝不把别人的上下文混进来）：
  · 按 `hash(第一条用户消息)` 分**桶**
  · 桶内每个会话记住自己的 **消息前缀指纹**（从首条用户消息起，最多 8 条用于桶指纹，全量 256 条用于会话树）
  · 解析时，只有 **恰好一个** 绑定的前缀（或分支检查点）匹配当前请求 → 才复用
  · 不唯一（0 个或多个）→ **新建上游会话**（fail-safe）

────────────────────────────────────────────────────────────────────
会话树分叉（Branching Session Tree / LCA Checkpoint Graph）
────────────────────────────────────────────────────────────────────
现代 AI Agent（Claude Code、Cline、LangGraph、Dify）与对话客户端（LobeChat、NextChat、
Cherry Studio）普遍支持「从历史第 K 轮分叉 / 编辑历史消息重发 / 回滚检查点」。
DeepSeek 网页端原生协议 (`/api/v0/chat/completion`) 本质上就是一棵以 `parent_message_id`
连接的 **有向树 (Message Tree)**：同一个 `chat_session_id` 下，只要把 `parent_message_id`
指向历史第 K 轮的 `message_id`，上游就会自动沿着 `Root -> ... -> Turn K` 的祖先链条推理，
并从第 K 轮长出一条全新的兄弟分支（Sibling Branch），100% 命中前 K 轮的 KV Cache！

本模块在 `SessionContext` 内维护多分支检查点图 (`tree_nodes`)：
  1. 每一轮对话完成 (`update_message_id`) 时，将该轮完整请求签名链 `sigs` 与上游返回的
     `message_id`、父节点 `parent_message_id`、轮次深度 `turn_count` 登记为树节点。
  2. 当收到分叉或回滚请求时，通过 **最长公共前缀祖先匹配 (LCA / Longest Common Prefix)**
     在 `tree_nodes` 中定位分叉点 K，将 `last_message_id` 精准回溯至第 K 轮的 `message_id`，
     在**同一个上游会话**内原地分叉，无需销毁重建会话、无需重传全量历史。
  3. 同时彻底消除旧版 `PREFIX_MAX_MESSAGES = 8` 造成的「第 4 轮之后回滚失明」缺陷。
"""

import hashlib
import json
import os
import re
import time
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from app.config import PROJECT_ROOT, config_manager
from app.engine.reasoning_director import ReasoningDirector

BINDINGS_FILE = Path(os.environ.get("DSG_BINDINGS_PATH", str(PROJECT_ROOT / "bindings.json")))
# 前缀指纹最多记几条消息（保持 8 以兼容既有契约测试）
PREFIX_MAX_MESSAGES = 8
# 会话树全视野签名上限：覆盖最多 256 条有效消息（约 128 轮对话），消除第 4 轮后的回滚盲区
TREE_MAX_MESSAGES = 256
# 单会话最多保存的分支检查点节点数（超出按时间戳 LRU 淘汰）
MAX_TREE_NODES_PER_SESSION = 64
# Responses API previous_response_id 索引容量上限
MAX_RESPONSE_ID_MAPPINGS = 2048
# 每个桶最多留几个绑定（同开头会话太多时按最近活跃淘汰，防止无界增长）
MAX_BINDINGS_PER_BUCKET = 16
# 绑定落盘节流
_BINDINGS_FLUSH_INTERVAL = float(os.environ.get("DSG_BINDINGS_FLUSH_INTERVAL", "30"))

_SESSION_ID_HEADERS = ("x-session-affinity", "x-session-id", "session_id", "conversation-id")
_PARENT_MESSAGE_ID_HEADERS = ("x-parent-message-id", "x-branch-parent-id")


def extract_client_session_id(headers, body=None) -> Tuple[Optional[str], str]:
    """从请求里取客户端提供的会话标识，返回 (id, 来源标记)。"""
    def _get(name):
        try:
            v = headers.get(name)
        except Exception:
            return None
        if v:
            return v
        try:
            for k in headers.keys():
                if str(k).lower() == name:
                    return headers[k]
        except Exception:
            pass
        return None

    for name in _SESSION_ID_HEADERS:
        v = _get(name)
        if v:
            return str(v).strip()[:128], f"header:{name}"
    if isinstance(body, dict):
        v = body.get("prompt_cache_key")
        if v:
            return str(v).strip()[:128], "body:prompt_cache_key"
    return None, "none"


def extract_explicit_parent_id(headers, body=None) -> Optional[str]:
    """从请求头或请求体提取客户端显式指定的分支父节点 message_id（可选高阶特性）。"""
    def _get(name):
        try:
            v = headers.get(name)
            if v:
                return v
            for k in headers.keys():
                if str(k).lower() == name:
                    return headers[k]
        except Exception:
            pass
        return None

    for name in _PARENT_MESSAGE_ID_HEADERS:
        v = _get(name)
        if v:
            return str(v).strip()[:128]
    if isinstance(body, dict):
        v = body.get("parent_message_id")
        if v:
            return str(v).strip()[:128]
    return None


class SessionContext:
    def __init__(self, conversation_id: str, account_label: str, deepseek_session_id: str, model_type: str = "default"):
        self.conversation_id = conversation_id
        self.account_label = account_label
        self.deepseek_session_id = deepseek_session_id
        self.model_type = model_type
        self.last_message_id: Optional[str] = None
        self.turn_count: int = 0
        self.created_at: float = time.time()
        self.last_active: float = time.time()
        self.file_ids: List[str] = []
        # T1: 消息前缀指纹（从首条用户消息起，最多 PREFIX_MAX_MESSAGES 条）
        self.prefix: List[str] = []
        self.bucket: str = ""
        # ── 会话树分叉 (Multi-Branch Session Tree) 状态 ──
        # 当前活跃分支的完整消息签名序列（最多 TREE_MAX_MESSAGES 条）
        self.active_sigs: List[str] = []
        # 正在执行中的本轮请求签名序列与父节点（等待 update_message_id 提交为检查点）
        self._pending_sigs: List[str] = []
        self._pending_parent_id: Optional[str] = None
        # 检查点映射表: sig_chain_hash -> {"sigs": [...], "message_id": str, "parent_message_id": str|None, "turn_count": int, "timestamp": float}
        self.tree_nodes: Dict[str, Dict[str, Any]] = {}
        # 累计触发分支回溯/分叉的次数
        self.branch_count: int = 0

    @staticmethod
    def _sig_chain_key(sigs: List[str]) -> str:
        """计算消息签名序列的确定性哈希键。"""
        raw = "\x1f".join(sigs)
        return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:20]

    def record_checkpoint(
        self,
        sigs: List[str],
        message_id: str,
        parent_message_id: Optional[str] = None,
        turn_count: Optional[int] = None,
    ) -> str:
        """将当前轮次（请求签名链 -> 上游返回的 assistant message_id）持久化为会话树分叉检查点。"""
        if not sigs or not message_id:
            return ""
        node_key = self._sig_chain_key(sigs)
        now = time.time()
        self.tree_nodes[node_key] = {
            "sigs": list(sigs),
            "message_id": str(message_id),
            "parent_message_id": str(parent_message_id) if parent_message_id is not None else None,
            "turn_count": int(turn_count if turn_count is not None else self.turn_count),
            "timestamp": now,
        }
        if len(self.tree_nodes) > MAX_TREE_NODES_PER_SESSION:
            sorted_keys = sorted(
                self.tree_nodes.keys(),
                key=lambda k: float(self.tree_nodes[k].get("timestamp") or 0.0),
            )
            for k in sorted_keys[: len(self.tree_nodes) - MAX_TREE_NODES_PER_SESSION]:
                self.tree_nodes.pop(k, None)
        return node_key

    def find_branch_checkpoint(
        self, incoming_sigs: List[str]
    ) -> Optional[Tuple[Optional[str], int, str, int]]:
        """在会话树检查点图中寻找与 incoming_sigs 匹配的最深公共祖先 (LCA)。

        返回:
            (target_parent_message_id, target_turn_count, branch_kind, matched_sig_len)
            若未找到可挂载的祖先检查点，返回 None。
        """
        if not incoming_sigs or not self.tree_nodes:
            return None

        # 1. 同轮重试 / 重新生成 (Regenerate / Retry Turn K):
        # 客户端重发了与已提交节点完全相同的消息序列 [U_1, A_1, ..., U_k]
        exact_key = self._sig_chain_key(incoming_sigs)
        exact_node = self.tree_nodes.get(exact_key)
        if exact_node is not None:
            parent_mid = exact_node.get("parent_message_id")
            # 异常防御：若为多轮请求（>2条签名），但检查点记录的 parent_message_id 为 None，
            # 说明该节点由旧版分叉逻辑异常截断遗留，绝不能作为重生成挂载点返回 None 割裂上下文
            if parent_mid or len(incoming_sigs) <= 2:
                prev_turns = max(0, int(exact_node.get("turn_count") or 1) - 1)
                return parent_mid, prev_turns, "regenerate", len(incoming_sigs)

        # 2. 历史检查点分叉 / 回滚 (Branch / Rollback from Turn K < N):
        # 寻找满足 node.sigs 是 incoming_sigs 严格前缀的最长检查点
        best_node: Optional[Dict[str, Any]] = None
        best_len = 0
        for node in self.tree_nodes.values():
            n_sigs = node.get("sigs") or []
            n_len = len(n_sigs)
            if 0 < n_len < len(incoming_sigs) and node.get("message_id"):
                if incoming_sigs[:n_len] == n_sigs:
                    if n_len > best_len or (
                        n_len == best_len
                        and best_node is not None
                        and float(node.get("timestamp") or 0) > float(best_node.get("timestamp") or 0)
                    ):
                        best_len = n_len
                        best_node = node

        if best_node is not None:
            return (
                str(best_node["message_id"]),
                int(best_node.get("turn_count") or 1),
                "rollback-branch",
                best_len,
            )
        return None

    def get_tree_topology(self) -> Dict[str, Any]:
        """导出当前会话树的拓扑结构摘要（用于调试、可观测性与测试验证）。"""
        nodes = []
        for k, v in sorted(self.tree_nodes.items(), key=lambda x: x[1].get("turn_count", 0)):
            nodes.append({
                "node_key": k,
                "message_id": v.get("message_id"),
                "parent_message_id": v.get("parent_message_id"),
                "turn_count": v.get("turn_count"),
                "sig_depth": len(v.get("sigs") or []),
            })
        return {
            "conversation_id": self.conversation_id,
            "deepseek_session_id": self.deepseek_session_id,
            "current_tip_message_id": self.last_message_id,
            "turn_count": self.turn_count,
            "branch_count": self.branch_count,
            "total_checkpoints": len(self.tree_nodes),
            "nodes": nodes,
        }

    def add_file_ids(self, fids: List[str]):
        for fid in fids:
            if fid and fid not in self.file_ids:
                self.file_ids.append(fid)

    def get_file_ids(self) -> List[str]:
        return list(self.file_ids)

    # ── 持久化（T1：绑定与会话树检查点必须跨重启存活） ──

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "account_label": self.account_label,
            "deepseek_session_id": self.deepseek_session_id,
            "model_type": self.model_type,
            "last_message_id": self.last_message_id,
            "turn_count": self.turn_count,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "file_ids": list(self.file_ids),
            "prefix": list(self.prefix),
            "bucket": self.bucket,
            "active_sigs": list(self.active_sigs),
            "tree_nodes": dict(self.tree_nodes),
            "branch_count": self.branch_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionContext":
        ctx = cls(
            str(d.get("conversation_id") or ""),
            str(d.get("account_label") or ""),
            str(d.get("deepseek_session_id") or ""),
            model_type=str(d.get("model_type") or "default"),
        )
        ctx.last_message_id = d.get("last_message_id")
        ctx.turn_count = int(d.get("turn_count") or 0)
        ctx.created_at = float(d.get("created_at") or time.time())
        ctx.last_active = float(d.get("last_active") or time.time())
        ctx.file_ids = [str(x) for x in (d.get("file_ids") or [])]
        ctx.prefix = [str(x) for x in (d.get("prefix") or [])]
        ctx.bucket = str(d.get("bucket") or "")
        raw_active = d.get("active_sigs")
        if isinstance(raw_active, list) and raw_active:
            ctx.active_sigs = [str(x) for x in raw_active]
        elif ctx.prefix:
            ctx.active_sigs = list(ctx.prefix)
        raw_nodes = d.get("tree_nodes")
        if isinstance(raw_nodes, dict):
            ctx.tree_nodes = raw_nodes
        ctx.branch_count = int(d.get("branch_count") or 0)
        return ctx


class SessionTracker:
    """管理客户端会话与 DeepSeek 会话树 (DAG) 的映射与状态"""

    def __init__(self, ttl: int = 86400, max_sessions: int = 1000,
                 storage_file: Optional[Path] = None):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._lock = threading.RLock()
        self._sessions: Dict[str, SessionContext] = {}
        # T1: bucket -> [conv_key, ...]，用于把"同开头的不同会话"区分开
        self._buckets: Dict[str, List[str]] = {}
        # 缓存最近一次 resolve_conversation 计算出的全视野签名链
        self._last_resolved_full_sigs: Dict[str, List[str]] = {}
        # Responses API response_id -> (conv_key, message_id) 映射表，支持 previous_response_id 精确分叉
        self._response_index: Dict[str, Tuple[str, str]] = {}
        self.storage_file = storage_file if storage_file is not None else BINDINGS_FILE
        self._last_flush = 0.0
        self._load_bindings()

    # ── T1: 绑定持久化 ──

    def _load_bindings(self):
        """服务启动时恢复会话绑定与多分支检查点树。"""
        if not self.storage_file.exists():
            return
        try:
            raw = json.loads(self.storage_file.read_text("utf-8"))
            items = raw.get("sessions") if isinstance(raw, dict) else None
            if not isinstance(items, dict):
                return
            now = time.time()
            pending = 0
            for key, d in items.items():
                if not isinstance(d, dict):
                    continue
                if now - float(d.get("last_active") or 0) > self.ttl:
                    continue
                ctx = SessionContext.from_dict(d)
                if not ctx.deepseek_session_id and not ctx.account_label:
                    continue
                if not ctx.deepseek_session_id:
                    pending += 1
                self._sessions[key] = ctx
                if ctx.bucket:
                    self._buckets.setdefault(ctx.bucket, []).append(key)
            resp_idx = raw.get("response_index") if isinstance(raw, dict) else None
            if isinstance(resp_idx, dict):
                for rid, pair in resp_idx.items():
                    if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] in self._sessions:
                        self._response_index[str(rid)] = (str(pair[0]), str(pair[1]))
            print(
                f"[SessionTree] 已恢复 {len(self._sessions)} 条会话绑定"
                f"（其中 {pending} 条已丢弃上游会话、待新建；跨重启保持会话亲和与分支树）"
            )
        except Exception as e:
            print(f"[SessionTree] 读取 {self.storage_file} 失败: {e}")
            self._sessions = {}
            self._buckets = {}

    def _flush_bindings_locked(self, force: bool = False):
        if not force and (time.time() - self._last_flush) < _BINDINGS_FLUSH_INTERVAL:
            return
        try:
            payload = {
                "saved_at": int(time.time()),
                "sessions": {k: v.to_dict() for k, v in self._sessions.items()},
                "response_index": {k: list(v) for k, v in self._response_index.items()},
            }
            tmp = self.storage_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.storage_file)
            self._last_flush = time.time()
        except Exception as e:
            print(f"[SessionTree] 落盘绑定失败: {e}")

    def flush_bindings(self, force: bool = True):
        with self._lock:
            self._flush_bindings_locked(force=force)

    # ── T1: 会话身份解析与签名计算 ──

    @classmethod
    def _message_signature(cls, msg: Dict[str, Any]) -> str:
        """单条消息的稳定指纹。"""
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if role == "user":
            text = cls._clean_user_content(content)
        elif isinstance(content, str):
            text = content
        elif content is None:
            text = ""
        else:
            try:
                text = json.dumps(content, ensure_ascii=False, sort_keys=True)
            except Exception:
                text = str(content)
        h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]
        extra = ""
        if msg.get("tool_call_id"):
            extra = "|tcid:" + str(msg["tool_call_id"])[:24]
        elif msg.get("name"):
            extra = "|n:" + str(msg["name"])[:24]
        elif msg.get("tool_calls"):
            try:
                extra = "|tc:" + hashlib.sha1(
                    json.dumps(msg["tool_calls"], ensure_ascii=False, sort_keys=True)
                    .encode("utf-8", "ignore")
                ).hexdigest()[:12]
            except Exception:
                extra = "|tc:?"
        return f"{role}:{h}{extra}"

    @classmethod
    def _all_signatures(cls, messages: List[Dict[str, Any]], max_messages: int = TREE_MAX_MESSAGES) -> List[str]:
        """从首条用户消息开始提取全视野签名序列（最多 TREE_MAX_MESSAGES 条），用于多分支会话树匹配。"""
        start = -1
        for i, m in enumerate(messages or []):
            if m.get("role") == "user":
                start = i
                break
        if start < 0:
            return []

        sigs: List[str] = []
        for m in messages[start:]:
            if len(sigs) >= max_messages:
                break
            if m.get("role") == "user" and not cls._clean_user_content(m.get("content")):
                continue
            s = cls._message_signature(m)
            if s:
                sigs.append(s)
        return sigs

    @classmethod
    def _prefix_signatures(cls, messages: List[Dict[str, Any]]) -> List[str]:
        """从首条用户消息开始的前缀指纹（最多 PREFIX_MAX_MESSAGES=8 条，保持既有接口契约）。"""
        return cls._all_signatures(messages, max_messages=PREFIX_MAX_MESSAGES)

    def _prune_bucket_locked(self, bucket: str):
        keys = [k for k in self._buckets.get(bucket, []) if k in self._sessions]
        if len(keys) > MAX_BINDINGS_PER_BUCKET:
            keys.sort(key=lambda k: self._sessions[k].last_active)
            for k in keys[:-MAX_BINDINGS_PER_BUCKET]:
                self._sessions.pop(k, None)
            keys = keys[-MAX_BINDINGS_PER_BUCKET:]
        if keys:
            self._buckets[bucket] = keys
        else:
            self._buckets.pop(bucket, None)

    def resolve_conversation(
        self,
        messages: List[Dict[str, Any]],
        model: str = "",
        custom_id: Optional[str] = None,
        allow_reuse: bool = True,
        scope_key: str = "",
    ) -> Tuple[str, str]:
        """解析本次请求属于哪个会话（及哪个会话树分支节点），返回 (conv_key, 来源标记)。

        来源标记 (`conv_src`) 取值：
          - `cid`: 显式会话 ID 线性延续或首次建立
          - `cid-branched`: 显式会话 ID 命中历史检查点并原地分叉/回滚（不销毁会话、不换号）
          - `cid-prefix-changed`: 显式会话 ID 上下文被重写/压缩（无公共祖先检查点，换会话但不换号）
          - `cid-forked`: 显式会话 ID 达到最大轮数自动分叉
          - `prefix`: 无显式 ID，桶内唯一前缀线性命中
          - `prefix-branched`: 无显式 ID，桶内唯一命中历史分支检查点并原地分叉/回滚
          - `prefix-forked`: 无显式 ID，达到最大轮数自动分叉
          - `new` / `no-reuse` / `ambiguous-new` / `empty`
        """
        self._clean_expired()
        scope = str(scope_key or "").strip()
        ns_model = f"{scope}:{model}" if (scope and model) else (scope or model)
        with self._lock:
            sigs = self._prefix_signatures(messages)
            full_sigs = self._all_signatures(messages)

            if custom_id:
                key = f"{ns_model}:cid:{custom_id}" if ns_model else f"cid:{custom_id}"
                if allow_reuse and full_sigs:
                    self._last_resolved_full_sigs[key] = list(full_sigs)

                ctx = self._sessions.get(key)
                if ctx is not None and allow_reuse:
                    max_turns = int(getattr(config_manager.config, "max_session_turns", 0) or 0)
                    if max_turns > 0 and ctx.turn_count >= max_turns:
                        if ctx.deepseek_session_id:
                            print(
                                f"[SessionFork] 显式会话 {custom_id!r} 轮数达到上限 ({ctx.turn_count} >= {max_turns})"
                                f" → 自动分叉：清除旧上游会话 ID，触发平滑轮换账号防风控",
                                flush=True,
                            )
                        ctx.deepseek_session_id = ""
                        ctx.last_message_id = None
                        ctx.turn_count = 0
                        ctx.tree_nodes.clear()
                        ctx.active_sigs = []
                        ctx._pending_sigs = list(full_sigs)
                        ctx._pending_parent_id = None
                        return key, "cid-forked"

                    # 老数据缺基线时就地补齐，不触发重建
                    if not ctx.prefix:
                        if sigs:
                            ctx.prefix = list(sigs)
                            ctx.active_sigs = list(full_sigs)
                            ctx._pending_sigs = list(full_sigs)
                            ctx._pending_parent_id = ctx.last_message_id
                        return key, "cid"

                    if full_sigs:
                        # 若 active_sigs 与 prefix 同步，则使用全视野 active_sigs 判断（解决 >8 条消息后的回滚盲区）
                        ref_sigs = (
                            ctx.active_sigs
                            if (ctx.active_sigs and ctx.active_sigs[: len(ctx.prefix)] == ctx.prefix)
                            else ctx.prefix
                        )
                        is_linear = (
                            len(full_sigs) >= len(ref_sigs)
                            and full_sigs[: len(ref_sigs)] == ref_sigs
                        )
                        # 特判：如果客户端重发了完全相同的消息链，且该消息链已经在 tree_nodes 提交过回复，
                        # 说明客户端正在对当前轮执行「重新生成 (Regenerate)」，应走分支回溯而非追加在自己后面
                        if (
                            is_linear
                            and len(full_sigs) == len(ref_sigs)
                            and SessionContext._sig_chain_key(full_sigs) in ctx.tree_nodes
                        ):
                            is_linear = False

                        if is_linear:
                            if len(sigs) > len(ctx.prefix):
                                ctx.prefix = list(sigs)
                            if len(full_sigs) > len(ctx.active_sigs):
                                ctx.active_sigs = list(full_sigs)
                            ctx._pending_sigs = list(full_sigs)
                            ctx._pending_parent_id = ctx.last_message_id
                            ctx.last_active = time.time()
                            return key, "cid"

                        # 非线性延续：优先在会话树检查点图中检索是否存在公共祖先节点 (Branch / Rollback / Regenerate)
                        if ctx.deepseek_session_id and ctx.tree_nodes:
                            branch_hit = ctx.find_branch_checkpoint(full_sigs)
                            if branch_hit is not None:
                                target_parent_mid, target_turn_count, branch_kind, matched_len = branch_hit
                                if not target_parent_mid and ctx.last_message_id and len(full_sigs) > 2:
                                    # 严禁将已有有效上游挂载点的长会话回滚为 None，避免在 DeepSeek 根节点脱节
                                    target_parent_mid = ctx.last_message_id
                                ctx.last_message_id = target_parent_mid
                                ctx.turn_count = target_turn_count
                                ctx.prefix = list(sigs)
                                ctx.active_sigs = list(full_sigs)
                                ctx._pending_sigs = list(full_sigs)
                                ctx._pending_parent_id = target_parent_mid
                                ctx.branch_count += 1
                                ctx.last_active = time.time()
                                print(
                                    f"[SessionTree] 🌿 显式会话 {custom_id!r} 命中会话树分叉 ({branch_kind}): "
                                    f"回溯挂载至父节点 parent_mid={target_parent_mid} "
                                    f"(匹配深度={matched_len}, 轮次={target_turn_count}, 累计分叉={ctx.branch_count})",
                                    flush=True,
                                )
                                return key, "cid-branched"

                        # 既非线性延续，也无任何已登记的历史分支检查点匹配（如客户端执行了全量摘要压缩）
                        if ctx.deepseek_session_id:
                            print(
                                f"[SessionTree] 显式会话 {custom_id!r} 的消息前缀已变化"
                                f"（存 {len(ctx.prefix)} 条 / 本轮 {len(sigs)} 条）"
                                f" → 丢弃上游会话、保留账号绑定（换会话但不换号）",
                                flush=True,
                            )
                        ctx.deepseek_session_id = ""
                        ctx.last_message_id = None
                        ctx.tree_nodes.clear()
                        ctx.active_sigs = []
                        ctx._pending_sigs = list(full_sigs)
                        ctx._pending_parent_id = None
                        return key, "cid-prefix-changed"
                return key, "cid"

            if not sigs:
                return (f"{ns_model}:hash:default" if ns_model else "hash:default"), "empty"

            head = hashlib.sha1(sigs[0].encode("utf-8", "ignore")).hexdigest()[:16]
            bucket = f"{ns_model}|{head}" if ns_model else head
            fresh = f"{bucket}#{uuid.uuid4().hex[:12]}"

            if not allow_reuse:
                return fresh, "no-reuse"

            linear_candidates = []
            branch_candidates = []
            for k in self._buckets.get(bucket, []):
                ctx = self._sessions.get(k)
                if ctx is None or not ctx.prefix:
                    continue
                ref_sigs = (
                    ctx.active_sigs
                    if (ctx.active_sigs and ctx.active_sigs[: len(ctx.prefix)] == ctx.prefix)
                    else ctx.prefix
                )
                is_linear = (
                    len(ref_sigs) <= len(full_sigs)
                    and full_sigs[: len(ref_sigs)] == ref_sigs
                )
                if (
                    is_linear
                    and len(full_sigs) == len(ref_sigs)
                    and SessionContext._sig_chain_key(full_sigs) in ctx.tree_nodes
                ):
                    is_linear = False

                if is_linear:
                    # 保持与既有测试一致的前缀评分口径：按 prefix 有效匹配长度排序
                    linear_candidates.append((len(ctx.prefix), len(ref_sigs), k))
                elif ctx.deepseek_session_id and ctx.tree_nodes:
                    branch_hit = ctx.find_branch_checkpoint(full_sigs)
                    if branch_hit is not None:
                        _, _, _, matched_len = branch_hit
                        branch_candidates.append((matched_len, k, branch_hit))

            if linear_candidates:
                best_len = max(n for n, _, _ in linear_candidates)
                winners = [k for n, _, k in linear_candidates if n == best_len]
                if len(winners) > 1:
                    # 若 prefix (前8条) 等长，再看全视野 ref_sigs 长度能否唯一区分
                    best_full_len = max(fn for n, fn, _ in linear_candidates if n == best_len)
                    full_winners = [k for n, fn, k in linear_candidates if n == best_len and fn == best_full_len]
                    if len(full_winners) == 1:
                        winners = full_winners

                if len(winners) == 1:
                    key = winners[0]
                    ctx = self._sessions[key]
                    max_turns = int(getattr(config_manager.config, "max_session_turns", 0) or 0)
                    if max_turns > 0 and ctx.turn_count >= max_turns:
                        if ctx.deepseek_session_id:
                            print(
                                f"[SessionFork] 前缀会话 {key!r} 轮数达到上限 ({ctx.turn_count} >= {max_turns})"
                                f" → 自动分叉：清除旧上游会话 ID，触发平滑轮换账号防风控",
                                flush=True,
                            )
                        ctx.deepseek_session_id = ""
                        ctx.last_message_id = None
                        ctx.turn_count = 0
                        ctx.tree_nodes.clear()
                        ctx.active_sigs = []
                        ctx._pending_sigs = list(full_sigs)
                        ctx._pending_parent_id = None
                        return key, "prefix-forked"

                    if len(sigs) > len(ctx.prefix):
                        ctx.prefix = list(sigs)
                    if len(full_sigs) > len(ctx.active_sigs):
                        ctx.active_sigs = list(full_sigs)
                    ctx._pending_sigs = list(full_sigs)
                    ctx._pending_parent_id = ctx.last_message_id
                    ctx.last_active = time.time()
                    return key, "prefix"

                print(
                    f"[SessionTree] 会话身份歧义（{len(winners)} 个候选的最长前缀都匹配，"
                    f"长度 {best_len}），判定为新会话以避免串线",
                    flush=True,
                )
                self._last_resolved_full_sigs[fresh] = list(full_sigs)
                return fresh, "ambiguous-new"

            if branch_candidates:
                best_len = max(n for n, _, _ in branch_candidates)
                winners = [(k, bh) for n, k, bh in branch_candidates if n == best_len]
                if len(winners) == 1:
                    key, branch_hit = winners[0]
                    ctx = self._sessions[key]
                    target_parent_mid, target_turn_count, branch_kind, matched_len = branch_hit
                    if not target_parent_mid and ctx.last_message_id and len(full_sigs) > 2:
                        target_parent_mid = ctx.last_message_id
                    ctx.last_message_id = target_parent_mid
                    ctx.turn_count = target_turn_count
                    ctx.prefix = list(sigs)
                    ctx.active_sigs = list(full_sigs)
                    ctx._pending_sigs = list(full_sigs)
                    ctx._pending_parent_id = target_parent_mid
                    ctx.branch_count += 1
                    ctx.last_active = time.time()
                    print(
                        f"[SessionTree] 🌿 前缀会话 {key!r} 命中会话树分叉 ({branch_kind}): "
                        f"回溯挂载至父节点 parent_mid={target_parent_mid} "
                        f"(匹配深度={matched_len}, 轮次={target_turn_count}, 累计分叉={ctx.branch_count})",
                        flush=True,
                    )
                    return key, "prefix-branched"

                print(
                    f"[SessionTree] 分支会话身份歧义（{len(winners)} 个候选的分支检查点都匹配，"
                    f"长度 {best_len}），判定为新会话以避免串线",
                    flush=True,
                )
                self._last_resolved_full_sigs[fresh] = list(full_sigs)
                return fresh, "ambiguous-new"

            self._last_resolved_full_sigs[fresh] = list(full_sigs)
            return fresh, "new"

    def register_session(
        self,
        conv_key: str,
        account_label: str,
        deepseek_session_id: str,
        model_type: str = "default",
        prefix: Optional[List[str]] = None,
    ) -> SessionContext:
        with self._lock:
            ctx = SessionContext(conv_key, account_label, deepseek_session_id, model_type=model_type)
            resolved_full = self._last_resolved_full_sigs.pop(conv_key, None)
            if prefix:
                ctx.prefix = list(prefix)[:PREFIX_MAX_MESSAGES]
                if resolved_full and resolved_full[: len(ctx.prefix)] == ctx.prefix:
                    ctx.active_sigs = list(resolved_full)
                else:
                    ctx.active_sigs = list(prefix)
                ctx._pending_sigs = list(ctx.active_sigs)
            elif resolved_full:
                ctx.prefix = list(resolved_full)[:PREFIX_MAX_MESSAGES]
                ctx.active_sigs = list(resolved_full)
                ctx._pending_sigs = list(resolved_full)

            if "#" in conv_key:
                ctx.bucket = conv_key.split("#", 1)[0]
            elif "|" in conv_key:
                ctx.bucket = conv_key.split("|", 1)[0]
            self._sessions[conv_key] = ctx
            if ctx.bucket:
                lst = self._buckets.setdefault(ctx.bucket, [])
                if conv_key not in lst:
                    lst.append(conv_key)
                self._prune_bucket_locked(ctx.bucket)
            self._clean_expired_locked()
            self._flush_bindings_locked()
            return ctx

    def get_session(self, conv_key: str) -> Optional[SessionContext]:
        self._clean_expired()
        with self._lock:
            return self._sessions.get(conv_key)

    def register_prefix(self, conv_key: str, messages: List[Dict[str, Any]]):
        """登记/延伸某个会话的消息前缀指纹与全视野签名"""
        sigs = self._prefix_signatures(messages)
        full_sigs = self._all_signatures(messages)
        if not sigs:
            return
        with self._lock:
            ctx = self._sessions.get(conv_key)
            if ctx is not None:
                if len(sigs) > len(ctx.prefix):
                    ctx.prefix = list(sigs)
                if len(full_sigs) > len(ctx.active_sigs):
                    ctx.active_sigs = list(full_sigs)
                ctx._pending_sigs = list(full_sigs)

    def update_message_id(self, conv_key: str, message_id: str, messages: Optional[List[Dict[str, Any]]] = None):
        """更新当前轮次产出的上游 message_id，并在会话树中登记分支检查点。"""
        with self._lock:
            ctx = self._sessions.get(conv_key)
            if ctx:
                prev_parent = ctx._pending_parent_id if ctx._pending_sigs else ctx.last_message_id
                ctx.last_message_id = message_id
                ctx.last_active = time.time()
                ctx.turn_count += 1
                target_sigs = (
                    self._all_signatures(messages)
                    if messages is not None
                    else (ctx._pending_sigs or ctx.active_sigs or ctx.prefix)
                )
                if target_sigs and message_id:
                    ctx.record_checkpoint(
                        target_sigs,
                        message_id,
                        parent_message_id=prev_parent,
                        turn_count=ctx.turn_count,
                    )
                    ctx.active_sigs = list(target_sigs)
                ctx._pending_parent_id = message_id
                self._flush_bindings_locked()

    def bind_response_id(self, response_id: str, conv_key: str, message_id: str):
        """将 Responses API 的 response_id 绑定到 (conv_key, message_id)，支持 previous_response_id 精确树分叉。"""
        if not response_id or not conv_key or not message_id:
            return
        with self._lock:
            self._response_index[str(response_id)] = (str(conv_key), str(message_id))
            if len(self._response_index) > MAX_RESPONSE_ID_MAPPINGS:
                # 淘汰最早写入的映射
                excess = len(self._response_index) - MAX_RESPONSE_ID_MAPPINGS
                for k in list(self._response_index.keys())[:excess]:
                    self._response_index.pop(k, None)
            self._flush_bindings_locked()

    def resolve_by_response_id(self, previous_response_id: str) -> Optional[Tuple[str, str]]:
        """通过 previous_response_id 查找对应的 (conv_key, parent_message_id) 并自动回溯会话树指针。"""
        if not previous_response_id:
            return None
        self._clean_expired()
        with self._lock:
            hit = self._response_index.get(str(previous_response_id).strip())
            if not hit:
                return None
            conv_key, target_mid = hit
            ctx = self._sessions.get(conv_key)
            if ctx is None or not ctx.deepseek_session_id:
                return None
            if ctx.last_message_id != target_mid:
                ctx.branch_count += 1
                print(
                    f"[SessionTree] 🌿 Responses API previous_response_id={previous_response_id!r} 触发分支回溯: "
                    f"parent_mid {ctx.last_message_id} -> {target_mid} (累计分叉={ctx.branch_count})",
                    flush=True,
                )
            ctx.last_message_id = target_mid
            ctx._pending_parent_id = target_mid
            ctx.last_active = time.time()
            return conv_key, target_mid

    def delete_session(self, conv_key: str):
        with self._lock:
            ctx = self._sessions.pop(conv_key, None)
            self._last_resolved_full_sigs.pop(conv_key, None)
            if ctx and ctx.bucket:
                lst = self._buckets.get(ctx.bucket)
                if lst and conv_key in lst:
                    lst.remove(conv_key)
                    if not lst:
                        self._buckets.pop(ctx.bucket, None)
            self._flush_bindings_locked(force=True)

    def reset_upstream_session(self, conv_key: str, reason: str = ""):
        """清除指定绑定的上游会话 ID（保留账号绑定与会话键），强制下一轮新建上游会话。"""
        with self._lock:
            ctx = self._sessions.get(conv_key)
            if ctx:
                if ctx.deepseek_session_id:
                    print(
                        f"[SessionTree] 重置上游会话: conv_key={conv_key!r}, "
                        f"old_sid={ctx.deepseek_session_id}, reason={reason or 'manual'}",
                        flush=True,
                    )
                ctx.deepseek_session_id = ""
                ctx.last_message_id = None
                ctx.turn_count = 0
                ctx.tree_nodes.clear()
                ctx.active_sigs = []
                ctx._pending_sigs = []
                ctx._pending_parent_id = None
                self._flush_bindings_locked()

    def _clean_expired_locked(self):
        """过期与容量淘汰（调用方必须已持锁）"""
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.last_active > self.ttl]
        for k in expired:
            del self._sessions[k]
            self._last_resolved_full_sigs.pop(k, None)
        if len(self._sessions) > self.max_sessions:
            sorted_sessions = sorted(self._sessions.items(), key=lambda x: x[1].last_active)
            excess = len(self._sessions) - self.max_sessions
            for k, _ in sorted_sessions[:excess]:
                del self._sessions[k]
                self._last_resolved_full_sigs.pop(k, None)
        if expired or len(self._sessions) > self.max_sessions:
            for bucket in list(self._buckets.keys()):
                kept = [k for k in self._buckets[bucket] if k in self._sessions]
                if kept:
                    self._buckets[bucket] = kept
                else:
                    self._buckets.pop(bucket, None)

    def _clean_expired(self):
        with self._lock:
            self._clean_expired_locked()

    # ── 工具结果压缩（T6 / T19）──────────────────────────────────────────
    TOOL_RESULT_MAX_CHARS = 32768
    TOOL_RESULT_HEAD_CHARS = 24000
    TOOL_RESULT_TAIL_CHARS = 8000

    @classmethod
    def _compress_tool_result(cls, text: str) -> str:
        """确定性截断超长工具结果。同输入必得同输出（可复现、可测试）。"""
        if not isinstance(text, str):
            text = str(text or "")
        n = len(text)
        if n <= cls.TOOL_RESULT_MAX_CHARS:
            return text
        head = text[: cls.TOOL_RESULT_HEAD_CHARS]
        tail = text[-cls.TOOL_RESULT_TAIL_CHARS:]
        omitted = n - cls.TOOL_RESULT_HEAD_CHARS - cls.TOOL_RESULT_TAIL_CHARS
        marker = (
            f"\n\n…［输出过长已截断：中间省略 {omitted:,} 字符"
            f"（原文共 {n:,} 字符）。以上为开头与结尾两部分，"
            f"如需查看被省略的中间内容，请用更精确的方式重新读取"
            f"（例如按行范围读取、或搜索关键词）］…\n\n"
        )
        compressed = head + marker + tail
        print(
            f"[ContextGov] 工具结果压缩: {n:,} → {len(compressed):,} 字符 "
            f"(省略 {omitted:,})",
            flush=True,
        )
        return compressed

    _INJECTED_PREFIXES = (
        "the approval policy changed from",
        "current runtime context",
        "this snapshot supersedes earlier runtime-context snapshots",
        "<system-reminder>",
        "[hana_reminder]",
        "[hana_reference]",
        "instructions from:",
        "the following workspace instructions",
        "time sampled while preparing",
        "time sampled",
        "browser time zone",
        "elapsed since the preceding",
        "background job ",
        "a skill is a reusable set",
    )

    @classmethod
    def _is_injected_metadata(cls, text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return True
        if any(t.startswith(p) for p in cls._INJECTED_PREFIXES):
            return True
        if "time sampled while preparing" in t or "browser time zone for this request" in t:
            return True
        if t.startswith("background job ") and ("(" in t and ")" in t):
            return True
        if re.match(r'^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?(?:\[.*\])?$', t):
            return True
        return False

    @classmethod
    def _clean_user_content(cls, content: Any) -> str:
        """从用户消息中清洗出真实的业务提问，坚决剔除 Agent 自动注入的时间戳与上下文快照。"""
        if isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        else:
            text = str(content or "")

        text = text.strip()
        if not text:
            return ""

        if "<system-reminder>" in text or "</system-reminder>" in text:
            text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"</?system-reminder>", " ", text, flags=re.IGNORECASE)
            text = re.sub(r"\s{2,}", " ", text).strip()
            if not text:
                return ""

        if "time sampled while preparing" in text.lower() or "browser time zone for this request" in text.lower():
            text = re.sub(
                r'Time sampled while preparing[^\n]*\n?'
                r'(?:Browser time zone for this request:[^\n]*\n?)?'
                r'(?:Elapsed since the preceding[^\n]*\n?)?',
                '',
                text,
                flags=re.IGNORECASE
            ).strip()
            if not text:
                return ""

        if text.startswith("[hana_reminder]"):
            return ""
        if cls._is_injected_metadata(text):
            return ""

        if text.startswith("[hana_reference]"):
            parts = text.split("\n\n")
            non_ref = [p.strip() for p in parts if not p.strip().startswith("[hana_reference]")]
            if non_ref:
                text = "\n\n".join(non_ref).strip()

        return text

    @classmethod
    def get_conversation_key(cls, messages: List[Dict[str, Any]], model: str = "", custom_id: Optional[str] = None) -> str:
        """【已废弃】旧的无状态会话指纹，仅保留供旧测试调用。"""
        if custom_id:
            return f"{model}:cid:{custom_id}" if model else f"cid:{custom_id}"
        sigs = cls._prefix_signatures(messages)
        if sigs:
            h = hashlib.sha1(sigs[0].encode("utf-8", "ignore")).hexdigest()[:16]
            return f"{model}|{h}" if model else h
        return f"{model}:hash:default" if model else "hash:default"

    @classmethod
    def prepare_prompt(
        cls,
        messages: List[Dict[str, Any]],
        has_existing_session: bool,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """
        准备本次提问的 Prompt 与 parent_message_id
        - 若已有会话 (含线性延续与分支回溯): 只发送当前最新一条用户提问 (Prompt 极其精简自然)
        - 若无已有会话但存在历史消息: 以人类自然对话摘要格式拼接背景，绝不注入特殊 Token
        - 支持推理强度提示词工程 (Reasoning Director): low / medium / max 动态注入
        """
        if not messages:
            return "", None

        reasoning_directive = (
            ReasoningDirector.get_prompt_directive(reasoning_effort)
            if reasoning_effort
            else ""
        )

        # 1. 检查末尾是否是工具执行结果 (Agent Tool Calling Loop 闭环)
        tail_tool_results = []
        for m in reversed(messages):
            role = m.get("role")
            if role == "tool":
                cid = m.get("tool_call_id", "")
                content = str(m.get("content", "")).strip()
                tail_tool_results.append(f"[工具返回 {cid}]: {cls._compress_tool_result(content)}")
            elif role == "user":
                content_str = str(m.get("content", "")).strip()
                if content_str.startswith("[hana_reminder]") or cls._is_injected_metadata(content_str):
                    continue
                elif "attached image" in content_str.lower():
                    tail_tool_results.append(f"[图片附件/视觉上下文]: {content_str}")
                    continue
                else:
                    break
            else:
                break

        if tail_tool_results:
            tail_tool_results.reverse()
            feedback_str = "\n".join(tail_tool_results)

            latest_user_instruction = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    cleaned = cls._clean_user_content(m.get("content"))
                    if cleaned:
                        latest_user_instruction = cleaned
                        break

            initial_user_task = ""
            for m in messages:
                if m.get("role") == "user":
                    cleaned = cls._clean_user_content(m.get("content"))
                    if cleaned:
                        initial_user_task = cleaned
                        break

            if latest_user_instruction:
                task_context = f"【当前核心目标（用户最新指令，最高优先级）】：\n👉 {latest_user_instruction}\n\n"
                if initial_user_task and initial_user_task != latest_user_instruction:
                    task_context += f"【会话初始背景（仅供参考，若与最新指令有差异，以最新指令为准）】：\n{initial_user_task}\n\n"
            elif initial_user_task:
                task_context = f"【用户任务目标】：\n{initial_user_task}\n\n"
            else:
                task_context = ""

            history_context = ""
            if not has_existing_session and len(messages) > len(tail_tool_results):
                prior_msgs = messages[:-len(tail_tool_results)]
                history_steps = []
                for m in prior_msgs:
                    r = m.get("role")
                    c = m.get("content", "")
                    tcs = m.get("tool_calls", [])
                    if r == "user":
                        cleaned = cls._clean_user_content(c)
                        if cleaned and cleaned != initial_user_task and "runtime context" not in cleaned.lower():
                            history_steps.append(f"【用户追问】: {cleaned}")
                    elif r == "assistant":
                        if tcs:
                            tc_names = [tc.get("function", {}).get("name", "") for tc in tcs]
                            history_steps.append(f"【助手调用工具】: {', '.join(tc_names)}")
                        elif c:
                            from app.engine.sensitive_filter import SafetyOutputGuard
                            c_brief = SafetyOutputGuard.strip_safety_tags(c)
                            if len(c_brief) > 200:
                                c_brief = c_brief[:200] + "..."
                            if c_brief:
                                history_steps.append(f"【助手输出】: {c_brief}")
                    elif r == "tool":
                        cid = m.get("tool_call_id", "")
                        c_str = str(c).strip()
                        if len(c_str) > 300:
                            c_str = c_str[:300] + "...(输出较长已折叠)"
                        history_steps.append(f"【前序工具返回 {cid}】: {c_str}")

                if history_steps:
                    history_context = "【先前已完成的执行历史】：\n" + "\n".join(history_steps) + "\n\n"

            available_tool_names = []
            if tools:
                for t in tools:
                    fn = t.get("function", t)
                    fn_name = fn.get("name")
                    if fn_name:
                        available_tool_names.append(fn_name)
            if not available_tool_names and messages:
                for m in messages:
                    if m.get("role") == "system":
                        found = re.findall(r'【工具:\s*([^】\s]+)】', str(m.get("content", "")))
                        if found:
                            available_tool_names.extend(found)
                            break
            tool_hint = f"（当前可用工具：{', '.join(available_tool_names)}）" if available_tool_names else ""

            directive_block = f"【推理执行准则】：\n{reasoning_directive}\n\n" if reasoning_directive else ""

            current_prompt = (
                f"{directive_block}"
                f"{task_context}"
                f"{history_context}"
                f"【工具执行输出】：\n"
                f"{feedback_str}\n\n"
                f"请结合上述工具执行结果，紧密围绕【用户最新指令】推进任务：\n"
                f"- 若任务已达成或已能满足用户最新需求，请直接向用户输出答复与总结；\n"
                f"- 若需继续执行操作，请调用下一步工具{tool_hint}。"
            )
            return current_prompt, None

        # 2. 常规用户最新提问（倒序查找最近一条非 reminder 的真实用户提问）
        latest_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                cleaned = cls._clean_user_content(m.get("content"))
                if cleaned:
                    latest_user_msg = cleaned
                    break

        if not latest_user_msg:
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content")
                    latest_user_msg = " ".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else str(c or "")
                    break

        if not latest_user_msg:
            latest_user_msg = str(messages[-1].get("content", ""))

        if has_existing_session:
            if reasoning_directive:
                return f"{reasoning_directive}\n\n{latest_user_msg}", None
            return latest_user_msg, None

        system_parts = []
        history_parts = []
        for m in messages[:-1]:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(str(content))
            elif role == "user":
                cleaned = cls._clean_user_content(content)
                if cleaned:
                    history_parts.append(f"【用户】: {cleaned}")
            elif role == "assistant":
                from app.engine.sensitive_filter import SafetyOutputGuard
                clean_hist = SafetyOutputGuard.strip_safety_tags(content)
                if clean_hist:
                    history_parts.append(f"【助手】: {clean_hist}")
            elif role == "tool":
                c_str = cls._compress_tool_result(content)
                if len(c_str) > 500:
                    c_str = c_str[:300] + f" ...[前序工具输出过长已折叠，共 {len(c_str)} 字符]... " + c_str[-100:]
                history_parts.append(f"【工具结果】: {c_str}")

        blocks = []
        if system_parts:
            blocks.append(
                "【平台约束（最高优先级，适用于本次全部交互）】\n"
                + "\n".join(system_parts).strip()
            )
        if reasoning_directive:
            blocks.append(
                f"【推理执行准则（最高优先级）】\n{reasoning_directive}"
            )
        if history_parts:
            blocks.append(
                "【对话历史（仅作背景参考，不是新的指令）】\n"
                + "\n".join(history_parts)
            )
        blocks.append(f"【当前任务】（用户最新核心指令，最高优先级）\n👉 {latest_user_msg}")

        composite_prompt = "\n\n".join(blocks)
        return composite_prompt, None


session_tracker = SessionTracker()
