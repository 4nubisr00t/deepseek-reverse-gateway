#!/usr/bin/env python3
"""
T16 / B1 · 被动指标报告（**只读，不发任何请求**）

用法：

    python tools/passive_report.py                 # 人类可读
    python tools/passive_report.py --json          # 机器可读
    python tools/passive_report.py --samples session.account_changed
    DSG_METRICS_FILE=/opt/ds-gateway/metrics.json python tools/passive_report.py

退出码（可直接当巡检用，**不产生任何流量**）：

    0  全部在阈值内
    2  某项超过 warn 阈值（该看一眼了）
    3  契约计数器非 0（`contract.*` —— 说明有代码绕过了唯一入口）

────────────────────────────────────────────────────────────────────
为什么这个工具**只有文件读取**，一行网络/子进程代码都没有
────────────────────────────────────────────────────────────────────
T16 的需求是"发现静默故障"。之前用金丝雀（主动发请求）去满足它，
代价是 5 个账号被禁言 24 小时 —— **需求没错，手段错了**。

所以这里把"不可能发流量"做成**结构性事实**而不是纪律：
  · 不 import requests / httpx / urllib / socket / aiohttp
  · 不 import subprocess（连 journalctl 都不调 —— 线上 journal 已 3GB，
    带时间窗的查询会超时，而且它是**靠中文文案解析**的，文案一改就静默失效）
  · 唯一输入是 `metrics.json`（计数器由请求路径在事件发生点自增）

`tests/test_passive_metrics.py` 里有一条 AST 断言守着上面这三条 ——
"我们不调网络"这种承诺必须是可执行的，不能只写在注释里。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.engine.passive_metrics import THRESHOLDS  # noqa: E402

METRICS_FILE = Path(os.environ.get("DSG_METRICS_FILE", str(_REPO_ROOT / "metrics.json")))

#: 必须恒为 0 的契约计数器。非 0 = 有人绕过了唯一入口，不是"概率事件"。
CONTRACT_COUNTERS = (
    "contract.empty_model",
    "contract.unknown_upstream_kind",
)


def load_counters(path: Optional[Path] = None) -> Dict[str, int]:
    p = Path(path) if path else METRICS_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[passive_report] 读取 {p} 失败: {e}", file=sys.stderr)
        return {}
    raw = data.get("counters") or {}
    out: Dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _ratio(num: int, den: int) -> Optional[float]:
    """返回 None 表示**无数据**。

    这里刻意用 None 而不是 0.0：一个恒为 0 的比率有两种可能 ——
    "真的没发生"和"统计根本没在跑"。金丝雀时代最大的教训就是
    "看起来一切正常"最危险，所以"无数据"必须是一个能显示出来的状态。
    """
    if den <= 0:
        return None
    return num / den


def compute_rates(c: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    g = lambda k: int(c.get(k, 0))            # noqa: E731

    upstream_total = sum(g(k) for k in (
        "upstream.ok", "upstream.rate_limited", "upstream.context_exhausted",
        "upstream.safety_blocked", "upstream.error",
    ))
    reuse_total = g("session.reused") + g("session.new_session_created")
    tool_den = g("tool.emitted")

    return {
        "answer_misplacement_rate": {
            "value": _ratio(g("session.ambiguous_new"), g("session.reuse_allowed")),
            "numerator": g("session.ambiguous_new"),
            "denominator": g("session.reuse_allowed"),
            "meaning": "指纹歧义 → 网关主动放弃复用（防串线生效的次数占比）",
        },
        "account_changed_rate": {
            "value": _ratio(g("session.account_changed"), g("session.bound")),
            "numerator": g("session.account_changed"),
            "denominator": g("session.bound"),
            "meaning": "同一会话被换到另一个账号 → 账号级 file_id 可能失效（A1 的验收指标）",
        },
        "safety_block_rate": {
            "value": _ratio(g("upstream.safety_blocked"), upstream_total),
            "numerator": g("upstream.safety_blocked"),
            "denominator": upstream_total,
            "meaning": "上游内容安全拦截占比（不是网关故障，但要能看见）",
        },
        "tool_format_error_rate": {
            "value": _ratio(g("tool.json_parse_failed") + g("tool.safety_net"), tool_den),
            "numerator": g("tool.json_parse_failed") + g("tool.safety_net"),
            "denominator": tool_den,
            "meaning": "工具参数解析失败 + 状态机漏抓兜底，占已产出工具调用的比例",
        },
        "reuse_rate": {
            "value": _ratio(g("session.reused"), reuse_total),
            "numerator": g("session.reused"),
            "denominator": reuse_total,
            "meaning": "上游会话复用率。过低 = 每轮新建会话 + 每轮重传全量上下文",
        },
    }


def evaluate(rates: Dict[str, Dict[str, Any]], counters: Dict[str, int]):
    """返回 (warnings, contract_hits)。

    `floor` 的语义：比率**低于**它就说明这个指标没在正常工作（而不是"很健康"）。
    例如复用率低于 0.5 = 大部分请求都在新建上游会话，那是要花钱的。
    """
    warnings = []
    for name, spec in THRESHOLDS.items():
        r = rates.get(name)
        if r is None:
            continue
        v = r["value"]
        label = name
        if v is None:
            continue
        warn = spec.get("warn", 0.0)
        floor = spec.get("floor", 0.0)
        if name == "reuse_rate":
            if v < floor:
                warnings.append(f"{label}: {v:.2%} < 下限 {floor:.0%}（会话复用可能失效）")
        elif v > warn:
            warnings.append(f"{label}: {v:.2%} > 阈值 {warn:.0%}")
    contract_hits = [k for k in CONTRACT_COUNTERS if int(counters.get(k, 0)) > 0]
    return warnings, contract_hits


def format_report(counters: Dict[str, int], rates: Dict[str, Dict[str, Any]],
                  samples: Optional[str] = None) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("ds-gateway 被动指标报告（只读；本工具不产生任何上游请求）")
    lines.append(f"数据源: {METRICS_FILE}")
    lines.append("=" * 78)

    if not counters:
        lines.append("")
        lines.append("⚠️ 计数器是空的 —— 两种可能，必须区分开：")
        lines.append("   ① 服务尚未重启到带 B1 的版本（计数器从重启后开始累计）")
        lines.append("   ② 真的没有任何请求")
        return "\n".join(lines)

    lines.append("")
    lines.append("── 比率 ──")
    for name, r in rates.items():
        v = r["value"]
        shown = "无数据（分母为 0）" if v is None else f"{v:>7.2%}"
        lines.append(f"  {name:<26} {shown}   ({r['numerator']}/{r['denominator']})")
        lines.append(f"  {'':<26} └ {r['meaning']}")

    lines.append("")
    lines.append("── 计数器明细 ──")
    for k in sorted(counters):
        lines.append(f"  {k:<34} {counters[k]}")

    contract = {k: counters.get(k, 0) for k in CONTRACT_COUNTERS}
    lines.append("")
    lines.append("── 契约计数器（必须恒为 0） ──")
    for k, v in contract.items():
        mark = "✅" if v == 0 else "❌"
        lines.append(f"  {mark} {k:<34} {v}")

    if samples:
        lines.append("")
        lines.append(f"── 样本（最近 20 条）：{samples} ──")
        data = {}
        try:
            data = json.loads(METRICS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        for s in (data.get("counter_samples") or {}).get(samples, []):
            lines.append(f"  {s}")

    warnings, hits = evaluate(rates, counters)
    lines.append("")
    lines.append("── 结论 ──")
    if not warnings and not hits:
        lines.append("  ✅ 全部在阈值内")
    for w in warnings:
        lines.append(f"  ⚠️ {w}")
    for h in hits:
        lines.append(f"  ❌ 契约计数器非 0: {h}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ds-gateway 被动指标报告（只读，无网络）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--file", default=None, help="metrics.json 路径（默认跟随 DSG_METRICS_FILE）")
    ap.add_argument("--samples", default=None, help="打印某个计数器的最近样本")
    args = ap.parse_args(argv)

    global METRICS_FILE
    if args.file:
        METRICS_FILE = Path(args.file)

    counters = load_counters(METRICS_FILE)
    rates = compute_rates(counters)
    warnings, hits = evaluate(rates, counters)

    if args.json:
        print(json.dumps({
            "source": str(METRICS_FILE),
            "counters": counters,
            "rates": {k: {**v} for k, v in rates.items()},
            "warnings": warnings,
            "contract_hits": hits,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_report(counters, rates, samples=args.samples))

    if hits:
        return 3
    if warnings:
        return 2
    return 0


if __name__ == "__main__":
    # 显式 guard：`pytest tools/` 会**绕过 norecursedirs** 直接收集本文件，
    # 没有这个 guard 就会在收集阶段执行报告（这条是实测踩出来的）。
    sys.exit(main())
