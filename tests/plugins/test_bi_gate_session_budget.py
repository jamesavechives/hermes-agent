"""会话累计扫描预算。

为什么有这一层：单次限额约束的是单次调用，不是一次会话总共拉走多少。

2026-08-27 在 dev 机上实测出来的：半年的 daily_active_users 查询被单次限额
拦下（预估 2.172 亿 > 上限 5000 万），模型随即把它拆成 6 个月度查询逐月执行、
再自己求和。每一次调用都合规，7 次累计扫了 2.412 亿行 —— 比那次被拦的还多。

模型不是在对抗，它在完成用户交代的任务。规则守住了自己的字面，意图从旁边
流走了。这跟同一天早些时候「被拦下 export 就改用文件工具写 CSV」是同一种
形状：前者换工具绕过，后者拆调用绕过。

这组测试钉住两件事：拆分绕不过去；进程重启也绕不过去。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate"

WINDOW_20D = {"start": "2026-08-01", "end": "2026-08-21"}


def _load(modname: str = "bi_gate_session_under_test"):
    spec = importlib.util.spec_from_file_location(
        modname, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """一个 profile：dau 每天 100 万行，单次上限 5000 万（约 50 天）。"""
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"default_timezone": "UTC+8", "metrics": [{
            "name": "dau",
            "dimensions": ["market"],
            "requires_time_window": True,
            "max_scan_rows": 50_000_000,
            "rows_per_day": 1_000_000,
        }]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BI_GATE_REGISTRY", str(registry))
    monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
    monkeypatch.setenv("BI_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("BI_GATE_ACTION_POLICY", raising=False)
    mod = _load()
    mod.reload_registry()
    mod.reset_session_counters()
    return mod


def _call(gate, start, end, session="S1"):
    return gate._on_pre_tool_call(
        tool_name="query_metric",
        args={"metric": "dau", "time_window": {"start": start, "end": end}},
        session_id=session,
    )


def _blocked(result) -> bool:
    return isinstance(result, dict) and result.get("action") == "block"


# ── 没声明就不检查 ──────────────────────────────────────────────────────

def test_unset_means_no_session_budget(gate, monkeypatch):
    """未声明会话预算 = 不做这项检查。

    这里刻意不套用「未声明按最严」：这一层加在单次限额之上，缺省取 0 会让
    任何查询都做不了。没声明这件事由 verify.py 显式报出来，不静默缺失。
    """
    monkeypatch.delenv("BI_GATE_SESSION_SCAN_MAX", raising=False)
    for _ in range(10):
        assert _call(gate, "2026-08-01", "2026-08-21") is None


def test_garbage_value_is_reported_not_silently_ignored(gate, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "很多")
    with caplog.at_level(logging.ERROR):
        assert _call(gate, "2026-08-01", "2026-08-21") is None
    assert any("BI_GATE_SESSION_SCAN_MAX" in r.getMessage() for r in caplog.records)


# ── 核心：拆分绕不过去 ──────────────────────────────────────────────────

def test_decomposition_is_blocked(gate, monkeypatch):
    """复现那次实测：单次都合规，累计超限时必须被拦。

    每月 30 天 × 100 万 = 3000 万，单次上限 5000 万，所以每一次都过得了。
    会话上限设 1 亿 → 第 4 个月应当被拦。
    """
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "100000000")
    months = [
        ("2026-01-01", "2026-01-31"),
        ("2026-02-01", "2026-02-28"),
        ("2026-03-01", "2026-03-31"),
        ("2026-04-01", "2026-04-30"),
        ("2026-05-01", "2026-05-31"),
        ("2026-06-01", "2026-06-30"),
    ]
    results = [_call(gate, a, b) for a, b in months]
    passed = [r for r in results if r is None]
    blocked = [r for r in results if _blocked(r)]
    assert len(passed) == 3, "前三个月每月 3000 万，累计 9000 万，都该放行"
    assert blocked, "第四个月起累计超过 1 亿，必须被拦"
    assert "会话" in blocked[0]["message"]


def test_blocked_calls_do_not_consume_budget(gate, monkeypatch):
    """被拦的调用没有真的去扫，不该占额度。"""
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "60000000")
    assert _call(gate, "2026-08-01", "2026-08-21") is None      # 2000 万
    # 这次单次就超限（80 天 = 8000 万 > 5000 万），被单次限额拦下
    assert _blocked(_call(gate, "2026-01-01", "2026-03-22"))
    # 额度应当还是只用了 2000 万，再来 3000 万仍可放行
    assert _call(gate, "2026-02-01", "2026-03-03") is None


def test_sessions_are_isolated(gate, monkeypatch):
    """额度按会话算，别的会话用光了不影响这一个。"""
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "25000000")
    assert _call(gate, "2026-08-01", "2026-08-21", session="A") is None
    assert _blocked(_call(gate, "2026-08-01", "2026-08-21", session="A"))
    assert _call(gate, "2026-08-01", "2026-08-21", session="B") is None


def test_deny_message_does_not_hand_out_a_bypass(gate, monkeypatch):
    """拒绝理由不能告诉模型「开个新会话就行」。

    拒绝理由要写明来源和处置方式，但处置方式必须是走审批，
    不能是一条能自己执行的绕过路径。
    """
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "10000000")
    msg = _call(gate, "2026-08-01", "2026-08-21")["message"]
    for leak in ("新会话", "新开", "重启", "换一个会话"):
        assert leak not in msg
    assert "授权变更流程" in msg


# ── 重启不清零 ──────────────────────────────────────────────────────────

def test_counter_is_seeded_from_the_audit_after_restart(gate, monkeypatch, tmp_path):
    """进程重启后额度不能清零，否则「重启即可绕过」。

    计数器在内存里，而会话可能跨进程（resume、gateway 重启、cron 续跑）。
    审计文件就是账本，重新加载时从它播种，不另外维护一份状态。
    """
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "45000000")
    assert _call(gate, "2026-08-01", "2026-08-21") is None   # 2000 万
    assert _call(gate, "2026-08-01", "2026-08-21") is None   # 累计 4000 万

    # 模拟重启：换一个模块实例，内存计数器是空的
    fresh = _load("bi_gate_session_after_restart")
    fresh.reload_registry()
    fresh.reset_session_counters()
    seeded = fresh._session_scanned_now("S1")
    assert seeded == 40_000_000, f"应从审计播种回 4000 万，实际 {seeded:,}"

    result = fresh._on_pre_tool_call(
        tool_name="query_metric",
        args={"metric": "dau", "time_window": WINDOW_20D},
        session_id="S1",
    )
    assert _blocked(result), "重启后额度仍应是用掉的状态，这次要被拦"


def test_only_passed_calls_are_seeded(gate, monkeypatch, tmp_path):
    """播种只认放行的记录 —— 被拦的调用没有扫过数据。"""
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "30000000")
    _call(gate, "2026-08-01", "2026-08-21")                    # 放行 2000 万
    _call(gate, "2026-01-01", "2026-03-22")                    # 单次超限被拦
    fresh = _load("bi_gate_session_seed_only_passed")
    fresh.reload_registry()
    fresh.reset_session_counters()
    assert fresh._session_scanned_now("S1") == 20_000_000


def test_unreadable_audit_starts_from_zero_and_shouts(gate, monkeypatch, caplog, tmp_path):
    """账本读不出来时按 0 起算，但要打 ERROR。

    这个方向是有意选的：读不出账本时把额度算少会放宽约束，不会误伤业务；
    反过来（读不出就当额度用光）会把一次文件故障变成全线不可用。
    """
    import logging

    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "30000000")
    bad = tmp_path / "notadir"
    bad.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BI_AUDIT_LOG", str(bad / "audit.jsonl"))
    fresh = _load("bi_gate_session_bad_audit")
    fresh.reload_registry()
    fresh.reset_session_counters()
    with caplog.at_level(logging.ERROR):
        assert fresh._session_scanned_now("S1") == 0


# ── 审计要记得住 ────────────────────────────────────────────────────────

def test_audit_records_the_estimate_for_passed_calls(gate, tmp_path, monkeypatch):
    """放行的调用也要记预估值 —— 播种和事后对账都靠它。"""
    monkeypatch.setenv("BI_GATE_SESSION_SCAN_MAX", "99999999999")
    _call(gate, "2026-08-01", "2026-08-21")
    records = [
        json.loads(l)
        for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    passed = [r for r in records if r.get("gate_result") == "passed"]
    assert passed and passed[-1]["estimated_rows"] == 20_000_000
