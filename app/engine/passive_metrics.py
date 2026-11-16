"""
T16 / B1 · 被动指标：**只用网关已经看到的东西**算比率，不发任何上游请求。

────────────────────────────────────────────────────────────────────
为什么要有这个模块（不是"多加几个日志"）
────────────────────────────────────────────────────────────────────
T16 的原始需求是"发现静默故障"。金丝雀（主动合成流量）试图用**发请求**来满足它，
代价是 5 个账号被禁言 24 小时 —— 因为"每 10 分钟、内容雷同、轮换账号"这个形态
本身就是异常流量。**需求是对的，手段是错的。**

被动口径的立足点：网关每个请求都已经看到了判断结果（会话身份怎么定的、
有没有换号、上游第一帧是什么、工具参数解析成不成功）。
把这些结果**在发生点**记进计数器，就能算出比率，而不需要额外发一个包。

为什么不用日志抓取（`journalctl | grep`）：
  · 线上 journal 已 3GB，带时间窗的查询会超时
  · 解析依赖**中文文案**，文案一改就静默失效 —— 表现为"指标恒为 0，看着一切正常"，
    这恰恰是 T16 要防的那种故障

────────────────────────────────────────────────────────────────────
这个模块算出来的四个比率（以及每个比率的"有牙"判据）
────────────────────────────────────────────────────────────────────
1. **答案错位率** = `session.ambiguous_new` / 有历史轮次的请求数
   指纹歧义 → 网关**主动放弃复用**（宁可多建一次会话也不串线）。
   分母口径见 `session.reuse_allowed`。这是 T1/T21"跨项目串线"的残余风险计量：
   它必须很小（歧义说明"同开头的不同会话"很多），但**不能为 0 就以为没问题** ——
   为 0 只说明没撞上，不代表逻辑在跑（见 `THRESHOLDS` 里的地板值）。

2. **换号率** = `session.account_changed` / `session.bound`
   **同一会话被分到了另一个账号。** `chat_session_id` / `file_id` 实测不可跨账号
   （`biz_code:1 invalid chat session id`），所以这个数每 +1 就代表
   "这条对话历史上引用的图片可能已经失效"。这是 A1 的验收指标，应当接近 0。

3. **安全拦截率** = `upstream.safety_blocked` / `upstream.*` 总数
   上游内容审核拦截。它**不是网关故障**，但当前被混进 5xx 统计里，
   既抬高失败率、又掩盖真正的故障。单独计数之后才谈得上"比率"。

4. **工具格式错误率** = (`tool.json_parse_failed` + `tool.safety_net`) / `tool.emitted`
   参数 JSON 解析失败、以及状态机漏抓后由兜底捞回（`[Gateway-Safety-Net]`）。
   后者是最严重的一档：说明流式状态机**漏掉了一整个工具调用**。

以及两个"必须恒为 0"的**契约计数器**（不是比率，是断言）：
  · `contract.empty_model` —— 响应里的 `model` 字段为空。
    `normalize_model_id()` 保证永远归一到 `CANONICAL_MODEL_ID`，所以它必须恒为 0。
  · `contract.unknown_upstream_kind` —— 分类器返回了调用方没处理的 kind。
"""

from typing import Any, Dict, Optional

from app.engine.metrics import metrics_engine


def _bump(_event: str, /, **fields):
    """统一入口：计数器失败绝不影响业务（metrics_engine.bump 内部已吞异常）。

    ⚠️ `_event` 是**位置限定参数**：`fields` 里本来就可能带一个叫 `name` 的键
    （`tool.emitted` 要记工具名），用普通参数声明会撞成
    `TypeError: got multiple values for argument`，而那个异常会被下面的
    `except` 吞掉 → 计数器永远为 0 且**毫无声响**。
    """
    try:
        metrics_engine.bump(_event, **fields)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 1. 会话身份：错位 / 换号 / 重建
# ─────────────────────────────────────────────────────────────
def note_session_resolution(
    conv_src: str,
    cached_session: Optional[Any],
    account_label: str,
    reuse_allowed: bool,
) -> None:
    """在一个请求**已经拿到账号、还没决定复用**的时刻记录会话身份。

    必须落在 `acquire_account` 之后：`account_changed` 只有两边都拿到才成立。
    """
    if reuse_allowed:
        _bump("session.reuse_allowed")
    if cached_session is not None:
        _bump("session.bound")
        bound = getattr(cached_session, "account_label", None)
        if bound and account_label and bound != account_label:
            _bump("session.account_changed", bound=bound, got=account_label,
                  conv_src=conv_src)
        if not getattr(cached_session, "deepseek_session_id", None):
            # 绑定还在（账号粘性保留）但上游会话已被丢弃 —— A1 的正常路径（压缩），
            # 或 context_exhausted 之后的清理。用来核对 A2 是否按预期工作。
            _bump("session.stickiness_lost", bound=bound, conv_src=conv_src)

    if conv_src == "ambiguous-new":
        _bump("session.ambiguous_new")
    elif conv_src == "cid-prefix-changed" or conv_src.startswith("cid-prefix-changed("):
        _bump("session.prefix_rebuild", bound=getattr(cached_session, "account_label", None))
    elif conv_src in ("cid-branched", "prefix-branched", "resp-id-branched") or conv_src.startswith("cid-branched("):
        _bump("session.branched", bound=getattr(cached_session, "account_label", None), conv_src=conv_src)
    elif conv_src == "new":
        _bump("session.new")


