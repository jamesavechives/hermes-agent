"""跨字段矛盾：每条单看都合法，合起来做不了事。

这一层为什么必须有
------------------
这类配置**不会报错、不会拦人**，只会让某个指标安静地查不了，然后被当成
「模型不行」。设计方案 §4.2 就是为这件事设的，但原先只抓了一种组合。

2026-08-27 补统计时区那轮实测，撞出了它漏掉的两种，而且都是真的：

- ``current_open_interest`` 既没有 ``rows_per_day`` 也没有 ``max_scan_rows``。
  加了会话累计预算之后，它的扫描量预估变成「判定不了」，会话预检按不通过处理
  —— **这个指标从加预算那天起就是死的，没人知道。**
- ``spot_trade_volume`` 的 ``max_scan_rows`` 是 2 亿，会话预算是 1 亿。
  第一次调用就会被会话预检拦下，那条单次上限**永远够不着**，是个假数字。

下面每条测试对应一种组合。它们守的不是"代码能跑"，是"这类配置进不了生产"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"

POLICY = {"version": "t", "default_level": "L0", "rules": []}


def _profile(tmp_path: Path, *, metrics, session_max=None, tools="query_metric",
             action_max="L1", default_timezone="UTC+8") -> Path:
    """造一个只有装配期检查需要的那几个文件的 profile。"""
    d = tmp_path / "p"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bi_registry.json").write_text(
        json.dumps({"default_timezone": default_timezone, "metrics": metrics}),
        encoding="utf-8")
    (d / "action_policy.json").write_text(json.dumps(POLICY), encoding="utf-8")
    (d / "approvals.json").write_text(json.dumps({
        "authorization": {"by": ["x"], "at": "2026-08-27", "ref": "r"},
        "facts": {"by": ["y"], "at": "2026-08-27", "ref": "r"},
    }), encoding="utf-8")
    env = [
        f"BI_GATE_REGISTRY={d / 'bi_registry.json'}",
        f"BI_GATE_ACTION_POLICY={d / 'action_policy.json'}",
        f"BI_GATE_ACTION_MAX={action_max}",
        f"BI_GATE_TOOLS={tools}",
        f"BI_AUDIT_LOG={d / 'audit.jsonl'}",
    ]
    if session_max is not None:
        env.append(f"BI_GATE_SESSION_SCAN_MAX={session_max}")
    (d / ".env").write_text("\n".join(env) + "\n", encoding="utf-8")
    return d


def _check(profile: Path):
    e = dict(os.environ)
    e["PYTHONPATH"] = f"{REPO}{os.pathsep}{e.get('PYTHONPATH','')}".rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "assemble_check.py"), str(profile), "--skip-runtime"],
        capture_output=True, text=True, timeout=120, env=e, cwd=str(REPO))


DAU = {"name": "dau", "dimensions": ["market"], "requires_time_window": True,
       "rows_per_day": 1_200_000, "max_scan_rows": 50_000_000}


# ---------------------------------------------------------------------------

def test_a_consistent_profile_passes(tmp_path):
    """基线：不矛盾的配置必须能过。

    没有这条，下面几条红了也说明不了问题 —— 可能是检查器本身把什么都判失败。
    """
    proc = _check(_profile(tmp_path, metrics=[DAU], session_max=100_000_000))
    assert proc.returncode == 0, proc.stdout
    assert "无指标与之矛盾" in proc.stdout


def test_metric_without_rows_per_day_is_dead_once_a_session_budget_exists(tmp_path):
    """无 rows_per_day + 有会话预算 → 该指标任何查询都做不了。

    实测确认过：``estimate_scan_rows`` 返回 UNDECIDABLE，单次预检放行
    （没声明上限就不检查），但会话预检按「判定不了」拒。
    两道检查各自都对，合起来把指标锁死了。
    """
    snapshot = {"name": "oi", "dimensions": ["symbol"], "requires_time_window": False}
    proc = _check(_profile(tmp_path, metrics=[DAU, snapshot], session_max=100_000_000))
    assert proc.returncode == 1
    assert "oi" in proc.stdout
    assert "任何查询都做不了" in proc.stdout


def test_same_metric_is_fine_without_a_session_budget(tmp_path):
    """没设会话预算时，同一个指标就没问题 —— 矛盾是**两个字段合起来**才成立的。

    这条防的是把检测写成「没有 rows_per_day 就报错」：那样会把一个本来能用的
    配置判死，而且理由是错的。
    """
    snapshot = {"name": "oi", "dimensions": ["symbol"], "requires_time_window": False}
    proc = _check(_profile(tmp_path, metrics=[DAU, snapshot], session_max=None))
    assert proc.returncode == 0, proc.stdout


def test_single_call_limit_above_the_session_budget_is_unreachable(tmp_path):
    """单次上限高于会话预算 → 那条上限永远够不着。

    不会让指标不可用（预算以下照常查），但那个数字是假的：看配置的人会以为
    单次能查到 2 亿行，实际 1 亿就被会话预检拦了。
    """
    big = dict(DAU, name="vol", rows_per_day=8_600_000, max_scan_rows=200_000_000)
    proc = _check(_profile(tmp_path, metrics=[big], session_max=100_000_000))
    assert proc.returncode == 1
    assert "永远够不着" in proc.stdout
    assert "200,000,000" in proc.stdout and "100,000,000" in proc.stdout, \
        "报告要带上具体数字，否则还得去翻配置"


def test_empty_registry_is_reported_not_silently_ok(tmp_path):
    """空注册表 → 所有查询都被拒。方向是对的，但不能报成 ✓。

    原先这里 0 个指标也报「✓ 0 个指标」，看着像通过。
    """
    proc = _check(_profile(tmp_path, metrics=[], session_max=100_000_000))
    assert proc.returncode == 1
    assert "一个指标都没有" in proc.stdout


def test_registry_is_dead_weight_without_query_metric_in_the_whitelist(tmp_path):
    """白名单里没有 query_metric → 整张注册表都用不上。

    白名单在派发路径上先拦，压根走不到指标判定。这种配置看起来"指标都配好了"，
    实际一条都查不了。
    """
    proc = _check(_profile(tmp_path, metrics=[DAU], session_max=100_000_000,
                           tools="tool_describe"))
    assert proc.returncode == 1
    assert "整张注册表都用不上" in proc.stdout


def test_declared_scan_limit_without_rows_per_day_still_caught(tmp_path):
    """原有的那条不能被新加的挤掉：声明了 max_scan_rows 却没 rows_per_day。"""
    broken = {"name": "m", "dimensions": ["d"], "max_scan_rows": 1000}
    proc = _check(_profile(tmp_path, metrics=[broken], session_max=None))
    assert proc.returncode == 1
    assert "rows_per_day" in proc.stdout


def test_the_shipped_example_profile_has_no_contradictions(tmp_path):
    """样例声明自己不许带矛盾。

    它同时是 CI 的输入和「照着这个建新人格」的模板 —— 带着矛盾发出去，
    等于把这两条坑复制给每一个新人格。
    （这条测试写出来的时候，样例里确实有两处矛盾，是被这一层抓出来才修的。）
    """
    yaml = pytest.importorskip("yaml", reason="build_profile 需要 pyyaml")
    del yaml
    src = PLUGIN_DIR / "profile.source.example"
    runtime = tmp_path / "rt"
    gen = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "build_profile.py"), str(src), str(runtime)],
        capture_output=True, text=True, timeout=60)
    assert gen.returncode == 0, gen.stderr

    proc = _check(runtime)
    assert proc.returncode == 0, f"样例声明自相矛盾：\n{proc.stdout}"
