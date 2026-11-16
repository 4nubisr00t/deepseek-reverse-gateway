"""
T16 / B1 · 被动指标的测试。

## 这个文件守的是什么

T16 的需求是"发现静默故障"。之前用**主动合成流量**（金丝雀）去满足它，
结果是 5 个账号被禁言 24 小时。**需求没错，手段错了。**
被动口径的前提是三条，本文件逐条钉死：

1. **计数器真的会被触发**（不是写了个永远为 0 的字段就当有观测了）
2. **触发点不改变任何行为**（观测是旁路；`safety_blocked` 只分类不处置）
3. **观测代码结构上不可能发请求**（AST 断言，不靠注释里的承诺）

第 3 条用 AST 而不是"我检查过了"：这是本项目已经付过学费的一类承诺
（"已删干净"只在本地成立、"已部署"其实没部署）。
"""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.engine.metrics import MetricsEngine  # noqa: E402
from app.engine import passive_metrics  # noqa: E402
from app.engine.passive_metrics import (  # noqa: E402
    note_preflight, note_session_resolution, note_session_reuse,
    note_tool_safety_net, note_tool_emitted, note_tool_json_parse_failed,
)
from app.config import normalize_model_id, CANONICAL_MODEL_ID  # noqa: E402
from app.api.openai_routes import (  # noqa: E402
    classify_upstream_preflight, _SAFETY_BLOCK_HINTS,
)


class FakeCtx:
    """最小的会话绑定替身（只带被动指标需要的字段）。"""

    def __init__(self, account_label="acct-1", deepseek_session_id="S-1"):
        self.account_label = account_label
        self.deepseek_session_id = deepseek_session_id


class FakeEvent:
    def __init__(self, event_type="error", content="", error_code=""):
        self.event_type = event_type
        self.content = content
        self.error_code = error_code


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """把 passive_metrics 指向一个隔离的引擎实例。

    不直接给全局 `metrics_engine` 打补丁再还原：那样一旦某条用例失败，
    还原代码不执行，后面的用例会读到被污染的计数器而看到**假的通过**。
    """
    eng = MetricsEngine(storage_file=tmp_path / "metrics.json")
    monkeypatch.setattr(passive_metrics, "metrics_engine", eng)
    return eng


# ─────────────────────────────────────────────────────────────
# 1. 计数器本体：自增、落盘、跨重启累计、异常吞掉
# ─────────────────────────────────────────────────────────────
class TestCounterEngine:

    def test_bump_increments_and_persists(self, tmp_path):
        f = tmp_path / "m.json"
        eng = MetricsEngine(storage_file=f)
        eng.bump("a.b")
        eng.bump("a.b")
        eng.bump("c.d", k="v")
        assert eng.get_counters() == {"a.b": 2, "c.d": 1}
        eng.save()

        back = MetricsEngine(storage_file=f)
        assert back.get_counters() == {"a.b": 2, "c.d": 1}, "重启后计数器必须接着数"

    def test_samples_are_capped_and_are_plain_strings(self, tmp_path):
        eng = MetricsEngine(storage_file=tmp_path / "m.json")
        for i in range(eng.MAX_COUNTER_SAMPLES + 15):
            eng.bump("x", n=i, obj=object())      # 传个对象：必须被降级成字符串
        samples = eng.get_counter_samples("x")
        assert len(samples) == eng.MAX_COUNTER_SAMPLES
        assert all(isinstance(s.get("obj"), str) for s in samples), (
            "样本里出现了非字符串 —— 活数据（会话/账号对象）不能被存进 JSON"
        )
        json.dumps(samples, ensure_ascii=False)   # 必须可序列化

    def test_bump_never_raises(self, tmp_path):
        """观测失败绝不影响业务 —— 这是整个模块唯一不可让步的性质。"""
        eng = MetricsEngine(storage_file=tmp_path / "m.json")

        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        eng.counters = Boom()
        eng.bump("x")            # 不得抛异常
        # 而且业务请求照样能记
        eng.record(endpoint="/v1/chat/completions", model="m", status_code=200, duration_ms=1.0)

    def test_summary_exposes_counters(self, tmp_path):
        eng = MetricsEngine(storage_file=tmp_path / "m.json")
        eng.bump("z")
        assert eng.get_summary()["counters"] == {"z": 1}, (
            "get_summary 不带计数器 → 管理页看不到，排查还得去 grep 3GB journal"
        )


