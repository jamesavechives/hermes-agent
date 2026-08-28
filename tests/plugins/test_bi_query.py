"""bi-query 的测试。

盯三件事：
1. **确定性** —— 同样入参永远同样结果。评估集的回归结论全靠这条，桩数据一旦
   有随机性，Golden Set 测出来的差异就分不清是模型变了还是数据变了。
2. **留痕** —— 每次执行恰好落一条执行记录，且带上真实行数与扫描量。
3. **不越界** —— 它不重复门禁的判定。没注册的指标、非法维度传进来，它照样
   执行（或如实报缺数据），因为拦截不是它的职责。这条看着反直觉，但正是
   「判定只能有一处」的必然结果，写成测试免得后人"顺手加个校验"。
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bi-query"


def _gated(args: dict) -> dict:
    """补上门禁放行时会注入的元数据。

    这些测试直接调 ``handle_query_metric``，等于「门禁已经放行」的那一刻。
    真实路径上门禁会往 args 里塞 ``_bi_principal``（以谁的名义查）和
    ``_bi_gate_call_id``（对账用的配对键）。不补的话测的就不是真实调用形状。

    **这不是绕过检查** —— bi-query 拒绝无主体执行的那条规则，由
    ``test_bi_gate_identity.py`` 里的用例专门守着。
    """
    out = dict(args)
    out.setdefault("_bi_principal", {"subject": "bi_test_principal", "display": "测试主体",
                                     "origin": "human", "verified": False})
    out.setdefault("_bi_gate_call_id", "test_call_id")
    return out

def _load():
    """按文件路径加载 —— 目录名带连字符，不是合法包名，正常 import 走不通。"""
    spec = importlib.util.spec_from_file_location(
        "bi_query_under_test",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bq():
    mod = _load()
    mod.reload_fixtures()
    return mod


WINDOW = {"start": "2026-08-01", "end": "2026-08-21"}


# ── 1. 确定性 ────────────────────────────────────────────────────────────

def test_same_args_same_result(bq):
    args = {"metric": "daily_active_users", "dimensions": ["market"], "time_window": WINDOW}
    first = bq.handle_query_metric(_gated(dict(args)))
    second = bq.handle_query_metric(_gated(dict(args)))
    assert first == second, "同样入参必须返回完全一样的字节，否则评估集回归没有意义"


def test_dimension_order_does_not_matter(bq):
    a = bq.handle_query_metric(_gated(
        {"metric": "daily_active_users", "dimensions": ["market", "channel"], "time_window": WINDOW}
    ))
    b = bq.handle_query_metric(_gated(
        {"metric": "daily_active_users", "dimensions": ["channel", "market"], "time_window": WINDOW}
    ))
    assert json.loads(a)["rows"] == json.loads(b)["rows"]


def test_no_dimensions_returns_total(bq):
    out = json.loads(bq.handle_query_metric(_gated({"metric": "daily_active_users", "time_window": WINDOW})))
    assert out["rows"] == [{"dau": 128400}]
    assert out["dimensions"] == []


def test_scanned_rows_scales_with_window(bq):
    short = json.loads(bq.handle_query_metric(_gated(
        {"metric": "spot_trade_volume", "time_window": {"start": "2026-08-01", "end": "2026-08-02"}})))
    long_ = json.loads(bq.handle_query_metric(_gated(
        {"metric": "spot_trade_volume", "time_window": {"start": "2026-08-01", "end": "2026-08-21"}})))
    assert long_["meta"]["scanned_rows"] > short["meta"]["scanned_rows"]


def test_bad_window_does_not_raise(bq):
    """时间窗格式坏掉时按 1 天算，不抛异常——格式校验是门禁的活，不是这里的。"""
    out = json.loads(bq.handle_query_metric(_gated(
        {"metric": "daily_active_users", "time_window": {"start": "last_7d", "end": "now"}})))
    assert out["rows"], "坏时间窗不应该让查询失败"


# ── 2. 留痕 ──────────────────────────────────────────────────────────────

def _audit_records(caplog):
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("bi-query audit "):
            out.append(json.loads(msg[len("bi-query audit "):]))
    return out


def test_one_audit_record_per_call(bq, caplog):
    with caplog.at_level(logging.INFO):
        bq.handle_query_metric(_gated({"metric": "daily_active_users", "dimensions": ["market"],
                                "time_window": WINDOW}))
    records = _audit_records(caplog)
    assert len(records) == 1
    r = records[0]
    assert r["event"] == "executed"
    assert r["source"] == "bi-query"
    assert r["tool"] == "query_metric"
    assert r["metric"] == "daily_active_users"
    assert r["row_count"] == 3
    assert r["scanned_rows"] > 0
    assert r["backend"] == "stub"


def test_audit_carries_action_shaping_params(bq, caplog):
    """granularity / export 必须进审计——它们决定动作级别，事后对账要看得见。"""
    with caplog.at_level(logging.INFO):
        bq.handle_query_metric(_gated({"metric": "daily_active_users", "granularity": "row",
                                "export": True, "time_window": WINDOW}))
    r = _audit_records(caplog)[0]
    assert r["granularity"] == "row"
    assert r["export"] is True


def test_missing_metric_is_rejected_and_logged(bq, caplog):
    with caplog.at_level(logging.INFO):
        out = json.loads(bq.handle_query_metric(_gated({})))
    assert "error" in out
    r = _audit_records(caplog)[0]
    assert r["event"] == "rejected_by_tool"


# ── 3. 不越界：判定不是它的职责 ──────────────────────────────────────────

def test_unregistered_metric_is_not_blocked_here(bq, caplog):
    """没注册的指标不该由工具拦——拦它是门禁的事。

    这里如实报「没有这份数据」，而不是「你无权查」。两者的区别很重要：
    前者是执行层的事实，后者是判定，说出后者就等于把判定复制到了第二处。
    """
    with caplog.at_level(logging.INFO):
        out = json.loads(bq.handle_query_metric(_gated({"metric": "revenue_v2", "time_window": WINDOW})))
    assert out["error"] == "no_fixture"
    assert "无权" not in json.dumps(out, ensure_ascii=False)
    assert _audit_records(caplog)[0]["event"] == "executed"


def test_unknown_dimension_reports_what_exists(bq):
    out = json.loads(bq.handle_query_metric(_gated(
        {"metric": "daily_active_users", "dimensions": ["uid"], "time_window": WINDOW})))
    assert out["error"] == "no_fixture_for_dimensions"
    assert "market" in out["available_dimension_sets"]


# ── 4. 契约 ──────────────────────────────────────────────────────────────

def test_schema_fields_match_gate_inputs(bq):
    """schema 的字段就是门禁的判定输入，少一个门禁就判不了。"""
    props = bq.SCHEMA["parameters"]["properties"]
    for field in ("metric", "dimensions", "time_window", "granularity", "export"):
        assert field in props, f"schema 缺字段 {field}，门禁对应的规则会失效"
    assert bq.SCHEMA["parameters"]["required"] == ["metric"]


def test_result_declares_stub_backend(bq):
    """结果必须自曝是桩数据——不然有人会拿去写报告。"""
    out = json.loads(bq.handle_query_metric(_gated({"metric": "daily_active_users", "time_window": WINDOW})))
    assert out["meta"]["backend"] == "stub"
    assert "不可用于对外结论" in out["meta"]["note"]


def test_register_wires_the_tool(bq):
    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(kw)

    bq.register(Ctx())
    assert len(calls) == 1
    assert calls[0]["name"] == "query_metric"
    assert calls[0]["toolset"] == "bi"
    assert calls[0]["handler"] is bq.handle_query_metric
