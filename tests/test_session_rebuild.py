"""
A1：把「账号粘性」与「上游会话复用」拆开（显式会话 ID 路径）。

────────────────────────────────────────────────────────────────────
故障链（为什么必须拆）
────────────────────────────────────────────────────────────────────
`resolve_conversation` 在 `acquire_account` **之前**执行，而

    preferred_account = cached_session.account_label

是**唯一**的账号粘性机制。于是这两件事被同一个 conv_key 绑死：

    conv_key 变了 → 账号跟着变
    conv_key 不变 → 上游会话被一直复用

客户端压缩上下文后，消息前缀**必然**变化，于是两条路各坏一半：

  · 走内容哈希（客户端没给显式会话 ID）
      → key 变 → **可能换号**，而 `chat_session_id` / `file_id` 实测
        **不可跨账号** → 历史里引用的图片全丢
  · 走显式 ID（客户端给了 x-session-id / conversation-id / prompt_cache_key）
      → key 不变 → **复用那个已经填满的会话**
        → 下一轮直接撞 `达到对话长度上限，请开启新对话`

修法：显式 ID 决定**账号**（key 稳定），前缀决定**会话**。
前缀不兼容时只清掉 `deepseek_session_id`、**保留这条绑定**：

    → 换会话，但不换号（conv_src=cid-prefix-changed）

本文件把这条语义钉死，并守住调用方那侧的闸门
（没清干净 / 没放行 = 又去复用满会话 = 上一行的努力白费）。
"""

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.engine.session_tree import SessionTracker  # noqa: E402

CID = "C-1"


def U(text):
    return {"role": "user", "content": text}


def A(text):
    return {"role": "assistant", "content": text}


def _turn(n, extra=None):
    """第 n 轮的"正常增长"消息序列（前缀逐轮变长，但始终兼容）。"""
    msgs = [U("项目开工"), A("好的"), U("第一件事")]
    for i in range(2, n + 1):
        msgs += [A(f"答{i}"), U(f"第 {i} 件事")]
    if extra:
        msgs += extra
    return msgs


class RebuildBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file = Path(self._tmp.name) / "bindings.json"
        self.t = SessionTracker(storage_file=self.file)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, msgs, cid=CID, model="m"):
        return self.t.resolve_conversation(
            msgs, model=model, custom_id=cid, allow_reuse=True,
        )

    def _rebuild_register(self, conv_key, msgs, account="acct-1", sid="S-new"):
        """模拟路由在"闸门放行"之后做的事：新建上游会话并重新登记绑定。"""
        return self.t.register_session(
            conv_key, account, sid,
            prefix=self.t._prefix_signatures(msgs),
        )


