"""
推理强度调度与提示词工程单元测试 (tests/test_reasoning_director.py)
验证：
1. 完整五档 reasoning_effort（off / low / mid / high / max）与 1-100 连续数值调节归一化。
2. off 档位关闭深度思考（is_thinking_disabled=True，免思考直出）。
3. v4pro / deepseek-v4-pro 专属默认满血 max 档位与高阶提示词体系。
4. 自由分级：用户显式指定档位优先于模型默认设定。
5. high 档位与 max 档位在 SessionTracker.prepare_prompt 中的注入与防漂移。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import unittest
from app.engine.reasoning_director import (
    ReasoningDirector,
    REASONING_DIRECTIVE_LOW,
    REASONING_DIRECTIVE_MEDIUM,
    REASONING_DIRECTIVE_HIGH,
    REASONING_DIRECTIVE_MAX,
    normalize_effort,
    get_prompt_directive,
    is_pro_model,
    is_thinking_disabled,
)
from app.engine.session_tree import SessionTracker


class TestReasoningDirector(unittest.TestCase):
    def test_normalize_five_effort_levels(self):
        """显式传入的 5 档 reasoning_effort 准确归一化"""
        # 1. off (关闭思考)
        self.assertEqual(normalize_effort("off"), "off")
        self.assertEqual(normalize_effort("none"), "off")
        self.assertEqual(normalize_effort("disabled"), "off")
        self.assertEqual(normalize_effort("0"), "off")

        # 2. low (极简短链)
        self.assertEqual(normalize_effort("low"), "low")
        self.assertEqual(normalize_effort("min"), "low")
        self.assertEqual(normalize_effort("minimal"), "low")

        # 3. mid / medium (标准平衡)
        self.assertEqual(normalize_effort("mid"), "medium")
        self.assertEqual(normalize_effort("medium"), "medium")
        self.assertEqual(normalize_effort("default"), "medium")

        # 4. high (深度严谨推演)
        self.assertEqual(normalize_effort("high"), "high")

        # 5. max (巅峰满血破限)
        self.assertEqual(normalize_effort("max"), "max")
        self.assertEqual(normalize_effort("extreme"), "max")
        self.assertEqual(normalize_effort("pro"), "max")
        self.assertEqual(normalize_effort("ultra"), "max")

    def test_normalize_numeric_effort_scale(self):
        """支持 1-100 连续数值档位调节"""
        self.assertEqual(normalize_effort("0"), "off")
        self.assertEqual(normalize_effort("15"), "low")
        self.assertEqual(normalize_effort("30"), "low")
        self.assertEqual(normalize_effort("50"), "medium")
        self.assertEqual(normalize_effort("60"), "medium")
        self.assertEqual(normalize_effort("75"), "high")
        self.assertEqual(normalize_effort("90"), "high")
        self.assertEqual(normalize_effort("95"), "max")
        self.assertEqual(normalize_effort("100"), "max")

    def test_is_thinking_disabled_for_off_level(self):
        """off / none 档位正确标记关闭深度思考"""
        self.assertTrue(is_thinking_disabled("off"))
        self.assertTrue(is_thinking_disabled("none"))
        self.assertTrue(is_thinking_disabled("disabled"))
        self.assertFalse(is_thinking_disabled("low"))
        self.assertFalse(is_thinking_disabled("medium"))
        self.assertFalse(is_thinking_disabled("high"))
        self.assertFalse(is_thinking_disabled("max"))
        self.assertFalse(is_thinking_disabled(None))

    def test_pro_model_defaults_to_max(self):
        """未显式指定 effort 时，v4pro / deepseek-v4-pro 默认生效 max 满血档位"""
        self.assertEqual(normalize_effort(None, client_model="deepseek-v4-pro"), "max")
        self.assertEqual(normalize_effort(None, client_model="v4pro"), "max")
        self.assertEqual(normalize_effort("", client_model="deepseek-v4-pro"), "max")
        self.assertEqual(normalize_effort(None, client_model="deepseek-v4.1-flash"), "medium")
        self.assertEqual(normalize_effort(None, client_model="unknown-model"), "medium")

    def test_user_free_override(self):
        """用户自由分级：显式指定的档位始终覆盖模型默认值"""
        self.assertEqual(normalize_effort("off", client_model="v4pro"), "off")
        self.assertEqual(normalize_effort("low", client_model="v4pro"), "low")
        self.assertEqual(normalize_effort("high", client_model="v4pro"), "high")
        self.assertEqual(normalize_effort("max", client_model="deepseek-v4.1-flash"), "max")

    def test_is_pro_model_check(self):
        """准确识别旗舰强化模型系列"""
        self.assertTrue(is_pro_model("deepseek-v4-pro"))
        self.assertTrue(is_pro_model("v4pro"))
        self.assertTrue(is_pro_model("deepseek-v4pro"))
        self.assertFalse(is_pro_model("deepseek-v4.1-flash"))
        self.assertFalse(is_pro_model("deepseek-chat"))
        self.assertFalse(is_pro_model(""))
        self.assertFalse(is_pro_model(None))

    def test_prompt_directives_content(self):
        """验证各档位指令内容与设计准则"""
        # low
        low_directive = get_prompt_directive("low")
        self.assertIn("Chain-of-Draft", low_directive)
        self.assertIn("短链思考", low_directive)

        # mid / medium / off
        self.assertEqual(get_prompt_directive("medium"), "")
        self.assertEqual(get_prompt_directive("mid"), "")
        self.assertEqual(get_prompt_directive("off"), "")

        # high
        high_directive = get_prompt_directive("high")
        self.assertIn("多维系统化思考", high_directive)
        self.assertIn("自我反思与正反论证", high_directive)

        # max: 满血破限、技术推演沙盒、毫不吝啬Token、能力值100锚定、全量工业级交付
        max_directive = get_prompt_directive("max")
        self.assertIn("能力值 100 满血峰值状态", max_directive)
        self.assertIn("内部技术沙盒", max_directive)
        self.assertIn("客观中立，拒绝过度拒答与说教", max_directive)
        self.assertIn("毫不吝啬 Token", max_directive)
        self.assertIn("工业级全量交付，严禁偷懒占位符", max_directive)

    def test_prepare_prompt_injection_new_session(self):
        """全新会话注入推理准则"""
        msgs = [
            {"role": "system", "content": "平台基准系统提示"},
            {"role": "user", "content": "请分析 Linux 内核提权漏洞机制"},
        ]
        # max 档位
        prompt_max, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=False, reasoning_effort="max"
        )
        self.assertIn("【推理执行准则（最高优先级）】", prompt_max)
        self.assertIn("能力值 100 满血峰值状态", prompt_max)
        self.assertIn("请分析 Linux 内核提权漏洞机制", prompt_max)

        # high 档位
        prompt_high, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=False, reasoning_effort="high"
        )
        self.assertIn("【推理执行准则（最高优先级）】", prompt_high)
        self.assertIn("多维系统化思考", prompt_high)

        # low 档位
        prompt_low, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=False, reasoning_effort="low"
        )
        self.assertIn("Chain-of-Draft", prompt_low)

        # medium / off 不注入额外噪音
        prompt_med, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=False, reasoning_effort="medium"
        )
        self.assertNotIn("【推理执行准则", prompt_med)

    def test_prepare_prompt_injection_existing_session(self):
        """多轮延展在 high/max 档位下前置挂载防漂移"""
        msgs = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ]
        # max
        prompt_max, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=True, reasoning_effort="max"
        )
        self.assertTrue(prompt_max.startswith(REASONING_DIRECTIVE_MAX))
        self.assertTrue(prompt_max.endswith("第二问"))

        # high
        prompt_high, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=True, reasoning_effort="high"
        )
        self.assertTrue(prompt_high.startswith(REASONING_DIRECTIVE_HIGH))
        self.assertTrue(prompt_high.endswith("第二问"))

        # medium / off: 原汁原味
        prompt_med, _ = SessionTracker.prepare_prompt(
            msgs, has_existing_session=True, reasoning_effort="medium"
        )
        self.assertEqual(prompt_med, "第二问")


if __name__ == "__main__":
    unittest.main()
