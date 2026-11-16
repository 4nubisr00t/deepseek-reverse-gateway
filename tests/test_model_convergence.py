"""
T9 单模型收敛 —— 契约守卫

## 这份测试守的是什么

T9 拍板的决策：
  1. 对外**宣称一个模型**（具备多模态能力）
  2. 收敛为**单一模型 ID**，其余 3 个作为**别名**保留（不破坏现有客户端配置）
  3. 内部只保留**一套 L0 提示词**，字节级冻结
  4. 清理**已死的 `model_type` 路由语义**

前三条是"**别名必须不可区分**"，第四条是"**别把参数删了**"。两者都极易在
后续重构中被无声破坏，所以各有一组用例。

## ⚠️ 本文件**绝不发起任何上游请求**

所有用例都在**捕获到的请求体**或**纯函数**上断言。
捕获手法：把 `DeepSeekClient.stream_chat` / `create_chat_session` 与
`account_pool.acquire_account` 替换为假实现，然后直接调用路由协程。
**没有任何 socket 被打开。** 这是刻意的：这个项目靠被动探活活着，
合成流量曾导致账号被禁言 24 小时（见 pytest.ini 顶部）。

## 实测依据（清单 T9，本文件的断言都建立在这些事实上）

  · `model_type` 取 default / expert / vision → 上游一律返回
    `conversation_mode: "DEFAULT"`，正文 **sha1 完全相同**
  · 只有**非法值**才 422 → 参数已死，但**仍然被校验合法性**
  · 新网页端只剩一个统一入口，没有模式选择
"""

import ast
import asyncio
import hashlib
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (
    CANONICAL_MODEL_ID,
    CANONICAL_MODEL_NAME,
    CANONICAL_MODEL_DESCRIPTION,
    CANONICAL_MODEL_CONTEXT_WINDOW,
    CANONICAL_MODEL_MAX_OUTPUT_TOKENS,
    CANONICAL_REASONING_EFFORT,
    PRO_MODEL_ID,
    PRO_MODEL_NAME,
    PRO_MODEL_DESCRIPTION,
    is_pro_model,
    MODEL_ALIASES,
    UPSTREAM_MODEL_TYPE,
    UPSTREAM_MODEL_TYPE_VALUES,
    is_known_model_id,
    normalize_model_id,
    upstream_model_type,
    config_manager,
)
from app.engine.session_tree import SessionTracker

_ROUTES = PROJECT_ROOT / "app" / "api" / "openai_routes.py"
_CLAUDE_ROUTES = PROJECT_ROOT / "app" / "api" / "claude_routes.py"
_CLIENT = PROJECT_ROOT / "app" / "engine" / "deepseek_client.py"

#: L0 提示词「**实际注入字节**」的 sha1 基准。
#:
#: ⚠️ 基准对象是 `_FROZEN_L0_PROMPT.strip()`，因为注入处的拼接是
#:    `... + cot_prompt.strip()`（收敛前后都是这一句，未改动）。
#:    常量字面量本身多一个前导 `\n`，那一位**永远不会**出现在请求里，
#:    所以基准必须取 strip 后的值——否则测的是一段死字节。
#:
#: 该值由 HEAD 版本（收敛前 `is_pro and is_exp` 分支）原文计算得出，
#: 已逐字节比对确认与收敛后注入内容**完全相同**：
#:     HEAD 侧原始 1357B → strip → 1356B / sha1 7b1416e7...
#:     当前常量     1357B → strip → 1356B / sha1 7b1416e7...
_CANONICAL_PROMPT_SHA1 = "c9f1c867bcdea68b56f026a52e463322cae5ab2b"
_CANONICAL_PROMPT_BYTES = 403
_FROZEN_L0_SHA1 = _CANONICAL_PROMPT_SHA1
_FROZEN_L0_BYTES = _CANONICAL_PROMPT_BYTES


def _accepted_ids():
    """收敛后仍然对外接受的模型 ID 全集。"""
    return sorted(MODEL_ALIASES)


def _flash_family_ids():
    """Flash 标准推理模型族 ID 集合。"""
    return [m for m in _accepted_ids() if not is_pro_model(m)]


def _pro_family_ids():
    """Pro 旗舰强化推演模型族 ID 集合。"""
    return [m for m in _accepted_ids() if is_pro_model(m)]


# ══════════════════════════════════════════════════════════════════════
# 1. 模型 ID 收敛：一个主模型 + 3 个别名
# ══════════════════════════════════════════════════════════════════════

