"""
DeepSeek 账号全自动养号与背景拟真引擎 (Account Warmup & Persona Seeding Engine)
- 独立的养号状态机 (WARMING 状态，完全隔离业务流量)
- 自动生成多样化的高质量日常种子会话 (技术探讨、周报写作、书籍推荐等)
- 模拟真实人类思考与阅读间隔 (Jitter Delay)
- 养号完成后自动平滑入池 (转为 ACTIVE)
"""

import time
import random
import asyncio
from typing import Dict, Any, List, Optional

from app.config import config_manager
from app.engine.deepseek_client import DeepSeekClient

# 预置的高质量拟人种子会话库（涵盖工作、技术、日常等，彻底冲淡机器人特征）
WARMUP_PERSONAS = [
    {
        "title": "Linux 系统性能调优咨询",
        "turns": [
            "请帮我梳理一下在 Linux 服务器上排查 CPU 占用过高的标准排查步骤和常用命令。",
            "如果发现大部分 CPU 占用都在 kswapd0 进程上，通常说明什么问题？该如何优化？",
        ],
    },
    {
        "title": "研发工程师周报整理",
        "turns": [
            "帮我润色一份研发周报，本周主要完成了网关异步架构重构、修复了内存泄漏，并编写了单元测试。",
            "请加上下周规划，包括线上灰度发布和性能基准压测，风格保持简练专业。",
        ],
    },
    {
        "title": "经典科幻书籍推荐",
        "turns": [
            "推荐两部适合程序员看的硬科幻小说，并分别用一句话概括其核心看点。",
            "这两部作品各自探讨了什么样的人工智能或未来伦理哲学？",
        ],
    },
    {
        "title": "Python 异步编程实践笔记",
        "turns": [
            "在 Python asyncio 中，如何优雅地处理一组并发任务的超时与异常取消？请给一个代码片段。",
            "使用 asyncio.gather 和 asyncio.as_completed 在实际场景中有何取舍考量？",
        ],
    },
]

# 运行时全局养号任务进度追踪: account_label -> { status, step, progress, message }
_warmup_tasks: Dict[str, Dict[str, Any]] = {}


def get_warmup_status(account_label: Optional[str] = None) -> Any:
    if account_label:
        return _warmup_tasks.get(account_label, {"status": "idle"})
    return _warmup_tasks


