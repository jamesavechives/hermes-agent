"""人格之间的隔离。

设计方案 §十二 把这一条列成了缺口：「profile 之间的隔离没有实测证据 ——
『互不影响』目前只是设计上成立」。这个文件把它补上，**并且划清楚边界**：
哪种部署形态下隔离成立，哪种不成立。

两种形态，结论不一样
--------------------
**一人格一进程（现实部署形态，也是阶段三的一人格一容器）** —— 隔离成立。
门禁的注册表、动作策略、动作上限、会话计数器都是**进程级全局**，各进程一份，
天然不串。下面 :func:`test_two_profiles_in_separate_processes_are_isolated`
用真子进程验这一条。

**同一进程里切换 HERMES_HOME** —— **隔离不成立**。那几个全局变量没有按 profile
键控，第一个 profile 载入之后就一直是它的值。这不是 bug，是没打算支持的用法；
但不写下来，别人会以为隔离是无条件的。:func:`test_same_process_does_not_isolate`
把这个事实测出来 —— 它红了说明有人加了按 profile 的缓存键，那时候该来更新这份
文档，而不是删掉测试。

还有一处即使分进程也会串
------------------------
会话累计预算是从审计文件播种回来的（进程重启不能清零）。两个人格如果**共用一个
审计文件**，只按 session_id 过滤就会把对方放行过的扫描量算进自己的额度 ——
表现是"莫名其妙额度就满了"，而且查不出为什么。所以播种同时按 profile 过滤，
:func:`test_session_seeding_does_not_bleed_across_profiles` 守这一条。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"


def _make_profile(root: Path, name: str, *, metric: str, action_max: str,
                  session_max: int, audit: Path) -> Path:
    """造一个 profile 目录。两个 profile 的注册表/上限/预算刻意都不一样。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "bi_registry.json").write_text(json.dumps({
        "default_timezone": "UTC+8",
        "metrics": [{"name": metric, "dimensions": ["market"],
                     "requires_time_window": True,
                     "rows_per_day": 1_000_000, "max_scan_rows": 50_000_000}],
    }), encoding="utf-8")
    (d / "action_policy.json").write_text(json.dumps({
        "version": "iso", "default_level": "L0",
        "rules": [{"level": "L2", "label": "导出",
                   "when": {"param_equals": {"export": True}}}],
    }), encoding="utf-8")
    (d / "env.json").write_text(json.dumps({
        "HERMES_HOME": str(d),
        "BI_GATE_REGISTRY": str(d / "bi_registry.json"),
        "BI_GATE_ACTION_POLICY": str(d / "action_policy.json"),
        "BI_GATE_ACTION_MAX": action_max,
        "BI_GATE_TOOLS": "query_metric",
        "BI_GATE_SESSION_SCAN_MAX": str(session_max),
        "BI_AUDIT_LOG": str(audit),
    }), encoding="utf-8")
    return d


PROBE = textwrap.dedent('''
    import importlib.util, json, os, sys
    from pathlib import Path
    REPO, PROFILE = Path(sys.argv[1]), Path(sys.argv[2])
    os.environ.update(json.loads((PROFILE / "env.json").read_text(encoding="utf-8")))
    sys.path.insert(0, str(REPO))
    PD = REPO / "plugins" / "bi-gate"
    spec = importlib.util.spec_from_file_location(
        "g", PD / "__init__.py", submodule_search_locations=[str(PD)])
    g = importlib.util.module_from_spec(spec); sys.modules["g"] = g
    spec.loader.exec_module(g); g.reload_registry()

    W = {"start": "2026-08-01", "end": "2026-08-05", "timezone": "UTC+8"}
    out = {"profile": g._profile_name(), "metrics": g._registry_now().names}
    for label, args in json.loads(sys.argv[3]).items():
        a = dict(args)
        if a.pop("_window", True):
            a["time_window"] = dict(W)
        v = g._on_pre_tool_call(tool_name="query_metric", args=a)
        out[label] = "blocked" if (v or {}).get("action") == "block" else "allowed"
    print(json.dumps(out, ensure_ascii=False))
''')


