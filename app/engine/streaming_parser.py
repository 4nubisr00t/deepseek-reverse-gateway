"""
Streaming Tool Calling Parser (流式有限状态机解析器)
- 零延迟打字机体验 (TTFT 降至 0ms)
- Markdown 代码块感知 (代码块内的 XML 标签强制免疫，不作为工具拦截)
- 跨 Chunk 分片拼接与自闭合 XML 解析
"""

import re
import json
import uuid
from typing import Dict, Any, List, Optional, Tuple, Set

# ── 被动指标钩子（T16/B1） ──
#
# 本模块刻意保持**零 app 依赖**的纯标准库状态机（连 engine 内的兄弟模块都不 import）：
# 它是最难排查的一块代码，任何导入都可能带进副作用与循环依赖。
# 所以观测不走 import，而是暴露一个**可注入的回调**：默认 no-op，
# 由 `app.engine.passive_metrics` 在导入时装上真计数器。
# 单测直接用本模块时钩子是空的 —— 这正是想要的隔离。
_metric_hook = None


def set_metric_hook(hook) -> None:
    """装上计数器回调（签名 `hook(name: str, **fields)`）。传 None 可卸下。"""
    global _metric_hook
    _metric_hook = hook


def _note(name: str, **fields) -> None:
    hook = _metric_hook
    if hook is None:
        return
    try:
        hook(name, **fields)
    except Exception:
        pass          # 观测失败绝不能影响解析


