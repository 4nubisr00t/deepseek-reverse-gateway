"""
多分支会话树 (Multi-Branch Session Tree / DAG Checkpoint Graph) 专项测试套件
验证：
1. 显式会话 ID (custom_id) 下的历史节点分叉 (Branching from Turn K < N)：
   - 保持上游 deepseek_session_id 与账号粘性不变（绝不误判为 cid-prefix-changed 销毁会话）
   - parent_message_id (last_message_id) 精准回溯至第 K 轮的 message_id
2. 平行分支来回切换 (Multi-Branch Ping-Pong: Branch A <-> Branch B <-> Branch C)：
   - 同一会话内多条分支并存，任意切换均能通过 LCA (最长公共前缀检查点) 命中对应分支末端的 message_id
3. 超长对话深水区分叉 (> 8 条消息 / 第 5 轮之后的检查点回滚)：
   - 修复旧版 PREFIX_MAX_MESSAGES=8 导致的深层回滚失明问题
4. 同轮重新生成 / 重试 (Regenerate / Retry Turn K)：
   - 客户端重发第 K 轮完全相同的消息序列时，parent_message_id 自动回退至第 K-1 轮的 message_id
5. 无显式会话 ID (Content-Hash / Prefix 桶模式) 下的分支回溯与防串线保护
6. 跨进程重启持久化：分支树拓扑 (tree_nodes / active_sigs / branch_count) 在重启后完整恢复
7. Responses API (/v1/responses) 基于 previous_response_id 的精确状态树分叉
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from main import app
from app.engine.session_tree import SessionTracker
from app.engine.deepseek_client import ChatEvent


def U(text: str):
    return {"role": "user", "content": text}


def A(text: str):
    return {"role": "assistant", "content": text}


class TestSessionTreeBranching(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bindings_file = Path(self._tmp.name) / "bindings.json"
        self.tracker = SessionTracker(storage_file=self.bindings_file)

    def tearDown(self):
        self._tmp.cleanup()

    def test_explicit_cid_branching_and_multi_branch_ping_pong(self):
        """验证显式会话 ID 下的分支创建与 A/B 多分支自由来回切换"""
        cid = "agent-branch-sess-01"

        # Turn 1: [U1] -> mid_1
        m1 = [U("请帮我设计一个高并发网关架构")]
        k1, src1 = self.tracker.resolve_conversation(m1, model="m", custom_id=cid)
        self.assertEqual(src1, "cid")
        self.tracker.register_session(
            k1, "acc-1", "ds_session_root", prefix=self.tracker._prefix_signatures(m1)
        )
        self.tracker.update_message_id(k1, "mid_1")

        # Turn 2 (Branch A): [U1, A1, U2_A] -> mid_2A
        m2_a = [U("请帮我设计一个高并发网关架构"), A("好的，我们可以采用异步架构"), U("分支A：使用 Python FastAPI 实现")]
        k2a, src2a = self.tracker.resolve_conversation(m2_a, model="m", custom_id=cid)
        self.assertEqual(src2a, "cid")
        ctx = self.tracker.get_session(k2a)
        self.assertEqual(ctx.deepseek_session_id, "ds_session_root")
        self.assertEqual(ctx.last_message_id, "mid_1")
        self.tracker.update_message_id(k2a, "mid_2A")

        # Turn 3 (Branch A 深入): [U1, A1, U2_A, A2_A, U3_A] -> mid_3A
        m3_a = m2_a + [A("这是 FastAPI 的核心代码"), U("分支A继续：加上 SQLite WAL 存储")]
        k3a, src3a = self.tracker.resolve_conversation(m3_a, model="m", custom_id=cid)
        self.assertEqual(src3a, "cid")
        self.assertEqual(self.tracker.get_session(k3a).last_message_id, "mid_2A")
        self.tracker.update_message_id(k3a, "mid_3A")

        # ★ 关键时刻：客户端从 Turn 1 (A1) 分叉出 Branch B！
        # 发送 [U1, A1, U2_B]，不再包含 U2_A / A2_A / U3_A
        m2_b = [U("请帮我设计一个高并发网关架构"), A("好的，我们可以采用异步架构"), U("分支B：如果改用 Rust Tokio 实现呢？")]
        k2b, src2b = self.tracker.resolve_conversation(m2_b, model="m", custom_id=cid)

        # 断言：必须识别为 cid-branched，绝不能是 cid-prefix-changed（绝不清空 deepseek_session_id）！
        self.assertEqual(src2b, "cid-branched")
        ctx_b = self.tracker.get_session(k2b)
        self.assertEqual(ctx_b.deepseek_session_id, "ds_session_root", "分叉绝不能销毁上游会话 ID")
        self.assertEqual(ctx_b.last_message_id, "mid_1", "parent_message_id 必须精准回滚至分叉点 mid_1")
        self.assertEqual(ctx_b.turn_count, 1)
        self.assertEqual(ctx_b.branch_count, 1)
        self.tracker.update_message_id(k2b, "mid_2B")

        # ★ 再次切换：用户从 Branch B 切回 Branch A 的末端继续追问 Turn 4A！
        m4_a = m3_a + [A("SQLite WAL 已配置完成"), U("分支A第4轮：如何做连接池监控？")]
        k4a, src4a = self.tracker.resolve_conversation(m4_a, model="m", custom_id=cid)
        self.assertEqual(src4a, "cid-branched")
        ctx_back_a = self.tracker.get_session(k4a)
        self.assertEqual(ctx_back_a.deepseek_session_id, "ds_session_root")
        self.assertEqual(ctx_back_a.last_message_id, "mid_3A", "切回分支 A 时必须精准挂载到 mid_3A")
        self.assertEqual(ctx_back_a.turn_count, 3)
        self.tracker.update_message_id(k4a, "mid_4A")

        # ★ 再切回 Branch B 继续追问 Turn 3B！
        m3_b = m2_b + [A("Rust Tokio 方案如下"), U("分支B第3轮：加上 Tower 中间件")]
        k3b, src3b = self.tracker.resolve_conversation(m3_b, model="m", custom_id=cid)
        self.assertEqual(src3b, "cid-branched")
        ctx_back_b = self.tracker.get_session(k3b)
        self.assertEqual(ctx_back_b.last_message_id, "mid_2B", "切回分支 B 时必须精准挂载到 mid_2B")
        self.tracker.update_message_id(k3b, "mid_3B")

    def test_deep_horizon_branching_beyond_8_messages(self):
        """验证超过 8 条消息（第 5 轮以后）的深水区回滚分叉，彻底解决旧版 PREFIX_MAX_MESSAGES=8 失明问题"""
        cid = "deep-tree-sess"
        msgs = [U("第 1 轮提问")]
        k, _ = self.tracker.resolve_conversation(msgs, model="m", custom_id=cid)
        self.tracker.register_session(k, "acc-deep", "ds_deep_sid", prefix=self.tracker._prefix_signatures(msgs))
        self.tracker.update_message_id(k, "mid_t1")

        # 连续推进到第 8 轮（共 15 条消息，远超 PREFIX_MAX_MESSAGES=8）
        turn_snapshots = {1: list(msgs)}
        for t in range(2, 9):
            msgs = msgs + [A(f"第 {t-1} 轮回答"), U(f"第 {t} 轮提问")]
            turn_snapshots[t] = list(msgs)
            self.tracker.resolve_conversation(msgs, model="m", custom_id=cid)
            self.tracker.update_message_id(k, f"mid_t{t}")

        self.assertEqual(self.tracker.get_session(k).last_message_id, "mid_t8")

        # 现在从第 6 轮（第 11 条消息之后，前 8 条消息与第 8 轮完全相同！）发起分叉：
        branch_from_t6 = turn_snapshots[6] + [A("第 6 轮回答"), U("从第 6 轮分叉出的新第 7 轮提问")]
        _, src = self.tracker.resolve_conversation(branch_from_t6, model="m", custom_id=cid)
        self.assertEqual(src, "cid-branched", "超过 8 条消息后的深层分叉必须被精准识别")
        self.assertEqual(
            self.tracker.get_session(k).last_message_id,
            "mid_t6",
            "深层分叉必须回溯至 mid_t6，绝不能错误沿用 mid_t8",
        )

    def test_same_turn_regenerate_rewinds_to_parent_message_id(self):
        """验证客户端对当前轮点击「重新生成 (Regenerate)」重发相同消息列表时，自动回退到上一轮的 parent_message_id"""
        cid = "regen-sess"
        m1 = [U("写一首诗")]
        k, _ = self.tracker.resolve_conversation(m1, model="m", custom_id=cid)
        self.tracker.register_session(k, "acc-1", "ds_regen_sid", prefix=self.tracker._prefix_signatures(m1))
        self.tracker.update_message_id(k, "mid_1")

        m2 = [U("写一首诗"), A("春眠不觉晓"), U("改成七言绝句")]
        self.tracker.resolve_conversation(m2, model="m", custom_id=cid)
        self.assertEqual(self.tracker.get_session(k).last_message_id, "mid_1")
        self.tracker.update_message_id(k, "mid_2_first_try")
        self.assertEqual(self.tracker.get_session(k).last_message_id, "mid_2_first_try")

        # 用户对第 2 轮不满意，点击「重新生成」，客户端再次发送完全相同的 m2
        _, src_regen = self.tracker.resolve_conversation(m2, model="m", custom_id=cid)
        self.assertEqual(src_regen, "cid-branched")
        self.assertEqual(
            self.tracker.get_session(k).last_message_id,
            "mid_1",
            "重新生成第 2 轮时，parent_message_id 必须回退至第 1 轮的 mid_1，而非 mid_2_first_try",
        )

    def test_prefix_bucket_mode_branching_and_persistence(self):
        """验证无显式会话 ID (Prefix 桶模式) 下的分支回溯，以及重启后分支树完整持久化恢复"""
        m1 = [U("无显式ID的分支测试首句")]
        k1, src1 = self.tracker.resolve_conversation(m1, model="m")
        self.assertEqual(src1, "new")
        self.tracker.register_session(k1, "acc-p", "ds_prefix_sid", prefix=self.tracker._prefix_signatures(m1))
        self.tracker.update_message_id(k1, "p_mid_1")

        m2 = m1 + [A("第一轮回复"), U("第二轮主线提问")]
        k2, src2 = self.tracker.resolve_conversation(m2, model="m")
        self.assertEqual(k2, k1)
        self.assertEqual(src2, "prefix")
        self.tracker.update_message_id(k2, "p_mid_2")

        # 落盘并模拟进程重启
        self.tracker.flush_bindings(force=True)
        restarted_tracker = SessionTracker(storage_file=self.bindings_file)

        # 重启后直接发起从第 1 轮的分叉请求
        m2_alt = m1 + [A("第一轮回复"), U("第二轮平行分支提问")]
        k_branch, src_branch = restarted_tracker.resolve_conversation(m2_alt, model="m")
        self.assertEqual(k_branch, k1, "无显式 ID 分叉应唯一命中原桶内会话")
        self.assertEqual(src_branch, "prefix-branched")
        ctx = restarted_tracker.get_session(k_branch)
        self.assertEqual(ctx.deepseek_session_id, "ds_prefix_sid")
        self.assertEqual(ctx.last_message_id, "p_mid_1")

        topo = ctx.get_tree_topology()
        self.assertEqual(topo["total_checkpoints"], 2)
        self.assertEqual(topo["branch_count"], 1)


class TestResponsesApiBranchingByPreviousResponseId(unittest.TestCase):
    """验证 /v1/responses 端点通过 previous_response_id 实现精确树分叉"""

    def setUp(self):
        from app.core.security import verify_api_key, get_identity
        app.dependency_overrides[verify_api_key] = lambda: True
        app.dependency_overrides[get_identity] = lambda: {
            "key_id": "k_branch_resp",
            "name": "branch_tester",
            "owner": "tester",
            "log_label": "tester",
            "sensitive_filter": False,
            "pool_fallback": True,
            "account_labels": [],
            "quota_daily_requests": 0,
            "quota_daily_tokens": 0,
        }
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("app.api.responses_routes.DeepSeekClient.create_chat_session", return_value="ds_resp_tree_sid")
    @patch("app.api.responses_routes.DeepSeekClient.stream_chat")
    @patch("app.api.responses_routes.account_pool.acquire_account")
    def test_previous_response_id_branching(self, mock_acquire, mock_stream, mock_create):
        mock_acc = MagicMock()
        mock_acc.token = "tok_tree"
        mock_acc.cookie = ""
        mock_acc.account_label = "acc_tree@test.com"
        mock_acquire.return_value = mock_acc

        call_log = []
        mid_counter = [0]

        async def fake_stream(token, session_id, prompt, parent_message_id=None, **kwargs):
            mid_counter[0] += 1
            cur_mid = f"ds_mid_{mid_counter[0]}"
            call_log.append({"session_id": session_id, "parent_message_id": parent_message_id, "prompt": prompt})
            yield ChatEvent("content", content=f"Reply {mid_counter[0]}")
            yield ChatEvent("message_id", message_id=cur_mid)

        mock_stream.side_effect = fake_stream

        # Turn 1: Root
        r1 = self.client.post("/v1/responses", json={"model": "deepseek-v4.1-flash", "input": "Root question"})
        self.assertEqual(r1.status_code, 200)
        resp_1_id = r1.json()["id"]
        self.assertEqual(call_log[-1]["parent_message_id"], None)

        # Turn 2A: 延续 resp_1_id -> parent_message_id 应为 ds_mid_1
        r2a = self.client.post(
            "/v1/responses",
            json={"model": "deepseek-v4.1-flash", "input": "Branch A question", "previous_response_id": resp_1_id},
        )
        self.assertEqual(r2a.status_code, 200)
        resp_2a_id = r2a.json()["id"]
        self.assertEqual(call_log[-1]["session_id"], "ds_resp_tree_sid")
        self.assertEqual(call_log[-1]["parent_message_id"], "ds_mid_1")

        # Turn 2B: 再次以 resp_1_id 为父节点分叉 -> parent_message_id 必须回溯为 ds_mid_1（而非 ds_mid_2）！
        r2b = self.client.post(
            "/v1/responses",
            json={"model": "deepseek-v4.1-flash", "input": "Branch B question", "previous_response_id": resp_1_id},
        )
        self.assertEqual(r2b.status_code, 200)
        self.assertEqual(call_log[-1]["session_id"], "ds_resp_tree_sid")
        self.assertEqual(call_log[-1]["parent_message_id"], "ds_mid_1")

        # Turn 3A: 切回 resp_2a_id 继续追问 -> parent_message_id 必须为 ds_mid_2！
        r3a = self.client.post(
            "/v1/responses",
            json={"model": "deepseek-v4.1-flash", "input": "Continue Branch A", "previous_response_id": resp_2a_id},
        )
        self.assertEqual(r3a.status_code, 200)
        self.assertEqual(call_log[-1]["session_id"], "ds_resp_tree_sid")
        self.assertEqual(call_log[-1]["parent_message_id"], "ds_mid_2")
        self.assertEqual(mock_create.call_count, 1, "全程在同一上游会话内完成树分叉，只创建过 1 次会话")




class TestUnifiedFrontendAndZeroEmoji(unittest.TestCase):
    """验证精简版 Admin SPA 路由覆盖及零 Emoji 规范"""

    def setUp(self):
        self.client = TestClient(app)

    def test_admin_spa_serves_all_admin_routes(self):
        for route in ("/login", "/admin", "/admin/login", "/admin/dashboard", "/admin/accounts", "/admin/ops", "/admin/register"):
            resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, f"路由 {route} 应返回 200")

    def test_zero_emojis_in_frontend(self):
        import re
        root = Path(__file__).resolve().parent.parent
        emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
        dist_html = (root / "frontend" / "dist" / "index.html").read_text("utf-8")
        self.assertEqual(emoji_re.findall(dist_html), [], "frontend/dist/index.html 必须严格零 Emoji，全部采用 SVG 图标")


if __name__ == "__main__":
    unittest.main()
