"""数据新鲜度 —— 「没有数据」不能被说成「数据是 0」。

背景（2026-08-28 查生产）
------------------------
整条数仓链路（dwd/dws/dim/ads）的 ``etl_time`` 停在 **2026-07-17**，
而业务库 ``unimargin_history`` 还在写（当天仍有成交）。也就是说这一刻问
「最近 7 天的日活」，底表里那 7 天一行都没有。

不拦的话会发生两件事之一，**都比报错糟**：

* 返回空结果 → 模型很可能解释成「这几天没有活跃用户」，那是假的业务结论；
* 模型自己把时间窗挪到有数据的范围 → 把 7 月的数字当成上周的报给用户。

这组测试守的就是这条线：助手可以说不知道，不可以把不知道说成 0。
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
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def R():
    return _load("bi_gate_rules_freshness", PLUGIN_DIR / "rules.py")


def _spec(R, **over):
    kw = dict(name="m", dimensions=frozenset(), requires_time_window=True,
              data_start="2026-01-01", data_end="2026-07-16")
    kw.update(over)
    return R.MetricSpec(**kw)


def _w(start, end):
    return {"start": start, "end": end, "timezone": "UTC+8"}


# ---------------------------------------------------------------------------
# 三种情形
# ---------------------------------------------------------------------------

def test_window_entirely_after_data_end_is_rejected(R):
    """整段在数据截止日之后 → 拒绝。这是数仓停更时最常见的一问。"""
    v = R.check_data_freshness(_w("2026-08-21", "2026-08-28"), _spec(R))
    assert v.blocked and v.code == R.REJECT_NO_DATA_IN_RANGE
    # 理由里必须给出真实范围，否则模型不知道该改成什么
    assert "2026-07-16" in v.reason
    # 而且必须点明这不是 0
    assert "不是 0" in v.reason


def test_window_entirely_before_data_start_is_rejected(R):
    v = R.check_data_freshness(_w("2025-01-01", "2025-02-01"), _spec(R))
    assert v.blocked and v.code == R.REJECT_NO_DATA_IN_RANGE


def test_partial_overlap_passes_but_reports_coverage(R):
    """部分超出 → 放行，但必须带出实际覆盖到哪天。

    放行是对的（那段确实有数据），但回答里不说清截止日，用户会以为
    整个区间都统计了 —— 那等于用一个残缺区间冒充完整区间。
    """
    v = R.check_data_freshness(_w("2026-07-10", "2026-08-01"), _spec(R))
    assert not v.blocked
    assert v.detail["data_coverage"] == "partial"
    assert v.detail["covered"] == "2026-07-10~2026-07-16"
    assert "2026-07-16" in v.detail["note"]


def test_fully_covered_window_passes_clean(R):
    v = R.check_data_freshness(_w("2026-07-01", "2026-07-10"), _spec(R))
    assert not v.blocked and not v.detail


# ---------------------------------------------------------------------------
# 不知道就不判
# ---------------------------------------------------------------------------

def test_unknown_data_end_skips_the_check(R):
    """``data_end`` 没声明就不做这项检查。

    不知道不能凭空拒（那会让没量过新鲜度的指标全部不可用），
    但也因此**没有任何东西拦得住陈旧数据** —— 所以生成器一定要填上，
    verify.py 会单独报出有多少指标缺它。这条钉住"缺了就是跳过"这个事实，
    免得日后有人以为它默认是安全的。
    """
    v = R.check_data_freshness(_w("2026-08-21", "2026-08-28"),
                               _spec(R, data_end=None))
    assert not v.blocked


def test_snapshot_metric_skips_the_check(R):
    """存量指标（不需要时间窗）不做新鲜度检查。"""
    v = R.check_data_freshness(None, _spec(R, requires_time_window=False))
    assert not v.blocked


# ---------------------------------------------------------------------------
# 接在 evaluate 里，且 detail 不会被后面的步骤丢掉
# ---------------------------------------------------------------------------

def test_evaluate_rejects_stale_window(R):
    reg = R.MetricRegistry([_spec(R, name="dau")], default_timezone="UTC+8")
    v = R.evaluate({"metric": "dau", "time_window": _w("2026-08-21", "2026-08-28")}, reg)
    assert v.blocked and v.code == R.REJECT_NO_DATA_IN_RANGE


def test_coverage_detail_survives_to_the_end_of_evaluate(R):
    """部分覆盖的 detail 必须一路带到 evaluate 的返回值。

    这条是照着一个真实踩过的坑写的：时区那次，``check_session_scan_budget``
    放行时返回不带 detail 的 ALLOW，直接把 evaluate 好不容易算出来的时区
    覆盖掉了，审计里 timezone 永远是 null。同一个形状不能再来一次。
    """
    reg = R.MetricRegistry([_spec(R, name="dau")], default_timezone="UTC+8")
    v = R.evaluate({"metric": "dau", "time_window": _w("2026-07-10", "2026-08-01")}, reg)
    assert not v.blocked
    assert v.detail.get("data_coverage") == "partial", v.detail
    # 时区也还在 —— 两个 detail 是合并不是互相覆盖
    assert v.detail.get("timezone"), v.detail


# ---------------------------------------------------------------------------
# 真实注册表
# ---------------------------------------------------------------------------

def test_generated_registry_carries_freshness():
    """生成器产出的注册表里，每个指标都要有 data_end。

    缺了它这一层就是静默跳过的。这条对着仓库里那份生成结果跑 ——
    没有生成结果时跳过，不假装检查过。
    """
    path = PLUGIN_DIR / "registry.ads.json"
    if not path.exists():
        pytest.skip("仓库里还没有从 ads 生成的注册表")
    reg = json.loads(path.read_text(encoding="utf-8"))
    missing = [m["name"] for m in reg["metrics"]
               if not (m.get("freshness") or {}).get("data_end")]
    assert not missing, f"这些指标没有 data_end，新鲜度检查会静默跳过：{missing[:10]}"


def test_generated_registry_has_no_individual_level_metric():
    """生成的注册表里不许出现个体级指标。

    StarRocks 这个版本没有行级安全（实测：``CREATE ROW ACCESS POLICY`` 语法
    都不存在），个体级数据一旦进来，谁能看哪些行就没有任何东西管得住。
    生成器有排除规则，这条是防止有人手工往里加。
    """
    path = PLUGIN_DIR / "registry.ads.json"
    if not path.exists():
        pytest.skip("仓库里还没有从 ads 生成的注册表")
    reg = json.loads(path.read_text(encoding="utf-8"))
    import re
    bad = re.compile(r"(^|\.)(uid|user_id|top_uid|invite_uid|account_id|login)", re.I)
    hits = [m["name"] for m in reg["metrics"]
            if bad.search(m["name"]) or bad.search(m["source"]["column"])]
    assert not hits, f"注册表里出现了个体级指标：{hits}"
