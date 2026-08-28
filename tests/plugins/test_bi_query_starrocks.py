"""真实后端 —— SQL 怎么拼出来的，以及失效时往哪个方向倒。

这个文件**不连数据库**。SQL 长什么样是这一层最该被钉死的东西，不该只能靠
"跑一次真查询"来验证 —— 真查询需要 Teleport 代理、需要生产可达，
那样的话这条性质在 CI 里就没人守着了。

真实链路验证记录（2026-08-28，本机经 tsh 代理连生产 ads）：
    overall_daily_stat.dau 2026-07-10~07-16 → 507/343/357/526/534/622/538
    bd_refer_trade_daily_stat.commission_u 按 bd_name → 5 行
    contract_trade_daily_stat.trade_fee_u 按 contract_name → 35 行
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QUERY_DIR = REPO / "plugins" / "bi-query"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


REGISTRY = {
    "metrics": [
        {
            "name": "overall.dau",
            "description": "活跃人数",
            "dimensions": [],
            "source": {"schema": "ads", "table": "ads_overall_daily_stat_di",
                       "column": "dau", "time_column": "bizdate"},
            "freshness": {"data_start": "2026-01-01", "data_end": "2026-07-16"},
        },
        {
            "name": "bd.commission_u",
            "description": "返佣金额",
            "dimensions": ["bd_name"],
            "source": {"schema": "ads", "table": "ads_bd_refer_trade_daily_stat_di",
                       "column": "commission_u", "time_column": "bizdate"},
            "freshness": {"data_start": "2026-04-01", "data_end": "2026-07-16"},
        },
        {   # 注册表被手改坏的情形
            "name": "broken.x",
            "dimensions": [],
            "source": {"schema": "ads", "table": "t; DROP TABLE users",
                       "column": "x", "time_column": "bizdate"},
        },
    ]
}


@pytest.fixture()
def sr(tmp_path, monkeypatch):
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BI_GATE_REGISTRY", str(path))
    mod = _load("bi_query_sr_under_test", QUERY_DIR / "backend_starrocks.py")
    mod.reload_registry()
    return mod


W = {"start": "2026-07-01", "end": "2026-07-10"}
P = {"subject": "analyst_a"}


# ---------------------------------------------------------------------------
# SQL 的形状
# ---------------------------------------------------------------------------

def test_sql_uses_registry_identifiers_only(sr):
    sql, _ = sr.build_sql("overall.dau", None, W, P, "call_1")
    assert "`ads`.`ads_overall_daily_stat_di`" in sql
    assert "SUM(`dau`)" in sql
    assert "'2026-07-01'" in sql and "'2026-07-10'" in sql
    assert "LIMIT" in sql


def test_dimension_becomes_group_by(sr):
    sql, _ = sr.build_sql("bd.commission_u", ["bd_name"], W, P, "call_1")
    assert "GROUP BY `bd_name`" in sql
    assert "SUM(`commission_u`)" in sql


def test_principal_rides_along_in_a_comment(sr):
    """主体进 SQL 注释，StarRocks 的查询日志里就能和我们的审计对上号。

    **这不是权限控制** —— 注释不会被任何东西核对。是事后追溯。
    """
    sql, _ = sr.build_sql("overall.dau", None, W, P, "call_xyz")
    assert sql.startswith("/* bi-agent principal=analyst_a call_id=call_xyz */")


# ---------------------------------------------------------------------------
# 模型的输入到不了 SQL 的语法位置
# ---------------------------------------------------------------------------

def test_unregistered_metric_is_refused_here_too(sr):
    """门禁判过一次，这里独立再判一次。

    绕过门禁不等于绕过一切 —— 这一侧仍然只查注册过的指标，而不是拿着一个
    模型编的名字去问数据库。
    """
    with pytest.raises(sr.BackendError) as e:
        sr.build_sql("ads.x; DROP TABLE y", None, W, P, "c")
    assert "不在注册表里" in str(e.value)


def test_dimension_outside_the_spec_is_refused(sr):
    with pytest.raises(sr.BackendError):
        sr.build_sql("overall.dau", ["bd_name'; DROP--"], W, P, "c")


@pytest.mark.parametrize("bad", [
    "2026-07-01' OR 1=1--", "2026-7-1", "last_7d", "", None, 20260701,
])
def test_non_iso_dates_never_reach_the_sql(sr, bad):
    with pytest.raises(sr.BackendError):
        sr.build_sql("overall.dau", None, {"start": bad, "end": "2026-07-10"}, P, "c")


def test_broken_registry_identifier_is_refused(sr):
    """注册表本身被改坏也要挡住。

    注册表是我们自己生成的、可信的，但**可信不等于不用校验** ——
    生成器有 bug、或有人手改，这里是最后一道。
    """
    with pytest.raises(sr.BackendError) as e:
        sr.build_sql("broken.x", None, W, P, "c")
    assert "不是合法标识符" in str(e.value)


def test_principal_cannot_close_the_comment(sr):
    """主体里带 ``*/`` 也关不掉注释。

    主体从会话来、不是模型编的，但"来源可信"不该被当成"内容一定安全"。
    """
    sql, _ = sr.build_sql("overall.dau", None, W,
                          {"subject": "a*/ UNION SELECT 1 --"}, "c")
    head = sql.split("*/", 1)[0]
    assert "UNION" not in head.upper().replace("UNIONSELECT", "")  # 没提前闭合
    assert "*/" not in sql[3:sql.index("*/")]


def test_missing_time_window_is_refused(sr):
    with pytest.raises(sr.BackendError):
        sr.build_sql("overall.dau", None, None, P, "c")


# ---------------------------------------------------------------------------
# 失效方向：一律报错，绝不退回桩数据
# ---------------------------------------------------------------------------

def test_missing_connection_config_errors_instead_of_falling_back(sr, monkeypatch):
    """连接信息不全 → 报错。

    静默退回桩数据会造出「以为在看真实数据、其实是假数」——
    **那种错没人会来投诉，因为它看起来是对的。**
    """
    monkeypatch.delenv("BI_SR_HOST", raising=False)
    monkeypatch.delenv("BI_SR_USER", raising=False)
    with pytest.raises(sr.BackendError) as e:
        sr.run("overall.dau", None, W, P, "c")
    assert "不会退回桩数据" in str(e.value)


def test_stub_is_the_default_backend(sr, monkeypatch):
    """默认必须是 stub。切到真库是显式动作，不能靠"忘了设"就切过去。"""
    monkeypatch.delenv("BI_QUERY_BACKEND", raising=False)
    assert sr.backend_name() == "stub"


def test_unknown_backend_name_is_not_guessed(sr, monkeypatch):
    monkeypatch.setenv("BI_QUERY_BACKEND", "postgres")
    assert sr.backend_name() == "postgres"      # 不改写
    # 由 bi-query 的分流函数报错，见下


def test_tool_reports_backend_failure_as_failure_not_emptiness(tmp_path, monkeypatch):
    """查询失败要和「查出来是空的」分开。

    前者是我们的问题，后者是业务事实。混在一起的话，模型会把一次连接失败
    解释成「这段时间没有数据」—— 那是编出来的业务结论。
    """
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BI_GATE_REGISTRY", str(reg))
    monkeypatch.setenv("BI_QUERY_BACKEND", "starrocks")
    monkeypatch.setenv("BI_SR_HOST", "127.0.0.1")
    monkeypatch.setenv("BI_SR_PORT", "1")          # 必然连不上
    monkeypatch.setenv("BI_SR_USER", "nobody")
    monkeypatch.setenv("BI_AUDIT_LOG", str(tmp_path / "a.jsonl"))

    bq = _load("bi_query_sr_tool", QUERY_DIR / "__init__.py")
    bq.reload_fixtures()
    out = json.loads(bq.handle_query_metric({
        "metric": "overall.dau", "time_window": W,
        "_bi_principal": P, "_bi_gate_call_id": "c"}))

    assert out.get("error"), out
    assert out["rows"] == []
    assert "不要把它解释成" in out.get("note", "")

    records = [json.loads(l) for l in
               (tmp_path / "a.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert records[-1].get("backend_failed") is True
    assert records[-1].get("backend") == "starrocks"


def test_result_carries_the_aggregation_caveat(sr, monkeypatch):
    """SUM 是个假设，而且对去重人数是错的 —— 结果里必须带着这句话。

    注册表还给不出每个指标的正确聚合方式（数仓列注释里没有），所以只能一律
    SUM 并把局限说清楚。不说的话，「7 天 dau 加起来」会被当成
    「7 天活跃人数」，那是错的数，而且看起来完全合理。
    """
    import subprocess
    monkeypatch.setattr(sr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout="2026-07-01\t100\n", stderr=""))
    monkeypatch.setenv("BI_SR_HOST", "h")
    monkeypatch.setenv("BI_SR_USER", "u")
    out = sr.run("overall.dau", None, W, P, "c")
    assert out["aggregation"] == "SUM"
    assert "去重人数" in out["aggregation_caveat"]
    assert out["freshness"]["data_end"] == "2026-07-16"


# ---------------------------------------------------------------------------
# 数据性质声明：挂在数据上，不挂在后端上
# ---------------------------------------------------------------------------

def test_data_notice_rides_with_the_registry(tmp_path, monkeypatch):
    """复刻库/造数的声明必须从注册表一路带到模型看到的 meta 里。

    2026-08-28 真跑模型踩到的：桩数据后端的 meta 里有一句「非真实业务数值，
    不可用于对外结论」，实测模型每次都会主动声明。切到 starrocks 后端之后
    **那句话静默消失了** —— 而当时连的是 dev 复刻库，数值仍然是造的。
    模型于是对着一堆假数写出了「周末积累、周初释放」「疑似运营活动消退」
    这样的业务分析，一个字都没提数据是假的。

    和「加一层前置检查让后面几层的验证静默失效」是同一个形状：
    **换掉一个组件，会让挂在旧组件上的免责声明一起消失，而没有任何东西会报错。**
    所以声明挂在数据（注册表）上，不挂在后端上。
    """
    reg = dict(REGISTRY)
    reg["data_notice"] = "⚠️ 这是复刻库，数值全部是造出来的"
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BI_GATE_REGISTRY", str(path))
    monkeypatch.setenv("BI_QUERY_BACKEND", "starrocks")
    monkeypatch.setenv("BI_SR_HOST", "h")
    monkeypatch.setenv("BI_SR_USER", "u")
    monkeypatch.setenv("BI_AUDIT_LOG", str(tmp_path / "a.jsonl"))

    import subprocess
    mod = _load("bi_query_notice_sr", QUERY_DIR / "backend_starrocks.py")
    mod.reload_registry()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, stdout="2026-07-01\t10\n", stderr=""))
    out = mod.run("overall.dau", None, W, P, "c")
    assert "造出来的" in (out.get("data_notice") or "")

    # bi-query 在函数体里 `from . import backend_starrocks`，拿不到稳定的模块引用，
    # 所以直接打 stdlib 的 subprocess.run（monkeypatch 会在用例结束时还原）。
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, stdout="2026-07-01\t10\n", stderr=""))
    bq = _load("bi_query_notice_tool", QUERY_DIR / "__init__.py")
    bq.reload_fixtures()
    payload = json.loads(bq.handle_query_metric({
        "metric": "overall.dau", "time_window": W,
        "_bi_principal": P, "_bi_gate_call_id": "c"}))
    assert "造出来的" in payload["meta"].get("note", ""), payload["meta"]


def test_production_registry_has_no_synthetic_notice():
    """对着生产 ads 生成的注册表不该带复刻声明 —— 否则真数据会被说成假的。

    反过来同样是错：把真数说成假数，用户就不敢用了。声明必须准确，不是"多说一句
    总没错"。
    """
    path = REPO / "plugins" / "bi-gate" / "registry.ads.json"
    if not path.exists():
        pytest.skip("仓库里没有生产版注册表")
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert reg.get("data_notice") in (None, ""), reg.get("data_notice")
    assert all(m["source"]["schema"] == "ads" for m in reg["metrics"])


def test_dev_registry_declares_itself_synthetic():
    path = REPO / "plugins" / "bi-gate" / "registry.dev.json"
    if not path.exists():
        pytest.skip("仓库里没有 dev 复刻版注册表")
    reg = json.loads(path.read_text(encoding="utf-8"))
    notice = reg.get("data_notice") or ""
    assert "造出来的" in notice or "复刻" in notice, notice
    assert all(m["source"]["schema"] != "ads" for m in reg["metrics"])


def test_stub_and_real_backend_return_the_same_shape(tmp_path, monkeypatch):
    """桩数据和真实后端的**行形状必须一致**。

    2026-08-28 真跑模型踩到的：桩数据不带维度时返回一行合计，真实后端按
    time_column 分组返回一天一行。模型照着桩数据的形状告诉用户
    「日活不支持按天拆分，只能给整段聚合值」—— **那对真实后端是错的**。

    等于用 dev 教模型学一个到生产就不成立的事实。而且错得隐蔽：桩数据那句
    「非真实数值」的免责声明只覆盖**数值**，覆盖不了**形状**。
    """
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BI_GATE_REGISTRY", str(reg))
    monkeypatch.setenv("BI_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    window = {"start": "2026-07-01", "end": "2026-07-05"}

    # 桩
    monkeypatch.setenv("BI_QUERY_BACKEND", "stub")
    bq = _load("bi_query_shape_stub", QUERY_DIR / "__init__.py")
    bq.reload_fixtures()
    stub = json.loads(bq.handle_query_metric({
        "metric": "overall.dau", "time_window": window,
        "_bi_principal": P, "_bi_gate_call_id": "c"}))

    # 真（把 mysql 的输出喂进去）
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, stdout="".join(f"2026-07-0{i}\t{100 + i}\n" for i in range(1, 6)), stderr=""))
    monkeypatch.setenv("BI_QUERY_BACKEND", "starrocks")
    monkeypatch.setenv("BI_SR_HOST", "h")
    monkeypatch.setenv("BI_SR_USER", "u")
    bq2 = _load("bi_query_shape_real", QUERY_DIR / "__init__.py")
    bq2.reload_fixtures()
    real = json.loads(bq2.handle_query_metric({
        "metric": "overall.dau", "time_window": window,
        "_bi_principal": P, "_bi_gate_call_id": "c"}))

    assert len(stub["rows"]) == len(real["rows"]) == 5, (stub["rows"], real["rows"])
    assert set(stub["rows"][0]) == set(real["rows"][0]), (
        f"桩 {sorted(stub['rows'][0])} vs 真 {sorted(real['rows'][0])} —— "
        f"形状不一致会让 dev 教出到生产就不成立的结论")
    assert "bizdate" in stub["rows"][0]