class TestAccountStickinessSurvivesPrefixChange(RebuildBase):
    """A1 的核心承诺：换会话，但不换号。"""

    def setUp(self):
        super().setUp()
        self.msgs1 = [U("项目开工"), A("好的"), U("第一件事")]
        self.key, src = self._resolve(self.msgs1)
        self.assertEqual(src, "cid")
        self.ctx = self._rebuild_register(
            self.key, self.msgs1, account="acct-7", sid="S-full")

    def test_prefix_change_forces_new_upstream_session(self):
        """客户端压缩上下文 → 前缀不兼容 → 必须换一个上游会话。

        不换的话下一轮会继续往那个已经填满的会话里发，
        实测结果就是 `达到对话长度上限，请开启新对话`（终态，重试无用）。
        """
        compacted = [U("【历史摘要】此前完成了三件事"), U("继续")]
        _key, src = self._resolve(compacted)
        self.assertEqual(
            src, "cid-prefix-changed",
            "前缀变了却仍报 cid —— 上游会话会被继续复用，必然撞长度上限",
        )
        self.assertEqual(
            self.t.get_session(self.key).deepseek_session_id, "",
            "必须清空上游会话 ID，否则调用方会照旧复用满会话",
        )

    def test_account_binding_is_kept(self):
        """同一个模板 ID 下，账号绝不能被换掉。

        `chat_session_id` 与 `file_id` 实测**不可跨账号**
        （biz_code:1 invalid chat session id），换号 = 历史里引用的图片全丢。
        """
        compacted = [U("【历史摘要】此前完成了三件事"), U("继续")]
        self._resolve(compacted)
        ctx = self.t.get_session(self.key)
        self.assertIsNotNone(
            ctx, "前缀变化时把整条绑定删掉 = 账号粘性一起丢，图片全废")
        self.assertEqual(ctx.account_label, "acct-7")
        self.assertEqual(ctx.conversation_id, self.key)

    def test_message_id_is_cleared_too(self):
        """`last_message_id` 属于旧会话的树，留着会被当成新会话的 parent。"""
        self.t.update_message_id(self.key, "mid-old")
        self._resolve([U("【历史摘要】"), U("继续")])
        self.assertIsNone(self.t.get_session(self.key).last_message_id)

    def test_old_session_id_is_not_resurrected_by_later_turns(self):
        """换会话之后，后续轮次复用到的必须是**新**会话。"""
        compacted = [U("【历史摘要】此前完成了三件事"), U("继续")]
        self._resolve(compacted)
        self._rebuild_register(self.key, compacted, sid="S-fresh")

        nxt = compacted + [A("好"), U("下一步")]
        _key, src = self._resolve(nxt)
        self.assertEqual(src, "cid")
        self.assertEqual(self.t.get_session(self.key).deepseek_session_id, "S-fresh",
                         "又拿到旧会话 = 白换了一次")

    def test_rebuild_happens_once_not_every_turn(self):
        """收敛性：换完会话后必须稳定下来，否则会变成"每轮新建"。

        每轮新建的代价是**每轮重传全量上下文**（实测：新会话 ~200-600ms
        且上游看到的是一个不断新建会话的账号）—— 这比复用满会话更危险。
        """
        compacted = [U("【历史摘要】"), U("继续")]
        self._resolve(compacted)
        self._rebuild_register(self.key, compacted, sid="S-1")

        cursor = compacted
        rebuilds = 0
        for i in range(6):
            cursor = cursor + [A(f"答{i}"), U(f"第 {i} 步")]
            _k, src = self._resolve(cursor)
            if src == "cid-prefix-changed":
                rebuilds += 1
                self._rebuild_register(self.key, cursor, sid=f"S-{i}")
            else:
                self.assertEqual(src, "cid")
        self.assertEqual(rebuilds, 0, "稳定增长的消息前缀被误判为变化")


