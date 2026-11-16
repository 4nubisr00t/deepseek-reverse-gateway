"""
网关与客户端（DSH）之间关于「上下文超限」的**契约测试**。

## 为什么要有这个文件

agent（DSH）**本来就有完整的压缩能力** —— 它带了整套压缩插件：

    @deepseek-ai/dsh-compaction / -compaction-basic
    / -compaction-tool-result-pruner / -token-meter / -output-retention …

而它的压缩中间件只有在失败码**严格等于** `CONTEXT_WINDOW_EXCEEDED` 时才触发
（`dsh-compaction-basic/lib/index.js`：

    if (failure.code !== CONTEXT_WINDOW_EXCEEDED_CODE || signal.aborted) return next();

），那个码由 `isContextWindowExceededError(detail)` 判定，而**它的判据全是英文正则**
（`dsh-llm/lib/types/error.js`）。

**旧文案是纯中文**，五条正则一条都匹配不上 → 客户端认不出"这是上下文超限"
→ 压缩根本不触发 → 只能无限重试。
**于是 agent 明明会压缩，却表现为"没有做到即时压缩"。**

本文件把 DSH 的判据**逐字搬进来**，让"我们的错误文案必须能被它识别"
成为一条可执行的断言 —— 而不是靠人去记"这里要写英文"。

## 特别说明：正则是从客户端源码抄来的，不是我们发明的

下面 `_DSH_*` 五个正则在语义上**逐字对应**
`node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm/lib/types/error.js`
里的 `isContextWindowExceededError()`。抄进来是为了**离线可测**，
而不是重新定义一套自己的规则。
"""

import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (
    CANONICAL_MODEL_CONTEXT_WINDOW,
    CANONICAL_MODEL_MAX_OUTPUT_TOKENS,
)
from app.api.openai_routes import classify_upstream_preflight


# ══════════════════════════════════════════════════════════════════════
# DSH 的判据（照抄自 dsh-llm/lib/types/error.js:isContextWindowExceededError）
# ══════════════════════════════════════════════════════════════════════

#: `(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-](?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])`
_DSH_STRUCTURED_CONTEXT_OVERFLOW = re.compile(
    r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
    r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)

#: `\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b`
_DSH_MAX_CONTEXT = re.compile(
    r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b",
    re.IGNORECASE,
)

#: TOO_LARGE_FOR_CONTEXT
_DSH_TOO_LARGE_FOR_CONTEXT = re.compile(
    r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?"
    r"too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?"
    r"(?:model(?:'s)?\s+)?context(?:\s+window)?\b",
    re.IGNORECASE,
)

#: `\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b`
_DSH_INPUT_TOO_LONG = re.compile(
    r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b",
    re.IGNORECASE,
)

#: EXCEEDS_MODEL_CONTEXT
_DSH_EXCEEDS_MODEL_CONTEXT = re.compile(
    r"\b(?:input|prompt|request|messages?)\b.{0,40}"
    r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
    r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b",
    re.IGNORECASE,
)

#: DSH 判定的失败码。压缩中间件要求 `failure.code` **严格等于**它才触发。
DSH_CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"


def dsh_recognizes_context_overflow(detail: str) -> bool:
    """等价于 DSH 的 `isContextWindowExceededError(detail)`。

    返回 True ⇒ DSH 会把它归类为 `CONTEXT_WINDOW_EXCEEDED` ⇒ **压缩会被触发**。
    """
    return bool(
        _DSH_STRUCTURED_CONTEXT_OVERFLOW.search(detail)
        or _DSH_MAX_CONTEXT.search(detail)
        or _DSH_TOO_LARGE_FOR_CONTEXT.search(detail)
        or _DSH_INPUT_TOO_LONG.search(detail)
        or _DSH_EXCEEDS_MODEL_CONTEXT.search(detail)
    )


