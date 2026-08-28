"""bi-gate 端到端冒烟：验证 hook 在真实派发路径里确实拦得住。

单测只证明判定函数算得对，证明不了「Hermes 真的会调用它、block 真的会让
工具体不执行」。这两件事只能驱动真实的 ``handle_function_call`` 来验。

硬证据是 ``_CALLS`` 计数器：被拦的调用它不能加一。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"

#: 假 query_metric 的执行次数。工具体跑了才加一 —— 这是「有没有被真正拦住」的唯一硬证据。
_CALLS: list[dict] = []


def _load_plugin():
    """按仓库既有做法从文件载入连字符目录的插件。"""
    ns = "hermes_plugins"
    if ns not in sys.modules:
        import types

        mod = types.ModuleType(ns)
        mod.__path__ = []
        sys.modules[ns] = mod
    name = f"{ns}.bi_gate"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """载入 bi-gate，并指向一份只有一个指标的注册表。"""
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "default_timezone": "UTC+8",
                "metrics": [
                    {
                        "name": "dau",
                        "dimensions": ["market"],
                        "requires_time_window": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BI_GATE_REGISTRY", str(registry))
    # 人格声明的工具白名单（字段 ③）。不声明的话门禁按最严处理、拦一切，
    # 所以每个用例都必须显式声明——这本身就是被测行为的一部分。
    monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
    plugin = _load_plugin()
    plugin.reload_registry()
    return plugin


@pytest.fixture
def dispatch(gate, monkeypatch):
    """把 bi-gate 的 hook 接进真实的 pre_tool_call 派发，并注册一个假 query_metric。

    返回可直接调用的 ``handle_function_call``。
    """
    _CALLS.clear()

    import model_tools
    from hermes_cli import plugins as plugins_mod

    # ── 让真实的 hook 派发只看到 bi-gate ──────────────────────────
    # 直接替换 invoke_hook，避免依赖 PluginManager 的发现流程（那会连带加载
    # 一堆可选插件）。派发路径本身仍是仓库自己的 _dispatch_pre_tool_call_hooks。
    def _invoke_hook(hook_name, **kwargs):
        if hook_name != "pre_tool_call":
            return []
        out = gate._on_pre_tool_call(**kwargs)
        return [out] if out is not None else []

    monkeypatch.setattr(plugins_mod, "invoke_hook", _invoke_hook)

    # ── 注册一个假的 query_metric ────────────────────────────────
    from tools import registry as tool_registry

    def _fake_query_metric(args, **_kwargs):
        # registry 以 handler(args, **kwargs) 调用，args 是参数字典
        _CALLS.append(args)
        return json.dumps({"rows": [{"dau": 12345}]})

    _register_tool(tool_registry, "query_metric", _fake_query_metric)
    return model_tools.handle_function_call


def _register_tool(tool_registry, name: str, handler) -> None:
    """把一个假工具塞进真实注册表（覆盖同名项，测试结束由 registry 自己承载）。"""
    tool_registry.registry.register(
        name=name,
        toolset="bi_gate_test",
        schema={
            "name": name,
            "description": "test double",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
        override=True,
    )


GOOD = {"metric": "dau", "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}


class TestBlockedCallsDoNotExecute:
    """被拦的调用，工具体一次都不能跑。"""

    @pytest.mark.parametrize(
        "args, expect_in_message",
        [
            ({"metric": "revenue_v2", "time_window": GOOD["time_window"]}, "不在受控事实层"),
            ({"metric": "dau"}, "必须带时间窗"),
            ({"metric": "dau", "time_window": {"start": "最近七天", "end": "2026-08-21"}}, "绝对时间"),
            (
                {"metric": "dau", "dimensions": ["device"], "time_window": GOOD["time_window"]},
                "不支持维度",
            ),
        ],
    )
    def test_body_never_runs(self, dispatch, args, expect_in_message):
        before = len(_CALLS)
        result = dispatch("query_metric", args)
        assert len(_CALLS) == before, "工具体被执行了 —— 门禁没拦住"
        assert expect_in_message in str(result)

    def test_block_message_names_the_gate(self, dispatch):
        result = dispatch("query_metric", {"metric": "nope", "time_window": GOOD["time_window"]})
        assert "bi-gate" in str(result), "拒绝理由必须写明拦截来源"


class TestAllowedCallsGoThrough:
    def test_valid_call_reaches_the_tool(self, dispatch):
        result = dispatch("query_metric", GOOD)
        assert len(_CALLS) == 1, "合法调用没能到达工具体"
        assert "12345" in str(result)

    def test_tool_outside_the_whitelist_never_runs(self, dispatch, monkeypatch):
        """白名单之外的工具，工具体一次都不许跑。

        这条替换了原来的 ``test_other_tools_are_untouched``——那条断言的是
        「门禁只管 query_metric，别的工具一律不碰」，而 2026-08-26 的实测
        证明那个行为就是漏洞本身：模型被拦下 export 之后，转手用文件工具把
        同样的数据写到了磁盘，审计里一条记录都没有。

        工具白名单必须由门禁在派发路径上自己比对，不能交给 Hermes 的
        toolsets 配置——那只决定模型看得到什么，不决定能执行什么。
        """
        from tools import registry as tool_registry

        seen = []
        _register_tool(
            tool_registry, "bi_gate_probe_tool", lambda args, **_kw: seen.append(args) or "ok"
        )
        result = dispatch("bi_gate_probe_tool", {"path": "/tmp/x"})
        assert len(seen) == 0, "白名单外的工具体执行了 —— 这就是那次绕过"
        assert "bi_gate_probe_tool" in json.dumps(result, ensure_ascii=False, default=str)

    def test_whitelisted_non_metric_tool_passes_through(self, dispatch, monkeypatch):
        """白名单内、但不是 query_metric 的工具，门禁不做指标判定，直接放行。"""
        from tools import registry as tool_registry

        monkeypatch.setenv("BI_GATE_TOOLS", "query_metric,bi_gate_probe_tool")
        seen = []
        _register_tool(
            tool_registry, "bi_gate_probe_tool", lambda args, **_kw: seen.append(args) or "ok"
        )
        dispatch("bi_gate_probe_tool", {"path": "/tmp/x"})
        assert len(seen) == 1

    def test_empty_whitelist_blocks_everything(self, dispatch, monkeypatch):
        """没声明白名单 = 空白名单 = 拦一切。

        与 action_max 未声明按 L0 是同一条原则：漏声明的后果应该是做不了事。
        反过来（未声明就不限制）会让「忘了配」和「配成全开」在行为上无法区分。
        """
        from tools import registry as tool_registry

        monkeypatch.delenv("BI_GATE_TOOLS", raising=False)
        seen = []
        _register_tool(
            tool_registry, "bi_gate_probe_tool", lambda args, **_kw: seen.append(args) or "ok"
        )
        dispatch("bi_gate_probe_tool", {"path": "/tmp/x"})
        dispatch("query_metric", {"metric": "dau", "dimensions": ["market"],
                                  "time_window": {"start": "2026-08-01", "end": "2026-08-21"}})
        assert len(seen) == 0
        assert len(_CALLS) == 0, "白名单为空时连 query_metric 自己也不许跑"


class TestFailureModes:
    """门禁自身出问题时的行为 —— 这些是真实故障，不是假想。"""

    def test_empty_registry_blocks_everything(self, dispatch, gate, monkeypatch, tmp_path):
        """注册表载入失败按空表处理，应当全拦（fail-closed）。"""
        monkeypatch.setenv("BI_GATE_REGISTRY", str(tmp_path / "does-not-exist.json"))
        gate.reload_registry()
        before = len(_CALLS)
        dispatch("query_metric", GOOD)
        assert len(_CALLS) == before, "注册表缺失时仍然放行了 —— 不是 fail-closed"

    def test_internal_error_is_converted_to_a_block(self, dispatch, gate, monkeypatch):
        """判定过程内部出错时，插件自己兜成拦截。

        这是 bi-gate 的兜底：Hermes 侧对 hook 异常是 fail-open（见下一条），
        所以异常绝不能逃出 ``_on_pre_tool_call``。这里让判定的核心函数抛异常，
        验证调用仍然被拦下、且理由说明是门禁故障而非调用有问题。
        """

        def _boom(*_a, **_kw):
            raise RuntimeError("evaluate exploded")

        monkeypatch.setattr(gate, "evaluate", _boom)
        before = len(_CALLS)
        result = dispatch("query_metric", GOOD)  # 本身完全合法
        assert len(_CALLS) == before, "判定内部出错时放行了 —— 兜底没生效"
        assert "门禁故障" in str(result), "拒绝理由要说清是门禁坏了，不是调用有问题"

    def test_upstream_fails_open_when_the_whole_hook_raises(self, dispatch, gate, monkeypatch):
        """整个 hook 被替换成抛异常的函数时，Hermes 会放行。

        这条不是 bi-gate 的行为，是 **Hermes 的既有行为**：``model_tools.py``
        里对 ``_dispatch_pre_tool_call_hooks`` 的调用包在 ``except Exception``
        中，只记 debug 日志然后继续执行。

        上面那条兜底能挡住"判定逻辑内部出错"，但挡不住"整个 hook 函数本身
        坏掉"（比如插件加载出错、签名不匹配）。所以插件内部兜底之外，仍然
        需要一条存活探针：定期发一个必然被拦的调用，拦不住就告警。

        这条测试把上游行为钉住 —— 若哪天上游改成 fail-closed，它会变红，
        届时应更新断言而不是默默接受。
        """

        def _boom(**_kwargs):
            raise RuntimeError("hook itself exploded")

        monkeypatch.setattr(gate, "_on_pre_tool_call", _boom)
        before = len(_CALLS)
        dispatch("query_metric", {"metric": "revenue_v2"})  # 本该被拦
        assert len(_CALLS) == before + 1, (
            "行为变了：hook 抛异常时调用没有被放行。若上游改成 fail-closed，"
            "这是好事，请更新本测试的断言。"
        )


class TestAuditDoesNotContradictItself:
    """审计日志不能对同一次调用既说"拦了"又说"放行了"。

    本地实测（2026-08-24）发现：Hermes 在 ``pre_tool_call`` 拦下调用之后，
    仍然会触发 ``post_tool_call``。post 钩子当时无条件记 ``gate_result: passed``，
    于是每条拒绝都配一条"放行"，事后没法从日志里数出拒绝了多少次。
    """

    def test_blocked_call_emits_no_passed_line(self, gate, dispatch, caplog):
        caplog.set_level("INFO")
        blocked = {"metric": "revenue_v2", "time_window": GOOD["time_window"]}
        result = dispatch("query_metric", blocked)
        # 模拟 Hermes 拦截后仍然触发 post 钩子
        gate._on_post_tool_call(tool_name="query_metric", args=blocked, result=result)

        passed_lines = [r for r in caplog.records if "bi_gate_passed_call" in r.getMessage()]
        assert passed_lines == [], f"被拦的调用不该记 passed：{[r.getMessage() for r in passed_lines]}"

    def test_passed_call_still_emits_passed_line(self, gate, dispatch, caplog):
        caplog.set_level("INFO")
        result = dispatch("query_metric", dict(GOOD))
        gate._on_post_tool_call(tool_name="query_metric", args=dict(GOOD), result=result)

        passed_lines = [r for r in caplog.records if "bi_gate_passed_call" in r.getMessage()]
        assert len(passed_lines) == 1, "真正执行了的调用必须留下 passed 记录"

    def test_escaped_block_message_also_recognised(self, gate):
        """结果里的中文若被转义成 \\uXXXX，仍要认得出是门禁拦的。"""
        from importlib import import_module

        rules = import_module(f"{gate.__name__}.rules")
        escaped = json.dumps({"error": rules.GATE_SOURCE})  # ensure_ascii=True
        assert gate._is_gate_block(escaped) is True
        assert gate._is_gate_block(json.dumps({"rows": []})) is False


class TestProbeRunsAsAStandaloneScript:
    """探针要能作为独立进程跑起来 —— cron / 监控就是这么用的。

    插件目录名是 ``bi-gate``（带连字符），不是合法 Python 包名，所以
    ``python -m plugins.bi_gate.probe`` 跑不了；probe.py 必须在没有包上下文时
    也能取到 ``GATE_SOURCE``。
    """

    def test_gate_source_resolves_without_package_context(self):
        spec = importlib.util.spec_from_file_location("_probe_standalone", PLUGIN_DIR / "probe.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_probe_standalone"] = mod
        spec.loader.exec_module(mod)
        assert mod.__package__ in (None, "", "_probe_standalone")
        assert "门禁" in mod._gate_source()
