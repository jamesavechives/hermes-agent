"""桥接工具与工具白名单的关系 —— 一次写错又更正的实测。

背景
----
Hermes 有三个「桥接工具」：``tool_search`` 找工具、``tool_describe`` 读某个
工具的参数 schema、``tool_call`` 按名字调用另一个工具。

**2026-08-27 我在这里写下的结论是错的**，错法值得留档。

当时读到 ``model_tools.handle_function_call`` 里 ``is_bridge_tool()`` 分支排在
pre_tool_call 派发点之前（``model_tools.py:1267`` 对 ``:1387``），实测也确实
穿了过去，于是断定「桥接工具不受门禁管，泄的是侦察面」。

**漏了：Hermes 有三个 pre_tool_call 派发点，真实 agent 走的不是那条。**

- ``agent/tool_executor.py:627`` —— 真实 agent 循环，**在派发之前**触发，
  对所有工具一视同仁；之后往下游传 ``skip_pre_tool_call_hook=True`` 防重复。
- ``agent/agent_runtime_helpers.py:3085`` —— 另一条运行时路径。
- ``model_tools.py:1387`` —— 直接调 ``handle_function_call`` 才会到，
  在桥接分支之后。**我当时测的是这条。**

而且 executor 对 ``tool_call`` 做的是**拆包再派发**（``:1144``，注释原文
"hooks must observe the real tool name"）—— hook 看到的是底层工具名，不是
``tool_call``。比我原先以为的更强。

8/28 用 qwen3.7-plus 真跑一轮验证：模型开局调 ``tool_search`` /
``tool_describe``，审计里两条都是 ``rejected_tool_not_allowed``。**拦住了。**

这个错的形状
------------
**测了一条真实系统不走的入口。** 和调研 §二第六条同类 —— 那次是探针命令在
44 个测试全绿的情况下根本执行不了，因为测试都按文件路径 import、没人真按
文档敲一次。判断一个实测算不算数，得先回答「真实系统走的是这条吗」。

这份文件现在测的
----------------
1. 插件自己不留代码级豁免（唯一一条我们完全说了算的）；
2. 真实 agent 路径的派发顺序 —— hook 在派发**之前**；
3. 借道 ``tool_call`` 调白名单外的工具必须拦住；
4. 我们的 hook 在任何输入下都不许抛异常 —— 因为宿主那层套着
   ``except Exception: return None``，**抛了就等于放行**。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gate(monkeypatch):
    """载入插件本体，白名单里只有 query_metric。"""
    monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
    monkeypatch.setenv("BI_GATE_REGISTRY", str(PLUGIN_DIR / "registry.example.json"))
    mod = _load("bi_gate_bridge_test", PLUGIN_DIR / "__init__.py")
    mod.reload_registry()
    return mod


# ---------------------------------------------------------------------------
# 一、不给我们自己的插件留豁免
# ---------------------------------------------------------------------------

def test_whitelist_has_no_builtin_exemptions(gate, monkeypatch):
    """白名单就是环境变量本身，代码里没有任何常驻允许项。

    这条是本文件真正的守卫：宿主的豁免我们改不了，但**我们自己不能再加一层**。
    「某某工具无害所以默认放行」是最容易被写进去的一行代码，也是最难被发现的
    那种 —— 它不报错、不留痕，只在有人真去调那个工具时才显形。
    """
    monkeypatch.setenv("BI_GATE_TOOLS", "")
    assert gate._allowed_tools_now() == frozenset()

    # 逐个试探那些"看着无害"的名字，一个都不许自动通过。
    for name in ("tool_describe", "tool_search", "tool_call", "help",
                 "noop", "ping", "whoami", "list_tools", "describe"):
        verdict = gate._on_pre_tool_call(tool_name=name, args={})
        assert verdict is not None and verdict.get("action") == "block", (
            f"{name!r} 在空白名单下被放行了 —— 插件里出现了代码级豁免"
        )


def test_declared_bridge_name_is_allowed_like_any_other(gate, monkeypatch):
    """自省工具**可以**被声明，走的是和别的工具一样的那条路。

    结论层面：不禁止业务方在人格声明里写 ``tool_describe``；禁止的是绕过声明。
    """
    monkeypatch.setenv("BI_GATE_TOOLS", "query_metric,tool_describe")
    assert gate._on_pre_tool_call(tool_name="tool_describe", args={}) is None
    assert gate._on_pre_tool_call(tool_name="tool_search", args={}) is not None


# ---------------------------------------------------------------------------
# 二、宿主派发结构的当前事实
# ---------------------------------------------------------------------------

def test_real_agent_path_fires_the_hook_before_dispatch():
    """真实 agent 走的那条路：hook 在派发之前，对所有工具生效。

    这是**更正后**该守的那条。原先这里断言的是 ``model_tools.py`` 里桥接分支
    排在 hook 之前 —— 那句话是真的，但真实 agent 不走那个入口，所以它红不红
    都说明不了门禁有没有拦住桥接工具。

    这条红了，说明上游把 executor 的派发顺序动了 —— 那才是真该去重测的时刻。
    """
    text = (REPO / "agent" / "tool_executor.py").read_text(encoding="utf-8")

    assert "_dispatch_pre_tool_call_hooks(" in text, (
        "tool_executor 里找不到 pre_tool_call 派发 —— 上游结构变了，"
        "桥接工具是否仍被门禁覆盖需要重新实测"
    )
    # executor 派发之后往下游传 skip=True，靠这个避免 model_tools 那层重复触发。
    # 反过来说：这个标记在，就证明 executor 这层确实已经触发过了。
    assert "skip_pre_tool_call_hook=True" in text, (
        "下游 skip 标记没了 —— executor 可能不再是首个派发点"
    )
    # tool_call 拆包：hook 看到底层工具名而不是桥接名。
    assert "hooks must observe the real tool name" in text, (
        "tool_call 的拆包注释没了 —— 确认 hook 看到的还是不是底层工具名"
    )


def test_our_hook_never_raises(gate):
    """我们的 hook 在任何输入下都不许抛异常 —— **抛了等于放行**。

    ``tool_executor.py`` 里调 hook 那段外面套着 ``except Exception: return None``
    （见 ``_resolve_pre_tool_block``），返回 None 的含义是「没有插件要拦」。
    所以我们这边一个未捕获的异常不会变成报错、不会留痕，只会安静地把这次调用
    放过去 —— 是失效开门，不是失效关门。

    这不是假想：门禁里有 json 解析、有类型转换、有注册表查找，任何一处对畸形
    参数不设防都会走到这里。
    """
    hostile = [
        {},
        {"metric": None},
        {"metric": 123},
        {"metric": ["a"]},
        {"metric": {"nested": "dict"}},
        {"metric": "daily_active_users", "time_window": "不是字典"},
        {"metric": "daily_active_users", "time_window": {"timezone": []}},
        {"metric": "daily_active_users", "time_window": {"start": object()}},
        {"metric": "daily_active_users", "max_scan_rows": "很多"},
        {"metric": "daily_active_users", "filters": float("nan")},
        {"metric": "\x00\uffff"},
        {"metric": "x" * 100000},
    ]
    for tool in ("query_metric", "tool_search", "什么都不是"):
        for args in hostile:
            try:
                gate._on_pre_tool_call(tool_name=tool, args=args)
            except Exception as exc:          # noqa: BLE001 —— 就是要抓一切
                raise AssertionError(
                    f"hook 对 tool={tool!r} args={args!r} 抛了 {type(exc).__name__}: {exc}。"
                    f"宿主会把它当成「无人拦截」，这次调用就放行了"
                ) from exc

    # 防空转：上面那堆输入必须真的走进门禁逻辑，而不是在某个早退分支就返回了。
    # 否则这个测试会在门禁被整个短路的情况下依然全绿 —— 那正是它要防的失效。
    assert gate._on_pre_tool_call(tool_name="什么都不是", args={}) is not None, (
        "白名单外的工具没被拦 —— 上面那轮压根没走到门禁逻辑，本测试是空转的"
    )
    assert gate._on_pre_tool_call(tool_name="query_metric", args={"metric": 123}) is not None, (
        "畸形 metric 没被拦 —— 同上"
    )

    # 参数不是 dict 的极端情况单独试 —— 宿主理论上不会这么传，但门禁不该假设。
    for bad_args in (None, "字符串", 42, []):
        try:
            gate._on_pre_tool_call(tool_name="query_metric", args=bad_args)
        except Exception as exc:              # noqa: BLE001
            raise AssertionError(
                f"hook 对 args={bad_args!r} 抛了 {type(exc).__name__} —— 同样等于放行"
            ) from exc


def test_tool_call_cannot_smuggle_a_blocked_tool(gate, monkeypatch, tmp_path):
    """``tool_call`` 借道调白名单外的工具，必须被拦。

    这条是执行面的底线。它红了不是文档要更新，是真出事了。
    """
    import model_tools
    from hermes_cli import plugins as plugins_mod
    from tools import registry as tool_registry

    calls = []

    def _victim(args, **_kw):
        calls.append(args)
        return json.dumps({"ok": True})

    tool_registry.registry.register(
        name="bi_gate_smuggle_victim",
        toolset="bi_gate_bridge_test",
        schema={"name": "bi_gate_smuggle_victim", "description": "白名单外的工具",
                "parameters": {"type": "object", "properties": {}}},
        handler=_victim,
        override=True,
    )

    def _invoke_hook(hook_name, **kwargs):
        if hook_name != "pre_tool_call":
            return []
        out = gate._on_pre_tool_call(**kwargs)
        return [out] if out is not None else []

    monkeypatch.setattr(plugins_mod, "invoke_hook", _invoke_hook)

    # 直调：必须被拦（这也确认 hook 确实挂上了，否则下面那条测的是空气）
    direct = model_tools.handle_function_call("bi_gate_smuggle_victim", {})
    assert gate._is_gate_block(direct), f"直调都没拦住，hook 没挂上：{direct!r}"
    assert calls == []

    # 借道 tool_call：也必须被拦，且工具体一次都不许执行
    routed = model_tools.handle_function_call(
        "tool_call", {"name": "bi_gate_smuggle_victim", "arguments": {}})
    assert calls == [], f"白名单外的工具通过 tool_call 被执行了：{routed!r}"
