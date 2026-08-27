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

#: 固定的绝对时间窗，所有用例共用。
_WINDOW = {"start": "2026-08-01", "end": "2026-08-21"}


def build_cases(registry):
    """按**当前 profile 实际注册的指标**生成用例，不假设指标叫什么。

    早先这里把探测指标硬编码成 ``dau``，而 ``registry.example.json`` 注册的是
    ``daily_active_users`` —— 照着 DEPLOY.md 用样例表建 profile 再跑自检，
    会看到「合法调用被拦」的红色结论，但门禁其实是对的：那个指标本来就没注册，
    拦下来才是正确行为。自检脚本要检的是「这个 profile 的链路通不通」，
    所以正例必须从它自己的注册表里取。（2026-08-26 在 dev 机上首次部署时发现）

    返回 (CASES, ACTION_CASES, probe_name)；注册表为空时 probe_name 为 None，
    只跑得了否定用例。
    """
    probe = None
    for name in registry.names:
        spec = registry.get(name)
        if spec and spec.requires_time_window and spec.dimensions:
            probe = (name, sorted(spec.dimensions)[0])
            break
    if probe is None:
        for name in registry.names:
            spec = registry.get(name)
            if spec:
                probe = (name, sorted(spec.dimensions)[0] if spec.dimensions else None)
                break

    cases = [
        ("未注册指标 revenue_v2", {"metric": "revenue_v2", "time_window": _WINDOW}, True),
    ]
    if probe is None:
        return cases, [], None

    m, dim = probe
    spec = registry.get(m)
    bad_dim = "__no_such_dim__"
    if spec and spec.requires_time_window:
        cases.append((f"已注册但没时间窗 {m}", {"metric": m}, True))
    cases.append((f"已注册但维度非法 {m}",
                  {"metric": m, "dimensions": [bad_dim], "time_window": _WINDOW}, True))
    cases.append((f"相对时间窗 last_7d",
                  {"metric": m, "time_window": {"start": "last_7d", "end": "now"}},
                  bool(spec and spec.requires_time_window)))
    legit = {"metric": m, "time_window": _WINDOW}
    if dim:
        legit["dimensions"] = [dim]
    cases.append((f"合法调用 {m}", legit, False))

    action_cases = [
        (f"汇总查询（应判 L0）", dict(legit)),
        (f"明细查询（应判 L1）", {"metric": m, "granularity": "row", "time_window": _WINDOW}),
        (f"导出（应判 L2）", {"metric": m, "export": True, "time_window": _WINDOW}),
    ]
    return cases, action_cases, m


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
    _reg = gate._registry_now()
    print(f"   载入的指标注册表: {_reg.names}")
    CASES, ACTION_CASES, _probe_metric = build_cases(_reg)
    if _probe_metric is None:
        print("   ⚠ 注册表为空 —— 只能跑否定用例，正例与动作分级跳过")
    else:
        print(f"   自检探测指标: {_probe_metric}（从该 profile 的注册表里选的）")

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

    # ── 判据自检 ─────────────────────────────────────────────────
    # 早先这里用「替身被执行了几次」当判据：0 次即视为拦下。bi-query 插件落地
    # 之后 query_metric 有了真实现，上面注册的替身不一定是真正被派发的那个，
    # 于是计数恒为 0，**每个正例都会误报成「被拦」**——检查器开始测空气，
    # 而且方向是红的，看起来像门禁出了问题，实际门禁完全正常。
    # （2026-08-27 加 §⑦ 时顺手撞见的。）
    #
    # 所以先花一次调用确认替身还活着，再决定用哪个判据：
    #   替身活着 → 「执行次数」，这是更强的证据（工具体一次都没跑）
    #   替身没跑 → 「返回值里有没有门禁来源」，与 probe.py 同一个判据
    double_live = False
    if _probe_metric is not None:
        _legit = next((a for lbl, a, sb in CASES if not sb), None)
        if _legit is not None:
            _before = len(_CALLS)
            model_tools.handle_function_call("query_metric", dict(_legit))
            double_live = len(_CALLS) > _before

    def _blocked(result, executed):
        return (executed == 0) if double_live else gate._is_gate_block(result)

    print("\n③ 派发路径实测：")
    if not double_live:
        print("   判据：返回值里有没有门禁来源（本环境 query_metric 有真实现，"
              "替身没被派发，「执行次数」这个判据不成立）")
    ok3 = True
    for label, args, should_block in CASES:
        before = len(_CALLS)
        result = model_tools.handle_function_call("query_metric", args)
        executed = len(_CALLS) - before
        blocked = _blocked(result, executed)
        hit = blocked == should_block
        ok3 = ok3 and hit
        note = " ← 拦截理由写明了来源" if blocked and gate._is_gate_block(result) else ""
        detail = f"（工具体执行 {executed} 次）" if double_live else ""
        print(f"   [{'✓' if hit else '✗'}] {label:<22} "
              f"{'拦下' if blocked else '放行'}{detail}{note}")

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
            blocked = _blocked(result, executed)
            hit = blocked == should_block
            ok_action = ok_action and hit
            detail = f"（工具体执行 {executed} 次）" if double_live else ""
            print(f"   [{'✓' if hit else '✗'}] {label:<20} 判为 {lv:<4} "
                  f"{'拦下' if blocked else '放行'}{detail}· {why}")
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

    # ── ⑥ 声明完整性 ──────────────────────────────────────────────
    # 这一段不判对错，只把「哪些约束这个 profile 根本没声明」摆出来。
    # 没声明的约束不会报错、不会拦人、也不会出现在任何日志里——那正是
    # 这套门禁一路在堵的失效方式，所以要在部署自检里显式点名。
    print("\n⑥ 声明完整性（未声明 = 该约束不生效，不是安全默认）：")
    declared = []
    missing = []
    (declared if os.environ.get("BI_GATE_TOOLS", "").strip() else missing).append(
        ("BI_GATE_TOOLS", "工具白名单（未声明则拦一切）"))
    (declared if os.environ.get("BI_GATE_ACTION_POLICY", "").strip() else missing).append(
        ("BI_GATE_ACTION_POLICY", "动作分级策略（未声明则不分级）"))
    (declared if os.environ.get("BI_GATE_ACTION_MAX", "").strip() else missing).append(
        ("BI_GATE_ACTION_MAX", "动作上限（未声明按 L0）"))
    (declared if os.environ.get("BI_GATE_SESSION_SCAN_MAX", "").strip() else missing).append(
        ("BI_GATE_SESSION_SCAN_MAX", "会话累计扫描预算（未声明则只有单次限额）"))
    (declared if os.environ.get("BI_AUDIT_LOG", "").strip()
        or os.environ.get("HERMES_HOME", "").strip() else missing).append(
        ("BI_AUDIT_LOG", "审计落盘路径（未声明则本次判定不留痕）"))
    for name, what in declared:
        print(f"   [✓] {name:<28} {what}")
    for name, what in missing:
        print(f"   [ ] {name:<28} {what}  ← 未声明")

    reg = _reg
    no_rows_per_day = [
        n for n in reg.names
        if (sp := reg.get(n)) and sp.max_scan_rows is not None and sp.rows_per_day is None
    ]
    if no_rows_per_day:
        print(f"   [✗] 注册表里声明了 max_scan_rows 却没有 rows_per_day 的指标："
              f"{'、'.join(no_rows_per_day)} —— 这些指标的任何查询都会被拒")

    # ── ⑦ 宿主级豁免：桥接工具 ────────────────────────────────────
    # 这一段不是我们的插件能决定的事，是 Hermes 本身的派发结构决定的：
    # ``model_tools.handle_function_call`` 里，``is_bridge_tool()`` 分支在
    # pre_tool_call 派发点**之前**就 return 了（本仓库 model_tools.py:1267
    # 对 :1384）。于是三个桥接工具根本走不到任何 pre_tool_call hook。
    #
    # 所以这里不做断言，做测量：每次部署自检都实地打一遍，把当前事实印出来。
    # 写死成"已知它不受管"的话，等哪天上游把桥接也接进 hook，我们不会知道；
    # 而"以为拦住了其实没拦"正是这套门禁一路在堵的那种失效。
    print("\n⑦ 桥接工具（宿主级，不由本插件决定）：")
    bridge_cases = [
        ("tool_search", {"query": ""}, "枚举本会话可调度的工具名与描述"),
        ("tool_describe", {"name": "__nonexistent__"}, "读任意可调度工具的参数 schema"),
    ]
    leaked = []
    for name, args, what in bridge_cases:
        try:
            raw = model_tools.handle_function_call(name, dict(args))
        except Exception as exc:
            print(f"   [?] {name:<14} 派发时异常：{exc}")
            continue
        if gate._is_gate_block(raw):
            print(f"   [✓] {name:<14} 被门禁拦下 —— 上游已把桥接接进 hook，本节可以删了")
        else:
            leaked.append(name)
            print(f"   [!] {name:<14} 不经过门禁 · {what}")

    # tool_call 是另一回事：它自己不过 hook，但会带着底层工具名递归回
    # handle_function_call，门禁在那一层拦得住。执行面因此是闭合的，
    # 泄的只是侦察面。这个区别必须实测，不能靠读代码得出。
    probe_underlying = None
    try:
        from tools import tool_search as _ts_probe
        from model_tools import get_tool_definitions as _get_defs
        _defs = _get_defs(quiet_mode=True, skip_tool_search_assembly=True) or []
        _scoped = sorted(_ts_probe.scoped_deferrable_names(_defs))
        # 挑一个不需要必填参数的，否则会被参数探针挡在派发之前，测不到递归。
        for _n in _scoped:
            if _ts_probe.validate_deferred_call_args(_n, {}) is None:
                probe_underlying = _n
                break
    except Exception:
        pass

    if probe_underlying is None:
        print("   [?] tool_call      本环境没有无必填参数的可调度工具，递归路径这次测不到")
        ok_bridge = True
    else:
        raw = model_tools.handle_function_call(
            "tool_call", {"name": probe_underlying, "arguments": {}})
        if gate._is_gate_block(raw):
            print(f"   [✓] tool_call      借道调 {probe_underlying} 被门禁拦下 —— 执行面闭合")
            ok_bridge = True
        else:
            print(f"   [✗] tool_call      借道调 {probe_underlying} **没被拦** —— 白名单可绕开")
            ok_bridge = False

    if leaked:
        print(f"   → 已知缺口：{'、'.join(leaked)} 绕过白名单。泄的是工具名与参数 schema，"
              f"不是执行权限。见设计方案 §5.3 与 §八。")

    allok = (ok1 and ok2 and ok3 and ok_action and ok4 and ok_bridge
             and not no_rows_per_day)
    print("\n结论：" + ("这个 profile 的门禁生效 ✓" if allok else "有环节未通 ✗"))
    if missing:
        print(f"注意：有 {len(missing)} 项约束未声明，它们此刻不生效（见 ⑥）。")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