class _FakeUpstreamError:
    """上游的终态错误事件（逐字取自清单 T24 的实测原文）"""

    def __init__(self, content, error_code=""):
        self.event_type = "error"
        self.content = content
        self.error_code = error_code


def _our_detail_for_upstream(content) -> str:
    d = classify_upstream_preflight(_FakeUpstreamError(content), None, 0.0)
    assert d.kind == "context_exhausted", f"没被识别为 context_exhausted（{d.kind}）"
    return d.detail


class DshContractTest(unittest.TestCase):
    """★ 核心：我们返给客户端的文案，必须能被客户端识别为"上下文超限"。"""

    #: 上游真实原文（清单 T24 实测）
    UPSTREAM_TEXT = "达到对话长度上限，请开启新对话"

    def test_our_message_is_recognized_by_dsh(self):
        detail = _our_detail_for_upstream(self.UPSTREAM_TEXT)
        self.assertTrue(
            dsh_recognizes_context_overflow(detail),
            f"★ 客户端**认不出**这是上下文超限 → 它的压缩中间件不会触发 →\n"
            f"agent 明明有压缩能力却只能无限重试。\n"
            f"实际文案：{detail!r}",
        )

    def test_named_regexes_that_match(self):
        """说清楚是**哪几条**正则在起作用（便于将来上游改文案时定位）"""
        detail = _our_detail_for_upstream(self.UPSTREAM_TEXT)
        matched = [
            name for name, rx in (
                ("STRUCTURED_CONTEXT_OVERFLOW", _DSH_STRUCTURED_CONTEXT_OVERFLOW),
                ("MAX_CONTEXT", _DSH_MAX_CONTEXT),
                ("TOO_LARGE_FOR_CONTEXT", _DSH_TOO_LARGE_FOR_CONTEXT),
                ("INPUT_TOO_LONG", _DSH_INPUT_TOO_LONG),
                ("EXCEEDS_MODEL_CONTEXT", _DSH_EXCEEDS_MODEL_CONTEXT),
            ) if rx.search(detail)
        ]
        self.assertTrue(matched, "一条正则都没命中")
        # 双保险：至少命中两条，这样将来上游改文案时不容易一次性全失效
        self.assertGreaterEqual(
            len(matched), 2,
            f"只有 {matched} 一条命中 —— 单点依赖，建议文案里再多覆盖一种说法",
        )

    def test_old_chinese_only_wording_would_not_be_recognized(self):
        """★ 守卫有牙：**旧的纯中文文案确实认不出来**。

        这条证明上面的断言不是自动成立的 —— 换句话说，如果我们把英文删掉，
        测试一定会红。没有它，上一条只是"碰巧通过"。
        """
        old = (
            "本会话上下文已达上游长度上限，无法继续在该会话中作答。"
            "网关已标记该会话需重建，请重试（将自动开启新会话）。"
        )
        self.assertFalse(
            dsh_recognizes_context_overflow(old),
            "旧的纯中文文案居然被认出来了 —— 那说明这组断言守不住任何东西",
        )

    def test_recognizer_accepts_the_wording_dsh_expects(self):
        """反向自检：这组正则对**已知的 OpenAI 系说法**确实有反应"""
        for known in (
            "This model's maximum context length is 8192 tokens",
            "context_length_exceeded",
            "prompt is too long for this model",
            "input exceeds the model context window",
        ):
            with self.subTest(detail=known):
                self.assertTrue(dsh_recognizes_context_overflow(known),
                                f"{known!r} 应被识别（DSH 自己认这个写法）")

    def test_recognizer_ignores_unrelated_errors(self):
        """不能什么都当成上下文超限 —— 否则每次限流都去压缩，代价很大"""
        for other in (
            "消息发送过于频繁，请稍后重试",
            "user is muted",
            "rate limit exceeded",
            "DeepSeek 错误: 服务暂时不可用 (503)",
        ):
            with self.subTest(detail=other):
                self.assertFalse(dsh_recognizes_context_overflow(other),
                                 f"{other!r} 不该被当成上下文超限")

    def test_the_code_dsh_compares_against_is_the_openai_one(self):
        """记录契约常量：DSH 比的是 `CONTEXT_WINDOW_EXCEEDED`，
        而 OpenAI 规范里同义的是 `context_length_exceeded` —— 后者也命中结构化正则。"""
        self.assertEqual(DSH_CONTEXT_WINDOW_EXCEEDED_CODE, "CONTEXT_WINDOW_EXCEEDED")
        self.assertTrue(dsh_recognizes_context_overflow("context_length_exceeded"))