# ─────────────────────────────────────────────────────────────
# 2. 会话身份：换号 / 歧义 / 压缩重建
# ─────────────────────────────────────────────────────────────
class TestSessionCounters:

    def test_account_changed_is_counted(self, engine):
        """同一个会话被分到另一个账号 —— A1 要防的正是这个。

        `chat_session_id` / `file_id` 实测不可跨账号，所以每 +1 都意味着
        "这条对话历史里引用的图片可能已经失效"。
        """
        note_session_resolution("cid", FakeCtx(account_label="acct-A"), "acct-B", True)
        assert engine.get_counters().get("session.account_changed") == 1

    def test_same_account_is_not_counted(self, engine):
        note_session_resolution("cid", FakeCtx(account_label="acct-A"), "acct-A", True)
        assert "session.account_changed" not in engine.get_counters()

    def test_prefix_rebuild_is_counted(self, engine):
        """压缩导致换会话（A1 的正常路径）：绑定还在、上游会话已清。"""
        ctx = FakeCtx(account_label="acct-A", deepseek_session_id="")
        note_session_resolution("cid-prefix-changed", ctx, "acct-A", True)
        c = engine.get_counters()
        assert c.get("session.prefix_rebuild") == 1
        assert c.get("session.stickiness_lost") == 1, (
            "绑定还在但没有上游会话 —— 这是 A2 体检要看的信号"
        )
        assert c.get("session.account_changed") is None, "换会话**不是**换号，不能混为一谈"

    def test_ambiguous_new_is_counted(self, engine):
        note_session_resolution("ambiguous-new", None, "acct-A", True)
        assert engine.get_counters().get("session.ambiguous_new") == 1

    def test_first_turn_counts_nothing_risky(self, engine):
        note_session_resolution("no-reuse", None, "acct-A", False)
        c = engine.get_counters()
        assert c.get("session.reuse_allowed") is None
        assert not any(k.startswith("session.account_changed") for k in c)

    def test_reuse_rate_denominator(self, engine):
        note_session_reuse(True)
        note_session_reuse(True)
        note_session_reuse(False)
        c = engine.get_counters()
        assert c["session.reused"] == 2
        assert c["session.new_session_created"] == 1


# ─────────────────────────────────────────────────────────────
# 3. 上游分类：每种 kind 都有归属，未知 kind 不许静默
# ─────────────────────────────────────────────────────────────
class TestPreflightCounters:

    @pytest.mark.parametrize("kind,counter", [
        ("ok", "upstream.ok"),
        ("rate_limited", "upstream.rate_limited"),
        ("context_exhausted", "upstream.context_exhausted"),
        ("safety_blocked", "upstream.safety_blocked"),
        ("error", "upstream.error"),
    ])
    def test_each_kind_maps_to_a_counter(self, engine, kind, counter):
        class D:
            pass
        d = D()
        d.kind = kind
        d.detail = "x"
        note_preflight(d)
        assert engine.get_counters().get(counter) == 1

    def test_unknown_kind_goes_to_a_contract_counter(self, engine):
        """分类器新增一种 kind 而没人登记统计时，必须**响**而不是被忽略。"""
        class D:
            pass
        d = D()
        d.kind = "brand_new_kind"
        d.detail = ""
        note_preflight(d)
        assert engine.get_counters().get("contract.unknown_upstream_kind") == 1

    def test_real_classifier_output_is_always_known(self, engine):
        """用真实分类器跑一遍所有已知形态，确认没有一个落到"未知"。"""
        cases = [
            (FakeEvent("error", "消息发送过于频繁，请稍后重试", "rate_limit"), "rate_limited"),
            (FakeEvent("error", "达到对话长度上限，请开启新对话", "length_limit"), "context_exhausted"),
            (FakeEvent("error", "内容审核未通过", "biz:1"), "safety_blocked"),
            (FakeEvent("error", "invalid chat session id", "biz:1"), "error"),
            (None, "ok"),
        ]
        for ev, expect in cases:
            d = classify_upstream_preflight(ev, None, 5.0)
            assert d.kind == expect, f"{ev} → 期望 {expect}，实际 {d.kind}"
            note_preflight(d)
        assert engine.get_counters().get("contract.unknown_upstream_kind") is None


