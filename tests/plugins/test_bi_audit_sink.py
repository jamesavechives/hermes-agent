"""审计落盘的测试 —— 门禁与执行层共有的一条性质。

为什么单独一个文件：这不是某个插件的内部细节，而是两边共有的契约。
门禁写判定、工具写执行，两条记录进同一个文件、格式同构，事后才能对账。
对账能回答的问题是：**有没有「执行了但没有对应判定」的调用** —— 那意味着
这次调用绕过了门禁（门禁没启用，或走的是别的工具名）。

背景：2026-08-26 在 dev 机上第一次跑通真实会话后发现，两边的审计都只走
``logger.info``，而宿主的 ``agent.log`` 里一条都没有。审计是合规唯一会查的
东西，落不了盘等于没有。于是改成写我们自己控制的 JSONL 文件，并补上这组测试。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


def _gated(args: dict) -> dict:
    """补上门禁放行时注入的元数据（主体 + 配对键）。

    这些测试直接调 ``handle_query_metric``，模拟的是"门禁已放行"那一刻。
    真实路径上主体由门禁塞进 args；不补的话，bi-query 会因为没有主体而拒绝执行，
    测的就不是落盘行为了。无主体拒绝那条规则由 test_bi_gate_identity.py 守着。
    """
    out = dict(args)
    out.setdefault("_bi_principal", {"subject": "bi_test_principal", "display": "测试主体",
                                     "origin": "human", "verified": False})
    out.setdefault("_bi_gate_call_id", "test_call_id")
    return out

def _load(dirname: str, modname: str):
    d = PLUGINS / dirname
    spec = importlib.util.spec_from_file_location(
        modname, d / "__init__.py", submodule_search_locations=[str(d)]
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gate():
    return _load("bi-gate", "bi_gate_audit_under_test")


@pytest.fixture()
def query():
    m = _load("bi-query", "bi_query_audit_under_test")
    m.reload_fixtures()
    return m


def _lines(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


WINDOW = {"start": "2026-08-01", "end": "2026-08-21"}


# ── 路径解析 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mod_name", ["gate", "query"])
def test_explicit_path_wins(mod_name, request, tmp_path, monkeypatch):
    mod = request.getfixturevalue(mod_name)
    target = tmp_path / "explicit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert mod._audit_path() == target


@pytest.mark.parametrize("mod_name", ["gate", "query"])
def test_falls_back_to_profile_home(mod_name, request, tmp_path, monkeypatch):
    mod = request.getfixturevalue(mod_name)
    monkeypatch.delenv("BI_AUDIT_LOG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert mod._audit_path() == tmp_path / "audit.jsonl"


@pytest.mark.parametrize("mod_name", ["gate", "query"])
def test_no_path_is_reported_not_silent(mod_name, request, monkeypatch):
    """两个环境变量都没有时必须返回 False —— 静默丢弃审计是最坏的情况。"""
    mod = request.getfixturevalue(mod_name)
    monkeypatch.delenv("BI_AUDIT_LOG", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert mod._audit_path() is None
    assert mod._write_audit_line({"event": "x"}) is False


# ── 真的写进去了 ────────────────────────────────────────────────────────

def test_query_execution_lands_on_disk(query, tmp_path, monkeypatch):
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    query.handle_query_metric(_gated(
        {"metric": "daily_active_users", "dimensions": ["market"], "time_window": WINDOW}
    ))
    records = _lines(target)
    assert len(records) == 1
    assert records[0]["source"] == "bi-query"
    assert records[0]["event"] == "executed"
    assert records[0]["scanned_rows"] > 0


def test_appends_not_overwrites(query, tmp_path, monkeypatch):
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    for _ in range(3):
        query.handle_query_metric(_gated({"metric": "daily_active_users", "time_window": WINDOW}))
    assert len(_lines(target)) == 3, "每次调用都要留一条，不能互相覆盖"


def test_parent_dir_is_created(query, tmp_path, monkeypatch):
    target = tmp_path / "nested" / "deeper" / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    query.handle_query_metric(_gated({"metric": "daily_active_users", "time_window": WINDOW}))
    assert target.exists()


def test_one_json_object_per_line(query, tmp_path, monkeypatch):
    """一行一条，不能跨行 —— 否则 grep / 流式解析全废。"""
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    query.handle_query_metric(_gated(
        {"metric": "spot_trade_volume", "dimensions": ["symbol", "market"], "time_window": WINDOW}
    ))
    raw = target.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert len([l for l in raw.splitlines() if l.strip()]) == 1


# ── 写不进去时不能打断业务 ──────────────────────────────────────────────

def test_unwritable_path_does_not_break_the_call(query, tmp_path, monkeypatch):
    """审计写不进去，查询本身仍然要返回结果。

    这是有意的取舍：审计挂了就拒绝服务，会把记录问题升级成业务不可用。
    代价是留下一次查不到的调用，所以 `_write_audit_line` 会打 ERROR。
    要不要反过来（记不上就拒绝），是待拍板的策略问题，不是实现细节。
    """
    blocked = tmp_path / "afile"
    blocked.write_text("我是文件不是目录", encoding="utf-8")
    monkeypatch.setenv("BI_AUDIT_LOG", str(blocked / "audit.jsonl"))
    out = json.loads(query.handle_query_metric(_gated({"metric": "daily_active_users",
                                               "time_window": WINDOW})))
    assert out["rows"], "审计失败不应该让查询失败"


def test_write_failure_is_reported_to_caller(query, tmp_path, monkeypatch):
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BI_AUDIT_LOG", str(blocked / "audit.jsonl"))
    assert query._audit({"event": "executed"}) is False


# ── 两边格式同构，能对账 ────────────────────────────────────────────────

def test_both_sides_share_the_sink_and_are_distinguishable(gate, query, tmp_path, monkeypatch):
    """门禁和工具写同一个文件，靠 source 区分，靠 tool 关联。"""
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))

    gate._write_audit_line({"event": "bi_gate_verdict", "source": "bi-gate",
                            "tool": "query_metric", "gate_result": "allow"})
    query.handle_query_metric(_gated({"metric": "daily_active_users", "time_window": WINDOW}))

    records = _lines(target)
    assert len(records) == 2
    sources = {r["source"] for r in records}
    assert sources == {"bi-gate", "bi-query"}
    assert all(r["tool"] == "query_metric" for r in records)


def test_reconcile_spots_execution_without_verdict(gate, query, tmp_path, monkeypatch):
    """对账的最小形态：执行数 > 判定数 ⇒ 有调用没经过门禁。

    这条就是图 5 里那几条绕过路径能被发现的唯一方式，所以写成测试钉住。
    """
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))

    # 只有工具执行，没有门禁判定 —— 模拟门禁没启用的情形
    query.handle_query_metric(_gated({"metric": "daily_active_users", "time_window": WINDOW}))

    records = _lines(target)
    verdicts = [r for r in records if r["source"] == "bi-gate"]
    executions = [r for r in records if r["source"] == "bi-query"]
    assert len(executions) > len(verdicts), "这正是要被告警抓出来的情况"


def test_gate_records_carry_source(gate, tmp_path, monkeypatch):
    """门禁记录必须带 source —— 对账全靠它区分两边。

    首次落盘时漏了这个字段，结果 3 条记录里 2 条 source 为 null，
    「执行数 vs 判定数」根本对不起来。
    """
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    import inspect

    src = inspect.getsource(gate._audit)
    assert '"source": GATE_SOURCE' in src, "gate 的审计记录里必须写死 source"
