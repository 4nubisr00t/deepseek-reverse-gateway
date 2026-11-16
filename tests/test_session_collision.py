"""
会话指纹串线的回归测试（T19/T1 核心）。

真实故障（已用 11 份真实会话交叉实证）：
  网关的 get_conversation_key 只取「第一条 user 消息」算 MD5，
  而 DSH 把 approval 通知、runtime context、AGENTS.md、skills 目录
  **作为持久 user 消息排在真人提问之前**。这些注入文本在所有项目里完全相同，
  于是不同项目算出同一个 key，绑到同一条上游 web 会话 → 上下文互相污染。

实测碰撞（2026-09-10，真实注入文本）：
    E:\\OH-WorkSpace\\projects\\fenglian\\feng-farm
    E:\\DeepSeek _Harness
    F:\\projects\\schoolbus1.0
  → 三者同为 deepseek-v4-pro:hash:cfc97c4790feb3fac9715fbed40ecd40
  该值与交接文档 CASE_STUDY_session_60cf 记录的碰撞值一致。

本测试用的注入文本**逐字取自真实会话日志**，不是构造的近似值。
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.session_tree import SessionTracker  # noqa: E402

# ── 逐字取自 dsh-session-d7ac8a81 的注入文本 ──────────────────────────
INJ_APPROVAL = 'The approval policy changed from "ask" to "never" (changed by the user).'
INJ_RUNTIME = (
    "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n"
    "Current DSH file policy: danger-full-access. The DSH file sandbox does not restrict "
    "file modifications by available operations."
)
INJ_AGENTS = (
    "<system-reminder>\nThe following workspace instructions may be relevant to your work. "
    "Use them as guidance when applicable. More specific instructions take precedence over "
    "broader ones.\n\nInstructions from: AGENTS.md\n\n# AGENTS.md\n\n"
    "# 智慧通勤综合服务平台开发约束说明\n</system-reminder>"
)
INJ_SKILLS = (
    "<system-reminder>\nA skill is a reusable set of task-specific instructions. "
    "The following skills are available in this session:\n<available_skills>\n"
    "- `cordis-plugin-development`: Create, modify, debug, or extend dynamic Cordis Plugins\n"
    "</available_skills>\n</system-reminder>"
)

HUMAN_SCHOOLBUS = r"F:\projects\schoolbus1.0\driver 扫一下这个app端的所有结构，搞清楚其逻辑"
HUMAN_FENG = "当前目录是我们正在开发的项目，我要你勘察整个目录"
HUMAN_HARNESS = "自检一下我的dsh是不是最新版"

# 旧实现的碰撞值（固化为断言，防止回归）
LEGACY_COLLISION_HASH = "cfc97c4790feb3fac9715fbed40ecd40"


def _dsh_ordered_messages(human_prompt: str):
    """按 DSH 真实发送顺序构造消息：注入在前，真人提问在后。"""
    return [
        {"role": "user", "content": INJ_APPROVAL},
        {"role": "user", "content": human_prompt},
        {"role": "user", "content": INJ_AGENTS},
        {"role": "user", "content": INJ_RUNTIME},
        {"role": "user", "content": INJ_SKILLS},
    ]


# ─────────────────────────────────────────────────────────────
# 1. 注入体必须被判定为非业务内容
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("label,text", [
    ("approval 通知", INJ_APPROVAL),
    ("runtime context", INJ_RUNTIME),
    ("AGENTS.md", INJ_AGENTS),
    ("skills 目录", INJ_SKILLS),
    ("[hana_reminder]", "[hana_reminder] 工具提醒"),
])
def test_injected_metadata_is_cleaned_away(label, text):
    assert SessionTracker._clean_user_content(text) == "", (
        f"{label} 未被清洗，会参与哈希并造成跨项目碰撞"
    )


def test_real_human_prompt_is_preserved():
    """真人提问不得被误清洗。"""
    got = SessionTracker._clean_user_content(HUMAN_SCHOOLBUS)
    assert got == HUMAN_SCHOOLBUS, f"真人提问被破坏了：{got!r}"


def test_inline_system_reminder_is_stripped_but_question_kept():
    """内嵌 <system-reminder> 应被剥离，正文保留。"""
    text = f"<system-reminder>一些注入</system-reminder>\n{HUMAN_FENG}"
    got = SessionTracker._clean_user_content(text)
    assert HUMAN_FENG in got
    assert "system-reminder" not in got


# ─────────────────────────────────────────────────────────────
# 2. ★ 核心：跨项目不得碰撞
# ─────────────────────────────────────────────────────────────
def test_different_projects_do_not_collide():
    """三个不同项目，在 DSH 真实发送顺序下必须得到三个不同的 key。

    这是本次修复的核心断言——修复前它们全部碰撞到同一个哈希。
    """
    keys = {
        "schoolbus": SessionTracker.get_conversation_key(
            _dsh_ordered_messages(HUMAN_SCHOOLBUS), model="deepseek-v4-pro"),
        "feng-farm": SessionTracker.get_conversation_key(
            _dsh_ordered_messages(HUMAN_FENG), model="deepseek-v4-pro"),
        "harness": SessionTracker.get_conversation_key(
            _dsh_ordered_messages(HUMAN_HARNESS), model="deepseek-v4-pro"),
    }
    assert len(set(keys.values())) == 3, (
        f"不同项目仍发生碰撞：{keys}"
    )


def test_no_longer_produces_the_legacy_collision_hash():
    """不得再产出交接文档记录的碰撞哈希值。"""
    for prompt in (HUMAN_SCHOOLBUS, HUMAN_FENG, HUMAN_HARNESS):
        key = SessionTracker.get_conversation_key(
            _dsh_ordered_messages(prompt), model="deepseek-v4-pro")
        assert LEGACY_COLLISION_HASH not in key, (
            f"仍复现历史碰撞值 {LEGACY_COLLISION_HASH}（项目={prompt[:30]!r}）"
        )


def test_key_is_derived_from_human_prompt_not_injection():
    """key 必须由真人提问决定：把 injection 换成另一份，key 不应改变。"""
    k1 = SessionTracker.get_conversation_key(
        _dsh_ordered_messages(HUMAN_SCHOOLBUS), model="deepseek-v4-pro")

    alt = _dsh_ordered_messages(HUMAN_SCHOOLBUS)
    alt[0] = {"role": "user", "content": 'The approval policy changed from "never" to "ask" (changed by the user).'}
    k2 = SessionTracker.get_conversation_key(alt, model="deepseek-v4-pro")
    assert k1 == k2, "key 随注入内容变化，说明哈希仍落在注入体上"


# ─────────────────────────────────────────────────────────────
# 3. 显式会话 ID 优先（唯一可靠的区分手段）
# ─────────────────────────────────────────────────────────────
def test_explicit_session_id_takes_priority():
    """有显式 session id 时必须直接用它，不再依赖内容哈希。"""
    msgs = _dsh_ordered_messages(HUMAN_SCHOOLBUS)
    k_cid = SessionTracker.get_conversation_key(msgs, model="m", custom_id="sess-A")
    assert k_cid == "m:cid:sess-A"


def test_explicit_ids_separate_identical_questions():
    """同一句提问被重复发起时，只有显式 ID 能区分。

    这是内容哈希**无法**解决的固有情形（实测：schoolbus 三个会话、
    feng-farm 两个会话都因相同提问而合并）。
    """
    msgs = _dsh_ordered_messages(HUMAN_SCHOOLBUS)
    a = SessionTracker.get_conversation_key(msgs, model="m", custom_id="sess-1")
    b = SessionTracker.get_conversation_key(msgs, model="m", custom_id="sess-2")
    assert a != b
    # 而无 ID 时它们必然相同——这是已知且无法避免的局限
    x = SessionTracker.get_conversation_key(msgs, model="m")
    y = SessionTracker.get_conversation_key(msgs, model="m")
    assert x == y


# ─────────────────────────────────────────────────────────────
# 4. 模型名仍参与隔离
# ─────────────────────────────────────────────────────────────
def test_model_still_part_of_key():
    msgs = _dsh_ordered_messages(HUMAN_SCHOOLBUS)
    assert (SessionTracker.get_conversation_key(msgs, model="a")
            != SessionTracker.get_conversation_key(msgs, model="b"))


def test_all_injected_messages_falls_back_safely():
    """全是注入体时应回落到 default，而不是抛异常或产出注入哈希。"""
    msgs = [{"role": "user", "content": INJ_APPROVAL},
            {"role": "user", "content": INJ_RUNTIME}]
    key = SessionTracker.get_conversation_key(msgs, model="m")
    assert key == "m:hash:default"
    assert LEGACY_COLLISION_HASH not in key