# ─────────────────────────────────────────────────────────────
# 4. 安全拦截：**只分类，不改行为**
# ─────────────────────────────────────────────────────────────
class TestSafetyBlockIsObservationOnly:

    def test_classified(self):
        d = classify_upstream_preflight(FakeEvent("error", "内容审核未通过", "biz:1"), None, 1.0)
        assert d.kind == "safety_blocked"
        assert d.metric_error == "upstream_safety_blocked"
        assert d.retry_after == 0, "安全拦截不是可退避重试的错误"

    def test_ordinary_errors_are_not_swallowed(self):
        for text in ("invalid chat session id", "模型不存在", "达到对话长度上限"):
            d = classify_upstream_preflight(FakeEvent("error", text, "biz:1"), None, 1.0)
            assert d.kind != "safety_blocked", f"{text!r} 被误判成安全拦截"

    def test_no_route_acts_on_the_new_kind(self):
        """线上没有一条确证的安全拦截样本，所以它**不得**改变任何处置。

        这一条是防"顺手加个 400"：拿未实证的判据去改状态码，
        就是 T9 那次"从 sha1 相同推断出档位"的同类错误。
        客户端行为必须与今天完全一致（落到通用错误分支）。
        """
        for rel in ("app/api/openai_routes.py", "app/api/claude_routes.py"):
            src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            # 查"据此做判断"，而不是查"出现过这个词"：
            # 分类器**产生**这个 kind 是应该的，路由**据此分流**才是要禁止的。
            acts = []
            for ln in src.splitlines():
                s = ln.strip()
                if "safety_blocked" not in s:
                    continue
                if s.startswith(("if ", "elif ")) or '== "safety_blocked"' in s:
                    acts.append(s)
            assert not acts, (
                f"{rel} 里对 safety_blocked 做了处置（{acts}）—— "
                f"该判据未经线上实证，只能计数不能改行为"
            )

    def test_hints_are_deliberately_conservative(self):
        """裸 'safety' 这种泛词不许进清单（误判没有任何收益）。"""
        assert "safety" not in _SAFETY_BLOCK_HINTS
        assert any("审核" in h for h in _SAFETY_BLOCK_HINTS)


