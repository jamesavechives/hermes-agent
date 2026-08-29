"""端到端健康检查 —— 和存活探针分工不同，两个都要。

|                | 存活探针 probe.py     | 健康检查 healthcheck.py |
|----------------|----------------------|------------------------|
| 发什么          | 必然被拦的调用         | 应该成功的查询           |
| 证明什么        | 门禁还在拦             | 整条链路还查得到数       |
| 碰数据库吗      | **不碰**              | 碰                      |
| 它红了说明      | 门禁失效（安全）       | 查不到数（可用性）        |

**只有探针是不够的。** 2026-08-29 之前探针一直是绿的，但那段时间机器根本连不到
StarRocks —— canary 指标在门禁那层就被拒了，走不到后端。「门禁在工作」和
「助手能用」是两回事。

（又是那条形状：一个检查只覆盖它实际经过的路径。加一层前置门槛，后面几层就不再
被这个检查覆盖，而它照样绿。）
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"


@pytest.fixture()
def hc():
    spec = importlib.util.spec_from_file_location(
        "bi_gate_healthcheck", PLUGIN_DIR / "healthcheck.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bi_gate_healthcheck"] = mod
    spec.loader.exec_module(mod)
    return mod


def _metric(name, dims=(), start="2026-01-01", end="2026-08-27"):
    return {"name": name, "dimensions": list(dims),
            "freshness": {"data_start": start, "data_end": end}}


# ---------------------------------------------------------------------------
# 挑指标与时间窗
# ---------------------------------------------------------------------------

def test_prefers_a_metric_without_dimensions(hc):
    """维度越少查询越简单，越不容易因为无关的原因误报。"""
    reg = {"metrics": [_metric("有维度的", ["bd_name"]), _metric("没维度的")]}
    assert hc.pick_metric(reg)["name"] == "没维度的"


def test_skips_metrics_without_freshness(hc):
    """没有 freshness 就挑不出可验证的时间窗 —— 不猜。"""
    reg = {"metrics": [{"name": "没有 freshness", "dimensions": []}]}
    assert hc.pick_metric(reg) is None


def test_window_comes_from_the_registry_not_from_today(hc):
    """时间窗取自注册表声明的数据范围，**不是「最近 N 天」**。

    用「最近 N 天」的话，数仓一停更健康检查就开始报不健康 —— 而链路其实是好的。
    数据新鲜度是另一条规则该管的事（rejected_no_data_in_range），别混进来。
    """
    w = hc.window_for(_metric("m", end="2026-07-16"), days=3)
    assert w["end"] == "2026-07-16"
    assert w["start"] == "2026-07-14"
    # 和今天无关
    assert dt.date.today().isoformat() not in (w["start"], w["end"])


def test_window_never_starts_before_the_data_does(hc):
    """数据只有 1 天时，窗口不能往前越界。"""
    w = hc.window_for(_metric("m", start="2026-08-27", end="2026-08-27"), days=7)
    assert w == {"start": "2026-08-27", "end": "2026-08-27", "timezone": "UTC+8"}


# ---------------------------------------------------------------------------
# 各种坏法都要报出来，且说清坏在哪一步
# ---------------------------------------------------------------------------

def _profile(tmp_path, *, registry=None, principals=None, extra_env=""):
    p = tmp_path / "prof"
    p.mkdir(exist_ok=True)
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(registry if registry is not None else
                              {"metrics": [_metric("m")]}, ensure_ascii=False),
                   encoding="utf-8")
    pm = tmp_path / "principals.json"
    pm.write_text(json.dumps({"principals": principals if principals is not None
                              else {"ou_x": {"subject": "s"}}}, ensure_ascii=False),
                  encoding="utf-8")
    (p / ".env").write_text(
        f"BI_GATE_REGISTRY={reg}\nBI_GATE_PRINCIPAL_MAP={pm}\n"
        f"BI_GATE_TOOLS=query_metric\nBI_AUDIT_LOG={tmp_path/'a.jsonl'}\n{extra_env}",
        encoding="utf-8")
    return p


def test_missing_registry_is_unhealthy(hc, tmp_path, monkeypatch):
    p = _profile(tmp_path)
    (p / ".env").write_text("BI_GATE_REGISTRY=/tmp/绝对不存在.json\n", encoding="utf-8")
    status, detail = hc.run(p)
    assert status == hc.UNHEALTHY and detail["step"] == "注册表"


def test_empty_registry_is_unhealthy(hc, tmp_path):
    p = _profile(tmp_path, registry={"metrics": []})
    status, detail = hc.run(p)
    assert status == hc.UNHEALTHY and detail["step"] == "注册表"


def test_empty_principal_list_is_unhealthy(hc, tmp_path):
    """名单里挑不出身份 —— 这是最常见的坏法之一，必须报出来而不是跳过。"""
    p = _profile(tmp_path, principals={})
    status, detail = hc.run(p)
    assert status == hc.UNHEALTHY and detail["step"] == "身份"


def test_zero_rows_counts_as_unhealthy(hc, tmp_path, monkeypatch):
    """返回 0 行算不健康。

    时间窗是照注册表声明的数据范围挑的，那段本来就该有数。0 行说明声明和实际
    对不上 —— 那是真问题，不该被当成"查询成功了只是没数据"。
    """
    p = _profile(tmp_path)
    monkeypatch.setattr(hc, "_load", lambda name, d: _FakeMod(rows=[]))
    monkeypatch.setattr(hc, "load_env", lambda profile: _env_from(p))
    status, detail = hc.run(p)
    assert status == hc.UNHEALTHY and detail["step"] == "结果"


class _FakeMod:
    """假的 gate / query 模块 —— 只为验证 run() 对返回值的判断。"""

    def __init__(self, rows):
        self._rows = rows

    def reload_registry(self): pass
    def reload_fixtures(self): pass

    def _on_pre_tool_call(self, tool_name=None, args=None, **kw):
        return {"action": "modify", "args": {}}

    def handle_query_metric(self, args, **kw):
        return json.dumps({"rows": self._rows, "meta": {"backend": "fake"}})


def _env_from(profile):
    import os
    for line in (profile / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()
    os.environ["HERMES_HOME"] = str(profile)


# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------

def test_exit_codes_distinguish_unhealthy_from_checker_error(hc, tmp_path, capsys):
    """不健康是 1，检查器自己出错是 2 —— 和 assemble_check 一致。

    混成一个的话，「链路坏了」和「检查坏了」在监控上分不开，
    而后者意味着前面那些绿都不算数。
    """
    p = _profile(tmp_path, registry={"metrics": []})
    assert hc.main(["--profile", str(p), "--json"]) == 1

    assert hc.main(["--profile", str(tmp_path / "根本没有这个目录"), "--json"]) == 2