class TestNoRegressionOnStablePrefix(RebuildBase):
    """A1 不得把"正常复用"一起打掉 —— 那是这次改动最大的风险。"""

    def test_unchanged_prefix_still_reuses(self):
        msgs = [U("项目开工"), A("好的"), U("第一件事")]
        key, src = self._resolve(msgs)
        self._rebuild_register(key, msgs, sid="S-1")

        key2, src2 = self._resolve(msgs)
        self.assertEqual(src2, "cid")
        self.assertEqual(key2, key)
        self.assertEqual(self.t.get_session(key).deepseek_session_id, "S-1",
                         "前缀没变却把会话丢了 = 每轮新建，比原来更糟")

    def test_prefix_growth_still_reuses(self):
        """逐轮增长是**兼容**（新前缀以旧前缀开头），绝不能算变化。"""
        cursor = [U("项目开工")]
        key, _ = self._resolve(cursor)
        self._rebuild_register(key, cursor, sid="S-1")

        for i in range(5):
            cursor = cursor + [A(f"答{i}"), U(f"第 {i} 件事")]
            k, src = self._resolve(cursor)
            self.assertEqual(src, "cid", f"第 {i+2} 轮被判成前缀变化")
            self.assertEqual(k, key)

    def test_prefix_extension_resets_the_baseline(self):
        """命中后必须把基线延伸到本次请求，否则下一轮永远只比旧基线。

        （只比旧基线不会出错，但会让"存 2 条 / 本轮 8 条"这类信息一直不更新，
        将来真变化时日志里的条数会误导排查。）
        """
        cursor = [U("项目开工")]
        key, _ = self._resolve(cursor)
        self._rebuild_register(key, cursor, sid="S-1")
        self.assertEqual(len(self.t.get_session(key).prefix), 1)

        cursor = cursor + [A("答"), U("第二件事")]
        self._resolve(cursor)
        self.assertEqual(len(self.t.get_session(key).prefix), 3)

    def test_first_call_with_no_binding_is_not_a_mismatch(self):
        """第一次用这个显式 ID：没有绑定可谈，必须是 cid 而不是 prefix-changed。"""
        key, src = self._resolve([U("第一次")])
        self.assertEqual(src, "cid")
        self.assertIsNone(self.t.get_session(key), "解析不该凭空造出绑定")

    def test_allow_reuse_false_does_not_touch_the_binding(self):
        """不复用的那一轮（无历史、无显式 ID）不能顺手清掉别人的会话。"""
        msgs = [U("项目开工")]
        key, _ = self._resolve(msgs)
        self._rebuild_register(key, msgs, sid="S-1")

        _k, src = self.t.resolve_conversation(
            [U("完全不同的一句话")], model="m", custom_id=CID, allow_reuse=False)
        self.assertEqual(src, "cid")
        self.assertEqual(self.t.get_session(key).deepseek_session_id, "S-1")

    def test_legacy_binding_without_prefix_is_baselined_not_rebuilt(self):
        """老版本落盘的绑定没有 prefix 字段 —— 不能因为"缺基线"就每轮重建。

        缺基线时**无法判断**前缀是否变化，此时沿用旧行为（复用）并就地补上基线，
        是唯一不会把老数据一击打死的选择：否则升级后所有在跑的会话会在下一轮
        全部变成新建（每轮重传全量上下文）。
        """
        import io
        import contextlib

        msgs = [U("项目开工"), A("好"), U("第一件事")]
        key, _ = self._resolve(msgs)
        self._rebuild_register(key, msgs, sid="S-legacy")
        self.t.get_session(key).prefix = []          # 模拟老数据

        _k, src = self._resolve([U("完全不同的一条"), U("继续")])
        self.assertEqual(src, "cid", "缺基线不该触发重建（会变成每轮新建）")
        self.assertEqual(self.t.get_session(key).deepseek_session_id, "S-legacy")
        self.assertEqual(len(self.t.get_session(key).prefix), 2,
                         "缺基线时必须就地补上，否则永远无法判断变化")

    def test_rebuild_is_visible_in_the_log(self):
        """重建必须留下一行可 grep 的日志 —— 否则线上无法判断它是否在按预期触发。

        `conv_src=cid-prefix-changed` 是**计数器**：每次上下文压缩应当只出现一次。
        若日志里每轮都出现，说明客户端的消息前缀不稳定，A2（让客户端带上稳定
        会话标识）必须回退 —— 这个判断只能靠这行日志做。
        """
        import io
        import contextlib

        msgs = [U("项目开工"), A("好"), U("第一件事")]
        key, _ = self._resolve(msgs)
        self._rebuild_register(key, msgs, sid="S-full")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _k, src = self._resolve([U("【历史摘要】"), U("继续")])
        out = buf.getvalue()
        self.assertEqual(src, "cid-prefix-changed")
        self.assertIn("换会话但不换号", out, "重建没留下可 grep 的日志")
        self.assertIn("前缀已变化", out)

    def test_rebuild_survives_restart(self):
        """清空后的绑定也要落盘 —— 否则重启会把满会话"复活"。"""
        msgs = [U("项目开工"), A("好"), U("第一件事")]
        key, _ = self._resolve(msgs)
        self._rebuild_register(key, msgs, sid="S-full")
        self.t.flush_bindings(force=True)

        fresh = SessionTracker(storage_file=self.file)
        self.assertEqual(fresh.get_session(key).deepseek_session_id, "S-full")

        self._resolve([U("【历史摘要】"), U("继续")])   # 清空
        self.t.flush_bindings(force=True)

        again = SessionTracker(storage_file=self.file)
        ctx = again.get_session(key)
        self.assertIsNotNone(ctx, "绑定必须还在（账号粘性靠它）")
        self.assertEqual(ctx.deepseek_session_id, "",
                         "重启后满会话被复活 = 又撞长度上限")


