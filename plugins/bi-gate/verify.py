"""bi-gate 部署自检 —— 一个 profile 的门禁到底装没装上、拦不拦得住。

和存活探针的分工
----------------
:mod:`probe` 只回答一个问题（门禁此刻还在吗），设计成能被 cron 反复跑、
输出一行 JSON、只看退出码。本脚本是**部署时跑一次**的自检：把整条链路
拆成四段逐段报告，方便新环境上线或排障时定位是哪一段断了。

四段分别是：

1. ``config.yaml`` 里的 ``plugins.enabled`` 有没有被 Hermes 读到 ——
   插件是 opt-in 的，漏这一行门禁完全不存在且没有任何报错；
2. 插件文件在不在应该在的目录里；
3. 真实派发路径上，非法调用是不是真的没让工具体跑起来 ——
   硬证据是工具体的执行计数，不是返回值长什么样；
4. 存活探针能不能正常出结果。

用法
----
    HERMES_HOME=/data/profiles/bi \\
    BI_GATE_REGISTRY=/data/profiles/bi/bi_registry.json \\
    PYTHONPATH=/opt/hermes \\
    python /opt/hermes/plugins/bi-gate/verify.py

退出码 0 = 四段全通；1 = 有环节没通；2 = 脚本自身跑不起来。

仓库根目录默认按本文件位置往上推两级；装在别处时用 ``HERMES_REPO`` 覆盖。

为什么要真的注册一个假 query_metric
-----------------------------------
探针独立跑时 ``query_metric`` 这个工具本身并不存在，于是"门禁不在"和
"工具不在"两种情况都表现为拦不住。本脚本先注册一个会计数的假工具再打，
所以第 3 段的结论是干净的：工具确实存在，调用确实被门禁挡在了工具体之前。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

REPO = Path(os.environ.get("HERMES_REPO") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(REPO))

#: 假 query_metric 的执行次数。工具体跑了才加一 —— 这是"有没有被真正拦住"的唯一硬证据。
_CALLS: list = []

#: (说明, 参数, 是否应当被拦)
CASES = [
    ("未注册指标 revenue_v2", {"metric": "revenue_v2",
        "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}, True),
    ("已注册但没时间窗 dau", {"metric": "dau"}, True),
    ("已注册但维度非法 dau", {"metric": "dau", "dimensions": ["uid"],
        "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}, True),
    ("相对时间窗 last_7d", {"metric": "dau",
        "time_window": {"start": "last_7d", "end": "now"}}, True),
    ("合法调用 dau", {"metric": "dau", "dimensions": ["market"],
        "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}, False),
]


def _load_from_path(name: str, path: Path, package: str = "", is_package: bool = False):
    """按文件路径加载模块。

    插件目录名带连字符（``bi-gate``），不是合法的 Python 包名，正常 import
    走不通，只能这么加载 —— 仓库里的测试和 Hermes 自身也是这个做法。
    """
    locations = [str(path.parent)] if is_package else None
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=locations)
    if spec is None or spec.loader is None:
        raise ImportError(f"读不到 {path}")
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    if is_package:
        module.__path__ = [str(path.parent)]
    # 必须先登记再执行：模块里用了 @dataclass，dataclasses 会按 cls.__module__
    # 回查 sys.modules，查不到就报 AttributeError。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    home = os.environ.get("HERMES_HOME")
    if not home:
        print("没有设 HERMES_HOME —— 这个脚本是按 profile 跑的，必须指明是哪一个。", file=sys.stderr)
        return 2

    print(f"HERMES_REPO = {REPO}")
    print(f"HERMES_HOME = {home}")
    cfg_path = Path(home) / "config.yaml"
    cfg_txt = cfg_path.read_text(encoding="utf-8").strip() if cfg_path.exists() else "<没有 config.yaml>"
    print(f"config.yaml = {cfg_txt!r}")

    # ── ① config 里的白名单是否被 Hermes 读到 ──────────────────────
    from hermes_cli.plugins import (
        _get_enabled_plugins,
        _get_disabled_plugins,
        get_bundled_plugins_dir,
    )

    enabled = _get_enabled_plugins()
    print(f"\n① Hermes 读到的启用白名单: {enabled}   禁用: {_get_disabled_plugins()}")
    ok1 = enabled is not None and "bi-gate" in enabled
    if not ok1:
        print("   ↑ bi-gate 不在白名单里。插件是 opt-in 的，缺这一行门禁完全不存在，"
              "且不会报任何错 —— 后面几段会具体演示后果。")

    # ── ② 插件文件在不在 ──────────────────────────────────────────
    bundled = get_bundled_plugins_dir()
    plugin_dir = bundled / "bi-gate"
    ok2 = (plugin_dir / "plugin.yaml").exists()
    print(f"② bi-gate 在插件目录里: {ok2}  ({plugin_dir})")
    if not ok2:
        print("   ↑ 文件都不在，后面没法验了。")
        return 1

    # ── ③ 真实派发路径 ────────────────────────────────────────────
    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = []
    sys.modules.setdefault("hermes_plugins", ns)
    gate = _load_from_path(
        "hermes_plugins.bi_gate", plugin_dir / "__init__.py",
        package="hermes_plugins.bi_gate", is_package=True,
    )
    gate.reload_registry()
    print(f"   载入的指标注册表: {gate._registry_now().names}")

    from hermes_cli import plugins as plugins_mod
    from tools import registry as tool_registry
    import model_tools

    def _fake_query_metric(args, **_kwargs):
        _CALLS.append(args)
        return json.dumps({"rows": [{"dau": 12345}]})

    tool_registry.registry.register(
        name="query_metric",
        toolset="bi_gate_verify",
        schema={"name": "query_metric", "description": "verify double",
                "parameters": {"type": "object", "properties": {}}},
        handler=_fake_query_metric,
        override=True,
    )

    # 只有 config 白名单里有 bi-gate 时才挂 hook —— 复现 Hermes 的 opt-in 加载行为。
    # 直接替换 invoke_hook 而不走 PluginManager 的发现流程，是为了不连带加载一堆
    # 可选插件；派发路径本身仍是仓库自己的 _dispatch_pre_tool_call_hooks。
    if ok1:
        def _invoke_hook(hook_name, **kwargs):
            if hook_name != "pre_tool_call":
                return []
            out = gate._on_pre_tool_call(**kwargs)
            return [out] if out is not None else []
    else:
        def _invoke_hook(hook_name, **kwargs):
            return []

    plugins_mod.invoke_hook = _invoke_hook

    print("\n③ 派发路径实测：")
    ok3 = True
    for label, args, should_block in CASES:
        before = len(_CALLS)
        result = model_tools.handle_function_call("query_metric", args)
        executed = len(_CALLS) - before
        blocked = executed == 0
        hit = blocked == should_block
        ok3 = ok3 and hit
        note = " ← 拦截理由写明了来源" if blocked and gate._is_gate_block(result) else ""
        print(f"   [{'✓' if hit else '✗'}] {label:<22} "
              f"{'拦下' if blocked else '放行'}（工具体执行 {executed} 次）{note}")

    # ── ④ 存活探针 ────────────────────────────────────────────────
    probe = _load_from_path(
        "hermes_plugins.bi_gate.probe", plugin_dir / "probe.py",
        package="hermes_plugins.bi_gate",
    )
    result = probe.probe()
    print(f"\n④ 存活探针: {result.status} (exit={result.exit_code})")
    ok4 = result.status == probe.ALIVE

    allok = ok1 and ok2 and ok3 and ok4
    print("\n结论：" + ("这个 profile 的门禁生效 ✓" if allok else "有环节未通 ✗"))
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