def _run(profile: Path, cases: dict, script: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(script), str(REPO), str(profile), json.dumps(cases)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture()
def script(tmp_path):
    p = tmp_path / "probe.py"
    p.write_text(PROBE, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------

def test_two_profiles_in_separate_processes_are_isolated(tmp_path, script):
    """分进程 = 真实部署形态。注册表、动作上限各管各的。

    这就是 §十二 缺失的那份实测证据。
    """
    a_audit, b_audit = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a = _make_profile(tmp_path, "persona-a", metric="metric_a",
                      action_max="L1", session_max=100_000_000, audit=a_audit)
    b = _make_profile(tmp_path, "persona-b", metric="metric_b",
                      action_max="L3", session_max=100_000_000, audit=b_audit)

    cases = {
        "own": {"metric": "metric_a", "dimensions": ["market"]},
        "other": {"metric": "metric_b", "dimensions": ["market"]},
        "export": {"metric": "metric_a", "dimensions": ["market"], "export": True},
    }
    ra = _run(a, cases, script)
    rb = _run(b, {**cases, "own": {"metric": "metric_b", "dimensions": ["market"]},
                  "other": {"metric": "metric_a", "dimensions": ["market"]},
                  "export": {"metric": "metric_b", "dimensions": ["market"], "export": True}},
              script)

    assert ra["profile"] == "persona-a" and rb["profile"] == "persona-b"
    assert ra["metrics"] == ["metric_a"] and rb["metrics"] == ["metric_b"]

    # 各自只认自己的指标
    assert ra["own"] == "allowed" and ra["other"] == "blocked"
    assert rb["own"] == "allowed" and rb["other"] == "blocked"

    # 动作上限也各管各的：A 是 L1 拦不住导出（L2），B 是 L3 放行
    assert ra["export"] == "blocked", "persona-a 的 action_max=L1 应拦下 L2 的导出"
    assert rb["export"] == "allowed", "persona-b 的 action_max=L3 应放行"

    # 审计各写各的文件
    assert a_audit.exists() and b_audit.exists()
    for path, name in ((a_audit, "persona-a"), (b_audit, "persona-b")):
        profiles = {json.loads(l).get("profile")
                    for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}
        assert profiles == {name}, f"{path.name} 里混进了别的人格的记录：{profiles}"


def test_audit_records_carry_the_profile(tmp_path, script):
    """审计记录必须带 profile。

    没有这一项，一旦有第二个人格就回答不了「这条判定是谁做的」——
    而那正是合规会问的第一个问题。
    """
    audit = tmp_path / "a.jsonl"
    a = _make_profile(tmp_path, "persona-a", metric="m", action_max="L1",
                      session_max=100_000_000, audit=audit)
    _run(a, {"one": {"metric": "m", "dimensions": ["market"]}}, script)
    recs = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert recs and all(r.get("profile") == "persona-a" for r in recs)


def test_session_seeding_does_not_bleed_across_profiles(tmp_path):
    """共用一个审计文件时，会话预算的播种不能把对方的量算进来。

    这是**即使分进程也会串**的那一处：会话计数从审计文件播种回来（进程重启
    不能清零），只按 session_id 过滤就会把对方放行过的扫描量算进自己的额度。
    表现是"莫名其妙额度就满了"，而且查不出为什么。
    """
    import importlib.util
    audit = tmp_path / "shared.jsonl"
    with open(audit, "w", encoding="utf-8") as fh:
        for profile, rows in (("persona-a", 30_000_000), ("persona-b", 40_000_000)):
            fh.write(json.dumps({"event": "bi_gate_verdict", "gate_result": "passed",
                                 "profile": profile, "session_id": "S1",
                                 "estimated_rows": rows, "ts": 1}) + "\n")
        # 没有 profile 的历史记录：要算进来，否则升级那一刻所有会话的历史额度
        # 突然归零 —— 那是把兼容问题变成一次静默的约束放宽。
        fh.write(json.dumps({"event": "bi_gate_verdict", "gate_result": "passed",
                             "session_id": "S1", "estimated_rows": 5_000_000, "ts": 1}) + "\n")

    home = tmp_path / "persona-a"
    home.mkdir(exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["BI_AUDIT_LOG"] = str(audit)
    try:
        spec = importlib.util.spec_from_file_location(
            "iso_gate", PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)])
        g = importlib.util.module_from_spec(spec)
        sys.modules["iso_gate"] = g
        spec.loader.exec_module(g)
        seeded = g._seed_session_from_audit("S1")
    finally:
        os.environ.pop("HERMES_HOME", None)
        os.environ.pop("BI_AUDIT_LOG", None)

    assert seeded == 35_000_000, (
        f"应只算 persona-a 的 3000 万 + 无 profile 的 500 万，实际 {seeded:,}")


def test_same_process_does_not_isolate(tmp_path):
    """**同一进程里换 HERMES_HOME，隔离不成立** —— 这是测量结果，不是期望。

    注册表、动作策略、动作上限都是进程级全局，没有按 profile 键控：第一个
    profile 载入之后就一直是它的值。一人格一进程的部署形态下这不会发生，
    但不写下来，别人会以为隔离是无条件的。

    这条红了说明有人加了按 profile 的缓存键 —— 那时候该来更新设计方案 §十二
    和这段注释，而不是删掉测试。
    """
    import importlib.util
    a = _make_profile(tmp_path, "pa", metric="metric_a", action_max="L1",
                      session_max=100_000_000, audit=tmp_path / "a.jsonl")
    b = _make_profile(tmp_path, "pb", metric="metric_b", action_max="L1",
                      session_max=100_000_000, audit=tmp_path / "b.jsonl")

    spec = importlib.util.spec_from_file_location(
        "iso_gate2", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    g = importlib.util.module_from_spec(spec)
    sys.modules["iso_gate2"] = g
    saved = {k: os.environ.get(k) for k in
             ("HERMES_HOME", "BI_GATE_REGISTRY", "BI_AUDIT_LOG")}
    try:
        os.environ.update(json.loads((a / "env.json").read_text(encoding="utf-8")))
        spec.loader.exec_module(g)
        g.reload_registry()
        assert g._registry_now().names == ["metric_a"]

        # 切到 B，但不调 reload_registry —— 这正是"同进程跑两个人格"会发生的事
        os.environ.update(json.loads((b / "env.json").read_text(encoding="utf-8")))
        assert g._registry_now().names == ["metric_a"], (
            "如果这里变成了 metric_b，说明缓存已按 profile 键控 —— "
            "去更新设计方案 §十二，隔离结论要改")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
