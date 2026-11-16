"""
DSML Stack Parser (符号下推自动机分析器)

基于编译原理语法分析模型（Chomsky 2型上下文无关文法 / Pushdown Automaton），
通过符号栈确定性维护标签嵌套层级，从物理上消灭平面正则清洗导致的：
1. 缺失尾随尖括号（如 900000</｜｜DSML｜｜）渗漏污染
2. 模糊无名闭合（如 </｜｜DSML｜｜>）作用域误判与参数截断
3. 参数内合法技术代码（如 a < b && c > d）误杀
4. 漏写参数闭合标签时的自动归约（Implicit Auto-Pop / Panic Mode Recovery）
"""

import re
import json
import uuid
from typing import Dict, Any, List, Optional, Tuple


class ParamNode:
    """参数抽象语法树节点"""
    def __init__(self, name: str):
        self.name: str = name
        self.chunks: List[str] = []

    @property
    def value(self) -> str:
        return "".join(self.chunks)


class InvokeNode:
    """工具调用抽象语法树节点"""
    def __init__(self, name: str):
        self.name: str = name
        self.args: Dict[str, Any] = {}
        self.raw_chunks: List[str] = []


class DSMLStackParser:
    """
    基于符号栈的 DSML 与工具调用解析器。
    统一为离线提取与流式管道提供确定性结构化参数解析。
    """

    # 全文/多调用词法 Token 正则
    TOKEN_PATTERN = re.compile(
        # 1. 容器开闭: <[| DSML] calls> / </[| DSML] calls>
        r'(?P<container_open><[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:calls|tool_calls)[\|｜\s]*>)'
        r'|(?P<container_close></[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:calls|tool_calls)[\|｜\s]*>|<[\|｜\s/]*(?:DSML[\|｜\s/]*)?/\s*(?:calls|tool_calls)[\|｜\s]*>)'
        # 2. Invoke 开标签: <[| DSML] invoke name="...">
        r'|(?P<invoke_open><[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:invoke|tool_call|call)\s+(?:name|tool|function)=["\']?(?P<inv_name>[a-zA-Z0-9_\-\.:]+)["\']?[^>]*>)'
        # 3. Invoke 显式闭标签: </[| DSML] invoke> 或 <[| DSML] /invoke> 或 [| DSML] invoke>
        r'|(?P<invoke_close></[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:invoke|tool_call|call)\s*>|<[\|｜\s/]*(?:DSML[\|｜\s/]*)?/\s*(?:invoke|tool_call|call)\s*>|[\|｜\s/]+DSML[\|｜\s/]*(?:invoke|tool_call|call)\s*>)'
        # 4. Parameter 开标签: <[| DSML] parameter name="..."> 或 <[| DSML] param_name" string="true">
        r'|(?P<param_open><[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:parameter\s+name=["\']?(?P<p_name1>[a-zA-Z0-9_\-]+)["\']?|(?P<p_name2>[a-zA-Z0-9_\-]+)["\']?\s*(?:string=|=))[^>]*>)'
        # 5. Parameter 显式闭标签: </[| DSML] parameter> 或 <[| DSML] /parameter> 或 [| DSML] parameter>
        r'|(?P<param_close></[\|｜\s/]*(?:DSML[\|｜\s/]*)?parameter\s*>|<[\|｜\s/]*(?:DSML[\|｜\s/]*)?/\s*parameter\s*>|[\|｜\s/]+DSML[\|｜\s/]*parameter\s*>)'
        # 6. 无名/残缺闭标签: </[| DSML] > 或 </[| DSML] (无 >) 或 [| DSML] >
        r'|(?P<headless_close></?[\|｜\s/]*DSML[\|｜\s]*>?|[\|｜\s/]+DSML[\|｜\s/]*>?)',
        re.IGNORECASE
    )

    # Invoke 内部参数级词法 Token 正则
    PARAM_TOKEN_PATTERN = re.compile(
        r'(?P<param_open><[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:parameter\s+name=["\']?(?P<p_name1>[a-zA-Z0-9_\-]+)["\']?|(?P<p_name2>[a-zA-Z0-9_\-]+)["\']?\s*(?:string=|=))[^>]*>)'
        r'|(?P<param_close></[\|｜\s/]*(?:DSML[\|｜\s/]*)?parameter\s*>|<[\|｜\s/]*(?:DSML[\|｜\s/]*)?/\s*parameter\s*>|[\|｜\s/]+DSML[\|｜\s/]*parameter\s*>)'
        r'|(?P<invoke_close></[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:invoke|call|calls|tool_call|tool_calls)\s*>|<[\|｜\s/]*(?:DSML[\|｜\s/]*)?/\s*(?:invoke|call|calls|tool_call|tool_calls)\s*>|[\|｜\s/]+DSML[\|｜\s/]*(?:invoke|call|calls|tool_call|tool_calls)\s*>)'
        r'|(?P<headless_close></?[\|｜\s/]*DSML[\|｜\s]*>?|[\|｜\s/]+DSML[\|｜\s/]*>?)'
        r'|(?P<xml_open><(?P<xml_tag>[a-zA-Z0-9_\-]+)>)'
        r'|(?P<xml_close></(?P<xml_close_tag>[a-zA-Z0-9_\-]+)>)',
        re.IGNORECASE
    )

    @classmethod
    def parse_invoke_body(
        cls,
        body: str,
        tool_name: str = "",
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        利用符号下推自动机解析 Invoke 标签体内部的参数结构。
        """
        if not body or not body.strip():
            return {}

        schema_props = set()
        if tools and tool_name:
            for t in tools:
                fn = t.get("function", t)
                if fn.get("name") == tool_name:
                    schema_props = set((fn.get("parameters", {}) or {}).get("properties", {}).keys())
                    break

        args: Dict[str, Any] = {}
        active_param: Optional[ParamNode] = None

        last_pos = 0
        for m in cls.PARAM_TOKEN_PATTERN.finditer(body):
            text_chunk = body[last_pos:m.start()]
            last_pos = m.end()

            # 将标签前的文本归属给当前活动参数（零污染吸入）
            if active_param and text_chunk:
                active_param.chunks.append(text_chunk)

            if m.group("param_open"):
                p_name = m.group("p_name1") or m.group("p_name2")
                if p_name and p_name.lower() not in ("invoke", "calls", "call", "tool_calls", "dsml"):
                    # 编译器隐式归约：如果已有未闭合参数，先封口前一个
                    if active_param:
                        args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = ParamNode(p_name.strip())

            elif m.group("param_close") or m.group("headless_close"):
                if active_param:
                    args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None

            elif m.group("invoke_close"):
                if active_param:
                    args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None

            elif m.group("xml_open"):
                tag_name = m.group("xml_tag")
                if schema_props and tag_name in schema_props:
                    if active_param:
                        args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = ParamNode(tag_name)
                elif active_param:
                    active_param.chunks.append(m.group(0))

            elif m.group("xml_close"):
                tag_name = m.group("xml_close_tag")
                if active_param and active_param.name == tag_name:
                    args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None
                elif active_param:
                    active_param.chunks.append(m.group(0))

        # 消费剩余文本
        trailing_text = body[last_pos:]
        if active_param and trailing_text:
            active_param.chunks.append(trailing_text)

        if active_param:
            args[active_param.name] = cls._coerce_value(active_param.value)
            active_param = None

        if args:
            return args

        # 兜底形态 1：整段是裸 JSON
        stripped = body.strip().strip("`").strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            parsed = cls._repair_and_loads(stripped)
            if isinstance(parsed, dict) and parsed:
                return parsed

        # 兜底形态 2：清除残余 DSML 标签后若为空则返回空字典（如无参工具 job_list）
        clean_text = re.sub(r'</?[\|｜\s/]*DSML[\|｜\s/]*(?:[^>\r\n]*>?)?', '', body).strip()
        clean_text = re.sub(r'<[\|｜\s/]*DSML[\|｜\s/]*[^>]*>', '', clean_text).strip()
        if not clean_text:
            return {}

        # 兜底形态 3：schema 里有唯一的必填字符串参数时，把整段 clean_text 当作它的值
        for t in tools or []:
            fn = t.get("function", t)
            if fn.get("name") == tool_name:
                params = fn.get("parameters", {}) or {}
                required = params.get("required", []) or []
                props = params.get("properties", {}) or {}
                if len(required) == 1 and props.get(required[0], {}).get("type") == "string":
                    field = required[0]
                    return {field: clean_text}
                break

        return {"input": clean_text}

    @classmethod
    def extract_tool_calls_from_text(
        cls,
        text: str,
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        利用符号下推自动机从文本中提取所有 DSML / tool_call 调用，
        并返回 (干净正文, 工具调用列表)。
        """
        if not text or not text.strip():
            return text, []

        if not cls.TOKEN_PATTERN.search(text):
            return text, []

        from app.engine.tool_bridge import ToolBridge

        clean_chunks: List[str] = []
        raw_invokes: List[InvokeNode] = []

        active_invoke: Optional[InvokeNode] = None
        active_param: Optional[ParamNode] = None

        last_pos = 0

        for m in cls.TOKEN_PATTERN.finditer(text):
            text_chunk = text[last_pos:m.start()]
            last_pos = m.end()

            # 分发标签间的文本
            if active_param:
                active_param.chunks.append(text_chunk)
            elif active_invoke:
                active_invoke.raw_chunks.append(text_chunk)
            else:
                if text_chunk:
                    clean_chunks.append(text_chunk)

            # 状态转移
            if m.group("container_open"):
                pass  # 容器开标签被静默消费

            elif m.group("container_close"):
                # 遇到 calls 闭标签，归约所有未完成的 invoke
                if active_param and active_invoke:
                    active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None
                if active_invoke:
                    raw_invokes.append(active_invoke)
                    active_invoke = None

            elif m.group("invoke_open"):
                inv_name = m.group("inv_name")
                if active_param and active_invoke:
                    active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None
                if active_invoke:
                    raw_invokes.append(active_invoke)
                active_invoke = InvokeNode(inv_name.strip())

            elif m.group("invoke_close"):
                if active_param and active_invoke:
                    active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None
                if active_invoke:
                    raw_invokes.append(active_invoke)
                    active_invoke = None

            elif m.group("param_open"):
                p_name = m.group("p_name1") or m.group("p_name2")
                if p_name:
                    if not active_invoke:
                        active_invoke = InvokeNode("")
                    if active_param:
                        active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = ParamNode(p_name.strip())

            elif m.group("param_close"):
                if active_param and active_invoke:
                    active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None

            elif m.group("headless_close"):
                # 无名/残缺闭合标签 (如 </|DSML|>)：
                # 1. 优先关闭活动参数 active_param
                if active_param and active_invoke:
                    active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
                    active_param = None
                # 2. 关键防护：当 active_param 已为 None 时，绝不在此提前强行切断 active_invoke！
                #    因为在模型漏写 parameter 关键字时（如 </|DSML|>），可能夹带重复或残缺标签；
                #    若此处草率将 active_invoke 提前断开，后续同属该调用的参数（如 content）将被截断或分裂为孤儿调用。
                #    invoke 只有在遇到显式 invoke_close、container_close、下一个 invoke_open 或到达 EOF 时才被封口。

        # 消费末尾文本
        trailing_text = text[last_pos:]
        if active_param and active_invoke:
            active_param.chunks.append(trailing_text)
            active_invoke.args[active_param.name] = cls._coerce_value(active_param.value)
            active_param = None
        elif active_invoke:
            active_invoke.raw_chunks.append(trailing_text)
            raw_invokes.append(active_invoke)
            active_invoke = None
        else:
            if trailing_text:
                clean_chunks.append(trailing_text)

        # 构造成 OpenAI 标准 tool_calls 结构
        tool_calls: List[Dict[str, Any]] = []
        for inv in raw_invokes:
            target_name = inv.name
            if not target_name:
                target_name = ToolBridge.infer_tool_name_from_args(inv.args, tools or []) or ""
            if not target_name:
                continue
            available_names = {t.get("function", t).get("name") for t in (tools or []) if t.get("function", t).get("name")}
            matched_name = ToolBridge.match_tool_name(target_name, available_names)
            if not inv.args and "".join(inv.raw_chunks).strip():
                # 若无 parameter 标签，尝试解析 raw_chunks 中的 JSON 或子标签
                inv.args = cls.parse_invoke_body("".join(inv.raw_chunks), matched_name, tools)

            norm_args = ToolBridge._normalize_tool_args(matched_name, inv.args, tools)
            call_id = f"call_{uuid.uuid4().hex[:10]}"
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": matched_name,
                    "arguments": json.dumps(norm_args, ensure_ascii=False),
                },
            })

        clean_text = "".join(clean_chunks).strip()
        # 清理多余空行
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
        return clean_text, tool_calls

    @staticmethod
    def _coerce_value(val: str) -> Any:
        """纯净字符串的基础类型自愈"""
        clean_val = val.strip()
        # 防御性截断末尾极端残余的边界前缀（如正好卡在 </| 切片或漏写尖括号的 ｜DSML｜｜ parameter>）
        clean_val = re.sub(r'(?:</?[\|｜\s/]*|[\|｜\s/]+)DSML[\|｜\s/]*(?:[^>\r\n]*>?)?$', '', clean_val).strip()

        if clean_val.lower() == "true":
            return True
        if clean_val.lower() == "false":
            return False
        if clean_val.lower() == "null":
            return None
        if clean_val.isdigit():
            return int(clean_val)
        if (clean_val.startswith("{") and clean_val.endswith("}")) or (clean_val.startswith("[") and clean_val.endswith("]")):
            try:
                return json.loads(clean_val)
            except Exception:
                pass
        return clean_val

    @staticmethod
    def _repair_and_loads(s: str) -> Optional[Any]:
        """容错解析 JSON，自动修复未转义反斜杠 (Windows 路径)、中文引号及尾随逗号"""
        if not s or not s.strip():
            return None
        try:
            return json.loads(s, strict=False)
        except Exception:
            pass
        fixed = s.replace('“', '"').replace('”', '"')
        fixed = re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', fixed)
        fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
        try:
            return json.loads(fixed, strict=False)
        except Exception:
            pass
        return None