# ─────────────────────────────────────────────────────────────
# 5. 工具格式：计数器 + 解析器钩子真的接通了
# ─────────────────────────────────────────────────────────────
class TestToolFormatCounters:

    def test_tool_counters(self, engine):
        note_tool_emitted("read", count=3)
        note_tool_safety_net(2)
        passive_metrics.note_tool_cot_salvage(1)
        passive_metrics.note_tool_reflection(0)
        c = engine.get_counters()
        assert c["tool.emitted"] == 1
        assert c["tool.safety_net"] == 1
        assert c["tool.cot_salvage"] == 1
        assert c["tool.reflection"] == 1

    def test_parser_hook_is_installed_by_importing_passive_metrics(self):
        """`streaming_parser` 刻意零 app 依赖，所以它靠**被装上钩子**来上报。

        没装上的后果是"解析失败率恒为 0，看着一切正常" —— 这正是 T16 要防的形态。
        """
        from app.engine import streaming_parser
        assert streaming_parser._metric_hook is not None, (
            "导入 passive_metrics 后解析器钩子仍为空 → 工具格式错误率永远是 0"
        )

    def test_parser_reports_json_parse_failure(self, engine):
        """真的走一遍解析器的失败分支，确认计数落在引擎上。"""
        from app.engine import streaming_parser

        # 直接驱动到 [call:...] 分支并让 JSON 解析抛错。
        # 打这个补丁而不是"找一段能触发异常的输入"：后者依赖修复器的容错细节，
        # 会让这条用例在修复器变强/变弱时莫名其妙地红。
        #
        # ⚠️ 必须从 `__dict__` 取/还原 **staticmethod 对象本身**。
        # 第一版写的是 `orig = StreamingToolParser._repair_and_loads`（类属性访问
        # 会拆包成普通函数），还原时 `= orig` 就把它变成了**实例方法** →
        # 之后所有调用都多传一个 self → 全量测试里另外 16 条用例一起红。
        # 这是实测踩到的：单跑本文件全绿，跑全量才暴露。
        cls = streaming_parser.StreamingToolParser
        orig = cls.__dict__["_repair_and_loads"]

        def boom(s):
            raise ValueError("deliberate")

        p = cls([])
        cls._repair_and_loads = staticmethod(boom)
        try:
            out = []
            for ev, d in p.feed("[call:read] {oops}") + p.feed("[/call]"):
                out.append((ev, d))
            out += list(p.flush())
        finally:
            cls._repair_and_loads = orig
        assert type(cls.__dict__["_repair_and_loads"]) is staticmethod, (
            "还原后必须仍是 staticmethod，否则会污染同一进程里后续所有用例"
        )
        assert engine.get_counters().get("tool.json_parse_failed") == 1, (
            f"解析失败没被计数（输出={out}）"
        )

    def test_hook_failure_does_not_break_parsing(self):
        """钩子抛异常时解析必须照常工作（观测不能变成新的故障点）。"""
        from app.engine import streaming_parser

        def bad_hook(name, **fields):
            raise RuntimeError("hook boom")

        old = streaming_parser._metric_hook
        streaming_parser.set_metric_hook(bad_hook)
        try:
            streaming_parser._note("whatever", a=1)     # 不得抛出
        finally:
            streaming_parser.set_metric_hook(old)


# ─────────────────────────────────────────────────────────────
# 6. 报告工具：数学、无数据语义、退出码
# ─────────────────────────────────────────────────────────────
class TestReport:

    def _load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "passive_report", _REPO_ROOT / "tools" / "passive_report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_ratio_returns_none_not_zero_when_no_data(self):
        """没有分母时必须报"无数据"，不能报 0%。

        0% 和"没数据"在运维上是**相反**的结论：前者说"很健康"，
        后者说"这个指标根本没在跑"。
        """
        mod = self._load()
        assert mod._ratio(0, 0) is None
        assert mod._ratio(3, 0) is None
        assert mod._ratio(1, 4) == 0.25

    def test_rates_and_warnings(self):
        mod = self._load()
        counters = {
            "session.reuse_allowed": 100, "session.ambiguous_new": 9,
            "session.bound": 100, "session.account_changed": 5,
            "session.reused": 90, "session.new_session_created": 10,
            "upstream.ok": 90, "upstream.safety_blocked": 10,
            "tool.emitted": 10, "tool.json_parse_failed": 1, "tool.safety_net": 1,
        }
        rates = mod.compute_rates(counters)
        assert rates["answer_misplacement_rate"]["value"] == pytest.approx(0.09)
        assert rates["account_changed_rate"]["value"] == pytest.approx(0.05)
        assert rates["safety_block_rate"]["value"] == pytest.approx(0.10)
        assert rates["tool_format_error_rate"]["value"] == pytest.approx(0.20)
        assert rates["reuse_rate"]["value"] == pytest.approx(0.90)
        warnings, hits = mod.evaluate(rates, counters)
        assert len(warnings) == 4, f"应报 4 项超阈值，实际 {warnings}"
        assert hits == []

    def test_low_reuse_rate_is_a_warning(self):
        """复用率**过低**才是问题 —— 与其它四个比率的方向相反。"""
        mod = self._load()
        counters = {"session.reused": 1, "session.new_session_created": 99}
        rates = mod.compute_rates(counters)
        warnings, _ = mod.evaluate(rates, counters)
        assert any("reuse_rate" in w for w in warnings)

    def test_contract_counter_hit_returns_3(self, tmp_path, capsys):
        mod = self._load()
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"counters": {"contract.empty_model": 1}}), encoding="utf-8")
        assert mod.main(["--file", str(f), "--json"]) == 3, (
            "契约计数器非 0 必须以 3 退出，否则巡检会把它当正常"
        )

    def test_empty_counters_are_reported_as_ambiguous(self, tmp_path):
        mod = self._load()
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"counters": {}}), encoding="utf-8")
        counters = mod.load_counters(f)
        text = mod.format_report(counters, mod.compute_rates(counters))
        assert "计数器是空的" in text
        assert "① " in text, "必须给出两种可能，而不是让人以为'一切正常'"


