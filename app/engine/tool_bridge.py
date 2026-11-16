"""
Tool Calling Bridge Engine (工具调用拦截与协议转换管道)
- 将 OpenAI 标准 tools 注入为强引导提示词
- 支持从 DeepSeek 输出中解析 XML 标签 (<bash>, <write> 等) 与 JSON 结构
- 自动适配 CLI Agent (如 opencode) 的 Schema 约束 (如 bash.description, write.filePath)
- 在流式 SSE 与非流式中生成 100% 兼容 OpenAI 规范的 tool_calls
"""

import re
import json
import uuid
from typing import Dict, Any, List, Optional, Tuple, Set


class ToolBridge:
    @staticmethod
    def format_tools_system_prompt(tools: List[Dict[str, Any]]) -> str:
        """格式化 tools 定义并注入提示词约束"""
        if not tools:
            return ""

        tool_descs = []
        for t in tools:
            fn = t.get("function", t)
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            properties = params.get("properties", {})
            required = params.get("required", [])

            param_lines = []
            for p_name, p_info in properties.items():
                p_type = p_info.get("type", "string")
                p_desc = p_info.get("description", "")
                req_mark = "(必填)" if p_name in required else "(可选)"
                param_lines.append(f"    - {p_name} [{p_type}] {req_mark}: {p_desc}")

            param_str = "\n".join(param_lines) if param_lines else "    (无参数)"
            tool_descs.append(f"【工具: {name}】\n  说明: {desc}\n  参数:\n{param_str}")

        all_tools_str = "\n\n".join(tool_descs)

        tool_names_set = set()
        for t in tools:
            fn = t.get("function", t)
            tool_names_set.add(fn.get("name", ""))

        instructions = (
            "\n\n# 可用工具规范 (Available Tools)\n"
            "你可以使用以下已注册的本地工具协助完成任务：\n\n"
            f"{all_tools_str}\n\n"
            "## 工具调用协议\n"
            "若需要调用工具，请在回答中使用如下格式输出。你可以根据任务需要单次回复调用多个工具（例如并发读取多个文件、并发执行多项检查等），每个工具使用独立的 <tool_call> 标签包裹：\n"
            "<tool_call>\n"
            '{"name": "工具名", "arguments": {"参数名": "参数值"}}\n'
            "</tool_call>\n\n"
            "- 当需要同时读取多个文件或并发执行多个互不依赖的操作时，请一次性连续输出多个 <tool_call> 标签以提升执行效率；\n"
            "- 严禁在多个 <tool_call> 标签之间或之后编造输出假想的工具执行结果；只有在所有工具调用完成、收到真实工具执行返回后，才进行后续回答；\n"
            "- 当确实需要读取文件、执行命令或调用系统能力时，请使用上述格式调用工具；\n"
            "- 若用户请求无需调用工具（如直接回答、方案设计、文本编辑或代码编写），请直接用清晰自然的语言回复用户，绝不要输出空工具调用。\n"
        )
        return instructions


    @staticmethod
    def check_duplicate_loop(messages: List[Dict[str, Any]], proposed_tool_calls: List[Dict[str, Any]]) -> bool:
        """
        检查新生成的 tool_calls 是否与最近历史中连续调用的工具完全一致（陷入死循环）。
        如果连续重复 >= 2 次相同命令/参数，判定为死循环熔断。
        """
        if not proposed_tool_calls or not messages:
            return False

        curr_fn = proposed_tool_calls[0].get("function", {})
        curr_name = curr_fn.get("name")
        curr_args_raw = curr_fn.get("arguments", "{}")
        try:
            curr_args = json.loads(curr_args_raw) if isinstance(curr_args_raw, str) else curr_args_raw
        except Exception:
            curr_args = curr_args_raw

        duplicate_count = 0
        tool_results = []
        for m in reversed(messages):
            r = m.get("role")
            if r == "tool":
                c = m.get("content", "")
                tool_results.append(str(c).strip())
            elif r == "assistant":
                prev_tcs = m.get("tool_calls", [])
                if prev_tcs:
                    p_fn = prev_tcs[0].get("function", {})
                    p_name = p_fn.get("name")
                    p_args_raw = p_fn.get("arguments", "{}")
                    try:
                        p_args = json.loads(p_args_raw) if isinstance(p_args_raw, str) else p_args_raw
                    except Exception:
                        p_args = p_args_raw

                    if p_name == curr_name and p_args == curr_args:
                        duplicate_count += 1
                    else:
                        break

        # 连续 2 次相同调用且工具返回结果完全一致无进展时，判定为死循环熔断
        if duplicate_count < 2:
            return False

        # 如果工具返回的结果在不断变化（例如轮询进度、日志追加、状态更新），说明有业务进展，绝不是死循环！
        if len(tool_results) >= 2:
            if tool_results[0] != tool_results[1]:
                return False

        return True

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
        try:
            import ast
            val = ast.literal_eval(fixed)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            pass
        return None

    @classmethod
    def match_tool_name(cls, name: Optional[str], available_names: Set[str]) -> str:
        """智能匹配工具名称（含常用别名、符号变体与跨平台映射）"""
        if not name or not name.strip():
            return ""
        name = name.strip()
        if name in available_names:
            return name

        norm = name.lower().replace("-", "_")
        for tn in available_names:
            if tn.lower().replace("-", "_") == norm:
                return tn

        cmd_aliases = {
            "run_command", "execute_command", "exec_command", "execute",
            "run_shell", "shell", "sh", "bash", "cmd", "terminal", "pwsh", "powershell"
        }
        if norm in cmd_aliases:
            for pref in ("pwsh", "bash", "exec_command"):
                if pref in available_names:
                    return pref

        file_write_aliases = {"write_file", "write_to_file", "create_file", "save_file", "writefile"}
        if norm in file_write_aliases:
            for pref in ("write", "write_file", "create_file"):
                if pref in available_names:
                    return pref

        file_read_aliases = {
            "read_file", "view_file", "cat", "open_file", "readfile", "viewfile",
            "read_document", "readdocument"
        }
        if norm in file_read_aliases or norm == "read":
            for pref in ("read", "read_file", "office_read-document", "view_file"):
                if pref in available_names:
                    return pref

        img_read_aliases = {"view_image", "read_img", "image_read", "inspect_image", "show_image", "readimage", "read_image"}
        if norm in img_read_aliases and "read_image" in available_names:
            return "read_image"

        alias_map = {
            "execute_command": "exec_command",
            "execute": "bash",
            "run_command": "exec_command",
            "run_shell": "bash",
            "shell": "bash",
            "sh": "bash",
            "read_file": "read",
            "write_file": "write",
            "view_file": "read",
            "view": "read",
            "read_img": "read_image",
            "readimage": "read_image",
            "read_document": "office_read-document",
        }
        if norm in alias_map and alias_map[norm] in available_names:
            return alias_map[norm]

        suffix_hits = [tn for tn in sorted(available_names) if len(name) >= 3 and (tn.endswith(name) or name.endswith(tn))]
        if len(suffix_hits) == 1:
            return suffix_hits[0]

        for tn in sorted(available_names):
            if (tn.startswith("exec") and norm.startswith("exec")) or \
               (tn.startswith("read") and norm.startswith("read")) or \
               (tn.startswith("write") and norm.startswith("write")) or \
               ("read-document" in tn and "read" in norm):
                if "image" in norm and "image" in tn:
                    return tn
                if "image" not in norm and "image" not in tn:
                    return tn
        return name

    @staticmethod
    def parse_invoke_body(body: str, tool_name: str, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """解析 <|invoke name="X"> 的**标签体**，兼容模型实际会写出的三种形态：

        1. DSML 参数标签：<|parameter name="path" string="true">C:\\a.py</|parameter>
        2. XML 子标签：    <path>C:\\a.py</path>
        3. 裸 JSON：       {"path": "C:\\a.py"}
        4. 裸纯文本：     当且仅当工具只有一个必填字段时，自动落入该字段；否则落入 {"input": ...}

        底层已彻底重构为基于符号下推自动机（DSMLStackParser）的词法栈模型，具备作用域隔离、自动归约与零污染纯净捕获能力。
        """
        from app.engine.dsml_stack_parser import DSMLStackParser
        return DSMLStackParser.parse_invoke_body(body, tool_name, tools)

    @staticmethod
    def infer_tool_name_from_args(args: Dict[str, Any], tools: List[Dict[str, Any]]) -> Optional[str]:
        """当模型在 DSML 中漏掉了 <invoke name="..."> 直接吐出参数时，
        根据提取到的参数集合反向推导最匹配的目标工具名。
        """
        if not args or not tools:
            return None

        arg_keys = set(args.keys())
        clean_keys = {k for k in arg_keys if k not in ("input", "raw")}
        if not clean_keys:
            return None

        best_tool = None
        best_score = -1

        for t in tools:
            fn = t.get("function", t)
            t_name = fn.get("name")
            if not t_name:
                continue
            params = fn.get("parameters", {}) or {}
            props = set((params.get("properties", {}) or {}).keys())
            reqs = set((params.get("required", []) or []))

            # 匹配属性交集
            matched_props = clean_keys & props
            score = len(matched_props) * 10

            # 满足全部 required 必填项，大幅加分
            if reqs and reqs.issubset(clean_keys):
                score += 100
            elif reqs and (reqs & clean_keys):
                score += len(reqs & clean_keys) * 20

            # 扣除缺失 required 必填项的惩罚分（防止参数繁多的复杂工具如 GitHub MCP 因撞车单个通用字段被误选）
            missing_reqs = reqs - clean_keys
            if missing_reqs:
                score -= len(missing_reqs) * 25

            # 强特征参数加权
            if ("command" in clean_keys or "cmd" in clean_keys) and t_name in ("pwsh", "bash", "terminal", "sh", "exec_command"):
                score += 50
            if ("file_path" in clean_keys or "filePath" in clean_keys or "path" in clean_keys) and ("old_string" in clean_keys or "new_string" in clean_keys) and t_name in ("edit", "str_replace_editor"):
                score += 50
            if ("content" in clean_keys) and ("file_path" in clean_keys or "filePath" in clean_keys or "path" in clean_keys) and t_name in ("write", "write_file"):
                score += 50

            if score > best_score and score >= 10:
                best_score = score
                best_tool = t_name

        return best_tool

    @classmethod
    def _extract_json_objects(cls, text: str) -> List[Tuple[str, Dict[str, Any]]]:
        """通过成对花括号提取合法的 JSON 对象（带容错修复）"""
        results = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == '{':
                depth = 0
                start = i
                in_str = False
                esc = False
                matched = False
                for j in range(i, n):
                    ch = text[j]
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
                                candidate = text[start : j + 1]
                                obj = cls._repair_and_loads(candidate)
                                if isinstance(obj, dict):
                                    results.append((candidate, obj))
                                i = j
                                matched = True
                                break
                if not matched and depth > 0:
                    candidate_raw = text[start:n]
                    tag_pos = candidate_raw.find("<")
                    if tag_pos != -1:
                        candidate_raw = candidate_raw[:tag_pos]
                    candidate_raw = candidate_raw.rstrip()
                    d = 0
                    in_s = False
                    e = False
                    for c in candidate_raw:
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
                        candidate = candidate_raw + ("}" * d)
                        obj = cls._repair_and_loads(candidate)
                        if isinstance(obj, dict) and any(k in obj for k in ("name", "command", "tool", "function", "action", "arguments", "parameters")):
                            results.append((text[start : start + len(candidate_raw)], obj))
                            i = start + len(candidate_raw)
                            matched = True
                            break
            i += 1
        return results

    @staticmethod
    def _parse_xml_attributes(attr_str: str) -> Dict[str, Any]:
        """解析 XML 标签属性，支持 string、JSON 数组/对象、布尔与数字"""
        args = {}
        pattern = re.compile(r'([a-zA-Z0-9_\-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\[[^\]]*\]|\{[^\}]*\}|[^\s>]+))')
        for m in pattern.finditer(attr_str):
            k = m.group(1)
            v = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
            if v is None:
                continue
            v = v.strip()
            if v.lower() == "true":
                args[k] = True
            elif v.lower() == "false":
                args[k] = False
            elif v.lower() == "null":
                args[k] = None
            elif (v.startswith("[") and v.endswith("]")) or (v.startswith("{") and v.endswith("}")):
                try:
                    args[k] = json.loads(v)
                except Exception:
                    args[k] = v
            elif v.isdigit():
                args[k] = int(v)
            else:
                try:
                    args[k] = float(v)
                except ValueError:
                    args[k] = v
        return args

    @staticmethod
    def _normalize_tool_args(tool_name: str, args: Any, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        根据工具 schema 规范化参数，自动补齐必填字段（如 opencode 的 bash.description、write.filePath）

        【类型防护】本函数原先无条件执行 `args.items()`，一旦 args 不是 dict 就抛
        `AttributeError: 'str'/'NoneType'/'list' object has no attribute 'items'`
        （实测：str / None / list / int 全部崩溃）。
        而调用方普遍写成 `args = self._repair_and_loads(args) or args`
        —— **修复失败时 args 仍是 str** —— 于是这条链路是真实可达的，
        会导致整轮请求 500，且错误信息完全指不到根因。

        这里统一收敛为 dict：无法解析的输入降级为 {"input": 原值}，
        既保住信息（客户端仍能看到原始内容）又不让请求崩溃。
        """
        if not isinstance(args, dict):
            if args is None or args == "":
                args = {}
            elif isinstance(args, str):
                # 先尝试把字符串还原成 dict（含 Windows 路径转义修复）
                parsed = ToolBridge._repair_and_loads(args)
                args = parsed if isinstance(parsed, dict) else {"input": args}
            else:
                # list / int / bool 等：包成 input，交由 schema 层处理
                args = {"input": args}

        # 如果模型输出包裹在 "arguments" 或 "parameters" 字段中（例如 OpenAI 格式嵌套），自动解包展平
        if "arguments" in args:
            sub_args = args["arguments"]
            if isinstance(sub_args, str):
                sub_args = ToolBridge._repair_and_loads(sub_args) or sub_args
            if isinstance(sub_args, dict):
                for k, v in sub_args.items():
                    if k not in args or not args[k]:
                        args[k] = v
                del args["arguments"]
        if "parameters" in args and isinstance(args["parameters"], dict):
            for k, v in args["parameters"].items():
                if k not in args or not args[k]:
                    args[k] = v
            del args["parameters"]
        if "name" in args and args["name"] == tool_name:
            del args["name"]

        # 0. 清理参数字符串中可能被大模型误包裹的同名 XML 子标签 (例如 <cmd>python ...</cmd>)
        # ⚠️ 必须仅在标签名与参数键名匹配时才剥离，严防误杀合法代码标签 (如 HTML 的 <script>、XML 的 <config> 等)
        for k, v in list(args.items()):
            if isinstance(v, str):
                v_clean = v.strip()
                m_tag = re.match(r'^<([a-zA-Z_][a-zA-Z0-9_-]*)>(.*?)(?:</\1>)?$', v_clean, re.DOTALL)
                if m_tag:
                    tag_low = m_tag.group(1).lower()
                    k_low = k.lower()
                    if (tag_low in ("cmd", "command") and k_low in ("cmd", "command")) or \
                       (tag_low == "script" and k_low in ("cmd", "command", "script")) or \
                       (tag_low == "content" and k_low in ("content", "file_content", "body")) or \
                       (tag_low in ("path", "filepath") and k_low in ("path", "file_path", "filepath")) or \
                       (tag_low == "code" and k_low in ("code", "content")) or \
                       (tag_low == "input" and k_low == "input"):
                        v_clean = m_tag.group(2).strip()
                args[k] = v_clean

        # 0.1 深度净化：彻底剥离任何渗漏的 DSML 内部标签与特殊 Token
        # ⚠️ 绝对严禁使用带有可选 ? 的通配正则，必须精准锁定 DSML 关键字，绝不误吞 HTML/XML 尖括号
        for k, v in list(args.items()):
            if isinstance(v, str):
                # 截断任何在参数值内残留的闭合标签（精确匹配包含 DSML 的闭合标记，含缺失开尖括号的容错形态）
                close_tag_match = re.search(
                    r'(?:(?:</?[\|｜\s/]*|[\|｜\s/]+)DSML[\|｜\s/]*(?:parameter|invoke|call|calls)\s*>|(?:</?[\|｜\s/]*|[\|｜\s/]+)DSML[\|｜\s/]*>)',
                    v,
                    re.IGNORECASE
                )
                if close_tag_match:
                    v = v[:close_tag_match.start()].strip()
                # 若字符串内部包含下一个参数或调用的开始标签，截断其后的泄漏内容
                next_param_match = re.search(
                    r'(?:<[\|｜\s/]*|[\|｜\s/]+)DSML[\|｜\s/]*(?:parameter\s+name=["\']?[a-zA-Z0-9_\-]+["\']?|[a-zA-Z0-9_\-]+["\']?\s*(?:string=|=))',
                    v,
                    re.IGNORECASE
                )
                if next_param_match:
                    v = v[:next_param_match.start()].strip()
                # 剥离任何残留的内部 DSML 标签片段（必须包含 DSML 关键字，绝不可选）
                v = re.sub(r'(?:</?[\|｜\s/]*|[\|｜\s/]+)DSML[\|｜\s/]*(?:[^>\r\n]*>?)?', '', v).strip()
                v = re.sub(r'<[\|｜\s/]*DSML[\|｜\s/]*[^>]*>', '', v).strip()
                args[k] = v

        if list(args.keys()) == ["input"] and not args["input"]:
            args = {}

        # 1. 路径与命令别名同步 (path / filePath / file_path / targetPath)
        p_val = args.get("path") or args.get("filePath") or args.get("file_path") or args.get("targetPath")
        if p_val:
            args["path"] = p_val
            args["filePath"] = p_val
            args["file_path"] = p_val

        if "cmd" in args and "command" not in args:
            args["command"] = args["cmd"]
        if "command" in args and "cmd" not in args:
            args["cmd"] = args["command"]

        # 1.1 编辑类工具别名同步 (old_string / old_str / oldText / search, new_string / new_str / newText / replace)
        old_val = (
            args.get("old_string")
            if "old_string" in args
            else args.get("old_str")
            if "old_str" in args
            else args.get("oldText")
            if "oldText" in args
            else args.get("search")
            if "search" in args
            else args.get("original")
        )
        if old_val is not None:
            args["old_string"] = old_val
            args["old_str"] = old_val

        new_val = (
            args.get("new_string")
            if "new_string" in args
            else args.get("new_str")
            if "new_str" in args
            else args.get("newText")
            if "newText" in args
            else args.get("replace")
            if "replace" in args
            else args.get("replacement")
        )
        if new_val is not None:
            args["new_string"] = new_val
            args["new_str"] = new_val

        # 2. 查找 schema 定义
        tool_def = None
        for t in tools:
            fn = t.get("function", t)
            if fn.get("name") == tool_name:
                tool_def = fn
                break

        # 3. 针对 CLI Agent 工具做智能字段回填
        if tool_name in ("bash", "terminal", "sh", "exec_command", "pwsh"):
            cmd = str(args.get("command", "") or args.get("cmd", "")).strip()
            if "cmd" not in args or not args["cmd"]:
                args["cmd"] = cmd
            if "command" not in args or not args["command"]:
                args["command"] = cmd
            if "description" not in args or not args["description"]:
                args["description"] = f"Execute: {cmd[:40]}" if cmd else "Execute command"
            # 注意：不再把空的 workdir 改写为 "."。
            # 模型没给 workdir 时，工作目录应由客户端/工具自身决定；
            # 网关替它选 "." 会改变执行位置（例如本该在项目根目录执行却在别处执行）。

        if tool_name in ("write", "read", "read_image", "read_file", "write_file", "view_file", "office_read-document"):
            if p_val:
                args["filePath"] = p_val
                args["path"] = p_val
                args["file_path"] = p_val
            # 注意：绝不在此处为 write 补 content=""。
            # 模型漏给 content 时补空串会让客户端把目标文件覆写成空文件（数据丢失）。
            # 缺失的必填字段交由下方 required 检查显式暴露，由调用方决定报错还是重试。

        # 3.1 针对 web_search 规整 query 与 queries 参数
        if tool_name in ("web_search", "search"):
            if "query" in args and "queries" not in args:
                q = args["query"]
                args["queries"] = [q] if isinstance(q, str) else list(q) if isinstance(q, (list, tuple)) else [str(q)]
            elif "queries" in args:
                if isinstance(args["queries"], str):
                    args["queries"] = [args["queries"]]
            if "queries" in args and isinstance(args["queries"], list) and "query" not in args:
                if len(args["queries"]) > 0 and isinstance(args["queries"][0], str):
                    args["query"] = args["queries"][0]

        # 3.2 针对 ask_user_question 规整 options 参数格式
        if tool_name in ("ask_user_question", "ask_question"):
            questions = args.get("questions")
            if isinstance(questions, list):
                for q in questions:
                    if isinstance(q, dict) and "options" in q and isinstance(q["options"], list):
                        new_opts = []
                        for opt in q["options"]:
                            if isinstance(opt, str):
                                new_opts.append({"id": opt, "label": opt, "text": opt})
                            else:
                                new_opts.append(opt)
                        q["options"] = new_opts

        # 4. 遍历 required：只做「别名派生」，绝不凭空发明语义值
        #    原则：能从模型已给的参数里推导出来的（如 path -> filePath）可以补；
        #    需要凭空决定的（workdir 取值、subagent_type 取值）一律不补。
        #    凭空补值会改变工具语义（例如替模型决定工作目录），且会让客户端以为模型给了参数。
        if tool_def:
            params = tool_def.get("parameters", {})
            required = params.get("required", [])
            for req in required:
                if req in args:
                    continue
                if req == "description":
                    # 纯描述性字段，从命令派生，不改变执行语义
                    src = args.get("command") or args.get("cmd") or tool_name
                    args["description"] = f"Run: {str(src)[:40]}"
                elif req == "filePath":
                    src = args.get("path") or args.get("file_path") or args.get("targetPath")
                    if src:
                        args["filePath"] = src
                elif req == "path":
                    src = args.get("filePath") or args.get("file_path") or args.get("targetPath")
                    if src:
                        args["path"] = src
                elif req == "file_path":
                    src = args.get("path") or args.get("filePath") or args.get("targetPath")
                    if src:
                        args["file_path"] = src
                elif req in ("old_string", "old_str", "oldText", "search"):
                    src = (
                        args.get("old_string")
                        or args.get("old_str")
                        or args.get("oldText")
                        or args.get("search")
                    )
                    if src is not None:
                        args[req] = src
                elif req in ("new_string", "new_str", "newText", "replace", "replacement"):
                    src = (
                        args.get("new_string")
                        or args.get("new_str")
                        or args.get("newText")
                        or args.get("replace")
                    )
                    if src is not None:
                        args[req] = src
                elif req == "prompt":
                    src = args.get("description")
                    if src:
                        args["prompt"] = src
                else:
                    # workdir / subagent_type / content 等：无源可派生，保持缺失
                    print(
                        f"[ToolBridge] 工具 {tool_name} 缺少必填参数 '{req}'，"
                        f"已保留缺失状态交由调用方处理（不伪造默认值）。args_keys={sorted(args.keys())}",
                        flush=True,
                    )

        # 5. Schema 强类型对齐与自愈 (Type Guard & Coercion)
        #    解决模型在 write 等工具中将 content 写为 JSON 列表/字典而被反序列化破坏类型的问题，
        #    以及数字/布尔字符串的合规转化，避免被下游工具校验库 (如 Zod) 拒收。
        if tool_def:
            props = (tool_def.get("parameters", {}) or {}).get("properties", {}) or {}
            for k, pdef in props.items():
                if k not in args:
                    continue
                expected_type = pdef.get("type")
                val = args[k]
                if expected_type == "string" and isinstance(val, (dict, list)):
                    args[k] = json.dumps(val, ensure_ascii=False)
                elif expected_type in ("integer", "number") and isinstance(val, str):
                    try:
                        f_val = float(val)
                        args[k] = int(f_val) if f_val.is_integer() else f_val
                    except ValueError:
                        pass
                elif expected_type == "boolean" and isinstance(val, str):
                    if val.lower() == "true":
                        args[k] = True
                    elif val.lower() == "false":
                        args[k] = False
                elif expected_type in ("array", "object") and isinstance(val, str):
                    try:
                        parsed_val = json.loads(val)
                        if expected_type == "array" and isinstance(parsed_val, list):
                            args[k] = parsed_val
                        elif expected_type == "object" and isinstance(parsed_val, dict):
                            args[k] = parsed_val
                    except Exception:
                        pass

        # 6. 通用兜底强类型对齐（即使 tool_def 缺失，也按常见命名约定自动转义数字和布尔，防 downstream schema 报错）
        for k, val in list(args.items()):
            if isinstance(val, str):
                lower_val = val.lower().strip()
                if lower_val == "true" and any(b in k.lower() for b in ("wait", "stream", "replace_all", "exact", "recursive", "background")):
                    args[k] = True
                elif lower_val == "false" and any(b in k.lower() for b in ("wait", "stream", "replace_all", "exact", "recursive", "background")):
                    args[k] = False
                elif lower_val.isdigit() and any(n in k.lower() for n in ("timeout", "limit", "count", "port", "retry", "size", "round", "tokens", "depth")):
                    try:
                        args[k] = int(lower_val)
                    except ValueError:
                        pass

        return args

    @classmethod
    def extract_tool_calls_from_text(
        cls, text: str, tools: List[Dict[str, Any]]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        从模型完整响应中提取工具调用
        返回: (clean_text, tool_calls_list)
        """
        tool_names = set()
        for t in tools:
            fn = t.get("function", t)
            tool_names.add(fn.get("name", ""))

        tool_calls = []
        clean_text = text

        # 0. 优先使用编译原理符号下推自动机（Pushdown Automaton）解析 DeepSeek DSML 与结构化 Invoke 调用
        from app.engine.dsml_stack_parser import DSMLStackParser
        clean_text, dsml_tool_calls = DSMLStackParser.extract_tool_calls_from_text(clean_text, tools)
        tool_calls.extend(dsml_tool_calls)

        # 0.2 匹配 DeepSeek V3 / R1 官方特殊 Token 风格 (<｜tool call begin｜>...)
        SEP = r'[\s\u2581_\-]'
        PIPE = r'[\|｜]'
        special_call_pattern = re.compile(
            rf'<+{PIPE}*tool{SEP}+call{SEP}+begin{PIPE}*>+'
            rf'(?:function{SEP}*)?'
            rf'(?:<+{PIPE}*tool{SEP}+sep{PIPE}*>+)?'
            rf'([a-zA-Z0-9_\-\.:]+)'
            rf'(?:<+{PIPE}*tool{SEP}+sep{PIPE}*>+)?'
            rf'\s*(?:```json)?(.*?)(?:```)?'
            rf'(?:<+/?{PIPE}*tool{SEP}+call{SEP}+end{PIPE}*>+|$)',
            re.DOTALL | re.IGNORECASE,
        )
        for sc in special_call_pattern.finditer(clean_text):
            t_name = sc.group(1).strip()
            matched_name = cls.match_tool_name(t_name, tool_names)
            if tool_names and matched_name not in tool_names:
                if len(tool_names) == 1:
                    matched_name = next(iter(tool_names))
            t_name = matched_name

            val = sc.group(2).strip()
            args = cls._repair_and_loads(val)
            if not isinstance(args, dict):
                args = {"input": val} if val else {}
            args = cls._normalize_tool_args(t_name, args, tools)
            call_id = f"call_{uuid.uuid4().hex[:10]}"
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": t_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
            clean_text = clean_text.replace(sc.group(0), "")

        # 清除残留的 DSML 结构标签以及特殊 Token 标签
        clean_text = re.sub(r'<[\|｜\s]*/?[\|｜\s]*DSML[\|｜\s]*[^>]*>', "", clean_text)
        clean_text = re.sub(rf'<+/?{PIPE}*tool{SEP}+(?:call|calls|sep)?{SEP}*(?:begin|end)?{PIPE}*>+', "", clean_text, flags=re.IGNORECASE)

        # 0.1 优先提取 <tool_call...> 标准单行/多行 JSON 块 (支持带闭合标签或无闭合标签)
        tc_tag_pattern = re.compile(
            r'<[\|｜\s]*(?:tool_call|tool-call|tool_calls|tool|call)(?:[\s:]+(?:name=[\"\']|tool=[\"\']|function=[\"\'])?([a-zA-Z0-9_\-\.:]+)[\"\']?)?[^>]*>',
            re.IGNORECASE
        )
        while True:
            m = tc_tag_pattern.search(clean_text)
            if not m:
                break
            tag_start = m.start()
            tag_end = m.end()
            attr_name = m.group(1).strip() if m.group(1) else ""
            remainder = clean_text[tag_end:]

            brace_pos = remainder.find("{")
            prefix_between = remainder[:brace_pos] if brace_pos != -1 else ""
            cleaned_prefix = prefix_between.replace("```json", "").replace("```", "").strip()

            if brace_pos == -1 or cleaned_prefix:
                clean_text = clean_text[:tag_start] + clean_text[tag_end:]
                continue

            json_slice = remainder[brace_pos:]
            inner_objs = cls._extract_json_objects(json_slice)
            if not inner_objs:
                clean_text = clean_text[:tag_start] + clean_text[tag_end:]
                continue

            raw_json, data = inner_objs[0]
            consumed_end = tag_end + brace_pos + len(raw_json)

            after_json = clean_text[consumed_end:]
            close_tag_m = re.match(r'^\s*</[\|｜\s]*(?:tool_call|tool-call|tool_calls|tool|call)(?::[a-zA-Z0-9_\-\.:]+)?[\|｜\s]*>', after_json, re.IGNORECASE)
            if close_tag_m:
                consumed_end += close_tag_m.end()

            t_name = data.get("name") or data.get("tool") or data.get("function") or attr_name
            matched_name = None
            if t_name and t_name.strip():
                t_name = t_name.strip()
                matched_name = t_name if (not tool_names or t_name in tool_names) else next(
                    (tn for tn in tool_names if len(t_name) >= 3 and (tn.endswith(t_name) or t_name.endswith(tn))), None
                )
                if not matched_name:
                    cmd_aliases = {"run_command", "execute_command", "exec_command", "execute", "run_shell", "shell", "sh", "bash", "cmd", "terminal", "pwsh", "powershell"}
                    if t_name in cmd_aliases:
                        if "pwsh" in tool_names:
                            matched_name = "pwsh"
                        elif "bash" in tool_names:
                            matched_name = "bash"
                        elif "exec_command" in tool_names:
                            matched_name = "exec_command"
                    elif not tool_names:
                        matched_name = t_name
            elif not t_name and tool_names and len(tool_names) == 1:
                matched_name = next(iter(tool_names))

            if matched_name:
                args = data.get("arguments") or data.get("parameters") or data.get("args")
                if args is None:
                    args = {k: v for k, v in data.items() if k not in ("name", "tool", "function", "action")}
                if isinstance(args, str):
                    try:
                        args = cls._repair_and_loads(args) or args
                    except Exception:
                        pass
                args = cls._normalize_tool_args(matched_name, args, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": matched_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })

            clean_text = clean_text[:tag_start] + clean_text[consumed_end:]

        # 清除任何遗留的孤立 </tool_call> 标签
        clean_text = re.sub(r'</[\|｜\s]*(?:tool_call|tool-call|tool_calls|tool|call)[^>]*>', '', clean_text, flags=re.IGNORECASE)

        # 0.2 优先提取 [call:tool_name]{...}[/call]
        bracket_pattern = re.compile(r'\[call:([a-zA-Z0-9_\-\.:]+)\](.*?)\[/call\]', re.DOTALL | re.IGNORECASE)
        for m in bracket_pattern.finditer(clean_text):
            t_name = m.group(1).strip()
            body = m.group(2).strip()
            matched_name = t_name if (not tool_names or t_name in tool_names) else next(
                (tn for tn in tool_names if tn.endswith(t_name) or t_name.endswith(tn)), t_name
            )
            try:
                args = json.loads(body) if body else {}
            except Exception:
                args = {"input": body}
            args = cls._normalize_tool_args(matched_name, args, tools)
            call_id = f"call_{uuid.uuid4().hex[:10]}"
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": matched_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
            clean_text = clean_text.replace(m.group(0), "")

        # 0.3 提取 [工具调用]: <tool_name>\n{...} 或 【工具调用】：<tool_name>\n{...}
        zh_pattern = re.compile(
            r'[\[【][\|｜\s]*(?:工具调用|tool_call|call|tool)[\|｜\s]*[\]】][:：]?\s*([a-zA-Z0-9_\-\.:]+)?\s*(\{.*?\})(?:\s*[\[<]/[\|｜\s]*(?:工具调用|tool_call|call|tool)[\|｜\s]*[\]>])?',
            re.DOTALL | re.IGNORECASE
        )
        for m in zh_pattern.finditer(clean_text):
            t_name = m.group(1).strip() if m.group(1) else ""
            body = m.group(2).strip()
            args = cls._repair_and_loads(body) or {}
            if not isinstance(args, dict):
                args = {"input": body}
            if not t_name:
                t_name = args.get("name") or args.get("tool") or args.get("function") or ""

            matched_name = t_name if t_name in tool_names else next(
                (tn for tn in tool_names if tn.endswith(t_name) or t_name.endswith(tn)), None
            )
            if not matched_name:
                cmd_aliases = {"run_command", "execute_command", "exec_command", "execute", "run_shell", "shell", "sh", "bash", "cmd", "terminal", "pwsh", "powershell"}
                if t_name in cmd_aliases:
                    if "pwsh" in tool_names:
                        matched_name = "pwsh"
                    elif "bash" in tool_names:
                        matched_name = "bash"
                    elif "exec_command" in tool_names:
                        matched_name = "exec_command"

            if matched_name:
                args = cls._normalize_tool_args(matched_name, args, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": matched_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                clean_text = clean_text.replace(m.group(0), "")

        # 1. 提取其他成对 JSON 对象 (例如 ```json { ... } ``` 或裸 json { ... })
        json_objs = cls._extract_json_objects(clean_text)
        for raw_snippet, data in json_objs:
            t_name = data.get("name") or data.get("tool") or data.get("function")
            matched_name = None
            if t_name and t_name.strip():
                t_name = t_name.strip()
                matched_name = t_name if (not tool_names or t_name in tool_names) else next(
                    (tn for tn in tool_names if len(t_name) >= 3 and (tn.endswith(t_name) or t_name.endswith(tn))), None
                )
                if not matched_name:
                    cmd_aliases = {"run_command", "execute_command", "exec_command", "execute", "run_shell", "shell", "sh", "bash", "cmd", "terminal", "pwsh", "powershell"}
                    if t_name in cmd_aliases:
                        if "pwsh" in tool_names:
                            matched_name = "pwsh"
                        elif "bash" in tool_names:
                            matched_name = "bash"
                        elif "exec_command" in tool_names:
                            matched_name = "exec_command"
                    elif not tool_names:
                        matched_name = t_name

            if matched_name:
                args = data.get("arguments") or data.get("parameters") or {}
                if isinstance(args, str):
                    try:
                        args = cls._repair_and_loads(args) or args
                    except Exception:
                        args = {"command": args} if matched_name in ("bash", "terminal", "sh", "pwsh") else {"input": args}
                elif not args and any(k in data for k in ("command", "cmd", "pattern", "path", "filePath")):
                    args = {k: v for k, v in data.items() if k not in ("name", "tool", "function", "action")}

                args = cls._normalize_tool_args(matched_name, args, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": matched_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                # 尝试把包含它的完整代码块或者 snippet 清理
                clean_text = clean_text.replace(raw_snippet, "")

        # 2. 匹配 XML / Code 风格: <bash>...</bash> 或 bash>...</bash>
        # 若文本包含明显的完成总结词汇，且没有使用显式 <bash> 标签，普通的 ```bash ``` 代码块保留给用户展示，避免陷入死循环
        is_summary = bool(re.search(r"(?:已完成|任务已完成|成果总结|成功生成|已生成|已保存为|生成的报告|已成功|successfully created|task complete)", text, re.IGNORECASE))
        xml_bash_matches = list(re.finditer(r"(?:<bash(?:[^>]*)>|bash>)(.*?)(?:</bash>)", text, re.DOTALL | re.IGNORECASE))
        md_bash_matches = []
        if not is_summary and not xml_bash_matches:
            md_bash_matches = list(re.finditer(r"```(?:bash|sh|powershell|cmd)\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE))

        bash_matches = xml_bash_matches + md_bash_matches
        if bash_matches and ("bash" in tool_names or "terminal" in tool_names or "sh" in tool_names):
            b_name = "bash" if "bash" in tool_names else ("terminal" if "terminal" in tool_names else "sh")
            for m in bash_matches:
                cmd = m.group(1).strip()
                if not cmd:
                    continue
                args = cls._normalize_tool_args(b_name, {"command": cmd}, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": b_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                clean_text = clean_text.replace(m.group(0), "")

        # 3. 匹配 XML 风格: <write filePath="...">...</write> 或 <write path="...">...</write>
        write_matches = list(re.finditer(r'(?:<write|write)\s+(?:path|filePath)=["\'](.*?)["\']>(.*?)</write>', text, re.DOTALL | re.IGNORECASE))
        if write_matches and "write" in tool_names:
            for m in write_matches:
                w_path = m.group(1).strip()
                w_content = m.group(2)
                args = cls._normalize_tool_args("write", {"filePath": w_path, "path": w_path, "content": w_content}, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                clean_text = clean_text.replace(m.group(0), "")

        # 4. 匹配 XML 风格: <read filePath="..." /> 或 <read><path>...</path></read>
        read_attr_matches = list(re.finditer(r'(?:<read|read)\s+(?:path|filePath)=["\'](.*?)["\']\s*/?>', text, re.DOTALL | re.IGNORECASE))
        read_tag_matches = list(re.finditer(r'<read>(.*?)</read>', text, re.DOTALL | re.IGNORECASE))
        if (read_attr_matches or read_tag_matches) and "read" in tool_names:
            for m in read_attr_matches:
                r_path = m.group(1).strip()
                args = cls._normalize_tool_args("read", {"filePath": r_path, "path": r_path}, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read", "arguments": json.dumps(args, ensure_ascii=False)},
                })
                clean_text = clean_text.replace(m.group(0), "")
            for m in read_tag_matches:
                body = m.group(1).strip()
                p_match = re.search(r'<(?:path|filePath)>(.*?)</(?:path|filePath)>', body, re.DOTALL | re.IGNORECASE)
                r_path = p_match.group(1).strip() if p_match else body
                args = cls._normalize_tool_args("read", {"filePath": r_path, "path": r_path}, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read", "arguments": json.dumps(args, ensure_ascii=False)},
                })
                clean_text = clean_text.replace(m.group(0), "")

        # 5. 匹配通用 <tool_name>...</tool_name> 与自闭合 XML 标签 <tool_name attr="val" />
        for t_name in tool_names:
            if t_name in ("bash", "write", "read", "terminal", "sh"):
                continue

            # 5.1 自闭合标签带属性: <tool_name attr="val" ... /> 或 <tool_name />
            sc_pattern = rf"<{t_name}(?:\s+([^>]*?))?\s*/>"
            for m in re.finditer(sc_pattern, clean_text, re.IGNORECASE):
                attr_str = m.group(1) or ""
                args = cls._parse_xml_attributes(attr_str)
                args = cls._normalize_tool_args(t_name, args, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": t_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                clean_text = clean_text.replace(m.group(0), "")

            # 5.2 成对标签带属性: <tool_name attr="val">body</tool_name>
            paired_attr_pattern = rf"<{t_name}\s+([^>]*?)>(.*?)</{t_name}>"
            for m in re.finditer(paired_attr_pattern, clean_text, re.DOTALL | re.IGNORECASE):
                attr_str = m.group(1)
                val = m.group(2).strip()
                args = cls._parse_xml_attributes(attr_str)
                child_tags = re.findall(r"<([a-zA-Z_][a-zA-Z0-9_-]*)>(.*?)</\1>", val, re.DOTALL)
                if child_tags:
                    for k, v in child_tags:
                        args[k] = v.strip()
                elif val:
                    try:
                        obj = json.loads(val)
                        if isinstance(obj, dict):
                            args.update(obj)
                    except Exception:
                        if "content" not in args and "cmd" not in args:
                            args["content"] = val

                args = cls._normalize_tool_args(t_name, args, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": t_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                clean_text = clean_text.replace(m.group(0), "")

            # 5.3 无属性标准成对标签: <tool_name>...</tool_name>
            pattern = rf"<{t_name}>(.*?)</{t_name}>"
            for m in re.finditer(pattern, clean_text, re.DOTALL | re.IGNORECASE):
                val = m.group(1).strip()
                child_tags = re.findall(r"<([a-zA-Z_][a-zA-Z0-9_-]*)>(.*?)</\1>", val, re.DOTALL)
                if child_tags:
                    args = {k: v.strip() for k, v in child_tags}
                else:
                    try:
                        args = json.loads(val)
                    except Exception:
                        args = {"input": val}
                args = cls._normalize_tool_args(t_name, args, tools)
                call_id = f"call_{uuid.uuid4().hex[:10]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": t_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
                clean_text = clean_text.replace(m.group(0), "")

        # 去除多余空行与残留代码块标签
        clean_text = re.sub(r"```(?:json)?\s*```", "", clean_text)
        clean_text = cls.sanitize_internal_tags(clean_text)
        return clean_text.strip(), tool_calls

    @classmethod
    def clean_tags(cls, text: str) -> str:
        """清洗内部特殊/工具调用标签（兼容 openai_routes 调用的历史接口）"""
        return cls.sanitize_internal_tags(text)

    @staticmethod
    def sanitize_internal_tags(text: str) -> str:
        """
        保证 <pulse> / <mood> / <reflect> 处于文本最开头（Hana 才能解析为心情卡片），
        并移除正文中后续多余的重复 <pulse> 块，防止泄露原始 XML 标签与 DSML 标签。
        """
        if not text:
            return text
        # 彻底移除可能残留的未闭合或漏网的 DSML 标签与特殊 Token
        SEP = r'[\s\u2581_\-]'
        PIPE = r'[\|｜]'
        # 彻底清洗 DSML 相关所有变体标签、闭合标签及孤立工具标签
        text = re.sub(r'<[\|｜\s]*/?[\|｜\s]*DSML[\|｜\s]*[^>]*>', "", text)
        text = re.sub(r'<[\|｜\s]*/*[\|｜]+[^>]*>', "", text)
        text = re.sub(r'</?(?:invoke|parameter|calls|tool_call|tool_calls)[^>]*>', "", text, flags=re.IGNORECASE)
        text = re.sub(rf'<+/?{PIPE}*tool{SEP}+(?:call|calls|sep)?{SEP}*(?:begin|end)?{PIPE}*>+', "", text, flags=re.IGNORECASE)
        # ★ 后缀必须可选：上游发的是 `专家模式暂不支持阅读链接，请使用快速模式`，
        # 但它**也会单独发前半句**（`StreamingToolParser.DISCLAIMERS` 里就同时列了
        # 长/短两个变体）。旧正则强制要求"请使用快速模式"跟在后面，
        # 于是**短变体在这条路径上清不掉、会原样泄漏给客户端**。
        # 这条漂移由 tests/test_streaming_fsm.py 的
        # `test_every_streaming_disclaimer_is_also_removed_by_tool_bridge` 抓出。
        text = re.sub(r'专家模式暂不支持阅读链接[，,]*\s*(?:请使用快速模式)?[。\n]*', '', text)
        text = re.sub(r'本回答由\s*AI\s*生成[，,]*内容仅供参考[，,]*请仔细甄别[。\n]*', '', text)
        pattern = re.compile(r"<(pulse|mood|reflect)>([\s\S]*?)</\1>", re.IGNORECASE)
        matches = list(pattern.finditer(text))
        if not matches:
            return text.strip()
        first_match = matches[0]
        first_tag_block = first_match.group(0).strip()
        cleaned_body = pattern.sub("", text).strip()
        return f"{first_tag_block}\n\n{cleaned_body}" if cleaned_body else first_tag_block

    @classmethod
    def check_zero_tolerance_suspicion(cls, text: str, tool_defs: List[Dict[str, Any]]) -> bool:
        """
        检查文本中是否包含未闭合或损坏的明确工具调用协议标签。
        只有在文本明确包含工具调用语法前缀（如 <tool_call、<|tool_calls、<invoke、[工具调用] 等），
        但状态机与解析层未能提取出任何合法工具调用时，才判定为“工具调用格式残缺异常”。
        坚决不误伤正常的 Markdown 代码块 (```python / ```bash / ```json) 或对话中提及的工具名称。
        """
        if not text:
            return False

        # 匹配明确的工具调用协议标签特征（带词边界或特定符号，严防误伤 <callback> 等合法代码）
        patterns = (
            r'<\s*tool_call\b',
            r'<\s*tool-call\b',
            r'<\s*tool_calls\b',
            r'<\s*invoke\b',
            r'<\s*function_call\b',
            r'<\s*/\s*tool_call\s*>',
            r'<\s*/\s*invoke\s*>',
            r'<[\|｜\s/]*DSML\b',
            r'<+[\|｜\s]*(?:tool[\s\u2581_\-]*(?:call|sep|calls)|invoke)',
            r'\[(?:call|tool_call|tool|工具调用|【工具调用)[:\s]',
        )
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return True

        return False

    @classmethod
    def salvage_content_from_thinking(cls, thinking_text: str) -> str:
        """
        当模型因单轮长度预算耗尽或思考循环导致正文为空时，
        从思考链 (reasoning_content) 中智能打捞出有效代码与最终推导结论。
        """
        if not thinking_text or not thinking_text.strip():
            return ""

        # 1. 查找所有 Markdown 代码块
        code_blocks = re.findall(r'(```(?:[a-zA-Z0-9_\-+]+)?\s*[\s\S]*?```)', thinking_text)

        # 筛选出最完整、有实际价值的代码块（优先选包含实现特征关键词，或者行数较多的）
        candidate_code = None
        for block in reversed(code_blocks):
            # 优先选择包含完整程序或核心逻辑的代码块
            if re.search(r'#include|int\s+main|def\s+|class\s+|function\s+|package\s+|public\s+class', block):
                candidate_code = block
                break
            if not candidate_code and len(block.strip().splitlines()) >= 5:
                candidate_code = block

        # 如果代码块含有行号前缀（例如思考过程中按行引用的 1: #include ... 或 1  #include），进行清洗规整
        if candidate_code:
            m = re.match(r'```([a-zA-Z0-9_\-+]*)\s*([\s\S]*?)```', candidate_code)
            if m:
                lang, code = m.group(1), m.group(2)
                lines = code.splitlines()
                non_empty = [ln for ln in lines if ln.strip()]
                if non_empty and sum(1 for ln in non_empty if re.match(r'^\s*\d+[:\s]', ln)) / len(non_empty) > 0.6:
                    lines = [re.sub(r'^\s*\d+[:\s]\s?', '', ln) for ln in lines]
                    candidate_code = f"```{lang}\n" + "\n".join(lines) + "\n```"

        # 2. 提取最后的推导思考结论（最后一段较为完整的句子或分析）
        # 若已有代码块，从尾部剔除代码块以避免重复和格式混乱
        tail = thinking_text[-1500:].strip()
        if candidate_code:
            tail = re.sub(r'```[\s\S]*?```', '', tail).strip()

        last_break = max(tail.rfind('。\n'), tail.rfind('。\r\n'), tail.rfind('。'), tail.rfind('\n\n'))
        if last_break > 100:
            conclusion = tail[:last_break + 1].strip()
        else:
            conclusion = tail

        # 如果既没有像样的代码块，结论又太短零碎，则判定无法打捞
        if not candidate_code and len(conclusion) < 80:
            return ""

        parts = [
            "【网关打捞提示】：模型在深度思考过程中耗尽了单轮长度限制，未及输出正文。网关已从思考链中提取最终推导出的代码与核心结论：\n"
        ]
        if candidate_code:
            parts.append("### 提取的代码实现\n" + candidate_code)

        if conclusion and len(conclusion) >= 20:
            parts.append("### 思考推导结论参考\n" + conclusion)

        return "\n\n".join(parts)



