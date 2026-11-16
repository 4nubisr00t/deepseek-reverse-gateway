import pytest
from app.engine.session_tree import SessionTracker


def test_time_sampled_injection_is_not_treated_as_user_instruction():
    """验证 DSH / Claude Code 注入的时间上下文采样快照绝不会截胡用户的真实提问"""
    messages = [
        {"role": "user", "content": "帮我查看一下C盘空间"},
        {
            "role": "user",
            "content": (
                "Time sampled while preparing turn 1, step 1: 2026-09-30T08:26:37+08:00[Asia/Shanghai]\n"
                "Browser time zone for this request: Asia/Shanghai. Interpret otherwise-unqualified dates and times in this zone.\n"
                "Elapsed since the preceding model-visible message: unavailable."
            ),
        },
    ]

    # 已有会话场景
    prompt_existing, _ = SessionTracker.prepare_prompt(messages, has_existing_session=True)
    assert prompt_existing == "帮我查看一下C盘空间"
    assert "Time sampled" not in prompt_existing

    # 新建会话场景
    prompt_new, _ = SessionTracker.prepare_prompt(messages, has_existing_session=False)
    assert "帮我查看一下C盘空间" in prompt_new
    assert "Time sampled" not in prompt_new


def test_tool_loop_with_time_sampled_injection():
    """验证在工具执行循环中，时间采样快照不会覆盖用户的最新指令"""
    messages = [
        {"role": "user", "content": "帮我查看一下C盘空间"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "pwsh"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Free: 20GB"},
        {
            "role": "user",
            "content": "Time sampled while preparing turn 2, step 1: 2026-09-30T08:27:12+08:00[Asia/Shanghai]",
        },
    ]

    prompt, _ = SessionTracker.prepare_prompt(messages, has_existing_session=True)
    assert "帮我查看一下C盘空间" in prompt
    assert "Time sampled" not in prompt
    assert "Free: 20GB" in prompt


def test_background_job_notice_injection():
    """验证后台任务完成通知不会被当成用户提问"""
    messages = [
        {"role": "user", "content": "搜索项目代码"},
        {
            "role": "user",
            "content": "background job pwsh-223 (pwsh: Get-ChildItem -Recurse) finished with result: Done",
        },
    ]
    prompt, _ = SessionTracker.prepare_prompt(messages, has_existing_session=True)
    assert prompt == "搜索项目代码"