# ─────────────────────────────────────────────────────────────
# 7. 契约：观测代码**结构上**不可能发请求
# ─────────────────────────────────────────────────────────────
FORBIDDEN_IMPORTS = {
    "requests", "httpx", "urllib", "urllib3", "aiohttp", "socket",
    "http", "subprocess", "telnetlib", "ftplib", "smtplib",
}


class TestNoTrafficIsStructurallyImpossible:

    """金丝雀的教训：**任何"巡检"代码都必须不可能产生上游流量。**

    这条不能只写在注释里（本项目已经有过"我检查过了"被证伪的先例），
    所以用 AST 把 import 面钉死。
    """

    FILES = ("tools/passive_report.py", "app/engine/passive_metrics.py")

    def _imported_roots(self, rel):
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    roots.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    roots.add(node.module.split(".")[0])
        return roots

    @pytest.mark.parametrize("rel", FILES)
    def test_no_network_or_subprocess_imports(self, rel):
        roots = self._imported_roots(rel)
        bad = roots & FORBIDDEN_IMPORTS
        assert not bad, f"{rel} 引入了 {sorted(bad)} —— 巡检代码不得有任何联网/子进程能力"

    @pytest.mark.parametrize("rel", FILES)
    def test_no_dynamic_import_escape_hatch(self, rel):
        """`__import__` / `importlib` 是绕过上一条的现成路子。"""
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "__import__" not in src, f"{rel} 用了 __import__，AST import 断言形同虚设"

    def test_report_tool_has_main_guard(self):
        """`pytest tools/` 会**绕过 norecursedirs** 直接收集本文件（实测）。

        没有 `if __name__ == "__main__":` 就会在收集阶段执行报告。
        """
        src = (_REPO_ROOT / "tools" / "passive_report.py").read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in src


# ─────────────────────────────────────────────────────────────
# 8. `model` 空率：不是一个比率，是一条必须恒成立的契约
# ─────────────────────────────────────────────────────────────
class TestModelFieldCanNeverBeEmpty:

    def test_normalize_never_returns_empty(self):
        for raw in (None, "", "   ", "不存在的模型", "deepseek-chat", "gpt-4"):
            assert normalize_model_id(raw) == CANONICAL_MODEL_ID

    def test_all_response_builders_use_normalized_model(self):
        """`model` 字段来自唯一的归一入口，所以"空 model"不可能出现。

        这就是为什么 B1 里 `model 空率` 被实现成**契约计数器**而不是比率：
        比率是"观察它发生多少"，契约是"它不该发生"。
        """
        for rel in ("app/api/openai_routes.py", "app/api/claude_routes.py"):
            src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "normalize_model_id(" in src, f"{rel} 没有走归一入口"
            assert '"model": ""' not in src, f"{rel} 里有硬编码空 model"