class StreamingToolParser:
    """
    流式增量解析器
    将混杂着思考链、普通文本对话与 XML/DSML 工具标签的流式输出，
    边接收边分离为即时文本流 ("text") 与标准 OpenAI tool_calls ("tool_call")。
    """

    def __init__(self, tools: List[Dict[str, Any]]):
        self.tools = tools or []
        self.tools_map = {}
        self.tool_names = set()

        for t in self.tools:
            fn = t.get("function", t)
            name = fn.get("name", "")
            if name:
                self.tool_names.add(name)
                self.tools_map[name] = t

        self.buffer = ""
        self.in_code_block = False
        self.code_block_fence = ""
        self.captured_tool_calls: List[Dict[str, Any]] = []
        self.is_start_of_stream = True
        self.has_emitted_content = False

    DISCLAIMERS = (
        "专家模式暂不支持阅读链接，请使用快速模式",
        "专家模式暂不支持阅读链接",
        "本回答由 AI 生成，内容仅供参考，请仔细甄别",
        "本回答由AI生成，内容仅供参考，请仔细甄别",
    )

    @classmethod
    def _clean_disclaimers(cls, text: str) -> str:
        if not text:
            return text
        for d in cls.DISCLAIMERS:
            text = text.replace(d, "")
        return text

    def feed(self, chunk: str) -> List[Tuple[str, Any]]:
        """
        接收一个新的流式文本片段，返回即时事件列表:
        [("text", "文本片段"), ("tool_call", {tool_call_dict}), ...]
        """
        if not chunk:
            return []

        self.buffer += chunk

        # 0. 流起始处的网页免责声明拦截与吸收
        if self.is_start_of_stream:
            if not self.has_emitted_content:
                self.buffer = self.buffer.lstrip("\r\n")
            matched_disclaimer = False
            possible_prefix = False
            for d in self.DISCLAIMERS:
                if self.buffer.startswith(d):
                    self.buffer = self.buffer[len(d):].lstrip("\r\n")
                    self.is_start_of_stream = False
                    matched_disclaimer = True
                    break
                elif d.startswith(self.buffer) and len(self.buffer) < len(d):
                    possible_prefix = True
                    break
            if not matched_disclaimer:
                if possible_prefix:
                    # 正在接收免责声明的前缀字符，暂停发射等待更多分片
                    return []
                else:
                    # 当前 buffer 已不可能匹配任何免责声明前缀，立即永久退出起始检测
                    self.is_start_of_stream = False

        events = []

        while self.buffer:
            # 1. 如果在 Markdown 代码块内部
            if self.in_code_block:
                # 寻找代码块闭合标记 (例如 ``` 或 ~~~)
                search_start = len(self.code_block_fence) if self.tools else 0
                close_idx = self.buffer.find(self.code_block_fence, search_start)
                if close_idx != -1:
                    end_pos = close_idx + len(self.code_block_fence)
                    code_text = self.buffer[:end_pos]

                    # 检查是否为显式 JSON 工具调用块 (例如 ```json {"tool": "write", "arguments": ...} ```)
                    json_tool_call = self._try_parse_json_code_block(code_text)
                    if json_tool_call:
                        events.append(("tool_call", json_tool_call))
                        self.captured_tool_calls.append(json_tool_call)
                    else:
                        # 核心防误杀：普通 Markdown 代码块（```bash 等）严禁强转为工具调用，保留为原样正文
                        events.append(("text", code_text))

                    self.buffer = self.buffer[end_pos:]
                    self.in_code_block = False
                    self.code_block_fence = ""
                    continue
                else:
                    # 未找到闭合标记，但是为了防止最后一个字符可能刚好是 ``` 的一部分，保留最后 3 个字符
                    if not self.tools:
                        if len(self.buffer) > 4:
                            safe_emit = self.buffer[:-4]
                            events.append(("text", safe_emit))
                            self.buffer = self.buffer[-4:]
                    break

            # 2. 不在代码块内部：寻找下一个特殊起始点 (``` 或者是 < 或者是 [call:)
            idx_fence = self.buffer.find("```")
            idx_tilde = self.buffer.find("~~~")
            idx_code = -1
            fence_str = ""
            if idx_fence != -1 and (idx_tilde == -1 or idx_fence < idx_tilde):
                idx_code = idx_fence
                fence_str = "```"
            elif idx_tilde != -1:
                idx_code = idx_tilde
                fence_str = "~~~"

            idx_tag = self.buffer.find("<")
            idx_dsml_bare = -1
            for d_pat in ("\uff5c", "||DSML"):
                p = self.buffer.find(d_pat)
                if p != -1 and (idx_dsml_bare == -1 or p < idx_dsml_bare):
                    idx_dsml_bare = p

            idx_bracket = -1
            for b_pat in ("[call:", "[工具调用", "【工具调用", "[tool_call", "[tool-call", "【tool_call", "[tool:"):
                p = self.buffer.find(b_pat)
                if p != -1 and (idx_bracket == -1 or p < idx_bracket):
                    idx_bracket = p

            idx_brace = self.buffer.find("{")

            # 寻找最早出现的起始点
            candidates = []
            if idx_code != -1:
                candidates.append((idx_code, "code"))
            if idx_tag != -1:
                candidates.append((idx_tag, "tag"))
            if idx_dsml_bare != -1:
                candidates.append((idx_dsml_bare, "dsml_bare"))
            if idx_bracket != -1:
                candidates.append((idx_bracket, "bracket"))
            if idx_brace != -1:
                candidates.append((idx_brace, "json"))

            if not candidates:
                if self.captured_tool_calls and not self.buffer.strip():
                    self.buffer = ""
                    break
                # 整段都是普通文本，立即发射！
                events.append(("text", self.buffer))
                self.buffer = ""
                break

            candidates.sort(key=lambda x: x[0])
            first_idx, first_type = candidates[0]

            if first_type == "code":
                if first_idx > 0:
                    events.append(("text", self.buffer[:first_idx]))
                    self.buffer = self.buffer[first_idx:]
                self.in_code_block = True
                self.code_block_fence = fence_str
                if not self.tools:
                    events.append(("text", fence_str))
                    self.buffer = self.buffer[len(fence_str):]
                continue

            elif first_type == "bracket":
                if first_idx > 0:
                    events.append(("text", self.buffer[:first_idx]))
                    self.buffer = self.buffer[first_idx:]
                probe_result = self._probe_and_consume_bracket_tool()
                if probe_result is None:
                    break
                elif probe_result is False:
                    events.append(("text", "["))
                    self.buffer = self.buffer[1:]
                elif isinstance(probe_result, tuple) and probe_result[0] == "fallback_text":
                    if probe_result[1]:
                        events.append(("text", probe_result[1]))
                    continue
                else:
                    tool_call = probe_result
                    events.append(("tool_call", tool_call))
                    self.captured_tool_calls.append(tool_call)
                continue

            elif first_type == "json":
                leading = self.buffer[:first_idx] if first_idx > 0 else ""
                self.buffer = self.buffer[first_idx:]
                probe_result = self._probe_and_consume_raw_json_tool()
                if probe_result is None:
                    self.buffer = leading + self.buffer
                    break
                elif probe_result is False:
                    if leading:
                        events.append(("text", leading))
                    events.append(("text", "{"))
                    self.buffer = self.buffer[1:]
                else:
                    if leading.strip():
                        events.append(("text", leading))
                    tool_call = probe_result
                    events.append(("tool_call", tool_call))
                    self.captured_tool_calls.append(tool_call)
                continue

            elif first_type == "dsml_bare":
                leading = self.buffer[:first_idx] if first_idx > 0 else ""
                self.buffer = self.buffer[first_idx:]
                probe_result = self._probe_and_consume_tool()
                if probe_result is None:
                    self.buffer = leading + self.buffer
                    break
                elif probe_result is False:
                    if leading:
                        events.append(("text", leading))
                    events.append(("text", self.buffer[0]))
                    self.buffer = self.buffer[1:]
                elif isinstance(probe_result, tuple) and probe_result[0] == "fallback_text":
                    if leading:
                        events.append(("text", leading))
                    if probe_result[1]:
                        events.append(("text", probe_result[1]))
                    continue
                else:
                    if leading.strip():
                        events.append(("text", leading))
                    tool_call = probe_result
                    events.append(("tool_call", tool_call))
                    self.captured_tool_calls.append(tool_call)
                continue

            else:  # first_type == "tag"
                leading = self.buffer[:first_idx] if first_idx > 0 else ""
                self.buffer = self.buffer[first_idx:]
                probe_result = self._probe_and_consume_tool()
                if probe_result is None:
                    self.buffer = leading + self.buffer
                    break
                elif probe_result is False:
                    if leading:
                        events.append(("text", leading))
                    events.append(("text", "<"))
                    self.buffer = self.buffer[1:]
                elif isinstance(probe_result, tuple) and probe_result[0] == "fallback_text":
                    if leading:
                        events.append(("text", leading))
                    if probe_result[1]:
                        events.append(("text", probe_result[1]))
                    continue
                else:
                    if leading.strip():
                        events.append(("text", leading))
                    tool_call = probe_result
                    events.append(("tool_call", tool_call))
                    self.captured_tool_calls.append(tool_call)
                continue

        cleaned_events = []
        for ev_type, val in events:
            if ev_type == "text":
                c = self._clean_disclaimers(val)
                if c:
                    cleaned_events.append((ev_type, c))
            else:
                cleaned_events.append((ev_type, val))
        if cleaned_events:
            self.has_emitted_content = True
            self.is_start_of_stream = False
        return cleaned_events

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
        # 1. 修复反斜杠 (如 Windows 路径 E:\OH-WorkSpace 中的 \O)
        fixed = re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', fixed)
        # 2. 修复尾随逗号
        fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
        try:
            return json.loads(fixed, strict=False)
        except Exception:
            pass
        try:
            import ast
            val = ast.literal_eval(fixed)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            pass
        return None

    def _match_tool_name(self, name: Optional[str]) -> str:
        """智能匹配工具名称（含常见别名与前后缀容错）"""
        from app.engine.tool_bridge import ToolBridge
        return ToolBridge.match_tool_name(name, self.tool_names)

    def _extract_first_json_tool(self, text: str, require_tool_match: bool = True, allow_repair: bool = True) -> Tuple[str, Optional[Dict[str, Any]], int, int]:
        """
        从 text 中切出第一个完整的工具 JSON 对象 (基于平衡花括号状态机)。
        返回: (status, parsed_dict, start_idx, end_idx)
        status:
          - "COMPLETE": 成功切出并解析出包含注册工具名/别名的完整工具 JSON
          - "INCOMPLETE": 正在流式接收工具 JSON，但尚未闭合
          - "NONE": 未检测到工具 JSON
        """
        start = 0
        while True:
            pos = text.find("{", start)
            if pos == -1:
                return "NONE", None, -1, -1

            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(pos, len(text)):
                ch = text[i]
                if esc:
                    esc = False
                    continue
                if ch == "\\":
                    if in_str:
                        esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if not in_str:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break

            if end != -1:
                candidate = text[pos:end]
                data = self._repair_and_loads(candidate)
                if isinstance(data, dict):
                    t_name = data.get("name") or data.get("tool") or data.get("function") or data.get("action")
                    if not t_name:
                        # 检查 JSON 紧邻的前方是否有 XML 标签属性、原生 Token 或方括号前缀
                        prefix_text = text[:pos].strip()
                        m_native = re.search(
                            r'<+[\|｜]*tool[\s\u2581_\-]+call[\s\u2581_\-]+begin[\|｜]*>+(?:function[\s\u2581_\-]*)?(?:<+[\|｜]*tool[\s\u2581_\-]+sep[\|｜]*>+)?([a-zA-Z0-9_\-\.:]+)(?:<+[\|｜]*tool[\s\u2581_\-]+sep[\|｜]*>+)?\s*$',
                            prefix_text,
                            re.IGNORECASE
                        )
                        if m_native:
                            t_name = m_native.group(1).strip()
                        else:
                            m_tag = re.search(
                                r'<[\|｜\s]*(?:tool_call|tool-call|tool_calls|tool|call|invoke|action)(?:[\s:]+(?:name=[\"\']|tool=[\"\']|function=[\"\'])?([a-zA-Z0-9_\-\.:]+)[\"\']?)?[^>]*>$',
                                prefix_text,
                                re.IGNORECASE
                            )
                            if m_tag and m_tag.group(1):
                                t_name = m_tag.group(1).strip()
                            else:
                                m_prefix = re.search(
                                    r'[\[【][\|｜\s]*(?:工具调用|tool_call|call)[\|｜\s]*[\]】][:：]?\s*([a-zA-Z0-9_\-\.:]+)',
                                    prefix_text,
                                    re.IGNORECASE
                                )
                                if m_prefix:
                                    t_name = m_prefix.group(1).strip()
                    if t_name:
                        matched = self._match_tool_name(t_name)
                        # ★ 宽松匹配只在"名字确实注册过"或"只有一个工具所以无歧义"时放行。
                        # 旧写法 `matched in self.tool_names or (not require_tool_match and bool(self.tools))`
                        # 会把任何未注册的名字当工具发射出去。
                        resolved = self._resolve_emittable_name(matched, require_tool_match)
                        if resolved:
                            data["name"] = resolved
                            # 若没有显式 arguments/parameters 键，整个字典剩余字段即为参数
                            if "arguments" not in data and "parameters" not in data and "args" not in data:
                                raw_args = {k: v for k, v in data.items() if k not in ("name", "tool", "function", "action")}
                                if raw_args:
                                    data["arguments"] = raw_args
                            return "COMPLETE", data, pos, end
                start = pos + 1
            else:
                snippet = text[pos:]
                # 针对大模型末尾漏写外层 '}' 的容错自愈
                # ★ allow_repair=False（流式中途）时绝不"补齐花括号"：半截 `{"name": "bash"` 补 `}` 后
                #   恰好是合法 JSON，会被当成**空参数**的完整工具调用提前消费，后面的参数全丢。
                #   补括号自愈只在 flush（流已结束）时才安全。
                if depth > 0 and allow_repair:
                    tag_idx = snippet.find("<")
                    clean_snippet = snippet[:tag_idx] if tag_idx != -1 else snippet
                    clean_snippet = clean_snippet.strip().rstrip("`").rstrip()
                    d = 0
                    in_s = False
                    e = False
                    for c in clean_snippet:
                        if e:
                            e = False
                            continue
                        if c == '\\':
                            if in_s:
                                e = True
                            continue
                        if c == '"':
                            in_s = not in_s
                            continue
                        if not in_s:
                            if c == '{':
                                d += 1
                            elif c == '}':
                                d -= 1
                    if d > 0:
                        repaired_candidate = clean_snippet + ("}" * d)
                        repaired_data = self._repair_and_loads(repaired_candidate)
                        if isinstance(repaired_data, dict):
                            t_name = repaired_data.get("name") or repaired_data.get("tool") or repaired_data.get("function")
                            if not t_name:
                                prefix_text = text[:pos].strip()
                                m_tag = re.search(
                                    r'<[\|｜\s]*(?:tool_call|tool-call|tool_calls|tool|call|invoke|action)(?:[\s:]+(?:name=[\"\']|tool=[\"\']|function=[\"\'])?([a-zA-Z0-9_\-\.:]+)[\"\']?)?[^>]*>$',
                                    prefix_text,
                                    re.IGNORECASE
                                )
                                if m_tag and m_tag.group(1):
                                    t_name = m_tag.group(1).strip()
                            if t_name:
                                matched = self._match_tool_name(t_name)
                                if matched in self.tool_names or (not require_tool_match and bool(self.tools)):
                                    repaired_data["name"] = matched
                                    if "arguments" not in repaired_data and "parameters" not in repaired_data and "args" not in repaired_data:
                                        raw_args = {k: v for k, v in repaired_data.items() if k not in ("name", "tool", "function", "action")}
                                        if raw_args:
                                            repaired_data["arguments"] = raw_args
                                    consumed_len = len(clean_snippet)
                                    after_clean = snippet[len(clean_snippet):]
                                    close_m = re.match(r'^\s*</[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|call)[\|｜\s]*>', after_clean, re.IGNORECASE)
                                    if close_m:
                                        consumed_len += close_m.end()
                                    return "COMPLETE", repaired_data, pos, pos + consumed_len

                if '"name"' in snippet or '"tool"' in snippet or re.search(r'[\[【<][\|｜\s]*(?:工具调用|tool_call|call|invoke)', text[:pos]):
                    return "INCOMPLETE", None, pos, -1
                # ★ 逐字符/小分片流下，`{`、`{"`、`{"na` 还认不出是不是工具 JSON，
                #   旧逻辑直接判 NONE → 首字符被当正文吐给客户端（"工具调用文本打印在输出流"的成因之一）。
                #   只要它仍是"某个工具键名的前缀"，就继续挂起，等下一个分片再判定。
                _rest = snippet[1:].lstrip()
                if len(snippet) < 32 and (
                    _rest == "" or _rest == '"'
                    or (_rest.startswith('"') and any(k.startswith(_rest[1:]) for k in ("name", "tool", "function", "action", "arguments", "parameters")))
                ):
                    return "INCOMPLETE", None, pos, -1
                start = pos + 1

    def _probe_and_consume_raw_json_tool(self) -> Optional[Any]:
        """探测当前以 '{' 开头的 buffer 是否为一个合法的顶层裸 JSON 工具调用"""
        status, data, j_start, j_end = self._extract_first_json_tool(self.buffer, allow_repair=False)
        if status == "COMPLETE":
            self.buffer = self.buffer[j_end:]
            t_name = data.get("name") or data.get("tool") or data.get("function")
            matched_name = self._match_tool_name(t_name)
            args = data.get("arguments") or data.get("parameters") or data.get("args") or data.get("params") or {}
            if isinstance(args, str):
                try:
                    args = self._repair_and_loads(args) or args
                except Exception:
                    pass
            from app.engine.tool_bridge import ToolBridge
            norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)
            return {
                "id": f"call_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": matched_name,
                    "arguments": json.dumps(norm_args, ensure_ascii=False)
                }
            }
        elif status == "INCOMPLETE":
            return None
        return False

    def _probe_and_consume_bracket_tool(self) -> Optional[Any]:
        """探测当前以 '[' 或 '【' 开头的 buffer 是否为一个完整的工具调用"""
        buf = self.buffer

        # 1. 兼容旧式 [call:tool_name]...[/call]
        open_m = re.match(r'^\[call:([a-zA-Z0-9_\-\.:]+)\]', buf, re.IGNORECASE)
        if open_m:
            t_name = open_m.group(1).strip()
            close_m = re.search(r'\[/call\]', buf, re.IGNORECASE)
            if not close_m:
                return None
            end_pos = close_m.end()
            body = buf[open_m.end():close_m.start()].strip()
            self.buffer = self.buffer[end_pos:]
            try:
                args = self._repair_and_loads(body) if body else {}
                if not isinstance(args, dict):
                    args = {"input": body}
                matched_name = self._match_tool_name(t_name)
                from app.engine.tool_bridge import ToolBridge
                norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)
                return {
                    "id": f"call_{uuid.uuid4().hex[:10]}",
                    "type": "function",
                    "function": {
                        "name": matched_name,
                        "arguments": json.dumps(norm_args, ensure_ascii=False)
                    }
                }
            except Exception as e:
                print(f"[StreamingParser] [call:] JSON解析失败: {e}, body={body[:100]}")
                _note("tool.json_parse_failed", body=str(body)[:120])
                return ("fallback_text", body)

        # 2. 匹配 [工具调用]: <tool_name> 或 【工具调用】：<tool_name> 及其后紧随的 JSON
        zh_m = re.match(r'^[\[【][\|｜\s]*(?:工具调用|tool_call|call|tool)[\|｜\s]*[\]】][:：]?\s*([a-zA-Z0-9_\-\.:]+)?', buf, re.IGNORECASE)
        if zh_m:
            prefix_len = zh_m.end()
            t_name_from_head = zh_m.group(1).strip() if zh_m.group(1) else ""
            remainder = buf[prefix_len:]

            # 寻找接下来的 JSON 起始 '{'
            brace_pos = remainder.find("{")
            if brace_pos == -1:
                # 尚未收到 '{'，若剩余长度较短且未出现连续换行，等待后续分片
                if len(remainder) < 40 and "\n\n" not in remainder:
                    return None
                # 确实没有 JSON，作为普通文本吐出
                self.buffer = self.buffer[prefix_len:]
                return ("fallback_text", zh_m.group(0))

            # 从 '{' 处切出完整的 JSON
            json_text = remainder[brace_pos:]
            depth = 0
            in_str = False
            esc = False
            json_end = -1
            for i in range(len(json_text)):
                ch = json_text[i]
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    if in_str:
                        esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if not in_str:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            json_end = i + 1
                            break

            if json_end == -1:
                # JSON 还在接收中，等待后续分片
                return None

            json_str = json_text[:json_end]
            args = self._repair_and_loads(json_str) or {}
            if not isinstance(args, dict):
                args = {"input": json_str}

            t_name = t_name_from_head or args.get("name") or args.get("tool") or args.get("function")
            matched_name = self._match_tool_name(t_name)

            # 检查是否有紧随的闭合标签 (如 [/工具调用] 或 </tool_call>)
            total_consumed = prefix_len + brace_pos + json_end
            after_json = buf[total_consumed:]
            close_zh = re.match(r'^\s*([\[<]/[\|｜\s]*(?:工具调用|tool_call|call|tool)[\|｜\s]*[\]>])', after_json, re.IGNORECASE)
            if close_zh:
                total_consumed += close_zh.end()

            self.buffer = self.buffer[total_consumed:]

            if matched_name in self.tool_names:
                from app.engine.tool_bridge import ToolBridge
                norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)
                return {
                    "id": f"call_{uuid.uuid4().hex[:10]}",
                    "type": "function",
                    "function": {
                        "name": matched_name,
                        "arguments": json.dumps(norm_args, ensure_ascii=False)
                    }
                }
            else:
                return ("fallback_text", buf[:total_consumed])

        if len(buf) < 10:
            return None
        return False

    def _probe_and_consume_tool(self) -> Optional[Any]:
        """
        探测当前以 '<' 开头的 buffer 是否为一个完整的工具调用。
        返回:
        - None: 处于工具接收中（还需更多分片数据，暂停发射）
        - False: 明确不是注册工具，可作为普通文本消费
        - Dict: 成功解析出一个 OpenAI tool_call
        """
        buf = self.buffer
        # -1.5 匹配 DeepSeek 官方特殊 Token 风格 (<｜tool call begin｜> / <｜｜tool▁call▁begin｜｜>)
        SEP = r'[\s\u2581_\-]'
        PIPE = r'[\|｜]'
        native_begin_pattern = re.compile(rf'^<+{PIPE}*tool{SEP}+call{SEP}+begin{PIPE}*>', re.IGNORECASE)
        m_begin = native_begin_pattern.match(buf)
        if m_begin:
            native_end_pattern = re.compile(rf'<+/?{PIPE}*tool{SEP}+call{SEP}+end{PIPE}*>', re.IGNORECASE)
            m_end = native_end_pattern.search(buf)
            if not m_end:
                return None  # 还在流式接收工具调用中，等待闭合标签

            native_call_pattern = re.compile(
                rf'^<+{PIPE}*tool{SEP}+call{SEP}+begin{PIPE}*>+'
                rf'(?:function{SEP}*)?'
                rf'(?:<+{PIPE}*tool{SEP}+sep{PIPE}*>+)?'
                rf'([a-zA-Z0-9_\-\.:]+)'
                rf'(?:<+{PIPE}*tool{SEP}+sep{PIPE}*>+)?'
                rf'\s*(?:```json)?(.*?)(?:```)?'
                rf'<+/?{PIPE}*tool{SEP}+call{SEP}+end{PIPE}*>+',
                re.DOTALL | re.IGNORECASE
            )
            full_match = native_call_pattern.match(buf)
            if full_match:
                t_name = full_match.group(1).strip()
                val = full_match.group(2).strip()
                matched_name = self._match_tool_name(t_name)
                resolved_name = self._resolve_emittable_name(matched_name, require_tool_match=False)
                if not resolved_name:
                    self.buffer = self.buffer[full_match.end():]
                    clean_val = re.sub(rf'<+/?{PIPE}*tool{SEP}+(?:call|calls|sep)?{SEP}*(?:begin|end)?{PIPE}*>+', "", val, flags=re.IGNORECASE)
                    return ("fallback_text", clean_val)
                args = self._repair_and_loads(val) or {}
                if not isinstance(args, dict):
                    args = {"input": val} if val else {}
                from app.engine.tool_bridge import ToolBridge
                norm_args = ToolBridge._normalize_tool_args(resolved_name, args, self.tools)
                self.buffer = self.buffer[full_match.end():]
                return {
                    "id": f"call_{uuid.uuid4().hex[:10]}",
                    "type": "function",
                    "function": {
                        "name": resolved_name,
                        "arguments": json.dumps(norm_args, ensure_ascii=False)
                    }
                }
            else:
                self.buffer = self.buffer[m_end.end():]
                return ("fallback_text", "")

        # -1. 消费 DSML / tool_calls / calls 顶层容器开闭标签，以及 invoke / tool_call / call 的**孤立闭合**标签
        #
        # ★ 回归修复（2026-10-23）：dd13e44 曾把 invoke|tool_call|call 也并进"开闭皆可"的分支，
        #   结果 `<tool_call>` 这个**开标签**被当成无意义容器静默吞掉，随后 `{"name":...}` JSON 体
        #   逐字符漏进正文（日志特征：Extracted-Tools count=0 → 靠 Safety-Net 兜底发射）。
        #   开标签 `<tool_call>` / `<invoke>` 必须留给下面的 tc_open / invoke_match 分支解析，
        #   这里只允许吞它们的**闭合**形态（`</tool_call>`、`</invoke>`、`｜DSML｜ invoke>`）。
        dsml_container = re.match(
            r'^(?:'
            r'(?:</?[\|｜\s]*|[\|｜\s]+)(?:DSML[\|｜\s]*)?(?:tool_calls|calls)[\|｜\s]*>'
            r'|'
            r'(?:</[\|｜\s]*|[\|｜\s]+)(?:DSML[\|｜\s]*)?(?:invoke|tool_call|call|parameter)[\|｜\s]*>'
            r'|'
            r'</+[\|｜\s]*(?:DSML[\|｜\s]*)?>'
            r'|'
            r'</(?:invoke|parameter|calls|tool_call|tool_calls)[^>]*>'
            r')',
            buf,
            re.IGNORECASE
        )
        if dsml_container:
            # 可观测：被静默消费的容器标签必须留痕，否则"标签吞噬"类回归会再次无声发生
            _note("tool.container_tag_consumed", tag=buf[:dsml_container.end()][:40])
            self.buffer = self.buffer[dsml_container.end():]
            return ("fallback_text", "")

        if buf.startswith(("\uff5c", "||")):
            if re.match(r'^[\|｜\s]*(?:D(?:S(?:M(?:L)?)?)?)?$', buf, re.IGNORECASE) or \
               (re.match(r'^[\|｜\s]*DSML', buf, re.IGNORECASE) and '>' not in buf):
                return None  # DSML 标签还在流式接收中，等待 '>' 闭合
            if not re.match(r'^[\|｜\s]*DSML', buf, re.IGNORECASE):
                return False  # 不是 DSML 标签，作为普通文本消费

        # 0. 检查是否正在接收 <tool_call> / <DSML 开标签的前缀（例如 <t, <tool, <tool_call, <inv, <invoke）
        tag_head_m = re.match(r'^<[\|｜\s]*([a-zA-Z0-9_\-]+)', buf)
        if tag_head_m:
            head_name = tag_head_m.group(1).lower()
            for cand in ("tool_call", "tool-call", "tool_calls", "calls", "call", "dsml", "invoke", "function_call"):
                if cand.startswith(head_name):
                    if '>' not in buf:
                        return None  # 开标签还没接收完整，等待 '>' 闭合

        # 0.1 优先检查 <tool_call>... 或 <tool_call name="..."> 或任何以 '<' 开头的工具 JSON 块（穿透重叠/口吃标签）
        tc_open = re.match(
            r'^<[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|tool|call)(?:[\s:]+(?:name=[\"\']|tool=[\"\']|function=[\"\'])?([a-zA-Z0-9_\-\.:]+)[\"\']?)?[^>]*>',
            buf,
            re.IGNORECASE
        )
        if tc_open or buf.startswith("<tool") or buf.startswith("<call"):
            status, data, j_start, j_end = self._extract_first_json_tool(buf, require_tool_match=False)
            if status == "COMPLETE":
                close_pattern = re.compile(
                    r'</[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|tool|call)(?::[a-zA-Z0-9_\-\.:]+)?[\|｜\s]*>',
                    re.IGNORECASE
                )
                close_m = close_pattern.search(buf[j_end:])
                if tc_open and not close_m and len(buf[j_end:]) < 50:
                    # 等待闭合标签到达，避免闭合标签滞后被误当作普通文本
                    return None

                total_end = j_end + (close_m.end() if close_m else 0)
                self.buffer = self.buffer[total_end:]

                tag_tool_name = tc_open.group(1) if tc_open and tc_open.group(1) else None
                t_name = data.get("name") or data.get("tool") or data.get("function") or tag_tool_name
                matched_name = self._match_tool_name(t_name)
                args = data.get("arguments") or data.get("parameters") or data.get("args") or {k: v for k, v in data.items() if k not in ("name", "tool", "function", "action")}
                if isinstance(args, str):
                    try:
                        args = self._repair_and_loads(args) or args
                    except Exception:
                        pass
                from app.engine.tool_bridge import ToolBridge
                norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)
                return {
                    "id": f"call_{uuid.uuid4().hex[:10]}",
                    "type": "function",
                    "function": {
                        "name": matched_name,
                        "arguments": json.dumps(norm_args, ensure_ascii=False)
                    }
                }
            elif status == "INCOMPLETE":
                return None  # 处于 JSON 参数流式接收中，暂停发射等待后续分片
            elif tc_open:
                close_pattern = re.compile(
                    r'</[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|tool|call)(?::[a-zA-Z0-9_\-\.:]+)?[\|｜\s]*>',
                    re.IGNORECASE
                )
                close_m = close_pattern.search(buf)
                if not close_m:
                    return None  # 闭合标签还没到，继续等待
                inner_body = buf[tc_open.end():close_m.start()].strip()
                repaired = self._repair_and_loads(inner_body)
                if not isinstance(repaired, dict):
                    d = inner_body.count("{") - inner_body.count("}")
                    if d > 0:
                        repaired = self._repair_and_loads(inner_body + ("}" * d))
                if isinstance(repaired, dict):
                    tag_tool_name = tc_open.group(1) if tc_open and tc_open.group(1) else None
                    t_name = repaired.get("name") or repaired.get("tool") or repaired.get("function") or repaired.get("action") or tag_tool_name
                    matched_name = self._match_tool_name(t_name)
                    if matched_name in self.tool_names or bool(self.tools):
                        args = repaired.get("arguments") or repaired.get("parameters") or repaired.get("args") or repaired.get("params") or {k: v for k, v in repaired.items() if k not in ("name", "tool", "function", "action")}
                        if isinstance(args, str):
                            try:
                                args = self._repair_and_loads(args) or args
                            except Exception:
                                pass
                        from app.engine.tool_bridge import ToolBridge
                        norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)
                        self.buffer = self.buffer[close_m.end():]
                        return {
                            "id": f"call_{uuid.uuid4().hex[:10]}",
                            "type": "function",
                            "function": {
                                "name": matched_name,
                                "arguments": json.dumps(norm_args, ensure_ascii=False)
                            }
                        }
                # 确实非工具调用：整体消费该闭合块，安全吐出纯文本，剥离 <tool_call> 标签杜绝泄露与错切首字符
                self.buffer = self.buffer[close_m.end():]
                return ("fallback_text", inner_body) if inner_body else None

        # 1. 检查 DSML / bare invoke / tool_call name= 格式:
        PIPE = r'[\|｜]'
        invoke_match = re.match(
            rf'^<+{PIPE}*(?:DSML{PIPE}*)?\s*(?:invoke|tool_call|tool-call|tool|call)\s+name=[\"\']([a-zA-Z0-9_\-\.:]+)[\"\'](?:\s+[^>]*)?>',
            buf,
            re.IGNORECASE
        )
        if invoke_match:
            t_name = invoke_match.group(1).strip()
            close_pattern = re.compile(
                rf'(?:'
                rf'</+{PIPE}*(?:DSML{PIPE}*)?\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf'|'
                rf'<+{PIPE}*(?:DSML{PIPE}*)?\s*/\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf'|'
                rf'<+{PIPE}*(?:DSML{PIPE}*)\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf'|'
                rf'</+{PIPE}*DSML{PIPE}*>(?!\s*<[\|｜\s/]*(?:DSML[\|｜\s/]*)?parameter)'
                rf'|'
                rf'{PIPE}+DSML{PIPE}*\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf')',
                re.IGNORECASE
            )
            close_m = close_pattern.search(buf)
            if not close_m:
                return None  # 等待闭合
            body = buf[invoke_match.end():close_m.start()]

            # 统一走 ToolBridge.parse_invoke_body，兼容三种 body 形态：
            #   1) <|parameter name=...> 值 </|parameter>
            #   2) <path>值</path>（XML 子标签）
            #   3) {"path": "值"}（裸 JSON）
            matched_name = self._match_tool_name(t_name)
            resolved_name = self._resolve_emittable_name(matched_name, require_tool_match=False)
            if not resolved_name:
                self.buffer = self.buffer[close_m.end():]
                return ("fallback_text", body if body.strip() else buf[:close_m.end()])

            from app.engine.tool_bridge import ToolBridge
            parsed_args = ToolBridge.parse_invoke_body(body, resolved_name, self.tools)

            # 完整性校验：如果闭合匹配仅仅是无名 headless 闭标签（如 </|DSML|>），且工具定义存在未满足的必填参数，
            # 说明该标签极可能是当前参数自身的闭标签，后续参数尚未接收完成，继续等待流式数据到达
            close_tag_text = close_m.group(0).lower()
            is_explicit_invoke_end = any(k in close_tag_text for k in ("invoke", "tool_call", "call", "calls"))
            if not is_explicit_invoke_end and self.tools:
                target_fn = next((t.get("function", t) for t in self.tools if (t.get("function", t).get("name") == resolved_name)), None)
                if target_fn:
                    reqs = set((target_fn.get("parameters", {}) or {}).get("required", []) or [])
                    missing_reqs = reqs - set(parsed_args.keys())
                    if missing_reqs and not re.search(r'</+[\|｜\s/]*(?:DSML[\|｜\s/]*)?(?:calls|tool_calls)\s*>', buf, re.IGNORECASE):
                        return None  # 继续等待后续必填参数到达

            if not parsed_args and body.strip():
                end_pos = close_m.end()
                self.buffer = self.buffer[end_pos:]
                return self._build_tool_call(t_name, body=body, is_dsml=True)
            norm_args = ToolBridge._normalize_tool_args(resolved_name, parsed_args, self.tools)
            end_pos = close_m.end()
            post = buf[end_pos:]
            extra_close = re.match(
                r'^\s*(?:</?[\|｜\s]*(?:DSML[\|｜\s]*)?(?:invoke|calls|tool_calls|tool_call|call)[\|｜\s]*>|</+[\|｜\s]*DSML[\|｜\s]*>)',
                post,
                re.IGNORECASE
            )
            if extra_close:
                end_pos += extra_close.end()
            self.buffer = self.buffer[end_pos:]
            return {
                "id": f"call_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": resolved_name,
                    "arguments": json.dumps(norm_args, ensure_ascii=False),
                },
            }

        # 1.1 检查缺失 <invoke name="..."> 标签的裸 DSML parameter 块（模型漏掉 invoke 开标签直接吐参数，由 </invoke> 或 </calls> 闭合）
        bare_param_m = re.match(
            r'^\s*<[\|｜\s/]*(?:DSML[\|｜\s/]*)?parameter\s+name=[\"\']?([a-zA-Z0-9_\-]+)',
            buf,
            re.IGNORECASE
        )
        if bare_param_m:
            close_pattern = re.compile(
                rf'(?:'
                rf'</+{PIPE}*(?:DSML{PIPE}*)?\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf'|'
                rf'<+{PIPE}*(?:DSML{PIPE}*)?\s*/\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf'|'
                rf'<+{PIPE}*(?:DSML{PIPE}*)\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf'|'
                rf'</+{PIPE}*DSML{PIPE}*>(?!\s*<[\|｜\s/]*(?:DSML[\|｜\s/]*)?parameter)'
                rf'|'
                rf'{PIPE}+DSML{PIPE}*\s*(?:invoke|calls?|tool_calls?|tool-call|tool|call)\s*>+'
                rf')',
                re.IGNORECASE
            )
            close_m = close_pattern.search(buf)
            if not close_m:
                return None  # 等待闭合标签到达，暂停流式吐出避免污染聊天正文

            body = buf[bare_param_m.start():close_m.start()]
            from app.engine.tool_bridge import ToolBridge
            parsed_args = ToolBridge.parse_invoke_body(body, "", self.tools)
            inferred_name = ToolBridge.infer_tool_name_from_args(parsed_args, self.tools)
            resolved_name = self._resolve_emittable_name(inferred_name, require_tool_match=False) if inferred_name else None
            if not resolved_name:
                self.buffer = self.buffer[close_m.end():]
                return ("fallback_text", body if body.strip() else buf[:close_m.end()])

            norm_args = ToolBridge._normalize_tool_args(resolved_name, parsed_args, self.tools)
            end_pos = close_m.end()
            post = buf[end_pos:]
            extra_close = re.match(
                r'^\s*(?:</?[\|｜\s]*(?:DSML[\|｜\s]*)?(?:invoke|calls|tool_calls|tool_call|call)[\|｜\s]*>|</+[\|｜\s]*DSML[\|｜\s]*>)',
                post,
                re.IGNORECASE
            )
            if extra_close:
                end_pos += extra_close.end()
            self.buffer = self.buffer[end_pos:]
            return {
                "id": f"call_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": resolved_name,
                    "arguments": json.dumps(norm_args, ensure_ascii=False),
                },
            }

        # 2. 检查普通 XML 标签名: <tag_name ...
        tag_match = re.match(r'^<([a-zA-Z0-9_\-\.:]+)', buf)
        if not tag_match:
            # 可能是 "<" 后面还没接字符，或者 "< " 后面带空格
            if len(buf) == 1:
                return None  # 仅有一个 '<'，等待下一个字符
            if not buf[1].isalpha() and buf[1] not in ('/', '|', '｜'):
                return False  # 例如 "< 3" 或 "<="，绝不是工具
            return None

        t_name = tag_match.group(1).strip()

        # 特例排除：如果是思考标签 <reflect> 或 <thought>，不作为工具拦截，交由客户端折叠展示
        if t_name.lower() in ("reflect", "thought", "thinking"):
            return False

        # 检查 t_name 是否是某个已注册工具 (或者是其别名或前缀)
        exact_match = None
        has_prefix_match = False

        matched_alias = self._match_tool_name(t_name)
        if matched_alias in self.tool_names:
            exact_match = matched_alias
        elif t_name in self.tool_names:
            exact_match = t_name
        else:
            # 模糊末尾匹配 (如 office_html-to-pdf 对应 html-to-pdf)
            for tn in self.tool_names:
                if tn.endswith(t_name) or t_name.endswith(tn):
                    exact_match = tn
                    break
                elif tn.startswith(t_name):
                    has_prefix_match = True

        if not exact_match:
            if has_prefix_match:
                # 可能是标签名还没推完 (例如刚到了 <off，还没到 <office_html-to-pdf)
                # 检查 buffer 里是否有 '>' 或空格，如果有了说明标签名已经结束却未匹配
                if '>' in buf or ' ' in buf or '\n' in buf:
                    return False
                return None
            return False

        # 确定匹配到工具名称 exact_match！
        # 3. 检查是否是自闭合标签: <t_name ... />
        # 寻找本标签不在属性引号内部的真实闭合 '>'
        first_gt = self._find_tag_end(buf, start=len(tag_match.group(0)))
        if first_gt == -1:
            return None  # 标签头都还没闭合，继续等待

        header = buf[:first_gt + 1]
        if header.rstrip('>').rstrip().endswith('/'):
            # 自闭合标签！例如 <office_html-to-pdf htmlPath="..." />
            raw_attrs = header[len(tag_match.group(0)):-2].strip()
            self.buffer = self.buffer[first_gt + 1:]
            return self._build_tool_call(exact_match, raw_attrs=raw_attrs)

        # 4. 成对闭合标签: <t_name ...>...</t_name>
        close_tag = f"</{t_name}>"
        close_idx = buf.find(close_tag)
        if close_idx == -1:
            # 尝试大小写不敏感匹配
            close_pattern = re.compile(rf'</{re.escape(t_name)}>', re.IGNORECASE)
            cm = close_pattern.search(buf)
            if not cm:
                return None  # 标签体还未接收完毕，挂起缓冲
            close_idx = cm.start()
            end_pos = cm.end()
        else:
            end_pos = close_idx + len(close_tag)

        raw_attrs = buf[len(tag_match.group(0)):first_gt].strip()
        body = buf[first_gt + 1:close_idx]
        self.buffer = self.buffer[end_pos:]
        return self._build_tool_call(exact_match, raw_attrs=raw_attrs, body=body)

    @staticmethod
    def _find_tag_end(buf: str, start: int = 0) -> int:
        """寻找不在引号内部的真实标签闭合 '>' 字符位置"""
        in_quote = None
        escape = False
        for i in range(start, len(buf)):
            c = buf[i]
            if escape:
                escape = False
                continue
            if c == '\\':
                escape = True
                continue
            if in_quote:
                if c == in_quote:
                    in_quote = None
            else:
                if c in ('"', "'"):
                    in_quote = c
                elif c == '>':
                    return i
        return -1

    def _build_tool_call(self, tool_name: str, raw_attrs: str = "", body: str = "", is_dsml: bool = False) -> Dict[str, Any]:
        """构建标准化 OpenAI tool_call 结构"""
        from app.engine.tool_bridge import ToolBridge

        args = {}
        if is_dsml:
            args = ToolBridge.parse_invoke_body(body, tool_name, self.tools)
            if not args and body.strip():
                try:
                    args = json.loads(body.strip())
                except Exception:
                    args = {"input": body.strip()}
        else:
            # 解析 XML 属性
            if raw_attrs:
                args.update(ToolBridge._parse_xml_attributes(raw_attrs))

            # 解析 XML 标签体
            body = body.strip()
            if body:
                schema_props = set()
                t = self.tools_map.get(tool_name)
                if t and "function" in t and "parameters" in t["function"] and "properties" in t["function"]["parameters"]:
                    schema_props = set(t["function"]["parameters"]["properties"].keys())

                try:
                    # 尝试解析 JSON 标签体
                    parsed_json = json.loads(body)
                    if isinstance(parsed_json, dict):
                        args.update(parsed_json)
                    else:
                        args["input"] = parsed_json
                except Exception:
                    # 尝试提取 XML 子标签
                    child_tags = re.findall(r'<([a-zA-Z0-9_\-]+)>(.*?)</\1>', body, re.DOTALL)
                    extracted_fields = {}
                    for k, v in child_tags:
                        if k in schema_props and k not in args:
                            extracted_fields[k] = v.strip()

                    # 确定当前工具的主字段
                    primary_field = None
                    if tool_name in ("bash", "sh", "terminal", "exec_command"):
                        primary_field = "cmd"
                    elif tool_name in ("write", "edit", "write_to_file"):
                        primary_field = "content"
                    elif tool_name == "office_html-to-pdf":
                        primary_field = "html"
                    elif tool_name in ("read", "view_file"):
                        primary_field = "path"

                    if primary_field:
                        if primary_field in extracted_fields:
                            # 明确使用了主字段标签包裹
                            args.update(extracted_fields)
                        else:
                            # 为防止内容中的 XML/HTML 被误判，放弃子标签提取，将整个 body 作为主字段
                            if primary_field not in args:
                                args[primary_field] = body
                                # 兼容别名
                                if primary_field == "cmd":
                                    args["command"] = body
                                if primary_field == "path":
                                    args["filePath"] = body
                    else:
                        # 其他普通工具，正常使用提取出来的字段
                        if extracted_fields:
                            args.update(extracted_fields)
                        elif not args:
                            args["input"] = body

        matched_name = self._match_tool_name(tool_name)
        # 参数规整
        norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)

        return {
            "id": f"call_{uuid.uuid4().hex[:10]}",
            "type": "function",
            "function": {
                "name": matched_name,
                "arguments": json.dumps(norm_args, ensure_ascii=False)
            }
        }

    def _try_parse_json_code_block(self, code_text: str) -> Optional[Dict[str, Any]]:
        """尝试从 Markdown 代码块中解析显式 JSON 工具调用"""
        status, data, _, _ = self._extract_first_json_tool(code_text)
        if status == "COMPLETE":
            t_name = data.get("tool") or data.get("name") or data.get("function")
            matched_name = self._match_tool_name(t_name)
            args = data.get("arguments") or data.get("parameters") or data.get("args") or {}
            if isinstance(args, str):
                try:
                    args = self._repair_and_loads(args) or args
                except Exception:
                    pass
            from app.engine.tool_bridge import ToolBridge
            norm_args = ToolBridge._normalize_tool_args(matched_name, args, self.tools)
            return {
                "id": f"call_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": matched_name,
                    "arguments": json.dumps(norm_args, ensure_ascii=False)
                }
            }
        return None

    def _try_parse_script_code_block(self, code_text: str) -> Optional[Dict[str, Any]]:
        """
        容错将 Markdown 脚本代码块（如 ```powershell ... ``` 或 ```bash ... ```）
        直接转换为对应的注册工具调用（如 pwsh / bash / exec_command）
        """
        if not self.tools:
            return None
        m = re.match(r'^(?:```|~~~)([a-zA-Z0-9_\-]+)?\s*\n?(.*?)\n?(?:```|~~~)$', code_text.strip(), re.DOTALL)
        if not m:
            return None
        lang = (m.group(1) or "").strip().lower()
        body = m.group(2).strip()
        if not body:
            return None

        matched_tool = None
        if lang in ("powershell", "pwsh", "cmd", "ps1", "dos"):
            if "pwsh" in self.tool_names:
                matched_tool = "pwsh"
            elif "bash" in self.tool_names:
                matched_tool = "bash"
            elif "exec_command" in self.tool_names:
                matched_tool = "exec_command"
        elif lang in ("bash", "sh", "shell", "zsh"):
            if "bash" in self.tool_names:
                matched_tool = "bash"
            elif "pwsh" in self.tool_names:
                matched_tool = "pwsh"
            elif "exec_command" in self.tool_names:
                matched_tool = "exec_command"

        if matched_tool:
            from app.engine.tool_bridge import ToolBridge
            norm_args = ToolBridge._normalize_tool_args(matched_tool, {"command": body}, self.tools)
            return {
                "id": f"call_{uuid.uuid4().hex[:10]}",
                "type": "function",
                "function": {
                    "name": matched_tool,
                    "arguments": json.dumps(norm_args, ensure_ascii=False)
                }
            }
        return None

    def _resolve_emittable_name(self, matched: str, require_tool_match: bool) -> Optional[str]:
        """把解析出的工具名收敛成**可以安全发射给客户端**的名字；无法确定时返回 None。

        ★ 这里拦住的是**网关编造工具调用**。

        真实缺陷（离线复现，无需联网）：正文里一段**未闭合**的 JSON ——

            配置文件示例如下：{"name": "my-service", "port": 8080

        在 `flush()` 的宽松路径下被当成一次工具调用发射给客户端（工具名 `my-service`），
        而本次请求注册的工具是 `read` / `write`。后果是**双向的**：
          · 客户端收到一个它**不认识**的工具名（DSH 侧直接报错或空转）
          · **模型真正说的那句话被吞掉**（`openai_routes.py` 把 flush 的 tool_call
            并入 captured_tool_calls 后就直接发射并 return，正文再也不发）

        关键点：`_match_tool_name` 对**未注册**的名字会**原样返回**
        （见它的 `return name` 兜底），所以"匹配成功了"不等于"该发射"。

        只有两种情况可以发射：
          · 名字确实在注册表里（含别名归一后的结果）
          · 允许宽松匹配、且**只注册了一个工具** —— 此时模型指哪个没有歧义，
            但必须**改写成那个工具的名字**，绝不能把模型编出来的名字透给客户端
        """
        if matched in self.tool_names:
            return matched
        if not require_tool_match and len(self.tool_names) == 1:
            return next(iter(self.tool_names))
        return None

    def flush(self) -> List[Tuple[str, Any]]:
        """流结束时清空并吐出残余文本（带残余工具 JSON 兜底提取与标签清洗）"""
        events = []
        if self.buffer:
            # 优先尝试使用 ToolBridge.extract_tool_calls_from_text 提取残余 buffer 中的工具调用（含 bare DSML 块）
            from app.engine.tool_bridge import ToolBridge
            clean_buf, extra_tcs = ToolBridge.extract_tool_calls_from_text(self.buffer, self.tools)
            if extra_tcs:
                for tc in extra_tcs:
                    events.append(("tool_call", tc))
                    self.captured_tool_calls.append(tc)
                if clean_buf.strip():
                    events.append(("text", clean_buf.strip()))
                self.buffer = ""
                cleaned_events = []
                for ev_type, val in events:
                    if ev_type == "text":
                        c = self._clean_disclaimers(val)
                        if c:
                            cleaned_events.append((ev_type, c))
                    else:
                        cleaned_events.append((ev_type, val))
                return cleaned_events

            status, data, j_start, j_end = self._extract_first_json_tool(self.buffer, require_tool_match=False)
            tc = None
            if status == "COMPLETE":
                t_name = data.get("name") or data.get("tool") or data.get("function")
                # ★ 第二处必须校验的地方：这里**重新**取了一次名字。
                # 名字无法收敛成"注册表里的某个工具"时**不发射**，
                # 直接落到下面的"当正文处理"分支 —— 相比发射一次编造的调用，
                # 把模型的原文留在正文里是明确更优的失败方向。
                resolved_name = self._resolve_emittable_name(
                    self._match_tool_name(t_name), require_tool_match=False)
                if resolved_name:
                    args = data.get("arguments") or data.get("parameters") or data.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = self._repair_and_loads(args) or args
                        except Exception:
                            pass
                    from app.engine.tool_bridge import ToolBridge
                    norm_args = ToolBridge._normalize_tool_args(resolved_name, args, self.tools)
                    tc = {
                        "id": f"call_{uuid.uuid4().hex[:10]}",
                        "type": "function",
                        "function": {
                            "name": resolved_name,
                            "arguments": json.dumps(norm_args, ensure_ascii=False)
                        }
                    }

            SEP = r'[\s\u2581_\-]'
            PIPE = r'[\|｜]'
            if tc is not None:
                if j_start > 0:
                    pre_text = self.buffer[:j_start]
                    pre_text = re.sub(r'</?[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|calls?|invoke|action)[^>]*>', '', pre_text, flags=re.IGNORECASE)
                    pre_text = re.sub(r'</?[\|｜\s]*DSML[\|｜\s]*(?:[^>\r\n]*>?)?', '', pre_text)
                    pre_text = re.sub(rf'<+/?{PIPE}*tool{SEP}+(?:call|calls|sep)?{SEP}*(?:begin|end)?{PIPE}*>+', '', pre_text, flags=re.IGNORECASE)
                    pre_text = pre_text.strip()
                    if pre_text:
                        events.append(("text", pre_text))
                events.append(("tool_call", tc))
                self.captured_tool_calls.append(tc)
                if j_end < len(self.buffer):
                    post_text = self.buffer[j_end:]
                    post_text = re.sub(r'</?[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|calls?|invoke|action)[^>]*>', '', post_text, flags=re.IGNORECASE)
                    post_text = re.sub(r'</?[\|｜\s]*DSML[\|｜\s]*(?:[^>\r\n]*>?)?', '', post_text)
                    post_text = re.sub(rf'<+/?{PIPE}*tool{SEP}+(?:call|calls|sep)?{SEP}*(?:begin|end)?{PIPE}*>+', '', post_text, flags=re.IGNORECASE)
                    post_text = post_text.strip()
                    if post_text:
                        events.append(("text", post_text))
                self.buffer = ""
            else:
                # 兜底清洗：防止未闭合或畸变的 <tool_call> / <DSML> 标签原样泄漏给客户端 UI
                # ★ 但**清洗不等于没事**：这里意味着模型发了一段残缺的工具调用，必须显式留痕，
                #   否则 AI 写错工具格式时报错不明显，只会一直错下去。
                if re.search(r'<[\|｜\s/]*(?:DSML|tool_call|tool-call|tool_calls|invoke|calls?)\b', self.buffer, re.IGNORECASE):
                    print(
                        f"[StreamingParser] ⚠️ flush 时仍残留未能解析的工具调用标记，已清洗后作为正文放出（工具未执行）。"
                        f"buffer_head={self.buffer[:120]!r}",
                        flush=True,
                    )
                    _note("tool.flush_unparsed_markup", head=self.buffer[:80])
                cleaned = re.sub(r'</?[\|｜\s]*(?:DSML[\|｜\s]*)?(?:tool_call|tool-call|tool_calls|calls?|invoke|action)[^>]*>', '', self.buffer, flags=re.IGNORECASE)
                cleaned = re.sub(r'</?[\|｜\s]*DSML[\|｜\s]*(?:[^>\r\n]*>?)?', '', cleaned)
                cleaned = re.sub(rf'<+/?{PIPE}*tool{SEP}+(?:call|calls|sep)?{SEP}*(?:begin|end)?{PIPE}*>+', '', cleaned, flags=re.IGNORECASE)
                if cleaned.strip():
                    events.append(("text", cleaned))
                self.buffer = ""

        cleaned_events = []
        for ev_type, val in events:
            if ev_type == "text":
                c = self._clean_disclaimers(val)
                if c:
                    cleaned_events.append((ev_type, c))
            else:
                cleaned_events.append((ev_type, val))
        return cleaned_events
