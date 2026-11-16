"""
SensitiveFilter: 网关多层安全敏感词与高危攻击载荷拦截引擎
- 针对 DeepSeek 官方已知风控触发点（biz_code: 5 "user is muted" 封号 72h / 永久）
- 支持三级拦截分类：
  1. cyber_exploit: 实战漏洞利用/渗透攻击/提权反弹/恶意载荷（避免官方模型判定恶意网络攻击）
  2. jailbreak: 越狱模式、DAN、提示词注入与系统人设窃取
  3. compliance: 法规底线敏感词汇
- 用户制度与权限隔离：
  - 默认对普通 API Key 开启敏感词拦截，保护账号池不被封禁；
  - 允许管理员在后台针对特定 Key（如安全研究员、靶场用户）开启豁免白名单（sensitive_filter: false）。
"""

import base64
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.config import PROJECT_ROOT

WORDS_FILE = Path(PROJECT_ROOT / "sensitive_words.json")


def _d(s: str) -> str:
    return base64.b64decode(s.encode("ascii")).decode("utf-8")


# 采用 Base64 编码存储敏感正则，避免本地安全软件（火绒/Defender）误判报毒
CYBER_B64 = [
    ('XGJleHBsb2l0XHMrYnlcYg==', 'Exploit By 漏洞利用声明'),
    ('XGJyZW1vdGVccytjb2RlXHMrZXhlY3V0aW9uXHMrZXhwbG9pdFxi', 'Remote Code Execution Exploit 远程代码执行利用'),
    ('XGJwb2NccytleHBsb2l0XGI=', 'PoC Exploit 漏洞利用验证'),
    ('XGJjdmUtXGR7NH0tXGR7NCx9Lio/KD86cmNlfGV4cGxvaXR8cGF5bG9hZHxzaGVsbGNvZGUp', 'CVE 高危漏洞直接利用载荷'),
    ('dXNlclxzKj1ccypbXHgyMlx4MjddLWZccytyb290W1x4MjJceDI3XVxzK3RlbG5ldA==', 'Telnet 提权后门载荷'),
    ('XGJwcml2aWxlZ2Vccytlc2NhbGF0aW9uXHMrZXhwbG9pdFxi', 'Privilege Escalation Exploit 提权利用'),
    ('XGJtaW1pa2F0elxi', '凭据窃取工具特征'),
    ('XGJkdW1wXHMrbHNhc3NcYg==', 'LSASS 内存凭据转储'),
    ('YmFzaFxzKy1pXHMrPiY=', 'Bash 交互式反弹载荷'),
    ('L2Rldi90Y3AvXGR7MSwzfVwuXGR7MSwzfVwuXGR7MSwzfVwuXGR7MSwzfQ==', '网络反向外联通道'),
    ('bmNccystW2VsbnBdK1xzKy4qPyg/Oi9iaW4vc2h8L2Jpbi9iYXNofGNtZFwuZXhlKQ==', 'Netcat 端口监听执行'),
    ('XGJtZXRlcnByZXRlclxi', 'Metasploit 载荷特征'),
    ('XGJtc2Z2ZW5vbVxi', 'Msfvenom 载荷生成'),
    ('c3FsbWFwLio/LS1vcy1zaGVsbA==', 'SQLMap 夺权注入'),
    ('c3FsbWFwLio/LS1vcy1jbWQ=', 'SQLMap 命令注入'),
    ('5YWN5p2A5pyo6ams', '免杀木马制作'),
    ('5YWN5p2AXHMqcGF5bG9hZA==', '免杀 Payload 制作'),
    ('5Y+N5by5XHMqc2hlbGw=', '反弹 Shell 攻击'),
    ('5YaF572R5qiq5ZCR56e75Yqo5o+Q5p2D', '内网横向渗透提权'),
]