class ContextWindowDeclarationTest(unittest.TestCase):
    """★ 第二半：声明多大的 `context_window`，直接决定客户端**什么时候**压缩。

    DSH 的压缩阈值 = `context_window × 0.8`（`DEFAULT_THRESHOLD_RATIO = .8`）。
    声明得太大 → 客户端压得太晚 → 撞墙。
    """

    #: 清单 T24「实测断点 E37」：这个量级实测**正常**
    MEASURED_OK_TOKENS = 800_000
    #: 实测**失败**的量级
    MEASURED_FAIL_TOKENS = 1_600_000
    #: DSH 的压缩阈值比例（照抄自 dsh-compaction-basic）
    DSH_THRESHOLD_RATIO = 0.8

    def test_client_threshold_lands_below_the_measured_limit(self):
        threshold = int(CANONICAL_MODEL_CONTEXT_WINDOW * self.DSH_THRESHOLD_RATIO)
        self.assertLess(
            threshold, self.MEASURED_OK_TOKENS,
            f"★ 客户端的压缩阈值（{threshold:,}）不在实测正常范围内 —— "
            f"它会等到踩线才开始压缩，等于不压。"
            f"（声明值 {CANONICAL_MODEL_CONTEXT_WINDOW:,} × {self.DSH_THRESHOLD_RATIO}）",
        )

    def test_declaration_is_not_absurdly_small(self):
        """也不能小到让客户端不停地压缩（每次压缩本身就是一次模型调用）"""
        self.assertGreater(
            CANONICAL_MODEL_CONTEXT_WINDOW * self.DSH_THRESHOLD_RATIO, 200_000,
            "声明太小 → 压缩过于频繁 → 每次压缩都是一次额外的模型调用",
        )

    def test_old_1m_declaration_would_have_been_too_late(self):
        """守卫有牙：旧值 1048576 算出来的阈值必须**落在实测的失败区间附近**。

        这条证明上面第一条断言不是恒真的 —— 旧的错误值会被它拦下。
        """
        old_threshold = int(1_048_576 * self.DSH_THRESHOLD_RATIO)
        self.assertGreaterEqual(
            old_threshold, self.MEASURED_OK_TOKENS,
            f"旧值算出来是 {old_threshold:,} —— 若它小于 {self.MEASURED_OK_TOKENS:,}，"
            f"那「1M 太晚」这个结论就不成立，需要重新核实",
        )

    def test_model_card_max_tokens_is_output_not_context(self):
        """模型卡里 `max_tokens` 必须是**最大输出**，不是上下文窗口（两个不同的量）"""
        import ast
        src = (PROJECT_ROOT / "app" / "api" / "openai_routes.py").read_text("utf-8")
        tree = ast.parse(src)
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "context_window" in keys and "max_tokens" in keys:
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value in ("context_window", "max_tokens"):
                        found[k.value] = ast.unparse(v)
        self.assertIn("max_tokens", found, "没找到模型卡里的 max_tokens —— 断言失效")
        self.assertIn(
            "CANONICAL_MODEL_MAX_OUTPUT_TOKENS", found["max_tokens"],
            f"`max_tokens` 被设成了 {found['max_tokens']} —— 它是**最大输出**，"
            f"不是上下文窗口；声明成上下文窗口会让客户端发出不合理的请求",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