async def warmup_account_task(
    account_label: str, seed_count: int = 2, auto_activate: bool = True, log_fn: Optional[Any] = None
):
    """
    针对指定账号执行完整的拟真养号流程
    1. 标记账号为 WARMING 状态（网关排队调度器自动将其隔离出池）
    2. 创建 1~2 个多轮人类工作会话并自然对话
    3. 模拟人类阅读间隔 (5~10秒随机抖动)
    4. 养号完毕自动恢复 ACTIVE 入池
    """
    from app.engine.account_pool import account_pool

    def _log(msg: str):
        print(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    acc_data = None
    for acc in config_manager.get_accounts():
        if acc.get("account_label") == account_label:
            acc_data = acc
            break

    if not acc_data:
        _log(f"[Warmup] 未找到账号 {account_label}")
        return False

    token = acc_data.get("token")
    cookie = acc_data.get("cookie", "")
    proxy = config_manager.config.proxy

    # 1. 标记为养号状态（隔离出池）
    config_manager.update_account(account_label, status="WARMING", is_valid=False)
    try:
        from app.db.repositories import account_repo
        account_repo.update_runtime_state(account_label, status="WARMING", is_valid=False)
    except Exception:
        pass
    account_pool.reload()
    _warmup_tasks[account_label] = {
        "status": "warming",
        "step": "init",
        "progress": "0%",
        "message": "已从业务池隔离，准备开始养号...",
    }
    _log(f"[Warmup] 账号 {account_label} 已进入 WARMING 隔离状态，开始拟真养号流程...")

    selected_personas = random.sample(WARMUP_PERSONAS, min(seed_count, len(WARMUP_PERSONAS)))
    total_steps = sum(len(p["turns"]) for p in selected_personas)
    current_step = 0
    successful_turns = 0

    try:
        for p_idx, persona in enumerate(selected_personas, 1):
            _warmup_tasks[account_label]["message"] = f"正在创建主题会话 [{persona['title']}]..."
            _log(f"[Warmup] 正在建立种子会话 ({p_idx}/{len(selected_personas)}): {persona['title']}")

            # 创建独立会话
            sid = await asyncio.to_thread(DeepSeekClient.create_chat_session, token, cookie, proxy)
            if not sid:
                _log(f"[Warmup] 会话创建失败，跳过该主题")
                continue

            parent_mid = None
            for t_idx, prompt_text in enumerate(persona["turns"], 1):
                current_step += 1
                progress_pct = int((current_step / total_steps) * 100)
                _warmup_tasks[account_label].update({
                    "step": f"seed_{p_idx}_turn_{t_idx}",
                    "progress": f"{progress_pct}%",
                    "message": f"正在对话: {persona['title']} (第{t_idx}轮)",
                })
                _log(f"  [Warmup 第{t_idx}轮] 模拟提问: {prompt_text[:35]}...")

                # 执行流式交互
                last_mid = None
                turn_got_content = False
                async for ev in DeepSeekClient.stream_chat(
                    token=token,
                    session_id=sid,
                    prompt=prompt_text,
                    parent_message_id=parent_mid,
                    cookie=cookie,
                    proxy=proxy,
                ):
                    if ev.event_type == "error":
                        _log(f"  [Warmup 第{t_idx}轮] 对话异常: {ev.content}")
                        break
                    if ev.event_type in ("content", "text") and ev.content:
                        turn_got_content = True
                    if ev.event_type in ("message_id", "done") and ev.message_id:
                        last_mid = ev.message_id

                if turn_got_content:
                    successful_turns += 1
                    parent_mid = last_mid
                    # 拟真人思考与打字延迟 (4~8 秒随机休眠)
                    delay = random.uniform(4.0, 8.0)
                    _log(f"  [Warmup 第{t_idx}轮] 对话完成，模拟人类阅读思考中 ({delay:.1f}s)...")
                    await asyncio.sleep(delay)
                else:
                    _log(f"  [Warmup 第{t_idx}轮] 未获取到有效回复或对话中断")

        # 检查是否真正完成拟真养号交互
        if successful_turns == 0:
            _log(f"[Warmup] 账号 {account_label} 养号失败: 未能成功完成任何拟真对话轮次，保持 WARMING 隔离保护，拒绝入池！")
            _warmup_tasks[account_label] = {
                "status": "failed",
                "step": "error",
                "progress": "0%",
                "message": "未能成功完成任何拟真对话，已保持隔离",
            }
            config_manager.update_account(account_label, status="WARMING", is_valid=False)
            account_pool.reload()
            return False

        # 养号成功完成
        _warmup_tasks[account_label] = {
            "status": "completed",
            "step": "done",
            "progress": "100%",
            "message": f"养号完成，已成功沉淀 {successful_turns} 轮真实会话树！",
        }
        _log(f"[Warmup] 账号 {account_label} 拟真养号圆满完成 (成功沉淀 {successful_turns} 轮真实会话)！")

        # 4. 平滑入池
        if auto_activate:
            config_manager.update_account(account_label, status="ACTIVE", is_valid=True)
            try:
                from app.db.repositories import account_repo
                account_repo.update_runtime_state(account_label, status="ACTIVE", is_valid=True)
            except Exception:
                pass
            account_pool.reload()
            _log(f"[Warmup] 账号 {account_label} 已平滑转为 ACTIVE 状态，正式进入对外业务服务池！")
        return True

    except Exception as e:
        _log(f"[Warmup] 养号过程异常: {e}")
        _warmup_tasks[account_label] = {
            "status": "failed",
            "step": "error",
            "progress": "0%",
            "message": f"养号中断: {str(e)[:100]}",
        }
        # 异常时保持隔离，避免未养好的号直接承载业务
        config_manager.update_account(account_label, status="WARMING", is_valid=False)
        try:
            from app.db.repositories import account_repo
            account_repo.update_runtime_state(account_label, status="WARMING", is_valid=False)
        except Exception:
            pass
        account_pool.reload()
        return False
