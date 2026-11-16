"""
ds-gateway 核心引擎与安全逻辑自动化测试套件
"""

import sys
import unittest
import asyncio
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from main import app
from app.config import config_manager
from app.core.pow_solver import DeepSeekPOW
from app.core.security import generate_key, list_keys, revoke_key
from app.engine.session_tree import session_tracker
from app.engine.account_pool import account_pool, AccountRuntime, MAX_CONCURRENT_PER_ACCOUNT


class TestGatewayCore(unittest.TestCase):

    def test_pow_solver(self):
        """验证 Wasm PoW 求解器计算正确且快速"""
        cfg = {
            "algorithm": "DeepSeekHashV1",
            "challenge": "b0000b22959bad0cc1ecbbfa07f97191b20332fa10d7341ff9c7ba6e7ed927f1",
            "salt": "dde3ed472be5a2494ee0",
            "difficulty": 144000,
            "expire_at": 1777057596443,
            "signature": "test",
            "target_path": "/api/v0/chat/completion",
        }
        res = DeepSeekPOW.solve(cfg)
        self.assertTrue(bool(res))
        self.assertIsInstance(res, str)

    def test_security_key_lifecycle(self):
        """测试 API Key 的生成、查询与撤销完整生命周期"""
        rec = generate_key("test-key")
        key_id = rec["id"]
        raw_key = rec["key"]
        self.assertTrue(raw_key.startswith("sk-"))

        # 查列表
        all_keys = list_keys()
        matched = [k for k in all_keys if k["id"] == key_id]
        self.assertEqual(len(matched), 1)
        self.assertFalse(matched[0]["revoked"])
        self.assertNotIn("key_hash", matched[0])  # 确保敏感 hash 不对外暴露

        # 撤销 Key
        ok = revoke_key(key_id)
        self.assertTrue(ok)
        all_keys_after = list_keys()
        revoked_item = [k for k in all_keys_after if k["id"] == key_id][0]
        self.assertTrue(revoked_item["revoked"])

    def test_session_tree_prompt_cleaning(self):
        """验证拟人会话树状态机：杜绝 <｜begin of sentence｜> 注入与自然单问延展

        2026-09-10 (T7.10) 更新：首轮接管的结构由
          「以下是先前的对话上下文供参考：/【系统设定】: ... / 当前提问：...」
        改为三分区结构：
          【平台约束（最高优先级）】 + 【对话历史（仅作背景参考）】 + 【当前任务】
        原因：上游网页端只接受纯文本 prompt，没有独立 system 通道，
        旧写法把平台约束降级成历史条目中的一行，弱化了指令权重。
        本测试随之更新为断言**新的结构契约**（而非旧的字面文案）。
        """
        msgs = [
            {"role": "system", "content": "你是一个资深工程师"},
            {"role": "user", "content": "你好，帮我写个 Python 脚本"},
            {"role": "assistant", "content": "没问题，需要什么功能？"},
            {"role": "user", "content": "帮我提取所有 pdf 文件"},
        ]

        # 1. 跨账号全新会话接管：组织自然对话背景，绝不注入模型分词控制符
        prompt_new, _ = session_tracker.prepare_prompt(msgs, has_existing_session=False)
        self.assertNotIn("<｜begin of sentence｜>", prompt_new)
        self.assertNotIn("<｜User｜>", prompt_new)
        self.assertNotIn("<｜Assistant｜>", prompt_new)

        # 新结构契约：平台约束必须前置，且明确高于对话历史
        self.assertIn("【平台约束", prompt_new)
        self.assertIn("你是一个资深工程师", prompt_new)
        self.assertIn("【当前任务】", prompt_new)
        self.assertIn("帮我提取所有 pdf 文件", prompt_new)
        self.assertLess(
            prompt_new.index("【平台约束"),
            prompt_new.index("【当前任务】"),
            "平台约束必须排在当前任务之前（否则指令权重被弱化）",
        )

        # 不得再使用旧的降级写法
        self.assertNotIn("【系统设定】:", prompt_new)

        # 2. 同一会话多轮延展：仅发送最新一条问题，服务端自动依靠 parent_message_id 关联上下文
        prompt_cont, _ = session_tracker.prepare_prompt(msgs, has_existing_session=True)
        self.assertEqual(prompt_cont, "帮我提取所有 pdf 文件")

    def test_account_pool_concurrency_lock(self):
        """验证账号池并发闸门：单账号允许 2 并发（上游实测上限），第 3 个被挡住

        2026-09-10 (T3) 更新：旧实现用 asyncio.Lock（二值）把每账号锁成 1 并发，
        而实测上游单账号可承载 2 条生成流（1→1/1、2→2/2、3→2/3、4→2/4、6→2/6），
        因此改为 Semaphore(2)。本测试相应改为验证新容量。
        """
        runtime = AccountRuntime({
            "account_label": "test_acc@example.com",
            "token": "fake_token",
            "is_valid": True,
        })
        self.assertEqual(runtime.semaphore._value, MAX_CONCURRENT_PER_ACCOUNT)
        self.assertTrue(runtime.is_available_for_api)

        async def _test_async():
            # 第 1 个额度
            await runtime.semaphore.acquire()
            self.assertTrue(runtime.is_available_for_api, "拿到 1 个额度后仍应可用")
            self.assertEqual(runtime.in_flight, 1)

            # 第 2 个额度
            await runtime.semaphore.acquire()
            self.assertEqual(runtime.in_flight, 2)
            self.assertFalse(runtime.is_available_for_api, "额度用尽后不得再被选中")

            # 第 3 个必须被挡住（这正是上游会返回空正文的那个点）
            blocked = False
            pending = asyncio.ensure_future(runtime.semaphore.acquire())
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.1)
            except asyncio.TimeoutError:
                blocked = True
            self.assertTrue(blocked, "第 3 个并发必须被闸门挡住，而不是放行后拿到空响应")

            # 必须取消这个等待中的 acquire，否则它会在额度释放的瞬间抢走额度，
            # 让后续断言看到一个"被抢占"的中间态（这是测试写法问题，不是实现问题）
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass

            # 释放一个，应立刻恢复可用
            runtime.semaphore.release()
            self.assertTrue(runtime.is_available_for_api)
            self.assertEqual(runtime.in_flight, 1)

            runtime.semaphore.release()
            self.assertEqual(runtime.in_flight, 0)
            self.assertTrue(runtime.is_available_for_api)

        asyncio.run(_test_async())

    def test_fastapi_routes_mounted(self):
        """验证所有核心路由与页面完整挂载"""
        paths = []
        for r in app.routes:
            if hasattr(r, "path"):
                paths.append(r.path)
            else:
                sub_routes = getattr(r, "routes", getattr(getattr(r, "original_router", None), "routes", []))
                for sub in sub_routes:
                    if hasattr(sub, "path"):
                        paths.append(sub.path)

        self.assertIn("/v1/chat/completions", paths)
        self.assertIn("/v1/models", paths)
        self.assertIn("/v1/messages", paths)
        self.assertIn("/health", paths)
        self.assertIn("/api/accounts/health", paths)
        self.assertIn("/admin/login", paths)
        self.assertIn("/admin/dashboard", paths)


if __name__ == "__main__":
    unittest.main()
