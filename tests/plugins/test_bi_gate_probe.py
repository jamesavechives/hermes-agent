"""存活探针的测试。

重点不是"门禁正常时探针说正常"——那太容易蒙对。重点是**门禁失效时探针必须报警**：
一个永远返回 alive 的探针比没有探针更糟，因为它让人以为有监控。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"


def _load(module: str):
    """从连字符目录载入插件的子模块（probe 会相对 import rules）。"""
    ns = "hermes_plugins"
    if ns not in sys.modules:
        parent = types.ModuleType(ns)
        parent.__path__ = []
        sys.modules[ns] = parent
    pkg = f"{ns}.bi_gate"
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg
        mod.__path__ = [str(PLUGIN_DIR)]
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)
    full = f"{pkg}.{module}"
    spec = importlib.util.spec_from_file_location(full, PLUGIN_DIR / f"{module}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def probe_mod():
    return _load("probe")


@pytest.fixture
def rules_mod():
    return _load("rules")


class TestDetectsAliveGate:
    def test_blocked_canary_means_alive(self, probe_mod, rules_mod):
        # 与生产一致：tools.registry.tool_error 用 ensure_ascii=False
        def _dispatch(_tool, _args):
            return json.dumps(
                {"error": f"{rules_mod.GATE_SOURCE}：指标不在受控事实层。"}, ensure_ascii=False
            )

        result = probe_mod.probe(dispatch=_dispatch)
        assert result.status == probe_mod.ALIVE
        assert result.exit_code == 0

    def test_escaped_message_still_counts_as_alive(self, probe_mod, rules_mod):
        """结果被 ensure_ascii=True 转义时也要认得出来。

        当前生产不会走到这条路，但序列化方式是别人的实现细节，改了不该让
        探针误报"门禁失效"——那会消耗对告警的信任。
        """

        def _dispatch(_tool, _args):
            return json.dumps({"error": f"{rules_mod.GATE_SOURCE}：拦截"})  # ensure_ascii 默认 True

        assert probe_mod.probe(dispatch=_dispatch).status == probe_mod.ALIVE


class TestDetectsDeadGate:
    """门禁失效的各种形态，探针都必须报出来。"""

    def test_call_goes_through_means_gate_down(self, probe_mod):
        # 门禁没加载：探针调用一路跑到工具体，返回了正常结果
        def _dispatch(_tool, _args):
            return json.dumps({"rows": []})

        result = probe_mod.probe(dispatch=_dispatch)
        assert result.status == probe_mod.GATE_DOWN
        assert result.exit_code == 1

    def test_unrelated_error_still_counts_as_down(self, probe_mod):
        """工具自己报错，但不是门禁拦的 —— 仍算门禁失效。

        这一条容易被写错成"有 error 就算拦住了"。真实场景里 query_metric 收到
        未注册指标会自己抛错，看着也像被拒，但门禁其实没生效。
        """

        def _dispatch(_tool, _args):
            return json.dumps({"error": "unknown metric __bi_gate_canary_never_register__"})

        result = probe_mod.probe(dispatch=_dispatch)
        assert result.status == probe_mod.GATE_DOWN

    def test_empty_result_counts_as_down(self, probe_mod):
        result = probe_mod.probe(dispatch=lambda _t, _a: "")
        assert result.status == probe_mod.GATE_DOWN


class TestProbeSelfFailure:
    """探针自身出问题时要和"门禁失效"区分开 —— 否则会把环境故障误报成安全事件。"""

    def test_dispatch_raising_is_probe_error(self, probe_mod):
        def _dispatch(_tool, _args):
            raise RuntimeError("dispatch exploded")

        result = probe_mod.probe(dispatch=_dispatch)
        assert result.status == probe_mod.PROBE_ERROR
        assert result.exit_code == 2

    def test_exit_codes_are_distinct(self, probe_mod):
        """三种状态的退出码必须两两不同，监控才能分开处理。"""
        codes = {
            probe_mod.ProbeResult(probe_mod.ALIVE, "").exit_code,
            probe_mod.ProbeResult(probe_mod.GATE_DOWN, "").exit_code,
            probe_mod.ProbeResult(probe_mod.PROBE_ERROR, "").exit_code,
        }
        assert codes == {0, 1, 2}


class TestCanary:
    def test_canary_metric_is_not_a_plausible_real_metric(self, probe_mod):
        """探针指标名必须不可能被真登记，否则探针会长期误报。"""
        name = probe_mod.CANARY_METRIC
        assert name.startswith("__") and name.endswith("__")
        assert "canary" in name

    def test_canary_would_be_rejected_by_the_rules(self, probe_mod, rules_mod):
        """探针调用在规则层面确实该被拒 —— 否则探针从设计上就不成立。"""
        verdict = rules_mod.evaluate(dict(probe_mod.CANARY_ARGS), rules_mod.MetricRegistry([]))
        assert verdict.blocked


def test_probe_reaches_past_the_identity_layer(monkeypatch):
    """探针必须走到身份之后的规则，不能只探到身份那一层。

    2026-08-28 加身份透传之后真实发生的：探针从定时器跑、没有会话，于是撞的是
    ``rejected_origin_not_allowed`` —— **它依然报"存活"，但指标注册、时间窗、
    扫描量那几条规则一条都没跑到**。

    「测试因为错误的原因通过」的又一例，而且这次是新加的前置检查造成的：
    **加一层新的前置门槛，会让所有原本测后面几层的东西静默地改成测新那层。**
    这条钉住探针确实绑了身份。
    """
    mod = _load("probe")
    seen = {}

    def _dispatch(name, args):
        from gateway.session_context import get_session_env
        seen["user_id"] = get_session_env("HERMES_SESSION_USER_ID")
        return '{"error": "BI 门禁（bi-gate 插件，在调用发出前拦截）：指标未登记"}'

    result = mod.probe(dispatch=_dispatch)
    assert result.status == mod.ALIVE
    assert seen.get("user_id") == mod.PROBE_PLATFORM_ID, (
        f"探针发调用时没有绑上身份（读到 {seen.get('user_id')!r}）—— "
        f"它会拦在身份那一层，后面的规则一条都验不到"
    )
    assert "已绑会话身份" in result.detail


def test_probe_says_so_when_it_could_not_bind(monkeypatch):
    """绑不上身份时，结论里要说清这轮只探到身份那一层。

    「存活」这两个字不能比实际验到的东西更有力。
    """
    mod = _load("probe")
    monkeypatch.setattr(mod, "_bind_probe_session", lambda: None)
    result = mod.probe(dispatch=lambda n, a: '{"error": "BI 门禁（bi-gate 插件，在调用发出前拦截）：拦了"}')
    assert result.status == mod.ALIVE
    assert "没能绑上会话身份" in result.detail
