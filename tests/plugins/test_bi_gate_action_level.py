"""action_max 的 L0–L3 判定。

这一层回答的问题和其它规则不同：其它规则问"这个调用合不合法"，它问
"这个人格被授权做到多重的动作"。同一个 query_metric，查汇总和导明细的风险
不是一回事，但工具名是同一个 —— 所以只按工具名授权在这里不够用。

测试分四组：
1. 分级本身算得对（就高不就低、判定不了要说判定不了）
2. 越权真的被拦，且拒因和参数错误分开
3. 配置坏掉时是 fail-closed（全拒）而不是放行
4. 在真实派发路径上工具体一次都不跑
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"

#: 假 query_metric 的执行次数。工具体跑了才加一 —— 唯一的硬证据。
_CALLS: list = []


def _load(name: str, path: Path, package: str = "", is_package: bool = False):
    locations = [str(path.parent)] if is_package else None
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=locations)
    mod = importlib.util.module_from_spec(spec)
    if package:
        mod.__package__ = package
    if is_package:
        mod.__path__ = [str(path.parent)]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rules():
    return _load("_bi_gate_rules_al", PLUGIN_DIR / "rules.py")


# ---------------------------------------------------------------------------
# 1. 分级本身
# ---------------------------------------------------------------------------

def _policy(rules, specs, default="L0", human_review_from=None):
    return rules.ActionPolicy(
        rules=tuple(
            rules.ActionRule(level=rules.parse_level(lv), when=when, label=label)
            for lv, when, label in specs
        ),
        default_level=rules.parse_level(default),
        human_review_from=rules.parse_level(human_review_from) if human_review_from else None,
        version="test",
    )


class TestClassify:
    def test_no_rule_matches_falls_back_to_default(self, rules):
        p = _policy(rules, [("L2", {"param_equals": {"export": True}}, "导出")], default="L0")
        level, why = rules.classify_action({"metric": "dau"}, p)
        assert level == 0
        assert "默认" in why

    def test_takes_the_highest_matching_rule(self, rules):
        """命中多条时就高不就低 —— 取最低就等于让人用一个轻规则洗掉重规则。"""
        p = _policy(rules, [
            ("L1", {"param_equals": {"granularity": "row"}}, "明细"),
            ("L2", {"param_equals": {"export": True}}, "导出"),
        ])
        level, why = rules.classify_action({"granularity": "row", "export": True}, p)
        assert level == 2, "同时命中 L1 和 L2 应判 L2"
        assert "导出" in why

    def test_rule_order_does_not_matter(self, rules):
        """规则顺序不该影响结果 —— 否则配置的语义就依赖行序，合规审起来会出错。"""
        specs = [
            ("L2", {"param_equals": {"export": True}}, "导出"),
            ("L1", {"param_equals": {"granularity": "row"}}, "明细"),
        ]
        args = {"granularity": "row", "export": True}
        assert rules.classify_action(args, _policy(rules, specs))[0] == 2
        assert rules.classify_action(args, _policy(rules, list(reversed(specs))))[0] == 2

    @pytest.mark.parametrize("args, expect", [
        ({"limit": 10000}, 1),
        ({"limit": 9999}, 0),
        ({"limit": 10001}, 1),
        ({}, 0),
    ])
    def test_param_gte_boundary(self, rules, args, expect):
        p = _policy(rules, [("L1", {"param_gte": {"limit": 10000}}, "大结果集")])
        assert rules.classify_action(args, p)[0] == expect

    @pytest.mark.parametrize("dims, expect", [
        (None, 0), (["a"], 0), (["a", "b", "c"], 0), (["a", "b", "c", "d"], 1), ("a", 0),
    ])
    def test_dimensions_count(self, rules, dims, expect):
        p = _policy(rules, [("L1", {"dimensions_count_gte": 4}, "高基数")])
        args = {} if dims is None else {"dimensions": dims}
        assert rules.classify_action(args, p)[0] == expect

    def test_all_conditions_in_one_rule_must_hold(self, rules):
        """同一条规则里的多个条件是 AND。"""
        p = _policy(rules, [
            ("L2", {"param_equals": {"export": True}, "param_present": ["destination"]}, "外发"),
        ])
        assert rules.classify_action({"export": True}, p)[0] == 0
        assert rules.classify_action({"export": True, "destination": "s3://x"}, p)[0] == 2


class TestUndecidable:
    """判定不了 ≠ 不匹配。把它当成不匹配，级别就被悄悄降下来了。"""

    def test_wrong_type_for_numeric_rule_is_undecidable(self, rules):
        p = _policy(rules, [("L1", {"param_gte": {"limit": 10000}}, "大结果集")])
        level, why = rules.classify_action({"limit": "很多"}, p)
        assert level is None
        assert "判定不了" in why

    def test_bool_is_not_a_number(self, rules):
        """True == 1 在 Python 里成立，但 limit=True 显然是参数写错了，不能当 1 用。"""
        p = _policy(rules, [("L1", {"param_gte": {"limit": 1}}, "任意 limit")])
        assert rules.classify_action({"limit": True}, p)[0] is None

    def test_explicit_mismatch_wins_over_undecidable(self, rules):
        """一条规则里既有明确不匹配又有判定不了，应判不匹配 —— 它确实没命中。"""
        p = _policy(rules, [
            ("L1", {"param_equals": {"granularity": "row"}, "param_gte": {"limit": 10}}, "x"),
        ])
        level, _ = rules.classify_action({"granularity": "summary", "limit": "多"}, p)
        assert level == 0

    def test_undecidable_call_is_denied_not_allowed(self, rules):
        p = _policy(rules, [("L1", {"param_gte": {"limit": 10000}}, "大结果集")])
        v = rules.check_action_level({"limit": "很多"}, p, action_max=3)
        assert v.blocked, "判定不了必须拒，哪怕 action_max 已经是最高"
        assert v.code == rules.REJECT_ACTION_LEVEL


# ---------------------------------------------------------------------------
# 2. 越权拦截
# ---------------------------------------------------------------------------

class TestActionMax:
    @pytest.mark.parametrize("action_max, expect_blocked", [
        (0, True), (1, False), (2, False), (3, False),
    ])
    def test_level_must_not_exceed_declared_max(self, rules, action_max, expect_blocked):
        p = _policy(rules, [("L1", {"param_equals": {"granularity": "row"}}, "明细")])
        v = rules.check_action_level({"granularity": "row"}, p, action_max=action_max)
        assert v.blocked is expect_blocked

    def test_undeclared_action_max_means_L0_not_unlimited(self, rules):
        """没声明 action_max 要当 L0（最严），不能当"不限制"。

        这是这一层唯一安全的默认值：漏声明的后果应该是做不了事，
        而不是什么都能做。
        """
        p = _policy(rules, [("L1", {"param_equals": {"granularity": "row"}}, "明细")])
        v = rules.check_action_level({"granularity": "row"}, p, action_max=None)
        assert v.blocked
        assert "未声明" in (v.reason or "")

    def test_reject_code_is_distinct_from_param_errors(self, rules):
        """越权和参数写错要分开记 —— 处理方式不同：一个走审批，一个改参数。"""
        p = _policy(rules, [("L2", {"param_equals": {"export": True}}, "导出")])
        v = rules.check_action_level({"export": True}, p, action_max=0)
        assert v.code == rules.REJECT_ACTION_LEVEL
        assert v.code != rules.REJECT_BAD_PARAM

    def test_reason_says_changing_params_will_not_help(self, rules):
        p = _policy(rules, [("L2", {"param_equals": {"export": True}}, "导出")])
        v = rules.check_action_level({"export": True}, p, action_max=0)
        assert "改参数没用" in (v.reason or "")
        assert rules.GATE_SOURCE in (v.reason or ""), "拒绝理由必须写明拦截来源"

    def test_detail_carries_both_levels_for_audit(self, rules):
        p = _policy(rules, [("L2", {"param_equals": {"export": True}}, "导出")])
        v = rules.check_action_level({"export": True}, p, action_max=1)
        assert v.detail["action_level"] == "L2"
        assert v.detail["action_max"] == "L1"


class TestHumanReview:
    """§7.2：L3（不可逆或涉资金）一律人审，无论 action_max 是多少。"""

    def test_blocked_even_when_action_max_allows_it(self, rules):
        p = _policy(rules, [("L3", {"param_equals": {"refund": True}}, "退款")],
                    human_review_from="L3")
        v = rules.check_action_level({"refund": True}, p, action_max=3)
        assert v.blocked
        assert "人工审批" in (v.reason or "")
        assert v.detail["needs_human_review"] is True

    def test_lower_levels_unaffected(self, rules):
        p = _policy(rules, [("L1", {"param_equals": {"granularity": "row"}}, "明细")],
                    human_review_from="L3")
        assert not rules.check_action_level({"granularity": "row"}, p, action_max=1).blocked

    def test_disabled_by_default(self, rules):
        p = _policy(rules, [("L3", {"param_equals": {"refund": True}}, "退款")])
        assert p.human_review_from is None
        assert not rules.check_action_level({"refund": True}, p, action_max=3).blocked


# ---------------------------------------------------------------------------
# 3. 配置坏掉时的方向
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_unavailable_policy_denies_everything(self, rules):
        p = rules.ActionPolicy(unavailable=True, version="<载入失败>")
        v = rules.check_action_level({"metric": "dau"}, p, action_max=3)
        assert v.blocked
        assert "授权策略配置有问题" in (v.reason or "")

    def test_unavailable_denial_names_the_gate_not_the_caller(self, rules):
        """拒绝理由要说清是门禁配置坏了，不是调用方写错了 ——

        否则模型会去反复改参数重试，既解决不了问题，也把真实故障淹掉。
        """
        p = rules.ActionPolicy(unavailable=True)
        v = rules.check_action_level({}, p, action_max=3)
        assert "不是你的调用有问题" in (v.reason or "")


class TestPolicyLoading:
    """坏配置不能"跳过坏规则、其余照用" —— 那等于一档授权被静默放宽。"""

    @pytest.fixture
    def gate(self, tmp_path, monkeypatch):
        registry = tmp_path / "registry.json"
        registry.write_text(json.dumps({"metrics": [
            {"name": "dau", "dimensions": ["market"], "requires_time_window": True}]}),
            encoding="utf-8")
        monkeypatch.setenv("BI_GATE_REGISTRY", str(registry))
        monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
        # 字段 ③ 的工具白名单。不声明则门禁按最严拦一切，每个用例都要显式声明。
        monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
        ns = "hermes_plugins"
        if ns not in sys.modules:
            import types
            m = types.ModuleType(ns); m.__path__ = []; sys.modules[ns] = m
        return _load(f"{ns}.bi_gate", PLUGIN_DIR / "__init__.py",
                     package=f"{ns}.bi_gate", is_package=True)

    def _write(self, tmp_path, monkeypatch, gate, payload):
        f = tmp_path / "policy.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("BI_GATE_ACTION_POLICY", str(f))
        return gate._load_policy()

    def test_unknown_operator_makes_the_whole_policy_unavailable(self, gate, tmp_path, monkeypatch):
        p = self._write(tmp_path, monkeypatch, gate, {"rules": [
            {"level": "L1", "when": {"param_equals": {"granularity": "row"}}},
            {"level": "L2", "when": {"param_matches_regex": {"destination": ".*"}}},
        ]})
        assert p.unavailable, "一条规则用了表外算子，整份策略就不该生效"

    def test_bad_level_makes_the_whole_policy_unavailable(self, gate, tmp_path, monkeypatch):
        p = self._write(tmp_path, monkeypatch, gate, {"rules": [
            {"level": "L9", "when": {"param_present": ["export"]}}]})
        assert p.unavailable

    def test_empty_when_is_rejected(self, gate, tmp_path, monkeypatch):
        """when 为空会匹配一切调用，几乎肯定是写漏了，不是有意为之。"""
        p = self._write(tmp_path, monkeypatch, gate, {"rules": [{"level": "L3", "when": {}}]})
        assert p.unavailable

    def test_missing_file_makes_policy_unavailable(self, gate, monkeypatch, tmp_path):
        monkeypatch.setenv("BI_GATE_ACTION_POLICY", str(tmp_path / "没有这个文件.json"))
        assert gate._load_policy().unavailable

    def test_unset_env_disables_the_layer_rather_than_blocking(self, gate, monkeypatch):
        """没配策略 ≠ 策略坏了。

        分档标准要业务方与合规定，没定之前技术侧不该塞一套默认值假装有授权控制；
        其余门禁规则照常生效。这两种情况必须分开，否则"还没定"会被当成故障。
        """
        monkeypatch.delenv("BI_GATE_ACTION_POLICY", raising=False)
        assert gate._load_policy() is None

    def test_shipped_example_policy_parses(self, gate, monkeypatch):
        """仓库里带的示例策略必须自己能过 —— 它是别人抄的模板。"""
        monkeypatch.setenv("BI_GATE_ACTION_POLICY", str(PLUGIN_DIR / "policy.example.json"))
        p = gate._load_policy()
        assert not p.unavailable
        assert len(p.rules) == 5

    @pytest.mark.parametrize("raw, expect", [("L2", 2), ("l2", 2), (" L0 ", 0), ("L9", None), ("", None)])
    def test_action_max_parsing(self, gate, monkeypatch, raw, expect):
        monkeypatch.setenv("BI_GATE_ACTION_MAX", raw)
        assert gate._load_action_max() == expect


# ---------------------------------------------------------------------------
# 4. 真实派发路径
# ---------------------------------------------------------------------------

class TestOnTheRealDispatchPath:
    """判定算得对，证明不了"越权的调用真的没跑起来"。这组驱动真实的
    ``handle_function_call``，硬证据是工具体的执行计数。"""

    @pytest.fixture
    def dispatch(self, tmp_path, monkeypatch):
        registry = tmp_path / "registry.json"
        registry.write_text(json.dumps({"metrics": [
            {"name": "dau", "dimensions": ["market", "channel"], "requires_time_window": True}]}),
            encoding="utf-8")
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({
            "version": "e2e-1",
            "default_level": "L0",
            "rules": [
                {"level": "L1", "label": "明细", "when": {"param_equals": {"granularity": "row"}}},
                {"level": "L2", "label": "导出", "when": {"param_equals": {"export": True}}},
            ],
        }), encoding="utf-8")
        monkeypatch.setenv("BI_GATE_REGISTRY", str(registry))
        monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
        monkeypatch.setenv("BI_GATE_ACTION_POLICY", str(policy))
        monkeypatch.setenv("BI_GATE_ACTION_MAX", "L1")

        ns = "hermes_plugins"
        if ns not in sys.modules:
            import types
            m = types.ModuleType(ns); m.__path__ = []; sys.modules[ns] = m
        gate = _load(f"{ns}.bi_gate", PLUGIN_DIR / "__init__.py",
                     package=f"{ns}.bi_gate", is_package=True)
        gate.reload_registry()
        gate.reload_policy()

        _CALLS.clear()
        import model_tools
        from hermes_cli import plugins as plugins_mod
        from tools import registry as tool_registry

        monkeypatch.setattr(plugins_mod, "invoke_hook", lambda hook, **kw: (
            [gate._on_pre_tool_call(**kw)]
            if hook == "pre_tool_call" and gate._on_pre_tool_call(**kw) else []))

        tool_registry.registry.register(
            name="query_metric", toolset="bi_gate_action_test",
            schema={"name": "query_metric", "description": "test double",
                    "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **_k: _CALLS.append(args) or json.dumps({"rows": []}),
            override=True)
        return gate, model_tools.handle_function_call

    WINDOW = {"start": "2026-08-01", "end": "2026-08-21"}

    def test_within_action_max_runs(self, dispatch):
        _gate, call = dispatch
        call("query_metric", {"metric": "dau", "granularity": "row", "time_window": self.WINDOW})
        assert len(_CALLS) == 1, "L1 在 action_max=L1 之内，应当执行"

    def test_above_action_max_body_never_runs(self, dispatch):
        _gate, call = dispatch
        result = call("query_metric", {"metric": "dau", "export": True, "time_window": self.WINDOW})
        assert len(_CALLS) == 0, "L2 超出 action_max=L1，工具体一次都不能跑"
        assert "L2" in str(result) and "L1" in str(result)

    def test_blocked_reason_names_the_gate(self, dispatch):
        _gate, call = dispatch
        result = call("query_metric", {"metric": "dau", "export": True, "time_window": self.WINDOW})
        assert "bi-gate" in str(result) or "门禁" in str(result)

    def test_action_level_recorded_for_passed_calls_too(self, dispatch, caplog):
        """放行的调用也要记级别 —— 只记被拦的，就只看得见失败的越权尝试，
        回答不了"这个人格实际都做到了几级"。"""
        gate, call = dispatch
        caplog.set_level("INFO")
        call("query_metric", {"metric": "dau", "granularity": "row", "time_window": self.WINDOW})
        verdicts = [json.loads(r.getMessage().split("bi-gate verdict ", 1)[1])
                    for r in caplog.records if "bi_gate_verdict" in r.getMessage()]
        assert verdicts, "放行的调用也该留下一条判定记录"
        assert verdicts[-1]["gate_result"] == "passed"
        assert verdicts[-1]["action_level"] == "L1"
        assert verdicts[-1]["action_max"] == "L1"

    def test_unknown_metric_still_reported_before_action_level(self, dispatch):
        """指标不存在时先报指标问题 —— 那个更可操作。"""
        _gate, call = dispatch
        result = call("query_metric", {"metric": "没这个指标", "export": True})
        assert len(_CALLS) == 0
        assert "受控事实层" in str(result)
