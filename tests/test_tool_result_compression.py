"""
工具结果压缩的回归测试（T6 / T19）。

实测依据（真实会话 ccd1 / d7ac8a81）：
  17 条 tool/result 共 515,968 字节，**单条最大 149,069 字节**（读源码文件），
  而客户端**每轮重发完整历史** —— 这是"code 挤压了其他规则的生存空间，
  以至于工具都用不出来"的直接来源。

策略：**确定性截断**（保留头部 24000 + 尾部 8000），不调用模型。
注：上游返回的是**捆绑式** token usage，无法精确控制单条结果的 token 成本，
所以按字符数近似（≈4 字符/token）。
"""
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.engine.session_tree import SessionTracker  # noqa: E402

C = SessionTracker._compress_tool_result


def test_small_content_untouched():
    """小内容必须原样返回——绝大多数请求不应受影响。"""
    for n in (0, 1, 1000, 32000, SessionTracker.TOOL_RESULT_MAX_CHARS):
        s = "x" * n
        assert C(s) == s, f"{n} 字符被误处理"


def test_oversized_content_is_truncated():
    big = "y" * 149_069
    out = C(big)
    assert len(out) < len(big)
    assert len(out) <= SessionTracker.TOOL_RESULT_MAX_CHARS + 400, (
        "截断后仍超过阈值（标注会占少量额外字符）"
    )


def test_marker_states_it_is_not_the_full_text():
    """必须显式告知"不是全文"，否则模型会误以为内容就这么长。"""
    out = C("z" * 100_000)
    assert "已截断" in out
    assert "100,000" in out, "未标注原文总长度"


def test_head_and_tail_are_preserved():
    big = "".join(chr(65 + i % 26) for i in range(90_000))
    out = C(big)
    assert out.startswith(big[:200]), "头部内容丢失"
    assert out.endswith(big[-200:]), "尾部内容丢失"


def test_deterministic_same_input_same_output():
    """确定性是硬要求：否则破坏可复现性（同一请求两轮结果不同）。"""
    big = "q" * 120_000
    assert C(big) == C(big)


def test_non_string_input_does_not_crash():
    for bad in (None, 123, ["a"], {"k": "v"}):
        out = C(bad)
        assert isinstance(out, str), f"{type(bad)} 输入未安全处理"


def test_threshold_is_conservative():
    """阈值必须足够保守，避免过早截掉有用内容。"""
    assert SessionTracker.TOOL_RESULT_MAX_CHARS >= 16_384, (
        "阈值过小，会截掉正常大小的工具结果"
    )


def test_head_quota_larger_than_tail():
    """头部配额应大于尾部（文件开头通常是声明/导入/结构）。"""
    assert SessionTracker.TOOL_RESULT_HEAD_CHARS > SessionTracker.TOOL_RESULT_TAIL_CHARS


def test_compression_is_wired_into_prompt_building():
    """结构断言：压缩必须真的接进了 prompt 构建路径。

    防止有人只加函数却没接线（那是最容易犯的错——代码在、但不生效）。
    """
    src_path = os.path.join(_REPO_ROOT, "app", "engine", "session_tree.py")
    src = open(src_path, encoding="utf-8").read()
    # 工具结果进入 prompt 的两条主要路径都应调用压缩
    assert "cls._compress_tool_result(content)" in src, (
        "工具结果未经过压缩就进入 prompt"
    )
    assert src.count("_compress_tool_result(") >= 3, (
        "_compress_tool_result 的定义 + 至少 2 处调用；当前接线不完整"
    )


def test_compression_reduces_real_world_payload():
    """用 T19 实测的最大单条结果（149KB）验证真实收益。"""
    real_size = 149_069
    out = C("a" * real_size)
    ratio = len(out) / real_size
    assert ratio < 0.30, f"压缩比仅 {ratio:.0%}，收益不足"
