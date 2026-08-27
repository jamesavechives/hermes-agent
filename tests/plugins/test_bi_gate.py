"""bi-gate 门禁的行为测试。

每条测试对应一条门禁规则的行为，而不是实现细节 —— 规则改了这些测试应该跟着改，
但重构 evaluate 的内部顺序不应该让它们变红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODNAME = "bi_gate_rules_under_test"


def _load_rules():
    """插件目录名带连字符，不能直接 import —— 按仓库既有做法从文件载入。

    exec 前必须先登记进 ``sys.modules``：``@dataclass`` 会回查
    ``sys.modules[cls.__module__]``，模块不在表里就会炸。
    """
    path = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate" / "rules.py"
    spec = importlib.util.spec_from_file_location(_MODNAME, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = mod
    spec.loader.exec_module(mod)
    return mod


_rules = _load_rules()

PASSED = _rules.PASSED
REJECT_BAD_PARAM = _rules.REJECT_BAD_PARAM
REJECT_NO_TIME_WINDOW = _rules.REJECT_NO_TIME_WINDOW
REJECT_SCAN = _rules.REJECT_SCAN
REJECT_UNKNOWN_METRIC = _rules.REJECT_UNKNOWN_METRIC
GATE_SOURCE = _rules.GATE_SOURCE
MetricRegistry = _rules.MetricRegistry
MetricSpec = _rules.MetricSpec
evaluate = _rules.evaluate
estimate_scan_rows = _rules.estimate_scan_rows


@pytest.fixture
def registry() -> MetricRegistry:
    return MetricRegistry(
        [
            MetricSpec(
                name="dau",
                dimensions=frozenset({"market", "channel"}),
                max_scan_rows=1_000_000,
                # 声明了扫描上限就必须给出 rows_per_day，否则预估不出来、
                # 按「判定不了」拒绝。这条是有意的压力：注册表填不全就用不了。
                rows_per_day=10_000,
            ),
            MetricSpec(
                name="open_interest",
                dimensions=frozenset({"symbol"}),
                requires_time_window=False,
            ),
        ]
    )


WINDOW = {"start": "2026-08-01", "end": "2026-08-21"}


class TestMetricRegistered:
    def test_registered_metric_passes(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).code == PASSED

    def test_unregistered_metric_is_blocked(self, registry):
        v = evaluate({"metric": "revenue_v2", "time_window": WINDOW}, registry)
        assert v.code == REJECT_UNKNOWN_METRIC
        assert v.blocked

    def test_missing_metric_is_blocked(self, registry):
        assert evaluate({"time_window": WINDOW}, registry).code == REJECT_UNKNOWN_METRIC

    def test_reason_lists_available_metrics(self, registry):
        # 拒绝理由要能让模型自己纠正，所以得把可用指标报出来
        v = evaluate({"metric": "nope", "time_window": WINDOW}, registry)
        assert "dau" in v.reason and "open_interest" in v.reason

    def test_empty_registry_blocks_everything(self):
        # fail-closed：注册表载入失败时门禁停摆，不放行
        v = evaluate({"metric": "dau", "time_window": WINDOW}, MetricRegistry([]))
        assert v.code == REJECT_UNKNOWN_METRIC


class TestDimensions:
    def test_declared_dimension_passes(self, registry):
        args = {"metric": "dau", "dimensions": ["market"], "time_window": WINDOW}
        assert evaluate(args, registry).code == PASSED

    def test_undeclared_dimension_is_blocked(self, registry):
        args = {"metric": "dau", "dimensions": ["market", "device"], "time_window": WINDOW}
        v = evaluate(args, registry)
        assert v.code == REJECT_BAD_PARAM
        assert "device" in v.reason

    def test_omitted_dimensions_pass(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).code == PASSED

    def test_non_list_dimensions_are_blocked(self, registry):
        args = {"metric": "dau", "dimensions": 42, "time_window": WINDOW}
        assert evaluate(args, registry).code == REJECT_BAD_PARAM


class TestTimeWindow:
    def test_absolute_window_passes(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).code == PASSED

    def test_missing_window_is_blocked(self, registry):
        assert evaluate({"metric": "dau"}, registry).code == REJECT_NO_TIME_WINDOW

    @pytest.mark.parametrize("bad", ["最近七天", "last_7d", "2026/08/01", ""])
    def test_relative_or_malformed_window_is_blocked(self, registry, bad):
        # 相对时间必须在调用前解析成绝对区间，否则评估集无法回归
        args = {"metric": "dau", "time_window": {"start": bad, "end": "2026-08-21"}}
        assert evaluate(args, registry).code == REJECT_NO_TIME_WINDOW

    def test_reversed_window_is_blocked(self, registry):
        args = {"metric": "dau", "time_window": {"start": "2026-08-21", "end": "2026-08-01"}}
        assert evaluate(args, registry).code == REJECT_NO_TIME_WINDOW

    def test_stock_metric_needs_no_window(self, registry):
        # 存量类指标（当前持仓）没有时间窗，不该被拦
        assert evaluate({"metric": "open_interest"}, registry).code == PASSED


class TestScanBudget:
    def test_within_budget_passes(self, registry):
        args = {"metric": "dau", "time_window": WINDOW}
        assert evaluate(args, registry, estimated_rows=999_999).code == PASSED

    def test_over_budget_is_blocked(self, registry):
        args = {"metric": "dau", "time_window": WINDOW}
        v = evaluate(args, registry, estimated_rows=1_000_001)
        assert v.code == REJECT_SCAN
        assert v.detail["limit"] == 1_000_000

    def test_missing_estimate_passes(self, registry):
        # 预检拿不到预估值时不应让业务不可用
        args = {"metric": "dau", "time_window": WINDOW}
        assert evaluate(args, registry, estimated_rows=None).code == PASSED

    def test_metric_without_limit_passes(self, registry):
        args = {"metric": "open_interest"}
        assert evaluate(args, registry, estimated_rows=10**9).code == PASSED


class TestRejectionReason:
    """拒绝理由必须写明拦截来源 —— 否则模型会自行编造归因。

    依据：《评估与 Reward v0.1》§2.4，两次实测模型都把 harness 的拦截
    说成了远端服务的行为。
    """

    @pytest.mark.parametrize(
        "args",
        [
            {"metric": "nope", "time_window": WINDOW},
            {"metric": "dau"},
            {"metric": "dau", "dimensions": ["device"], "time_window": WINDOW},
        ],
    )
    def test_every_rejection_names_the_gate(self, registry, args):
        v = evaluate(args, registry)
        assert v.blocked
        assert v.reason.startswith(GATE_SOURCE)

    def test_pass_carries_no_reason(self, registry):
        assert evaluate({"metric": "dau", "time_window": WINDOW}, registry).reason is None


class TestScanBudget:
    """扫描量预检 —— 这条规则以前在生产路径上从来没生效过。

    钩子里写死了 ``estimated_rows=None``，于是 rules.py 里有规则、有测试，
    而真实调用永远拿不到预估值。2026-08-27 改成由 evaluate 自己从注册表声明
    与时间窗跨度算出来。这组测试钉住新行为。
    """

    def test_estimate_uses_declaration_and_window(self, registry):
        spec = registry.get("dau")
        # 10,000 行/天 × 20 天
        assert estimate_scan_rows({"time_window": WINDOW}, spec) == 200_000

    def test_estimate_ignores_dimension_count(self, registry):
        """维度个数不进公式：列存下多切一维是多读一列，不是多读一批行。"""
        spec = registry.get("dau")
        a = estimate_scan_rows({"time_window": WINDOW}, spec)
        b = estimate_scan_rows({"time_window": WINDOW, "dimensions": ["market", "channel"]}, spec)
        assert a == b

    def test_long_window_exceeds_budget_and_is_blocked(self, registry):
        """10,000 行/天 × 200 天 = 200 万 > 上限 100 万。"""
        long_window = {"start": "2026-01-01", "end": "2026-07-20"}
        verdict = evaluate({"metric": "dau", "time_window": long_window}, registry)
        assert verdict.code == REJECT_SCAN
        assert "缩小时间窗" in verdict.reason

    def test_missing_rows_per_day_is_undecidable_and_denied(self):
        """声明了上限却没声明 rows_per_day —— 判定不了当不通过。

        反过来做（当成"没有上限"）会让填不全的注册表静默变成没有扫描限制，
        正是这套门禁要消灭的失效方式。
        """
        reg = MetricRegistry([
            MetricSpec(name="m", dimensions=frozenset({"d"}), max_scan_rows=1000),
        ])
        verdict = evaluate({"metric": "m", "time_window": WINDOW}, reg)
        assert verdict.code == REJECT_SCAN
        assert "rows_per_day" in verdict.reason

    def test_no_declared_limit_means_no_check(self):
        """没声明 max_scan_rows = 没人要求限额，放行。"""
        reg = MetricRegistry([
            MetricSpec(name="m", dimensions=frozenset({"d"}), rows_per_day=10**9),
        ])
        assert evaluate({"metric": "m", "time_window": WINDOW}, reg).code == PASSED

    def test_point_in_time_metric_counts_one_day(self):
        """存量类指标没有时间窗，按一个快照算。"""
        reg = MetricRegistry([
            MetricSpec(name="oi", dimensions=frozenset({"symbol"}),
                       requires_time_window=False, rows_per_day=500, max_scan_rows=1000),
        ])
        assert evaluate({"metric": "oi"}, reg).code == PASSED

    def test_caller_can_override_with_a_real_estimate(self, registry):
        """调用方真做了 EXPLAIN 时可以传进来覆盖推算值。"""
        verdict = evaluate({"metric": "dau", "time_window": WINDOW}, registry,
                           estimated_rows=5_000_000)
        assert verdict.code == REJECT_SCAN

    def test_explicit_none_skips_the_check(self, registry):
        """显式传 None = 明确不做这项检查，与"漏传"要能区分开。"""
        long_window = {"start": "2026-01-01", "end": "2026-07-20"}
        assert evaluate({"metric": "dau", "time_window": long_window}, registry,
                        estimated_rows=None).code == PASSED

    def test_omitting_the_argument_does_not_skip_the_check(self, registry):
        """漏传参数必须照常检查 —— 这正是以前那个 bug 的形状。"""
        long_window = {"start": "2026-01-01", "end": "2026-07-20"}
        assert evaluate({"metric": "dau", "time_window": long_window}, registry).code == REJECT_SCAN
