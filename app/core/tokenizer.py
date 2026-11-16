"""
Token 计量与估算核心模块 (TokenEstimator)
对齐 OpenAI (cl100k_base)、DeepSeek 原生分词与 Anthropic 标准，
支持全上下文 Prompt（含网关注入的工具规范与 Schema）、思考链 Reasoning、正文及并发工具调用的高精度计费统计。
"""

import json
import re
from typing import List, Dict, Any, Optional

try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
    try:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODER = None
        _TIKTOKEN_AVAILABLE = False
except ImportError:
    _TIKTOKEN_AVAILABLE = False
    _ENCODER = None


class TokenEstimator:
    """高精度 Token 计量器"""

    @classmethod
    def estimate_text_tokens(cls, text: Optional[str]) -> int:
        """计算单段文本的 Token 数量"""
        if not text:
            return 0
        if _TIKTOKEN_AVAILABLE and _ENCODER is not None:
            try:
                return len(_ENCODER.encode(text, disallowed_special=()))
            except Exception:
                pass
        
        # 高保真规则分词 Fallback
        # 1. CJK 字符（中文字符、韩文、日文假名等）：每个字符在 BPE 下约 1.25 ~ 1.45 Token
        cjk_chars = len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text))
        cjk_tokens = int(cjk_chars * 1.35 + 0.5)

        # 2. 剥离 CJK 后的文本
        non_cjk = re.sub(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', ' ', text)

        # 3. 标点符号与特殊运算符（在 BPE 分词中单标点或运算符绝大多数独立为 1 个 Token）
        symbols = len(re.findall(r'[()\[\]{},.:;\'\"!?<>=+\-*/_\\|@#$%^&~`]', non_cjk))

        # 4. 英文单词与数字（平均 3.6 字符 1 个 token）
        words = re.findall(r'[a-zA-Z0-9]+', non_cjk)
        word_tokens = sum(max(1, int(len(w) / 3.6 + 0.5)) for w in words)

        # 5. 空格缩进与换行符号开销（代码缩进每 2~4 个连续空格即为 1 个 Token）
        indent_spaces = sum(len(sp) // 4 for sp in re.findall(r' {4,}', non_cjk))
        newlines = len(re.findall(r'[\r\n]+', non_cjk))

        return max(1, cjk_tokens + symbols + word_tokens + indent_spaces + newlines)

    @classmethod
    def estimate_messages_tokens(
        cls, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> int:
        """
        完整遍历客户端传入的历史上下文（System, User, Assistant, Tool），
        准确核算整个请求的 Prompt Tokens（含工具 schema、注入的协议规范和多模态图片折算）。
        """
        if not messages:
            return 0

        total_tokens = 3  # 每轮对话基础起始标记 (ChatML <|im_start|> overhead)

        for m in messages:
            total_tokens += 4  # 基础角色标记开销 (<|im_start|>{role}\n{content}<|im_end|>)
            role = m.get("role", "")
            content = m.get("content", "")

            # 1. 文本与多模态内容计算
            if isinstance(content, str):
                total_tokens += cls.estimate_text_tokens(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        ptype = part.get("type", "")
                        if ptype == "text":
                            total_tokens += cls.estimate_text_tokens(part.get("text", ""))
                        elif ptype in ("image_url", "image"):
                            # 多模态图片 Token 折算（标准高保真每张图约 1,000 ~ 1,200 Token）
                            total_tokens += 1024

            # 2. Assistant 历史中的 tool_calls 参数计算
            tool_calls = m.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        total_tokens += cls.estimate_tool_call_tokens(tc)

            # 3. Tool 回复的 tool_call_id 与 name 开销
            if role == "tool":
                total_tokens += 4  # tool 响应前缀开销
                if m.get("tool_call_id"):
                    total_tokens += cls.estimate_text_tokens(m.get("tool_call_id"))
                if m.get("name"):
                    total_tokens += cls.estimate_text_tokens(m.get("name"))

        # 4. 注入 Tools 规范与 Schema 定义开销
        if tools and isinstance(tools, list):
            try:
                from app.engine.tool_bridge import ToolBridge
                # 网关实际为模型注入的工具协议规范与完整参数 Schema 描述
                tool_prompt = ToolBridge.build_system_prompt(tools)
                total_tokens += cls.estimate_text_tokens(tool_prompt)
            except Exception:
                # 兜底：工具原始 JSON + 工具使用说明协议 (~350 tokens)
                try:
                    tools_json = json.dumps(tools, ensure_ascii=False)
                    total_tokens += cls.estimate_text_tokens(tools_json) + 350
                except Exception:
                    total_tokens += len(tools) * 160 + 350

        return total_tokens

    @classmethod
    def estimate_tool_call_tokens(cls, tool_call: Dict[str, Any]) -> int:
        """计算单次输出工具调用的参数开销（含 XML 包装与 JSON 结构）"""
        tokens = 10  # <tool_call>\n{"name": "...", "arguments": ...}\n</tool_call> 标签与结构开销
        fn = tool_call.get("function", {})
        fname = fn.get("name", "")
        fargs = fn.get("arguments", "")
        tokens += cls.estimate_text_tokens(fname)
        if isinstance(fargs, str):
            tokens += cls.estimate_text_tokens(fargs)
        elif isinstance(fargs, dict):
            tokens += cls.estimate_text_tokens(json.dumps(fargs, ensure_ascii=False))
        return tokens
