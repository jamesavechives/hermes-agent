"""bi-gate 部署自检 —— 一个 profile 的门禁到底装没装上、拦不拦得住。

和存活探针的分工
----------------
:mod:`probe` 只回答一个问题（门禁此刻还在吗），设计成能被 cron 反复跑、
输出一行 JSON、只看退出码。本脚本是**部署时跑一次**的自检：把整条链路
拆成五段逐段报告，方便新环境上线或排障时定位是哪一段断了。

五段分别是：

1. ``config.yaml`` 里的 ``plugins.enabled`` 有没有被 Hermes 读到 ——
   插件是 opt-in 的，漏这一行门禁完全不存在且没有任何报错；
2. 插件文件在不在应该在的目录里；
3. 真实派发路径上，非法调用是不是真的没让工具体跑起来 ——
   硬证据是工具体的执行计数，不是返回值长什么样；
4. 动作分级（L0–L3）判得对不对，越出 action_max 的调用有没有被拦；
5. 存活探针能不能正常出结果。

用法
----
    HERMES_HOME=/data/profiles/bi \\
    BI_GATE_REGISTRY=/data/profiles/bi/bi_registry.json \\
    BI_GATE_ACTION_POLICY=/data/profiles/bi/action_policy.json \\
    BI_GATE_ACTION_MAX=L1 \\
    PYTHONPATH=/opt/hermes \\
    python /opt/hermes/plugins/bi-gate/verify.py

退出码 0 = 五段全通；1 = 有环节没通；2 = 脚本自身跑不起来。

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
import logging
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

#: 动作分级用例。只在 profile 配了 BI_GATE_ACTION_POLICY 时跑；
#: 第三项的期望取决于该人格声明的 action_max，所以由运行时算，不写死。
ACTION_CASES = [
    ("汇总查询（应判 L0）", {"metric": "dau", "dimensions": ["market"],
        "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}),
    ("明细查询（应判 L1）", {"metric": "dau", "granularity": "row",
        "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}),
    ("导出（应判 L2）", {"metric": "dau", "export": True,
        "time_window": {"start": "2026-08-01", "end": "2026-08-21"}}),
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


class _AuditCollector(logging.Handler):
    """把 bi-gate 写出的判定记录收下来，用于验证"留痕"这一半。

    拦得住和记得对是两件事。2026-08-24 实测过一次：调用确实被拦了，
    但审计里同时留下一条"放行"，事后没法从日志里数出拒绝了多少次。
    所以自检不能只看拦没拦，还要看记录对不对。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "bi_gate_verdict" not in msg:
            return
        try:
            self.records.append(json.loads(msg.split("bi-gate verdict ", 1)[1]))
        except (IndexError, ValueError):
            pass


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

    # ── ④ 动作分级（action_max）────────────────────────────────────
    policy = gate._policy_now()
    action_max = gate._action_max_now()
    if policy is None:
        print("\n④ 动作分级：未启用（该 profile 没设 BI_GATE_ACTION_POLICY）")
        print("   分档标准要业务方与合规定，没定之前不启用是对的；其余门禁规则不受影响。")
        ok_action = True
    elif policy.unavailable:
        print("\n④ 动作分级：策略载入失败，所有调用都会被拒（fail-closed）")
        ok_action = False
    else:
        max_txt = gate.level_name(action_max) if action_max is not None else "未声明（按 L0）"
        print(f"\n④ 动作分级：策略 version={policy.version or '(未标版本)'}，"
              f"{len(policy.rules)} 条规则，该人格 action_max={max_txt}")
        ok_action = True
        effective_max = 0 if action_max is None else action_max
        audit = _AuditCollector()
        gate_logger = logging.getLogger(gate.__name__)
        gate_logger.addHandler(audit)
        gate_logger.setLevel(logging.INFO)
        for label, args in ACTION_CASES:
            before = len(_CALLS)
            result = model_tools.handle_function_call("query_metric", args)
            executed = len(_CALLS) - before
            level, why = gate.classify_action(args, policy)
            lv = gate.level_name(level) if level is not None else "判定不了"
            should_block = level is None or level > effective_max
            hit = (executed == 0) == should_block
            ok_action = ok_action and hit
            print(f"   [{'✓' if hit else '✗'}] {label:<20} 判为 {lv:<4} "
                  f"{'拦下' if executed == 0 else '放行'}（工具体执行 {executed} 次）· {why}")
        gate_logger.removeHandler(audit)

        # 留痕：每次调用都该留下恰好一条判定记录，且带级别。
        # "拦得住"和"记得对"是两件事，这里验后一半。
        got = len(audit.records)
        want = len(ACTION_CASES)
        with_level = [r for r in audit.records if r.get("action_level")]
        contradictory = [r for r in audit.records
                         if r.get("gate_result") == "passed" and r.get("action_level") is None]
        ok_audit = got == want and len(with_level) == want and not contradictory
        ok_action = ok_action and ok_audit
        print(f"   [{'✓' if ok_audit else '✗'}] 留痕：{got}/{want} 条判定记录，"
              f"其中 {len(with_level)} 条带动作级别")
        if not ok_audit:
            print(f"      记录内容：{audit.records!r}")

    # ── ⑤ 存活探针 ────────────────────────────────────────────────
    probe = _load_from_path(
        "hermes_plugins.bi_gate.probe", plugin_dir / "probe.py",
        package="hermes_plugins.bi_gate",
    )
    result = probe.probe()
    print(f"\n⑤ 存活探针: {result.status} (exit={result.exit_code})")
    ok4 = result.status == probe.ALIVE

    allok = ok1 and ok2 and ok3 and ok_action and ok4
    print("\n结论：" + ("这个 profile 的门禁生效 ✓" if allok else "有环节未通 ✗"))
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