def note_session_reuse(reused: bool, conv_src: str = "") -> None:
    """复用闸门最终的决定。`session.reused / (reused + session.new)` 就是复用率。"""
    _bump("session.reused" if reused else "session.new_session_created", conv_src=conv_src)


# ─────────────────────────────────────────────────────────────
# 2. 上游分类：限流 / 上下文超限 / 安全拦截 / 普通错误
# ─────────────────────────────────────────────────────────────
#: kind → 计数器名。分类器新增 kind 而这里没登记时，会落到
#: `contract.unknown_upstream_kind` —— 契约计数器，避免"新错误类型没人统计"。
_KIND_COUNTERS = {
    "ok": "upstream.ok",
    "rate_limited": "upstream.rate_limited",
    "context_exhausted": "upstream.context_exhausted",
    "session_broken": "upstream.session_broken",
    "safety_blocked": "upstream.safety_blocked",
    "error": "upstream.error",
}


def note_preflight(decision: Any) -> None:
    """记录 `classify_upstream_preflight()` 的结论。

    **只观测，不改行为**：调用方对 kind 的处理完全不变，所以这个调用
    是纯粹的旁路。把它插在分类之后一行，任何一条路径（流式/非流式/
    OpenAI/Claude）都不会漏统。
    """
    kind = getattr(decision, "kind", None)
    name = _KIND_COUNTERS.get(kind)
    if name is None:
        _bump("contract.unknown_upstream_kind", kind=kind)
        return
    detail = getattr(decision, "detail", "") or ""
    _bump(name, detail=str(detail)[:120])


# ─────────────────────────────────────────────────────────────
# 3. 工具调用格式
# ─────────────────────────────────────────────────────────────
def note_tool_emitted(name: str = "", count: int = 1) -> None:
    """成功产出一个（或一批）工具调用 —— 工具格式错误率的**分母**。

    没有分母的比率是没法解释的：`tool.json_parse_failed = 3` 到底是
    "3/4 全错了"还是"3/10000 可以忽略"，只有这里能回答。
    """
    _bump("tool.emitted", name=name, count=count)


def note_tool_json_parse_failed(body_snippet: str = "") -> None:
    """工具参数 JSON 解析失败（streaming_parser）。"""
    _bump("tool.json_parse_failed", body=str(body_snippet)[:120])


def note_tool_safety_net(count: int = 1) -> None:
    """流式状态机漏抓，靠正文全文兜底才捞回工具调用。

    这是工具格式里最严重的一档：说明状态机**整体漏掉**了一个工具调用，
    而不是某个参数写歪。
    """
    _bump("tool.safety_net", count=count)


def note_tool_cot_salvage(count: int = 1, nonstream: bool = False) -> None:
    """从思考链（reasoning_content）里把工具调用打捞回来。"""
    _bump("tool.cot_salvage", count=count, nonstream=nonstream)


def note_tool_reflection(round_index: int = 0, nonstream: bool = False) -> None:
    """检测到非标工具调用意图 → 打回让模型自己改。"""
    _bump("tool.reflection", round=round_index, nonstream=nonstream)


# ─────────────────────────────────────────────────────────────
# 4. 契约计数器（必须恒为 0）
# ─────────────────────────────────────────────────────────────
def note_empty_model() -> None:
    """响应里的 `model` 为空。`normalize_model_id()` 保证不可能发生 —— 
    所以这个计数器一旦非 0，说明有人绕过了唯一入口。"""
    _bump("contract.empty_model")


def _install_parser_hook() -> None:
    """把计数器接到流式解析器上。

    `streaming_parser` 刻意零 app 依赖（纯标准库状态机），所以它不 import 本模块，
    而是等着被装上一个回调。装在这里而不是各个路由里：本模块的职责就是观测，
    任何导入了它的进程都自动获得这套计数，不会出现"某条路径漏装"。
    """
    try:
        from app.engine import streaming_parser

        def _hook(_event, /, **fields):
            # 位置限定参数：`fields` 里可能带一个叫 `name` 的键（工具名），
            # 用普通参数声明会在调用时撞成 TypeError，而它会被吞掉 →
            # 计数器永远为 0 且没有任何声响。
            _bump(_event, **fields)

        streaming_parser.set_metric_hook(_hook)
    except Exception as e:
        print(f"[PassiveMetrics] 未能挂上解析器计数器（已忽略）: {e}")


_install_parser_hook()


#: 报告用的阈值。`warn` 是"该看一眼了"，`floor` 是"低于它就说明这个指标没在跑"。
#:
#: 为什么要有 `floor`：一个永远为 0 的比率有两种可能 —— **真的没发生**，
#: 或者**统计本身失效了**。金丝雀时代的教训是"看起来一切正常"最危险，
#: 所以地板值强迫报告去区分这两种情况（分母为 0 时必须显式报"无数据"）。
THRESHOLDS: Dict[str, Dict[str, float]] = {
    "answer_misplacement_rate": {"warn": 0.05, "floor": 0.0},
    "account_changed_rate": {"warn": 0.01, "floor": 0.0},
    "safety_block_rate": {"warn": 0.02, "floor": 0.0},
    "tool_format_error_rate": {"warn": 0.02, "floor": 0.0},
    "reuse_rate": {"warn": 0.0, "floor": 0.5},
}