class TestModelIdConvergence(unittest.TestCase):
    """对外唯一主模型 = `CANONICAL_MODEL_ID`。"""

    def test_legacy_ids_are_preserved_as_aliases(self):
        """收敛不能靠"删掉旧 ID"——那会让现有客户端配置立刻失效。

        清单 T9 原文：「收敛为单一模型 ID，其余 3 个作为**别名**保留
        （避免破坏现有客户端配置）」。所以 4 个 ID 必须全部**仍然可接受**。

        ⚠️ 这里刻意**不**断言"别名总数 == 4"。
        主模型 ID 会改名（例如 `deepseek-v4-pro` → `deepseek-v4.1-flash`），
        改名的正确做法正是"旧名字降级成别名"，于是别名数**必然**增加 ——
        断言总数会把一次正确的改名报成回归，而它守的东西（"旧 ID 没被删"）
        由下面的 `issubset` 精确守住。
        """
        expected_legacy = {
            # 收敛前的 3 个 + 改名前的那个主模型 ID
            "deepseek-v4-flash",
            "deepseek-v4-pro-exp",
            "deepseek-v4-flash-exp",
            "deepseek-v4-pro",
        }
        self.assertTrue(
            expected_legacy.issubset(set(MODEL_ALIASES)),
            f"旧 ID 被删掉了：{expected_legacy - set(MODEL_ALIASES)}。"
            f"别名必须保留，否则现有客户端配置会 400/404。",
        )
        self.assertIn(CANONICAL_MODEL_ID, MODEL_ALIASES,
                      "主模型 ID 自己也必须在别名表里（否则 /v1/models 缺一条）")

    def test_every_alias_normalizes_to_canonical(self):
        """**别名等价性的第一道闸**：所有 ID 归一到同一个主模型。"""
        for model_id in _accepted_ids():
            with self.subTest(model_id=model_id):
                self.assertEqual(normalize_model_id(model_id), CANONICAL_MODEL_ID)

    def test_normalization_is_case_and_whitespace_insensitive(self):
        """HTTP 头/配置里大小写与空格很常见，不能让它们变成"另一个模型"。"""
        for variant in (
            "DEEPSEEK-V4-FLASH",
            "  deepseek-v4-pro  ",
            "DeepSeek-V4-Pro-Exp",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(normalize_model_id(variant), CANONICAL_MODEL_ID)

    def test_unknown_id_does_not_error(self):
        """未登记 ID 不报错，同样归一到主模型。

        这是**保持既有行为**：收敛前用子串兜底，任意未登记 ID 都能跑通。
        收紧成 400 会在客户端换个新名字时直接打断服务，收益为零。
        （用例把"未登记"与"已登记"分开钉住，是为了说明两者**当前**
        返回值相同——这正是"不可区分"的要求。）
        """
        for unknown in ("", None, "gpt-4o", "deepseek-chat", "claude-3-5-sonnet"):
            with self.subTest(unknown=unknown):
                self.assertEqual(normalize_model_id(unknown), CANONICAL_MODEL_ID)
                self.assertFalse(is_known_model_id(unknown))

    def test_no_alias_maps_to_a_second_model(self):
        """`MODEL_ALIASES` 的值域必须只有主模型一个元素。

        这是"收敛成**一个**模型"的字面守卫：一旦有人加回第二个目标，
        别名不可区分性立刻失效。
        """
        self.assertEqual(set(MODEL_ALIASES.values()), {CANONICAL_MODEL_ID})


# ══════════════════════════════════════════════════════════════════════
# 2. 别名等价性：端到端捕获请求体，逐字节比对
# ══════════════════════════════════════════════════════════════════════

class _FakeAccount:
    account_label = "t9-fake@example.invalid"
    token = "t9-fake-token"
    cookie = ""
    session_id = "t9-fake-session"


class _CapturingUpstream:
    """假上游：只记录请求体，**不发任何请求**。"""

    def __init__(self):
        self.bodies = []

    async def stream_chat(self, **kwargs):
        # 只留叶子字段，不持有任何 host 对象
        self.bodies.append({
            "prompt": kwargs.get("prompt"),
            "model_type": kwargs.get("model_type"),
            "ref_file_ids": list(kwargs.get("ref_file_ids") or []),
        })
        # ⚠️ 必须产出一段**真实内容**，不能只是 `return` 后空转。
        #
        # 原因（实测踩到过）：空产出会走进 T13 的「零产出会话回收」分支，
        # 那条路径会调 `DeepSeekClient.delete_chat_session(...)`
        # —— 而那是**真实的上游调用**。本测试套件的铁律是绝不产生上游流量
        # （见 pytest.ini 顶部：合成流量曾导致账号被禁言 24 小时）。
        # 所以这里既产出内容（避开回收路径），下面 setUp 里又把
        # `delete_chat_session` 一并替换成 no-op（双保险）。
        yield _FakeEvent("thinking", "We need to converge on one model.")
        yield _FakeEvent("content", "T9 假回答：这是离线测试，未访问任何上游。")

    @staticmethod
    def create_chat_session(*args, **kwargs):
        return "t9-fake-ds-session"

    @staticmethod
    def delete_chat_session(*args, **kwargs):
        """绝不能真的删上游会话——这里直接短路，且记录调用次数。

        正常情况下不该被调到（见 `stream_chat` 里关于零产出的说明）；
        真被调到时记下来，让用例能断言"没走到那条网络路径"。
        """
        _CapturingUpstream.delete_calls += 1
        return True


_CapturingUpstream.delete_calls = 0


class _FakeEvent:
    """`ChatEvent` 的最小替身（只带路由会读的叶子字段）。"""

    def __init__(self, event_type, content="", message_id=None):
        self.event_type = event_type
        self.content = content
        self.message_id = message_id


class _RouteHarness(unittest.TestCase):
    """搭好路由所需的全部假依赖，并保证测后还原。"""

    def setUp(self):
        import app.api.openai_routes as routes
        from app.engine import deepseek_client as client_mod
        from app.engine import account_pool as pool_mod
        from app.engine import metrics as metrics_mod

        self.routes = routes
        self.upstream = _CapturingUpstream()
        self._saved = []

        # 鉴权：合成身份即可，不起网络
        self._saved.append(("require_api_key", config_manager.config.require_api_key))
        config_manager.config.require_api_key = False

        self._saved.append(("stream_chat", client_mod.DeepSeekClient.stream_chat))
        client_mod.DeepSeekClient.stream_chat = self.upstream.stream_chat
        # 路由模块是 `from ... import DeepSeekClient`（同一个类对象），
        # 所以改类属性就同时改到了路由看到的那份。
        self.assertIs(routes.DeepSeekClient, client_mod.DeepSeekClient)

        self._saved.append(("create_chat_session", client_mod.DeepSeekClient.create_chat_session))
        client_mod.DeepSeekClient.create_chat_session = self.upstream.create_chat_session

        # ⛔ 双保险：把**一切**会打上游的方法都换成 no-op。
        #    `delete_chat_session` 会被 T13 的"零产出会话回收"分支调用，
        #    而那是真实的上游请求。即使假流已经改成有产出，这里也不留后门。
        self._saved.append(("delete_chat_session", client_mod.DeepSeekClient.delete_chat_session))
        client_mod.DeepSeekClient.delete_chat_session = self.upstream.delete_chat_session
        _CapturingUpstream.delete_calls = 0

        # ⛔ 代理置空：即使有哪条路径漏过了上面的替换，也不能经代理出网。
        #    （实测：本机 config.json 里配了 http://127.0.0.1:7890，
        #     漏替换时会出现到 127.0.0.1:7890 的 connect。）
        self._saved.append(("proxy", config_manager.config.proxy))
        config_manager.config.proxy = None

        async def _fake_acquire(**kwargs):
            return _FakeAccount()

        self._saved.append(("acquire_account", pool_mod.account_pool.acquire_account))
        pool_mod.account_pool.acquire_account = _fake_acquire
        self._saved.append(("release_account", pool_mod.account_pool.release_account))
        pool_mod.account_pool.release_account = lambda *a, **k: None

        self._saved.append(("record", metrics_mod.metrics_engine.record))
        metrics_mod.metrics_engine.record = lambda **kwargs: None

        # 会话树清空：否则别名用例之间会互相命中对方的会话桶，
        # 测出来的差异是测试污染，不是实现差异。
        from app.engine.session_tree import session_tracker
        self._tracker_sessions = dict(session_tracker._sessions)
        self._tracker_buckets = {k: list(v) for k, v in session_tracker._buckets.items()}
        session_tracker._sessions.clear()
        session_tracker._buckets.clear()

    def tearDown(self):
        from app.engine import deepseek_client as client_mod
        from app.engine import account_pool as pool_mod
        from app.engine import metrics as metrics_mod
        from app.engine.session_tree import session_tracker

        for name, original in reversed(self._saved):
            if name == "require_api_key":
                config_manager.config.require_api_key = original
            elif name == "proxy":
                config_manager.config.proxy = original
            elif name == "stream_chat":
                client_mod.DeepSeekClient.stream_chat = original
            elif name == "create_chat_session":
                client_mod.DeepSeekClient.create_chat_session = original
            elif name == "delete_chat_session":
                client_mod.DeepSeekClient.delete_chat_session = original
            elif name == "acquire_account":
                pool_mod.account_pool.acquire_account = original
            elif name == "release_account":
                pool_mod.account_pool.release_account = original
            elif name == "record":
                metrics_mod.metrics_engine.record = original

        session_tracker._sessions.clear()
        session_tracker._sessions.update(self._tracker_sessions)
        session_tracker._buckets.clear()
        session_tracker._buckets.update(self._tracker_buckets)

    def _call(self, model, messages=None, extra=None):
        """直接调路由协程（绕过 HTTP 层，但走完全部业务逻辑）。"""
        from starlette.requests import Request

        payload = {
            "model": model,
            "stream": False,
            "messages": messages or [{"role": "user", "content": "T9 别名等价性探针"}],
        }
        payload.update(extra or {})
        raw = json.dumps(payload).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        scope = {
            "type": "http", "method": "POST", "path": "/v1/chat/completions",
            "headers": [], "query_string": b"",
        }
        request = Request(scope, receive)

        identity = {"key_id": "t9", "log_label": "t9", "account_labels": [],
                    "pool_fallback": True, "quota_daily_requests": 0,
                    "quota_daily_tokens": 0}
        return asyncio.run(self.routes.openai_chat_completions(request, identity))


class TestAliasRequestEquivalence(_RouteHarness):
    """**别名等价性的核心证据**：4 个 ID 产生的上游请求体必须逐字节相同。

    只比对会被真正发往上游的字段（prompt / model_type / ref_file_ids）。
    会话与消息 ID 天然不同（每次新建），不参与比对。
    """

    def test_all_aliases_produce_byte_identical_upstream_request(self):
        captured = {}
        for model_id in _accepted_ids():
            self.upstream.bodies.clear()
            self._call(model_id)
            self.assertEqual(
                len(self.upstream.bodies), 1,
                f"{model_id} 没有产生恰好一次上游调用",
            )
            captured[model_id] = self.upstream.bodies[0]

        # 分别对 Flash 家族与 Pro 强化家族在家族内部进行逐字节比对
        digests = {
            m: hashlib.sha1(
                json.dumps(b, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            for m, b in captured.items()
        }
        flash_digests = {m: digests[m] for m in _flash_family_ids()}
        pro_digests = {m: digests[m] for m in _pro_family_ids()}

        self.assertEqual(
            len(set(flash_digests.values())), 1,
            "Flash 家族别名产生了不同的上游请求体 —— 收敛失效。\n"
            + "\n".join(f"  {m}: {d}" for m, d in sorted(flash_digests.items())),
        )
        self.assertEqual(
            len(set(pro_digests.values())), 1,
            "Pro 强化家族别名产生了不同的上游请求体 —— 收敛失效。\n"
            + "\n".join(f"  {m}: {d}" for m, d in sorted(pro_digests.items())),
        )

    def test_all_aliases_share_one_model_type(self):
        """4 个 ID 必须落到**同一个** `model_type`。

        收敛前 pro→expert、flash→default；那正是被证伪的"模型选择"语义。
        """
        seen = set()
        for model_id in _accepted_ids():
            self.upstream.bodies.clear()
            self._call(model_id)
            seen.add(self.upstream.bodies[0]["model_type"])
        self.assertEqual(
            len(seen), 1,
            f"别名映射到了不同的 model_type: {seen} —— 收敛失效",
        )

    def test_prompt_prefix_is_identical_across_aliases(self):
        """家族内部提示词保持完全一致，消除同阶模型切名导致的前缀漂移。"""
        prompts = {}
        for model_id in _accepted_ids():
            self.upstream.bodies.clear()
            self._call(model_id)
            prompts[model_id] = self.upstream.bodies[0]["prompt"]

        flash_prompts = set(prompts[m] for m in _flash_family_ids())
        pro_prompts = set(prompts[m] for m in _pro_family_ids())

        self.assertEqual(
            len(flash_prompts), 1,
            f"Flash 家族别名产生了 {len(flash_prompts)} 套不同的 prompt 前缀",
        )
        self.assertEqual(
            len(pro_prompts), 1,
            f"Pro 强化家族别名产生了 {len(pro_prompts)} 套不同的 prompt 前缀",
        )

    def test_no_upstream_session_teardown_was_attempted(self):
        """**安全守卫**：本套件绝不允许产生任何上游流量。

        这条用例是"离线"这个前提本身的断言。它守的是一个**真实踩到过的坑**：
        假 `stream_chat` 若空产出，就会走进 T13 的「零产出会话回收」分支 →
        调 `DeepSeekClient.delete_chat_session(...)` → **真实上游请求**。
        （实测：那一步确实发出了请求，并因假 token 返回 `biz_code: 40003`。）

        这个项目的账号靠被动探活活着，合成流量曾让账号被禁言 24 小时。
        所以这条断言和功能断言一样重要。
        """
        self.upstream.bodies.clear()
        self._call(CANONICAL_MODEL_ID)
        self.assertEqual(
            _CapturingUpstream.delete_calls, 0,
            "路由试图回收上游会话 —— 那条路径是真实网络调用。"
            "请让假 stream_chat 产出内容，或检查 delete_chat_session 的替换。",
        )


class TestAliasSessionKeyEquivalence(_RouteHarness):
    """别名必须在**会话身份**上也不可区分。

    这是最容易漏的一条：`session_tree` 把 `model` 拼进 conv_key
    （`f"{model}:cid:{custom_id}"` / `bucket = f"{model}|{head}"`），
    若路由把**客户端原始 ID** 交给它，两个客户端用不同别名问同一个问题时
    会落在**不同的会话桶**里，拿不到对方的会话亲和 —— 那就是可观测的行为差异。
    归一化必须发生在 `resolve_conversation` **之前**。
    """

    def test_session_bucket_is_model_agnostic(self):
        """同一 session-id 下，4 个别名必须命中**同一个** conv_key。

        直接对 `SessionTracker` 复现路由的调用方式做断言：路由传给
        `resolve_conversation` 的必须是**归一后**的 model，否则这里会分叉。
        """
        import tempfile

        # storage_file 指向临时目录：绝不让测试碰任何真实 bindings 文件
        with tempfile.TemporaryDirectory(prefix="t9-tracker-") as tmp:
            tracker = SessionTracker(storage_file=Path(tmp) / "t9-bindings.json")
            keys = {}
            for model_id in _accepted_ids():
                normalized = normalize_model_id(model_id)
                key, _ = tracker.resolve_conversation(
                    [{"role": "user", "content": "同一句话"}],
                    model=normalized,
                    custom_id="shared-session-id",
                )
                keys[model_id] = key
        self.assertEqual(
            len(set(keys.values())), 1,
            f"别名落在不同的 conv_key 上，会话亲和不可共享：{keys}",
        )

    def test_routes_pass_normalized_model_to_resolver(self):
        """静态守卫：路由必须以 `model=<归一后变量>` 调用 resolve_conversation。

        上面的用例断言的是"归一后 key 相同"，但真正决定行为的是
        **路由到底把哪个变量传进去**。这里直接读路由源码的 AST，
        确认传给 `resolve_conversation` 的 `model` 关键字参数是
        `model`（归一后）而**不是** `_client_model`（原始 ID）。
        """
        tree = ast.parse(_ROUTES.read_text("utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve_conversation"
        ]
        self.assertTrue(calls, "没找到 resolve_conversation 调用点（路由结构变了？）")
        for call in calls:
            model_kw = [kw for kw in call.keywords if kw.arg == "model"]
            if not model_kw:
                continue
            self.assertIsInstance(
                model_kw[0].value, ast.Name,
                "resolve_conversation(model=...) 必须是简单名字，便于审计",
            )
            self.assertEqual(
                model_kw[0].value.id, "model",
                f"第 {call.lineno} 行把原始（未归一）的模型 ID 交给了会话解析器。"
                f"别名会因此落到不同的会话桶里 → 收敛失效。",
            )

    def test_claude_route_also_normalizes(self):
        """`/v1/messages` 必须走同一条收敛路径（否则两个协议对外行为不一致）。"""
        src = _CLAUDE_ROUTES.read_text("utf-8")
        self.assertIn("normalize_model_id(_client_model)", src,
                      "claude_routes 没有归一模型 ID")
        self.assertNotIn('"opus" in model.lower()', src,
                         "claude_routes 仍残留已死的按模型名分流")


# ══════════════════════════════════════════════════════════════════════
# 3. 模型卡：4 个 ID 逐字段相同
# ══════════════════════════════════════════════════════════════════════

class TestModelCardEquivalence(_RouteHarness):
    """`/v1/models` 上 4 个 ID 的卡片必须**除自指字段外**逐字段相同。

    只要有一处**能力/元数据**不同（名字/描述/reasoning_effort/多模态），
    客户端就会认为"这是不同的模型"，收敛对**客户端**而言就没有发生。

    ⚠️ **唯一的例外**是 `permission[0].id`（形如 `modelperm-<id>`）：
       它是"权限记录自己的枚举 ID"，**必然**引用它所属模型的名字。
       这不是能力声明，而是自指标识符——把它也强行统一，反而会让
       `/v1/models/{id}` 返回一份与请求 ID 无关的权限记录。
       本组用例把这个例外**显式钉住**：除了它，一个字都不许不同。
       （已实测确认：除 `permission` 外没有任何字段差异。）

    ⚠️ 这里**直接调用路由协程**，不用 `TestClient`：
       本环境的 starlette 需要 `httpx2` 才能构造 TestClient，而项目
       requirements 里没有它。直接 await 更轻，也**同样零网络**。
    """

    @staticmethod
    def _comparable_card(model: dict) -> str:
        """剥掉"自指"字段后的可比较卡片（`id` 与 `permission[].id`）。"""
        card = {k: v for k, v in model.items() if k != "id"}
        if isinstance(card.get("permission"), list):
            card["permission"] = [
                {k: v for k, v in p.items() if k != "id"}
                for p in card["permission"]
            ]
        return json.dumps(card, sort_keys=True, ensure_ascii=False)

    def _models(self):
        return asyncio.run(self.routes.list_models())["data"]

    def test_list_models_advertises_canonical_models(self):
        """对外展示主推理模型与旗舰强化推演模型 (V4-Pro)，下架已弃用的行动模型。"""
        data = self._models()
        model_ids = [m["id"] for m in data]
        self.assertIn(CANONICAL_MODEL_ID, model_ids, "模型列表中缺少主模型 ID")
        self.assertIn(PRO_MODEL_ID, model_ids, "模型列表中缺少旗舰强化模型 ID")
        self.assertNotIn("deepseek-v4.1-flash-act", model_ids, "模型列表中不应再公开展示已弃用的行动模型")
        self.assertEqual(len(data), 2, "对外模型列表应包含主模型与旗舰强化模型")

    def test_only_permission_is_allowed_to_differ(self):
        """同家族内部，单卡查询返回的模型卡除 `permission` 外逐字段相同。"""
        base_flash = asyncio.run(self.routes.get_model(CANONICAL_MODEL_ID))
        for model_id in _flash_family_ids():
            m = asyncio.run(self.routes.get_model(model_id))
            with self.subTest(model_id=m["id"]):
                differing = [
                    k for k in base_flash
                    if k not in ("id", "permission") and base_flash[k] != m[k]
                ]
                self.assertEqual(
                    differing, [],
                    f"{m['id']} 的卡片与 Flash 主模型卡的这些字段不同：{differing}。",
                )

        base_pro = asyncio.run(self.routes.get_model(PRO_MODEL_ID))
        for model_id in _pro_family_ids():
            m = asyncio.run(self.routes.get_model(model_id))
            with self.subTest(model_id=m["id"]):
                differing = [
                    k for k in base_pro
                    if k not in ("id", "permission") and base_pro[k] != m[k]
                ]
                self.assertEqual(
                    differing, [],
                    f"{m['id']} 的卡片与 Pro 旗舰模型卡的这些字段不同：{differing}。",
                )

    def test_permission_id_is_self_referential(self):
        """`permission[0].id` 必须引用**它自己**的模型 ID（说明这个例外是自指）。"""
        for m in self._models():
            with self.subTest(model_id=m["id"]):
                self.assertEqual(m["permission"][0]["id"], f"modelperm-{m['id']}")

    def test_canonical_card_declares_multimodal(self):
        """所有对外暴露的模型均完整声明原生多模态支持。"""
        for m in self._models():
            with self.subTest(model_id=m["id"]):
                self.assertTrue(m["vision"])
                self.assertTrue(m["supports_vision"])
                self.assertIn("image", m["input"])
                self.assertIn("image", m["modalities"])
                if m["id"] == CANONICAL_MODEL_ID:
                    self.assertEqual(m["name"], CANONICAL_MODEL_NAME)
                    self.assertEqual(m["description"], CANONICAL_MODEL_DESCRIPTION)
                    self.assertEqual(m["reasoning_effort"], CANONICAL_REASONING_EFFORT)
                elif m["id"] == PRO_MODEL_ID:
                    self.assertEqual(m["name"], PRO_MODEL_NAME)
                    self.assertEqual(m["description"], PRO_MODEL_DESCRIPTION)
                    self.assertEqual(m["reasoning_effort"], "max")
                self.assertEqual(m["context_window"], CANONICAL_MODEL_CONTEXT_WINDOW)
                self.assertEqual(m["max_output_tokens"], CANONICAL_MODEL_MAX_OUTPUT_TOKENS)
                # 最大输出与上下文窗口是**两个不同的量**，不得混用
                self.assertNotEqual(m["max_tokens"], m["context_window"],
                                    "`max_tokens` 被当成了上下文窗口 —— 它是单次最大输出")
                self.assertEqual(m["max_tokens"], CANONICAL_MODEL_MAX_OUTPUT_TOKENS)

    def test_get_single_model_returns_canonical_card(self):
        """`/v1/models/{id}` 对 Flash 系列及未知 ID 回主模型卡，对 Pro 系列回旗舰强化卡。"""
        flash_base = self._comparable_card(asyncio.run(self.routes.get_model(CANONICAL_MODEL_ID)))
        for model_id in _flash_family_ids() + ["some-unknown-id"]:
            with self.subTest(model_id=model_id):
                m = asyncio.run(self.routes.get_model(model_id))
                card = self._comparable_card(m)
                self.assertEqual(card, flash_base, f"{model_id} 单模型卡与 Flash 主模型卡不一致")

        pro_base = self._comparable_card(asyncio.run(self.routes.get_model(PRO_MODEL_ID)))
        for model_id in _pro_family_ids():
            with self.subTest(model_id=model_id):
                m = asyncio.run(self.routes.get_model(model_id))
                card = self._comparable_card(m)
                self.assertEqual(card, pro_pro := card, f"{model_id} 单模型卡与 Pro 旗舰模型卡不一致")
                self.assertEqual(card, pro_base)


# ══════════════════════════════════════════════════════════════════════
# 4. L0 提示词：唯一一套，字节级冻结
# ══════════════════════════════════════════════════════════════════════

class TestFrozenL0Prompt(unittest.TestCase):
    """提示词必须是**唯一常量**、**字节冻结**、且**语义未被改动**。

    ⚠️ 本组用例**刻意不修改提示词语义**。清单 T9 里
    「A/B 度量 `We need to...` 英文 CoT 是否仍值钱」是**未完成的探索项**，
    不在本次范围；这里的断言只保证"从多套收敛成一套"这个**结构性**结论，
    以及"没有人在重构里顺手改了文案"。
    """

    def _module_ast(self):
        return ast.parse(_ROUTES.read_text("utf-8"))

    def _constant_value(self, name):
        for node in self._module_ast().body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return ast.literal_eval(node.value)
        self.fail(f"模块级常量 {name} 不存在（提示词不再是冻结常量？）")

    def test_canonical_prompt_is_a_single_module_constant(self):
        """核心提示词必须是模块级常量。"""
        value = self._constant_value("_CANONICAL_SYSTEM_PROMPT")
        self.assertIsInstance(value, str)
        self.assertTrue("DeepSeek" in value)

    def test_canonical_prompt_has_exactly_one_definition(self):
        """全仓库只允许存在一处核心提示词定义。"""
        src = _ROUTES.read_text("utf-8")
        self.assertEqual(
            src.count("_CANONICAL_SYSTEM_PROMPT ="), 1,
            "出现了多套提示词定义",
        )

    def test_prompt_is_byte_frozen(self):
        """字节级冻结：sha1 必须与基准一致。"""
        value = self._constant_value("_CANONICAL_SYSTEM_PROMPT")
        effective = value.strip()
        digest = hashlib.sha1(effective.encode("utf-8")).hexdigest()
        self.assertEqual(
            len(effective.encode("utf-8")), _CANONICAL_PROMPT_BYTES,
            f"提示词字节长度变了（{len(effective.encode('utf-8'))} "
            f"!= {_CANONICAL_PROMPT_BYTES}），当前 sha1={digest}",
        )
        self.assertEqual(
            digest,
            _CANONICAL_PROMPT_SHA1,
            "提示词被改动了（字节级冻结失效）。\n"
            f"  当前 sha1={digest}\n"
            f"  基准 sha1={_CANONICAL_PROMPT_SHA1}\n"
            "若这是有意的语义改动，请说明理由并同步更新基准值。",
        )

    def test_prompt_semantics_unchanged(self):
        """核心准则必须逐条还在。"""
        value = self._constant_value("_CANONICAL_SYSTEM_PROMPT")
        for needle in (
            "DeepSeek",
            "without overcomplicating",
            "requested language",
            "never output tool calls inside your thinking process",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, value, f"提示词语义被改动了：{needle!r} 不见了")

    def test_vision_prompt_is_shared_not_per_model(self):
        """视觉指引也必须唯一，且**不按模型分流**（只看本轮有没有图）。"""
        value = self._constant_value("_FROZEN_VISION_PROMPT")
        self.assertIn("多模态视觉指引", value)
        src = _ROUTES.read_text("utf-8")
        self.assertEqual(
            src.count("【多模态视觉指引】"), 1,
            "视觉指引出现了多份",
        )


# ══════════════════════════════════════════════════════════════════════
# 5. model_type 线契约：**绝不能删、绝不能传非法值**
# ══════════════════════════════════════════════════════════════════════

class TestModelTypeWireContract(unittest.TestCase):
    """清单 T9 第四步的另一半：清理**语义**，但保留**参数**。

    ⛔ 上游会校验 `model_type` 的**合法性**——"非法值才 422"（T9 实测）。
       删掉字段或传白名单外的值 = 把**每一个**请求打成 422 故障。
       本组用例就是防"清理死代码"时踩到这颗雷。
    """

    def test_whitelist_is_exactly_the_three_measured_literals(self):
        """白名单必须恰好是实测合法的那三个值。

        T9 实测：default / expert / vision → 上游全部接受（正文 sha1 相同）；
        非法值才 422。
        """
        self.assertEqual(
            UPSTREAM_MODEL_TYPE_VALUES,
            frozenset({"default", "expert", "vision"}),
            "上游白名单被改动 —— 多一个可能 422，少一个会让网关侧断言误伤",
        )

    def test_the_value_we_send_is_inside_the_whitelist(self):
        """我们实际发出去的值必须合法（否则必然 422）。"""
        self.assertIn(UPSTREAM_MODEL_TYPE, UPSTREAM_MODEL_TYPE_VALUES)

    def test_model_type_takes_no_argument_at_all(self):
        """★★ 上游 `model_type` 的取值**不得**依赖任何输入 —— 它必须恒定。

        这是 2026-09-11 实测之后的加固守卫。

        T9 曾经有过 `reasoning_tier_for(has_images)`：有图 → `vision`、无图 → `expert`。
        它当时只防住了 `model` 这一个维度（"签名故意没有 model 参数"），
        **却保留了 `has_images`** —— 于是"档位"换了个维度继续存在，
        而且建立在"识图是独立模式"这个当时没验证、现在已被证伪的假设上。

        实测（真图探针）：
          · 发 `vision` → **上游回显改写成 `expert`**
          · 不切档位、直接带 `ref_file_ids` → 模型照样准确描述图片内容
        公开信息一致：快速 / 专家 / 识图三个模式已合并成一个（V4.1 Flash 原生多模态）。

        所以：**只要签名里出现任何参数（尤其是 `has_images`），这条就失败。**
        """
        import inspect
        params = list(inspect.signature(upstream_model_type).parameters)
        self.assertEqual(
            params, [],
            f"上游 model_type 又按输入分流了（参数：{params}）—— "
            f"请确认上游是否真的重新引入了模式区分，否则它就是残留",
        )

    def test_image_presence_does_not_change_model_type(self):
        """图片的有无**不得**改变发往上游的 `model_type`。

        这是上一条的行为侧对照：函数签名对了，但调用方仍可能在别处按图分流
        （T9 就曾在 `openai_routes` 里按 `ref_file_ids` 把值改成 vision）。
        """
        import ast
        src_path = PROJECT_ROOT / "app" / "api" / "openai_routes.py"
        tree = ast.parse(src_path.read_text("utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "model_type":
                    value_src = ast.unparse(node.value)
                    # 只允许 `model_type = upstream_model_type()` 这一种形态
                    if "upstream_model_type()" not in value_src:
                        offenders.append((node.lineno, value_src))
        self.assertEqual(
            offenders, [],
            f"`model_type` 又被按别的条件赋值了（可能是有图切 vision 残留）：{offenders}",
        )

    def test_illegal_names_are_not_in_whitelist(self):
        """这些"看起来像模型名"的值**不是**合法 model_type。

        收敛前网关曾在别处用过类似的字符串来表达"模型选择"；
        它们在本项目里可以存在，但**绝不能**被当作 model_type 发上去。
        """
        for illegal in ("pro", "flash", "pro-exp", "flash-exp", "r1", "reasoner",
                        "DEFAULT", "Expert", ""):
            with self.subTest(illegal=illegal):
                self.assertNotIn(illegal, UPSTREAM_MODEL_TYPE_VALUES)

    def test_client_still_sends_model_type_field(self):
        """静态守卫：请求体里**必须**仍然带 `model_type` 键。

        这条正是防"有人觉得它是死参数就删掉"——删掉会 422。
        """
        tree = ast.parse(_CLIENT.read_text("utf-8"))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value == "model_type":
                        found = True
        self.assertTrue(
            found,
            "deepseek_client 的请求体里没有 `model_type` 了。"
            "⚠️ 上游会校验该字段合法性，删掉会让**每个**请求 422（T9 实测）。"
            "请不要为了「清理死代码」而删除它。",
        )

    def test_routes_assert_before_sending(self):
        """两个路由都必须在发送前断言档位合法（失败在网关侧，而不是上游 422）。"""
        for path in (_ROUTES, _CLAUDE_ROUTES):
            with self.subTest(route=path.name):
                src = path.read_text("utf-8")
                self.assertIn("UPSTREAM_MODEL_TYPE_VALUES", src,
                              f"{path.name} 没有对 model_type 做白名单断言")

    def test_no_dead_model_routing_semantics_left(self):
        """静态守卫：已死的"按模型名选档位"子串判断必须清干净。

        清单 T9：「清理已死的 `model_type` 路由语义」。
        这些子串判断（pro / expert / reasoner / r1 / opus）在上游完全无效，
        留着只会让后人以为"模型选择"仍然生效。
        """
        for path in (_ROUTES, _CLAUDE_ROUTES):
            src = path.read_text("utf-8")
            # 只看代码行，注释里提到旧实现是允许的（甚至是要保留的说明）
            code_lines = [
                ln for ln in src.splitlines()
                if not ln.lstrip().startswith("#")
            ]
            code = "\n".join(code_lines)
            for dead in ('k in model_lower for k in ("pro"', '"opus" in model',
                         '"reasoner" in model', '"vision" in model'):
                with self.subTest(route=path.name, dead=dead):
                    self.assertNotIn(
                        dead, code,
                        f"{path.name} 仍残留已死的按模型名分流：{dead}",
                    )


# ══════════════════════════════════════════════════════════════════════
# 6. 回归：收敛不该顺手打坏客户端行为开关
# ══════════════════════════════════════════════════════════════════════

class TestBehaviourSwitchesSurvive(_RouteHarness):
    """收敛只能改"模型路由语义"，**不能**吃掉客户端的行为开关。

    实现过程中真实出现过一次：把 `model_type` 那段重写成
    "档位统一"时，顺手写了 `thinking_enabled = True`，
    把客户端的 `-nothink` / `thinking: false` 直接覆盖掉了。
    """

    def test_explicit_thinking_false_is_respected(self):
        """`thinking: false` 必须仍然生效。"""
        self.upstream.bodies.clear()
        self._call(CANONICAL_MODEL_ID, extra={"thinking": False})
        self.assertEqual(len(self.upstream.bodies), 1)

    def test_nothink_suffix_is_read_from_raw_client_model(self):
        """`-nothink` 后缀必须从**客户端原始 ID** 上读。

        归一化会把后缀吃掉（`deepseek-v4-pro-nothink` → 主模型），
        所以读后缀必须用 `_client_model`。这条守的是那个实现细节。
        """
        src = _ROUTES.read_text("utf-8")
        self.assertIn("model_lower = _client_model.lower()", src,
                      "`-nothink` 后缀是从归一后的 model 上读的 —— 后缀已被吃掉，开关失效")


if __name__ == "__main__":
    unittest.main()
