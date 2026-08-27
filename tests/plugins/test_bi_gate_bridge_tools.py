"""桥接工具与工具白名单的关系 —— 实测，不是读代码得出的。

背景
----
Hermes 有三个「桥接工具」：``tool_search`` 找工具、``tool_describe`` 读某个
工具的参数 schema、``tool_call`` 按名字调用另一个工具。

``model_tools.handle_function_call`` 里，``is_bridge_tool()`` 分支在
pre_tool_call 的派发点**之前**就 return 了。结果是：

- ``tool_search`` / ``tool_describe`` 走不到任何 hook —— 我们的白名单管不着；
- ``tool_call`` 自己也走不到，但它会带着底层工具名**递归回**
  ``handle_function_call``，那一次是完整派发，门禁在那里拦得住。

所以执行面是闭合的，泄的是侦察面（工具名、描述、参数 schema）。

为什么把「缺口存在」也写成测试
------------------------------
这三条断言的是**当前事实**，不是我们期望的状态。上游哪天把桥接接进 hook，
这里会红，我们就知道该去把设计方案 §八的已知缺口删掉。反过来，如果只在文档
里记一句「桥接不受管」，等它被修好了我们也不会知道，文档就开始骗人。

同一份理由的反面：如果哪天 ``tool_call`` 的递归被改掉了，
:func:`test_tool_call_cannot_smuggle_a_blocked_tool` 会红 —— 那是真出事了。
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

def test_bridge_branch_returns_before_the_hook_dispatch():
    """源码层面确认顺序：桥接分支在 pre_tool_call 派发点之前。

    这条用行号比对，粗糙但直接。它红了说明上游动了派发结构 —— 那正是需要有人
    去重测一遍 §⑦ 的时刻。
    """
    text = (REPO / "model_tools.py").read_text(encoding="utf-8")
    lines = text.splitlines()

    bridge_line = next(
        (i for i, ln in enumerate(lines) if "is_bridge_tool(function_name)" in ln), None)
    hook_line = next(
        (i for i, ln in enumerate(lines) if "_dispatch_pre_tool_call_hooks(" in ln
         and "import" not in ln), None)

    assert bridge_line is not None, "找不到桥接分支 —— 上游结构变了"
    assert hook_line is not None, "找不到 pre_tool_call 派发点 —— 上游结构变了"
    assert bridge_line < hook_line, (
        "桥接分支现在排在 hook 之后了 —— 如果桥接工具已经过 hook，"
        "设计方案 §八里那条已知缺口该删掉了"
    )


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
