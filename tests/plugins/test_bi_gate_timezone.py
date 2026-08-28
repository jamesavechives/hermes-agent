"""统计时区。

为什么补这一项
--------------
Tex 的《BI + AI 需求梳理》里，用户意图识别那节明确让用户在 UTC8 和 UTC0 之间选。
我们的时间窗**只有起止日期，没有时区** —— 这一块整个漏了。

漏了的后果不是"查不出来"，是**查出来的数字看着完全正常，只是差了几小时**。
「上周」在 UTC+8 和 UTC+0 下是两段不同的数据，而返回的 DAU 是个普通的数，
没人看得出它按哪个时区算的。这正是本项目一路在防的那种失效：错误和正确长得一样。

所以处理方向和「相对时间窗一律拒绝」一致：**定不出来就拒绝，绝不猜一个默认值。**
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def r():
    return _load("bi_gate_rules_tz", PLUGIN_DIR / "rules.py")


@pytest.fixture()
def dau(r):
    return r.MetricSpec(name="daily_active_users",
                        dimensions=frozenset({"market"}),
                        requires_time_window=True,
                        rows_per_day=1_200_000)


@pytest.fixture()
def snapshot(r):
    """快照类指标：没有时间窗，也就没有时区歧义。"""
    return r.MetricSpec(name="current_open_interest",
                        dimensions=frozenset({"symbol"}),
                        requires_time_window=False)


WINDOW = {"start": "2026-08-01", "end": "2026-08-07"}


# ---------------------------------------------------------------------------
# 时区字符串的规范化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("UTC", "UTC"),
    ("utc", "UTC"),
    ("UTC+8", "UTC+08:00"),
    ("UTC-5", "UTC-05:00"),
    ("UTC+05:30", "UTC+05:30"),
    ("  UTC+8  ", "UTC+08:00"),
])
def test_fixed_offsets_are_normalized(r, raw, expected):
    assert r.normalize_timezone(raw) == expected


def test_iana_names_are_accepted(r):
    assert r.normalize_timezone("Asia/Shanghai") == "Asia/Shanghai"


@pytest.mark.parametrize("raw", [
    "UTC+15",        # 超出真实偏移范围
    "Asia/Shangai",  # 拼错的 IANA 名
    "北京时间",
    "GMT+8",
    "",
    None,
    8,
])
def test_unrecognized_timezones_return_none(r, raw):
    """认不出来一律 None，**绝不回落到某个默认值**。

    悄悄回落是这里最危险的实现方式：写错时区照样返回数据，数字看着正常、
    只是差了几小时，而且没有任何痕迹说明它按错的时区算过。
    """
    assert r.normalize_timezone(raw) is None


# ---------------------------------------------------------------------------
# 解析优先级
# ---------------------------------------------------------------------------

def test_call_beats_registry_default(r, dau):
    reg = r.MetricRegistry([dau], default_timezone="UTC")
    args = {"metric": dau.name, "time_window": dict(WINDOW, timezone="UTC+8")}
    tz, source, verdict = r.resolve_timezone(args, dau, reg)
    assert (tz, source) == ("UTC+08:00", "call")
    assert not verdict.blocked


def test_registry_default_is_used_when_call_is_silent(r, dau):
    reg = r.MetricRegistry([dau], default_timezone="UTC+8")
    tz, source, verdict = r.resolve_timezone(
        {"metric": dau.name, "time_window": dict(WINDOW)}, dau, reg)
    assert (tz, source) == ("UTC+08:00", "registry_default")
    assert not verdict.blocked


def test_snapshot_metrics_skip_the_check(r, snapshot):
    """没有时间窗就没有时区歧义 —— 这一项对快照类指标不该生效。"""
    reg = r.MetricRegistry([snapshot], default_timezone=None)
    tz, source, verdict = r.resolve_timezone({"metric": snapshot.name}, snapshot, reg)
    assert (tz, source) == (None, "not_applicable")
    assert not verdict.blocked


# ---------------------------------------------------------------------------
# 定不出来就拒绝
# ---------------------------------------------------------------------------

def test_undetermined_timezone_is_rejected(r, dau):
    reg = r.MetricRegistry([dau], default_timezone=None)
    _tz, source, verdict = r.resolve_timezone(
        {"metric": dau.name, "time_window": dict(WINDOW)}, dau, reg)
    assert source == "undetermined"
    assert verdict.blocked and verdict.code == r.REJECT_TIMEZONE


def test_rejection_offers_two_ways_out(r, dau):
    """拒绝理由要给出路，而且是**两条**。

    对应 Tex 那份里「让用户做选择题而不是问答题」：一条是这次调用自己带时区，
    一条是让业务方把默认值定下来。只说"被拒了"的理由等于把死路交给用户。
    """
    reg = r.MetricRegistry([dau], default_timezone=None)
    _tz, _src, verdict = r.resolve_timezone(
        {"metric": dau.name, "time_window": dict(WINDOW)}, dau, reg)
    assert "timezone" in verdict.reason
    assert "默认时区" in verdict.reason
    assert r.GATE_SOURCE in verdict.reason, "拒绝理由必须写明拦截来源"


def test_bad_timezone_in_call_is_rejected_not_ignored(r, dau):
    """调用里写错时区 → 拒绝。**不能忽略它然后用默认值。**

    忽略是最坏的处理：用户以为自己指定了 UTC+8，实际拿到的是默认时区的数据。
    """
    reg = r.MetricRegistry([dau], default_timezone="UTC")
    _tz, _src, verdict = r.resolve_timezone(
        {"metric": dau.name, "time_window": dict(WINDOW, timezone="北京时间")}, dau, reg)
    assert verdict.blocked and verdict.code == r.REJECT_TIMEZONE
    assert "北京时间" in verdict.reason


def test_bad_registry_default_says_it_is_a_config_problem(r, dau):
    """注册表里的默认时区写错 → 拒绝，且理由要指向配置而不是调用方。

    让调用方去猜"是不是我参数写错了"，会把一次配置错误变成一串无效重试。
    """
    reg = r.MetricRegistry([dau], default_timezone="Asia/Shangai")
    _tz, _src, verdict = r.resolve_timezone(
        {"metric": dau.name, "time_window": dict(WINDOW)}, dau, reg)
    assert verdict.blocked
    assert "配置问题" in verdict.reason and "事实层责任人" in verdict.reason


# ---------------------------------------------------------------------------
# 接进 evaluate 之后
# ---------------------------------------------------------------------------

def test_evaluate_blocks_when_timezone_undetermined(r, dau):
    reg = r.MetricRegistry([dau], default_timezone=None)
    v = r.evaluate({"metric": dau.name, "time_window": dict(WINDOW),
                    "dimensions": ["market"]}, reg)
    assert v.blocked and v.code == r.REJECT_TIMEZONE


def test_evaluate_carries_timezone_out_for_the_audit(r, dau):
    """放行时也要把时区带出去 —— 审计必须记下这次按哪个时区算的。

    不记的话，事后对账时「这个数为什么和看板差一天」就查不出来了。
    """
    reg = r.MetricRegistry([dau], default_timezone="UTC+8")
    v = r.evaluate({"metric": dau.name, "time_window": dict(WINDOW),
                    "dimensions": ["market"]}, reg)
    assert not v.blocked
    assert v.detail["timezone"] == "UTC+08:00"
    assert v.detail["timezone_source"] == "registry_default"


def test_timezone_is_checked_after_the_window_itself(r, dau):
    """窗口格式不对时，先说窗口的事。

    两个都错的时候报哪一个是有讲究的：窗口格式是调用方立刻能改的，
    时区未定则可能要等业务方。先报能改的那个。
    """
    reg = r.MetricRegistry([dau], default_timezone=None)
    v = r.evaluate({"metric": dau.name, "time_window": {"start": "last_7d", "end": "now"},
                    "dimensions": ["market"]}, reg)
    assert v.code == r.REJECT_NO_TIME_WINDOW


def test_snapshot_metric_still_passes_without_any_timezone(r, snapshot):
    reg = r.MetricRegistry([snapshot], default_timezone=None)
    v = r.evaluate({"metric": snapshot.name, "dimensions": ["symbol"]}, reg)
    assert not v.blocked