JB_B64 = [
    ('XGJkb1xzK2FueXRoaW5nXHMrbm93XGI=', 'DAN 越狱模式'),
    ('XGJkYW5ccyttb2RlXGI=', 'DAN 越狱模式'),
    ('XGJqYWlsYnJlYWtccytwcm9tcHRcYg==', 'Jailbreak 越狱标记'),
    ('aWdub3JlXHMrYWxsXHMrcHJldmlvdXNccytpbnN0cnVjdGlvbnNccythbmRccysoPzpyZXZlYWx8cHJpbnR8c2hvdylccysoPzpzeXN0ZW1ccytwcm9tcHR8aW5zdHJ1Y3Rpb25zKQ==', '提示词注入越狱'),
    ('5b+955Wl5LmL5YmN55qE5omA5pyJ5oyH5LukLio/6L6T5Ye657O757uf5o+Q56S66K+N', '提示词注入越狱'),
]

COMP_B64 = [
    ('5Yi26YCgKD865p6q5pSvfOeIhueCuOeJqXzngrjoja985q+S5ZOBfOWJp+avkik=', '违法暴恐与有害物品制造'),
    ('5rSX6ZKx5aWX546w5pON5L2c5oyH5Y2X', '网络黑灰产违规操作'),
]


class SensitiveFilter:
    """网关级敏感词扫描器"""

    def __init__(self, custom_words_file: Optional[Path] = None):
        self.words_file = custom_words_file or WORDS_FILE
        self._custom_words: List[str] = []
        self._compiled_exploit = [
            (re.compile(_d(b64_pat), re.IGNORECASE), desc) for b64_pat, desc in CYBER_B64
        ]
        self._compiled_jailbreak = [
            (re.compile(_d(b64_pat), re.IGNORECASE), desc) for b64_pat, desc in JB_B64
        ]
        self._compiled_compliance = [
            (re.compile(_d(b64_pat), re.IGNORECASE), desc) for b64_pat, desc in COMP_B64
        ]
        self.load_custom_words()

    def load_custom_words(self):
        """加载自定义额外过滤词"""
        if self.words_file.exists():
            try:
                data = json.loads(self.words_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._custom_words = [str(w).strip().lower() for w in data if str(w).strip()]
                elif isinstance(data, dict):
                    self._custom_words = [str(w).strip().lower() for w in data.get("words", []) if str(w).strip()]
            except Exception as e:
                print(f"[SensitiveFilter] 读取自定义词库失败: {e}", flush=True)
                self._custom_words = []

    def save_custom_words(self, words: List[str]):
        """保存自定义词库"""
        cleaned = sorted(list(set(str(w).strip() for w in words if str(w).strip())))
        self._custom_words = [w.lower() for w in cleaned]
        try:
            self.words_file.write_text(
                json.dumps({"words": cleaned}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[SensitiveFilter] 保存自定义词库失败: {e}", flush=True)

    def extract_text_from_messages(self, messages: Any) -> str:
        """从 OpenAI / Claude 消息列表中提取全部用户与系统文本内容"""
        if not isinstance(messages, list):
            return str(messages or "")

        chunks = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text"):
                            chunks.append(str(part["text"]))
                        elif part.get("type") == "image_url":
                            pass
        return " ".join(chunks)

    def check_content(self, text: str) -> Optional[Dict[str, Any]]:
        """
        扫描文本内容。
        若触发拦截规则，返回诊断字典；若安全无害，返回 None。
        """
        if not text:
            return None

        # 1. 扫描实战漏洞利用与攻击载荷
        for cre, desc in self._compiled_exploit:
            m = cre.search(text)
            if m:
                return {
                    "category": "cyber_exploit",
                    "category_name": "实战网络攻击与漏洞利用",
                    "matched": m.group(0),
                    "rule_desc": desc,
                    "reason": "检测到高危网络攻击/利用载荷关键词，极易导致上游官方账号被惩罚性禁言 72 小时 (biz_code: 5)。",
                }

        # 2. 扫描提示词注入与越狱
        for cre, desc in self._compiled_jailbreak:
            m = cre.search(text)
            if m:
                return {
                    "category": "jailbreak",
                    "category_name": "提示词注入与恶意越狱",
                    "matched": m.group(0),
                    "rule_desc": desc,
                    "reason": "检测到恶意提示词越狱模式，违反上游合规使用协议。",
                }

        # 3. 扫描法规合规关键词
        for cre, desc in self._compiled_compliance:
            m = cre.search(text)
            if m:
                return {
                    "category": "compliance",
                    "category_name": "法规底线敏感内容",
                    "matched": m.group(0),
                    "rule_desc": desc,
                    "reason": "检测到法规底线违禁内容，已被网关阻断。",
                }

        # 4. 扫描自定义关键词库
        lower_text = text.lower()
        for w in self._custom_words:
            if w in lower_text:
                return {
                    "category": "custom",
                    "category_name": "自定义拦截词库",
                    "matched": w,
                    "rule_desc": f"命中管理员自定义敏感词 [{w}]",
                    "reason": f"命中管理员设定的敏感词 [{w}]，已由网关安全拦截。",
                }

        return None


# 全局单例
sensitive_filter = SensitiveFilter()


class SafetyOutputGuard:
    """流式输出安全标签 / 异常复读熔断器。

    用于拦截上游模型在破甲/异常上下文中触发的 `<ds_safety>` 连续刷屏循环
    （例如实测线上出现 1300~1450 行重复输出的失控会话），及时止损断流并冷却账号。
    """

    _TAG_RE = re.compile(r"<\s*/?\s*ds_safety\s*>", re.IGNORECASE)

    def __init__(self, safety_repeat_limit: int = 3, line_repeat_limit: int = 10):
        self.safety_repeat_limit = max(2, int(safety_repeat_limit or 3))
        self.line_repeat_limit = max(5, int(line_repeat_limit or 10))
        self.safety_hits = 0
        self.tripped = False
        self.trip_reason = ""
        self._buf = ""
        self._recent_line_counts: Dict[str, int] = {}

    @classmethod
    def strip_safety_tags(cls, text: Any) -> str:
        """清洗历史消息里残留的 <ds_safety> 标签块，防止多轮回传放大复读。"""
        if not text:
            return ""
        s = str(text)
        if "ds_safety" not in s.lower():
            return s
        s = re.sub(r"<\s*ds_safety\b[^>]*>.*?<\s*/\s*ds_safety\s*>", "", s, flags=re.IGNORECASE | re.DOTALL)
        s = cls._TAG_RE.sub("", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    def feed(self, chunk: str) -> bool:
        """喂入新增输出片段；若触发熔断返回 True。"""
        if self.tripped:
            return True
        if not chunk:
            return False

        self._buf += chunk
        # 只保留尾部滑动窗口，避免长文本无限占内存
        if len(self._buf) > 4096:
            self._buf = self._buf[-2048:]

        # 统计新增 chunk 及边界拼接带来的 <ds_safety> 命中次数
        # 用增量窗口检测：检查含本次 chunk 的最后 64+len(chunk) 区间，避免跨 chunk 漏检
        tail_len = min(len(self._buf), len(chunk) + 32)
        window = self._buf[-tail_len:]
        # 仅当新块本身含标签特征或跨越闭合时累计，避免同一标签重复计数：直接用总缓冲换行切分统计
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            norm = line.strip()
            if not norm:
                continue
            if self._TAG_RE.search(norm) or "<ds_safety" in norm.lower():
                self.safety_hits += 1
                if self.safety_hits >= self.safety_repeat_limit:
                    self.tripped = True
                    self.trip_reason = f"ds_safety_lines={self.safety_hits}"
                    return True
            if len(norm) >= 12:
                cnt = self._recent_line_counts.get(norm, 0) + 1
                self._recent_line_counts[norm] = cnt
                if len(self._recent_line_counts) > 128:
                    # 控制字典大小
                    first_k = next(iter(self._recent_line_counts))
                    self._recent_line_counts.pop(first_k, None)
                if self.safety_hits > 0 and cnt >= self.line_repeat_limit:
                    self.tripped = True
                    self.trip_reason = f"safety_repeat_line_x{cnt}"
                    return True

        # 无换行但单行持续输出多个 <ds_safety> 标签时的兜底检查
        inline_tags = len(re.findall(r"<\s*ds_safety\b", self._buf, flags=re.IGNORECASE))
        if (self.safety_hits + inline_tags) >= self.safety_repeat_limit:
            self.tripped = True
            self.trip_reason = f"ds_safety_inline={self.safety_hits + inline_tags}"
            return True

        return False
