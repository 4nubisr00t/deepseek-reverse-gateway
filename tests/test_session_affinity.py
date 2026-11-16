"""
T1 会话亲和测试：会话身份解析、抗串线、跨重启持久化。

背景（实测复现，2026-09-10）：
旧实现把 `hash(第一条用户消息)` 当会话身份，注册是覆盖语义。于是两个无关会话
只要首条用户消息相同（例如都以「继续」开头）：

    会话 A 第 1 轮 → 绑定 K → S_A
    会话 B 第 1 轮 → 绑定 K → S_B     ← S_A 被覆盖
    会话 A 第 2 轮 → 命中 K → 拿到 S_B → 只发 A 的最新一句 → 模型在 B 的上下文里回答

这不是效率问题，是**答案串线**。本文件把新规则钉死：
  · 桶内多个候选前缀都匹配 → **不复用**（宁可多建一次会话）
  · 只有唯一匹配才复用
  · 没有冲突时行为与旧实现一致（不引入回归）
  · 绑定必须跨重启存活
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.engine.session_tree import (
    SessionTracker,
    SessionContext,
    extract_client_session_id,
    PREFIX_MAX_MESSAGES,
)


def U(text):
    return {"role": "user", "content": text}


def A(text):
    return {"role": "assistant", "content": text}


class TrackerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file = Path(self._tmp.name) / "bindings.json"
        self.t = SessionTracker(storage_file=self.file)

    def tearDown(self):
        self._tmp.cleanup()

    def _new(self) -> SessionTracker:
        """模拟进程重启：新建一个 tracker，从磁盘恢复"""
        return SessionTracker(storage_file=self.file)

    def _register(self, conv_key, account="acct-1", sid="sess-1", msgs=None):
        return self.t.register_session(
            conv_key, account, sid,
            prefix=self.t._prefix_signatures(msgs) if msgs else None,
        )


class TestAntiBlending(TrackerBase):
    """核心：同开头的不同会话绝不能互相复用。"""

    def test_same_opening_second_turn_is_not_confused(self):
        """A 第 2 轮绝不能拿到 B 的会话（旧实现正是这里串线）"""
        # A 第 1 轮 —— 真实路由在这一轮会传 allow_reuse=False（无历史、无显式标识）
        a1_msgs = [U("继续")]
        ka1, src = self.t.resolve_conversation(a1_msgs, model="m", allow_reuse=False)
        self.assertEqual(src, "no-reuse")
        self._register(ka1, sid="S_A", msgs=a1_msgs)

        # B 第 1 轮（首条消息与 A 完全相同）
        b1_msgs = [U("继续")]
        kb1, src = self.t.resolve_conversation(b1_msgs, model="m", allow_reuse=False)
        self.assertEqual(src, "no-reuse", "第 1 轮不该复用（与旧实现一致）")
        self.assertNotEqual(kb1, ka1, "必须给 B 一个独立的会话键，绝不能覆盖 A 的绑定")
        self._register(kb1, sid="S_B", msgs=b1_msgs)

        # A 第 2 轮：两个候选（A1、B1）的前缀都匹配 → 必须判定为新会话
        a2_msgs = [U("继续"), A("好的"), U("A 项目的问题")]
        ka2, src = self.t.resolve_conversation(a2_msgs, model="m")
        self.assertEqual(src, "ambiguous-new",
                         "同开头的两个会话都匹配时必须放弃复用")
        self.assertNotIn(ka2, (ka1, kb1))
        # 关键断言：绝不能落到 S_B 上
        got = self.t.get_session(ka2)
        self.assertIsNone(got, "歧义时不该返回任何已有会话")
        self._register(ka2, sid="S_A2", msgs=a2_msgs)

        # 顺便断言 A 第 1 轮的绑定没被 B 覆盖（旧实现正是在这里被覆盖的）
        self.assertEqual(self.t.get_session(ka1).deepseek_session_id, "S_A")

        # B 第 2 轮（内容与 A 不同）→ 同样歧义 → 新会话
        b2_msgs = [U("继续"), A("请问要做什么"), U("B 项目的问题")]
        kb2, src = self.t.resolve_conversation(b2_msgs, model="m")
        self.assertEqual(src, "ambiguous-new")
        self._register(kb2, sid="S_B2", msgs=b2_msgs)

        # 第 3 轮起收敛：A 唯一命中 A2，B 唯一命中 B2
        a3 = a2_msgs + [A("……"), U("继续 A")]
        ka3, src = self.t.resolve_conversation(a3, model="m")
        self.assertEqual(src, "prefix")
        self.assertEqual(self.t.get_session(ka3).deepseek_session_id, "S_A2")

        b3 = b2_msgs + [A("……"), U("继续 B")]
        kb3, src = self.t.resolve_conversation(b3, model="m")
        self.assertEqual(src, "prefix")
        self.assertEqual(self.t.get_session(kb3).deepseek_session_id, "S_B2")

    def test_no_reuse_never_hands_back_existing_key(self):
        """回归：`allow_reuse=False` 时绝不能交出已有会话的 key。

        否则调用方随后的 `register_session(conv_key, ...)` 会**覆盖**那个会话的
        绑定 —— 串线又回来了。这是单测抓到的一个真实缺陷：
        解析（resolve）与注册（register）的语义原本不自洽。
        """
        m = [U("同一句话")]
        k, _ = self.t.resolve_conversation(m, model="m", allow_reuse=False)
        ctx = self._register(k, sid="S_原会话", msgs=m)

        k2, src = self.t.resolve_conversation(m, model="m", allow_reuse=False)
        self.assertEqual(src, "no-reuse")
        self.assertNotEqual(k2, k, "不复用时必须给全新的 key")
        self._register(k2, sid="S_新会话", msgs=m)

        # 原来那个会话的绑定必须完好无损
        self.assertEqual(self.t.get_session(k).deepseek_session_id, "S_原会话")

    def test_no_collision_behaves_like_before(self):
        """没有冲突时必须照常复用 —— 不能为了安全把会话复用整体关掉"""
        msgs = [U("帮我看看这个项目")]
        k1, src = self.t.resolve_conversation(msgs, model="m")
        self.assertEqual(src, "new")
        self._register(k1, sid="S1", msgs=msgs)

        for i in range(1, 6):
            later = msgs + [A(f"答{i}"), U(f"追问{i}")]
            k, src = self.t.resolve_conversation(later, model="m")
            self.assertEqual(k, k1, f"第 {i+1} 轮应当复用同一个会话")
            self.assertEqual(src, "prefix")
            msgs = later

    def test_longest_unique_prefix_wins(self):
        """桶内多种前缀长度都匹配时，取最长且唯一的那个"""
        m1 = [U("同开头")]
        k1, _ = self.t.resolve_conversation(m1, model="m")
        self._register(k1, sid="S1", msgs=m1)

        m2 = [U("同开头"), A("答"), U("问题2")]
        k2, _ = self.t.resolve_conversation(m2, model="m")
        self.assertEqual(k2, k1)          # 前缀 [u1] 是 m2 的前缀 → 唯一 → 复用
        self._register(k2, sid="S2", msgs=m2)   # 覆盖同一个 key，前缀延伸到 3 条

        m3 = m2 + [A("答2"), U("问题3")]
        k3, src = self.t.resolve_conversation(m3, model="m")
        self.assertEqual(src, "prefix")
        self.assertEqual(self.t.get_session(k3).deepseek_session_id, "S2")

    def test_prefix_extension_makes_future_turns_unique(self):
        """命中后延伸前缀，让后续轮次更容易唯一命中"""
        m = [U("开头")]
        k, _ = self.t.resolve_conversation(m, model="m")
        ctx = self._register(k, msgs=m)
        self.assertEqual(len(ctx.prefix), 1)
        m2 = m + [A("答"), U("问题2")]
        self.t.resolve_conversation(m2, model="m")
        self.assertEqual(len(self.t.get_session(k).prefix), 3,
                         "命中后应把前缀延伸到本次请求")

    def test_prefix_is_capped(self):
        msgs = [U("开头")] + [A(f"a{i}") for i in range(20)]
        sigs = self.t._prefix_signatures(msgs)
        self.assertEqual(len(sigs), PREFIX_MAX_MESSAGES)

    def test_different_first_message_never_collides(self):
        k1, _ = self.t.resolve_conversation([U("项目A")], model="m")
        self._register(k1, sid="SA", msgs=[U("项目A")])
        k2, src = self.t.resolve_conversation([U("项目B")], model="m")
        self.assertEqual(src, "new")
        self.assertNotEqual(k1, k2)

    def test_model_isolated(self):
        msgs = [U("同样的话")]
        k1, _ = self.t.resolve_conversation(msgs, model="pro")
        self._register(k1, sid="S1", msgs=msgs)
        k2, src = self.t.resolve_conversation(msgs, model="flash")
        self.assertEqual(src, "new", "不同模型不能共用会话（上游 model_type 不同）")

    def test_injected_reminders_do_not_break_prefix(self):
        """DSH 每轮注入的 system-reminder 内容不同，不该让前缀匹配失效"""
        first = [U("问题一"), U("<system-reminder>\n第一轮注入\n</system-reminder>")]
        k1, _ = self.t.resolve_conversation(first, model="m")
        self._register(k1, sid="S1", msgs=first)

        later = [
            U("问题一"),
            A("答一"),
            U("<system-reminder>\n完全不同的第二轮注入\n</system-reminder>"),
            U("问题二"),
        ]
        k2, src = self.t.resolve_conversation(later, model="m")
        self.assertEqual(src, "prefix",
                         "注入体参与指纹会让复用静默失效（T21 的同类问题）")
        self.assertEqual(k2, k1)

    def test_tool_messages_are_distinguished(self):
        """两条内容相同但 tool_call_id 不同的工具结果不能算同一条消息"""
        fn = lambda tcid: {"role": "tool", "content": "ok", "tool_call_id": tcid}
        s1 = self.t._message_signature(fn("call_1"))
        s2 = self.t._message_signature(fn("call_2"))
        self.assertNotEqual(s1, s2)


class TestPersistence(TrackerBase):
    """绑定必须跨重启存活 —— 否则一次重启就把所有会话亲和清零。"""

    def test_bindings_survive_restart(self):
        msgs = [U("持久化测试")]
        k, _ = self.t.resolve_conversation(msgs, model="m")
        self._register(k, account="acct-9", sid="S_session", msgs=msgs)
        self.t.flush_bindings(force=True)

        fresh = self._new()
        ctx = fresh.get_session(k)
        self.assertIsNotNone(ctx, "重启后必须还能找回会话绑定")
        self.assertEqual(ctx.deepseek_session_id, "S_session")
        self.assertEqual(ctx.account_label, "acct-9")

        # 而且重启后仍然能正确解析到它
        k2, src = fresh.resolve_conversation(msgs + [A("答"), U("追问")], model="m")
        self.assertEqual(src, "prefix")
        self.assertEqual(k2, k)

    def test_expired_bindings_not_restored(self):
        msgs = [U("过期测试")]
        k, _ = self.t.resolve_conversation(msgs, model="m")
        ctx = self._register(k, sid="S", msgs=msgs)
        ctx.last_active = time.time() - self.t.ttl - 100
        self.t.flush_bindings(force=True)

        fresh = self._new()
        self.assertIsNone(fresh.get_session(k), "过期绑定不该恢复")

    def test_delete_session_removes_bucket_entry(self):
        msgs = [U("删除测试")]
        k, _ = self.t.resolve_conversation(msgs, model="m")
        ctx = self._register(k, sid="S", msgs=msgs)
        self.assertIn(ctx.bucket, self.t._buckets)
        self.t.delete_session(k)
        self.assertIsNone(self.t.get_session(k))
        self.assertNotIn(ctx.bucket, self.t._buckets)

    def test_file_ids_survive_restart(self):
        msgs = [U("图片测试")]
        k, _ = self.t.resolve_conversation(msgs, model="m")
        ctx = self._register(k, sid="S", msgs=msgs)
        ctx.add_file_ids(["fid_1", "fid_2"])
        self.t.flush_bindings(force=True)
        self.assertEqual(self._new().get_session(k).get_file_ids(), ["fid_1", "fid_2"])

    def test_corrupt_file_does_not_crash(self):
        self.file.write_text("{ this is not json", encoding="utf-8")
        fresh = self._new()   # 不得抛异常
        self.assertEqual(fresh.get_session("nope"), None)

    def test_bucket_pruning_bounds_growth(self):
        """同开头会话极多时不能无界增长"""
        from app.engine.session_tree import MAX_BINDINGS_PER_BUCKET
        for i in range(MAX_BINDINGS_PER_BUCKET + 8):
            k, _ = self.t.resolve_conversation([U("同一个开头")], model="m")
            self._register(k, sid=f"S{i}", msgs=[U("同一个开头"), A("x" * (i + 1))])
        total = sum(len(v) for v in self.t._buckets.values())
        self.assertLessEqual(total, MAX_BINDINGS_PER_BUCKET)


class TestClientSessionId(unittest.TestCase):

    def test_header_priority(self):
        sid, src = extract_client_session_id({"x-session-affinity": "abc"})
        self.assertEqual((sid, src), ("abc", "header:x-session-affinity"))

        sid, src = extract_client_session_id({"x-session-id": "def"})
        self.assertEqual((sid, src), ("def", "header:x-session-id"))

        sid, src = extract_client_session_id({"session_id": "ghi"})
        self.assertEqual((sid, src), ("ghi", "header:session_id"))

        # 多个都在时按明确度优先
        sid, src = extract_client_session_id(
            {"x-session-affinity": "win", "x-session-id": "lose"})
        self.assertEqual(sid, "win")

    def test_client_request_id_is_deliberately_ignored(self):
        """x-client-request-id 名字里的 "request" 意味着可能有客户端每请求换值，
        用它当会话身份会让亲和彻底失效 —— 所以刻意不采用。"""
        sid, src = extract_client_session_id({"x-client-request-id": "per-request-uuid"})
        self.assertIsNone(sid)
        self.assertEqual(src, "none")

    def test_prompt_cache_key_fallback(self):
        sid, src = extract_client_session_id({}, {"prompt_cache_key": "sess-xyz"})
        self.assertEqual((sid, src), ("sess-xyz", "body:prompt_cache_key"))

    def test_header_beats_body(self):
        sid, _ = extract_client_session_id(
            {"x-session-affinity": "hdr"}, {"prompt_cache_key": "body"})
        self.assertEqual(sid, "hdr")

    def test_nothing_present(self):
        self.assertEqual(extract_client_session_id({}, {}), (None, "none"))

    def test_plain_dict_is_case_insensitive(self):
        sid, _ = extract_client_session_id({"X-Session-Affinity": "up"})
        self.assertEqual(sid, "up")

    def test_explicit_id_short_circuits_hashing(self):
        t = SessionTracker(storage_file=Path(tempfile.mkdtemp()) / "b.json")
        k1, src = t.resolve_conversation([U("随便什么")], model="m", custom_id="C-1")
        self.assertEqual(src, "cid")
        k2, _ = t.resolve_conversation([U("完全不同的内容")], model="m", custom_id="C-1")
        self.assertEqual(k1, k2, "显式会话标识必须稳定（与内容无关）")


class TestContextRoundtrip(unittest.TestCase):

    def test_to_from_dict(self):
        ctx = SessionContext("k", "acct", "sess", model_type="expert")
        ctx.last_message_id = "mid"
        ctx.turn_count = 3
        ctx.add_file_ids(["f1"])
        ctx.prefix = ["u:1", "a:2"]
        ctx.bucket = "m|abc"
        back = SessionContext.from_dict(ctx.to_dict())
        self.assertEqual(back.conversation_id, "k")
        self.assertEqual(back.deepseek_session_id, "sess")
        self.assertEqual(back.model_type, "expert")
        self.assertEqual(back.last_message_id, "mid")
        self.assertEqual(back.turn_count, 3)
        self.assertEqual(back.get_file_ids(), ["f1"])
        self.assertEqual(back.prefix, ["u:1", "a:2"])
        self.assertEqual(back.bucket, "m|abc")

    def test_from_dict_tolerates_missing_fields(self):
        back = SessionContext.from_dict({"conversation_id": "k"})
        self.assertEqual(back.prefix, [])
        self.assertEqual(back.file_ids, [])
        self.assertEqual(back.model_type, "default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
