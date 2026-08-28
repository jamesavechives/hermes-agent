"""审计上报与对账。

对账在回答一个别处回答不了的问题：**有没有调用绕过了门禁。**

§5.3 第一例（模型被拦住导出后换个工具把文件写到别处，还告诉用户"数据已导出"）
就是靠「审计里什么都没有」才发现的 —— 但那是人工翻出来的。对账把它变成一条
能自动响的告警。

判定和执行各写一行，用同一个 ``call_id`` 配对（门禁放行时通过宿主的 modify
通道塞进工具参数）。执行了却找不到对应的放行判定 = 那次调用没经过门禁。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate"


@pytest.fixture(scope="module")
def sh():
    spec = importlib.util.spec_from_file_location(
        "bi_gate_audit_shipper", PLUGIN_DIR / "audit_shipper.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["bi_gate_audit_shipper"] = m
    spec.loader.exec_module(m)
    return m


NOW = 1_787_880_000
W0, W1 = NOW - 600, NOW


def verdict(call_id, ts=NOW - 60, result="passed"):
    return {"event": "bi_gate_verdict", "gate_result": result,
            "call_id": call_id, "ts": ts, "tool": "query_metric"}


def executed(call_id, ts=NOW - 60):
    return {"event": "executed", "source": "bi-query", "call_id": call_id,
            "ts": ts, "tool": "query_metric"}


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------

def test_paired_calls_reconcile_clean(sh):
    recs = [verdict("a"), executed("a"), verdict("b"), executed("b")]
    out = sh.reconcile(recs, W0, W1)
    assert out["status"] == sh.RECON_OK
    assert (out["passed"], out["executed"], out["paired"]) == (2, 2, 2)


def test_execution_without_a_verdict_is_a_mismatch(sh):
    """执行了、但没有对应的放行判定 → **这次调用没经过门禁**。

    这是对账存在的全部理由。它红了不是配置问题，是安全事件。
    """
    recs = [verdict("a"), executed("a"), executed("sneaky")]
    out = sh.reconcile(recs, W0, W1)
    assert out["status"] == sh.RECON_MISMATCH
    assert out["executed_without_verdict"] == 1
    assert "sneaky" in out["executed_without_verdict_ids"]
    assert "没经过门禁" in out["detail"]


def test_passed_without_execution_is_not_a_mismatch(sh):
    """放行了但没执行 —— 不算不平。

    模型放弃了、工具报错了都会这样。把它算成异常，告警会天天响。
    """
    recs = [verdict("a"), executed("a"), verdict("abandoned")]
    out = sh.reconcile(recs, W0, W1)
    assert out["status"] == sh.RECON_OK
    assert out["passed_without_execution"] == 1


def test_records_without_call_id_are_counted_separately(sh):
    """没有 call_id 的旧记录**不算成绕过门禁**。

    加配对键之前写的记录全是这样。把"查不了"算成"出事了"，会让这条告警
    一上线就全是假警报 —— 而假警报直接毁掉它的可信度。
    """
    recs = [verdict("a"), executed("a"),
            {"event": "executed", "ts": NOW - 60},          # 无 call_id
            {"event": "bi_gate_verdict", "gate_result": "passed", "ts": NOW - 60}]
    out = sh.reconcile(recs, W0, W1)
    assert out["status"] == sh.RECON_OK
    assert out["legacy_records"] == 2


def test_rejected_verdicts_do_not_count_as_passed(sh):
    """被拦的判定不参与配对 —— 它本来就不该有执行记录。"""
    recs = [verdict("blocked", result="rejected_scan")]
    out = sh.reconcile(recs, W0, W1)
    assert out["passed"] == 0
    assert out["status"] == sh.RECON_NO_DATA


def test_empty_window_is_no_data_not_ok(sh):
    """窗口内什么都没有 → no_data，不是 ok。

    「没有数据」和「对上了」是两件事。报成 ok 的话，审计链路断掉时对账会
    一直显示正常 —— 那正是这套东西一路在防的失效（同探针静默）。
    """
    out = sh.reconcile([], W0, W1)
    assert out["status"] == sh.RECON_NO_DATA


# ---------------------------------------------------------------------------
# 增量读与断点
# ---------------------------------------------------------------------------

def _write(path: Path, records):
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_checkpoint_makes_reads_incremental(sh, tmp_path):
    audit = tmp_path / "a.jsonl"
    _write(audit, [verdict("a"), executed("a")])
    recs, offset, inode = sh.read_new_lines(audit)
    assert len(recs) == 2
    sh.write_checkpoint(audit, offset, inode)

    recs2, _o, _i = sh.read_new_lines(audit)
    assert recs2 == []

    _write(audit, [verdict("b")])
    recs3, _o, _i = sh.read_new_lines(audit)
    assert len(recs3) == 1


def test_truncated_file_is_read_from_the_start(sh, tmp_path):
    """文件被截断/重建 → 从头读。

    继续按旧 offset 读会跳过整个新文件的开头，而且**没有任何迹象** ——
    上报看起来一切正常，只是少了一段。
    """
    audit = tmp_path / "a.jsonl"
    _write(audit, [verdict("a"), executed("a"), verdict("b")])
    _recs, offset, inode = sh.read_new_lines(audit)
    sh.write_checkpoint(audit, offset, inode)

    audit.write_text("", encoding="utf-8")
    _write(audit, [verdict("c")])
    recs, _o, _i = sh.read_new_lines(audit)
    assert len(recs) == 1, "截断后应从头读"


def test_corrupt_checkpoint_falls_back_to_reading_everything(sh, tmp_path):
    """断点文件坏了 → 从头读。宁可重复上报，也不要漏。

    重复的记录靠 call_id 在查询侧能去重；漏掉的没人知道。
    """
    audit = tmp_path / "a.jsonl"
    _write(audit, [verdict("a"), executed("a")])
    sh.checkpoint_path(audit).write_text("这不是 JSON", encoding="utf-8")
    recs, _o, _i = sh.read_new_lines(audit)
    assert len(recs) == 2


def test_window_read_uses_a_half_open_interval(sh, tmp_path):
    """时间窗是左闭右开 —— 和项目里其它按时间取数的地方一致。"""
    audit = tmp_path / "a.jsonl"
    _write(audit, [verdict("early", ts=W0 - 1), verdict("inside", ts=W0),
                   verdict("edge", ts=W1), verdict("late", ts=W1 + 1)])
    got = {r["call_id"] for r in sh.read_window(audit, W0, W1)}
    assert got == {"inside"}


# ---------------------------------------------------------------------------
# 上报格式
# ---------------------------------------------------------------------------

def test_render_adds_routing_fields_and_stringifies_detail(sh):
    """``detail`` 有时是 dict（门禁的结构化补充），但 VictoriaLogs 的 _msg 要字符串。"""
    payload = sh.render([{"event": "bi_gate_verdict", "ts": NOW,
                          "detail": {"metric": "dau"}}], sh.SINK_APP_AUDIT)
    doc = json.loads(payload.strip())
    assert doc["app"] == sh.SINK_APP_AUDIT
    assert doc["time"].endswith("Z")
    assert isinstance(doc["detail"], str) and "dau" in doc["detail"]


def test_render_of_nothing_is_empty_not_a_blank_line(sh):
    """没有记录时不要推一个空行上去 —— 那会在日志里留下一条无意义的记录。"""
    assert sh.render([], sh.SINK_APP_AUDIT) == ""


def test_the_pairing_key_constant_matches_on_both_sides(sh):
    """两个插件各写了一份 ``_bi_gate_call_id``（为了能独立部署），必须一致。

    不一致的后果是：每次执行都找不到对应的判定 → 对账永远 mismatch →
    告警天天响 → 没人再看它。
    """
    def _const(rel, name):
        spec = importlib.util.spec_from_file_location(
            f"probe_{name}", PLUGIN_DIR.parent / rel,
            submodule_search_locations=[str((PLUGIN_DIR.parent / rel).parent)])
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"probe_{name}"] = m
        spec.loader.exec_module(m)
        return m
    gate = _const("bi-gate/__init__.py", "gate")
    query = _const("bi-query/__init__.py", "query")
    assert gate.CALL_ID_ARG == query.GATE_CALL_ID_ARG