# ─────────────────────────────────────────────────────────────
# 调用方那侧的闸门：解析层清干净了，路由不放行照样白搭
# ─────────────────────────────────────────────────────────────
def _read(rel):
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _reuse_gate_test_source(rel):
    """取出"复用上游会话"那个 if 的**条件表达式源码**（注释与文档串不可能满足它）。

    用 AST 而不是字符串搜索，是因为这两种要求必须区分开：
    条件里**真的**读了 `deepseek_session_id`，和某段注释里写了这个词。
    """
    src = _read(rel)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.get_source_segment(src, node.test) or ""
        if "cached_session" in test and "account_label" in test:
            return test
    return None


class TestRouteGatesOnSessionPresence(unittest.TestCase):
    """两个路由都必须"确实有上游会话可复用"才复用。

    少了这个条件，A1 在解析层清空的 `deepseek_session_id` 会被无视：
    `cached_session` 对象还在（这正是我们要的，账号粘性靠它），
    于是闸门照旧放行 → 又去复用那个满会话 → 前缀变化的处理完全失效。
    """

    ROUTES = ("app/api/openai_routes.py", "app/api/claude_routes.py")

    def test_both_routes_gate_on_deepseek_session_id(self):
        for rel in self.ROUTES:
            with self.subTest(route=rel):
                test = _reuse_gate_test_source(rel)
                self.assertIsNotNone(
                    test, f"{rel} 里找不到复用闸门（cached_session + account_label）")
                self.assertIn(
                    "cached_session.deepseek_session_id", test,
                    f"{rel} 的复用闸门没检查上游会话是否存在："
                    f"A1 清空的会话会被继续复用。当前条件={test!r}")

    def test_resolution_happens_before_the_cache_lookup(self):
        """必须先 resolve 再 get_session。

        反过来（先取 cached_session 再 resolve）在**内容哈希路径**上会读到
        另一个会话的 key —— 账号粘性会指到错的账号上，而账号级 file_id
        不可跨账号，历史图片照样丢。
        """
        for rel in self.ROUTES:
            with self.subTest(route=rel):
                src = _read(rel)
                i_res = src.find("resolve_conversation(")
                i_get = src.find("get_session(conv_key)")
                self.assertNotEqual(i_res, -1, f"{rel} 没有 resolve_conversation")
                self.assertNotEqual(i_get, -1, f"{rel} 没有 get_session(conv_key)")
                self.assertLess(
                    i_res, i_get,
                    f"{rel}: get_session 跑在 resolve_conversation 之前 —— "
                    f"会拿到错的会话/账号")

    def test_resolver_verdict_is_not_overwritten_in_logs(self):
        """`conv_src` 必须保留解析层的判定，不能被 `sid_src` 整条覆盖。

        旧写法 `conv_src = sid_src if _client_sid else conv_src` 在**有显式 ID
        时**（也就是唯一会走 A1 分支的那条路）把 `cid-prefix-changed` 丢掉，
        于是"前缀变化 → 换会话不换号"在日志里根本看不见，改动不可验证。
        """
        src = _read("app/api/openai_routes.py")
        self.assertNotIn(
            "conv_src = sid_src if _client_sid else conv_src", src,
            "conv_src 被 sid_src 覆盖 → A1 的 cid-prefix-changed 永远进不了日志")
        i = src.find("conv_src = f\"{conv_src}({sid_src})\"")
        self.assertNotEqual(i, -1, "conv_src 未保留解析层判定")

    def test_claude_path_reports_session_source(self):
        """/v1/messages 原来一条会话来源日志都没有 —— 该路径上的故障不可观测。"""
        src = _read("app/api/claude_routes.py")
        self.assertIn("conv_src={conv_src}", src,
                      "/v1/messages 没有会话来源日志，A1 在这条路径上不可验证")


if __name__ == "__main__":
    unittest.main(verbosity=2)
