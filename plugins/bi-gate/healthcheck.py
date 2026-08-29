#!/usr/bin/env python3
"""端到端健康检查 —— 真发一次会查到数据的查询，确认整条链路活着。

和存活探针（probe.py）的分工
----------------------------
两个都要，测的不是一件事：

===============  ==================================  =========================
                 存活探针 probe.py                    健康检查 healthcheck.py
===============  ==================================  =========================
发什么            一个**必然被拦**的调用                一个**应该成功**的查询
证明什么          门禁还在拦                            整条链路还通得到数据
碰不碰数据库      **不碰**                              碰
它红了说明        门禁失效（安全问题）                   查不到数（可用性问题）
===============  ==================================  =========================

**只有探针是不够的。** 2026-08-29 之前，探针一直是绿的，但那段时间机器根本
连不到 StarRocks —— 因为 canary 指标在门禁那层就被拒了，压根走不到后端。
「门禁在工作」和「助手能用」是两回事，各自需要一个检查。

（这也是那条反复出现的形状：一个检查只覆盖它实际经过的路径。加一层前置门槛，
后面几层就不再被这个检查覆盖，而它照样绿。）

判什么算健康
------------
1. 注册表载得进来，且里面有指标；
2. 挑一个指标，用它自己声明的数据范围里的一小段时间窗查一次；
3. 门禁放行、后端返回了行。

**任何一步不成立都算不健康，包括「返回了 0 行」** —— 因为时间窗是照着注册表
声明的数据范围挑的，那段本来就该有数。0 行说明声明和实际对不上。

用法
----
    ./.venv/bin/python plugins/bi-gate/healthcheck.py --profile /data/profiles/bi

退出码：0 健康，1 不健康，2 检查器自身出错（和 assemble_check 一致）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
CHECK_ERROR = "check_error"

#: 健康检查用的身份。**必须是真登记过的主体** —— 用一个假的能测出链路，
#: 但测不出「名单配对了没有」，而名单配错是最常见的一种坏法。
PROBE_SUBJECT_ENV = "BI_HEALTHCHECK_SUBJECT"


def _load(name: str, directory: Path):
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_env(profile: Path) -> None:
    path = profile / ".env"
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()
    os.environ["HERMES_HOME"] = str(profile)


def pick_metric(registry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """挑一个用来做健康检查的指标。

    优先挑**没有维度**且数据范围最长的那个 —— 维度越少查询越简单，
    范围越长越不容易因为边界问题误报。
    """
    candidates = []
    for m in registry.get("metrics", []):
        f = m.get("freshness") or {}
        start, end = (f.get("data_start") or "")[:10], (f.get("data_end") or "")[:10]
        if not start or not end:
            continue
        candidates.append((len(m.get("dimensions") or ()), start, end, m))
    if not candidates:
        return None
    # 维度少优先，其次数据范围晚（越新越能反映当前链路）
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][3]


def window_for(metric: Dict[str, Any], days: int = 3) -> Dict[str, str]:
    """在指标自己声明的数据范围里挑一小段。

    **不用「最近 N 天」** —— 数仓一停更那就落在没有数据的区间里，健康检查会开始
    报不健康，而链路其实是好的。数据新鲜度是另一个检查该管的事，别混进来。
    """
    f = metric.get("freshness") or {}
    end = dt.date.fromisoformat(f["data_end"][:10])
    start_bound = dt.date.fromisoformat(f["data_start"][:10])
    start = max(start_bound, end - dt.timedelta(days=days - 1))
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC+8"}


def run(profile: Path) -> Tuple[str, Dict[str, Any]]:
    load_env(profile)
    detail: Dict[str, Any] = {"profile": profile.name}

    reg_path = os.environ.get("BI_GATE_REGISTRY", "")
    if not reg_path or not Path(reg_path).exists():
        return UNHEALTHY, {**detail, "step": "注册表", "message": f"读不到 {reg_path!r}"}
    registry = json.loads(Path(reg_path).read_text(encoding="utf-8"))
    detail["metrics"] = len(registry.get("metrics", []))
    if not registry.get("metrics"):
        return UNHEALTHY, {**detail, "step": "注册表", "message": "注册表里一个指标都没有"}

    metric = pick_metric(registry)
    if metric is None:
        return UNHEALTHY, {**detail, "step": "选指标",
                           "message": "没有任何指标带 freshness，挑不出可验证的时间窗"}
    detail["metric"] = metric["name"]

    # 身份：用名单里第一个主体，或环境变量指定的那个。
    pmap_path = os.environ.get("BI_GATE_PRINCIPAL_MAP", "")
    principals = {}
    if pmap_path and Path(pmap_path).exists():
        principals = json.loads(Path(pmap_path).read_text(encoding="utf-8")).get(
            "principals", {})
    want = os.environ.get(PROBE_SUBJECT_ENV, "").strip()
    platform_id = None
    for key, item in principals.items():
        if not want or item.get("subject") == want or key == want:
            platform_id = key
            break
    if platform_id is None:
        return UNHEALTHY, {**detail, "step": "身份",
                           "message": f"主体名单里挑不出可用身份（{pmap_path!r}）"}
    detail["principal"] = platform_id

    gate = _load("healthcheck_bi_gate", REPO / "plugins" / "bi-gate")
    query = _load("healthcheck_bi_query", REPO / "plugins" / "bi-query")
    gate.reload_registry()
    query.reload_fixtures()

    args = {"metric": metric["name"], "time_window": window_for(metric)}
    detail["time_window"] = args["time_window"]

    from gateway.session_context import set_session_vars, clear_session_vars
    tokens = set_session_vars(platform="healthcheck", user_id=platform_id,
                              user_name="健康检查", session_id="healthcheck")
    try:
        verdict = gate._on_pre_tool_call(tool_name="query_metric", args=dict(args))
        if verdict and verdict.get("action") == "block":
            return UNHEALTHY, {**detail, "step": "门禁",
                               "message": verdict["message"][:300]}
        args.update((verdict or {}).get("args") or {})
        out = json.loads(query.handle_query_metric(args))
    finally:
        clear_session_vars(tokens)

    detail["backend"] = (out.get("meta") or {}).get("backend")
    if out.get("error"):
        return UNHEALTHY, {**detail, "step": "后端", "message": str(out["error"])[:300]}
    rows = out.get("rows") or []
    detail["rows"] = len(rows)
    if not rows:
        # 时间窗是照注册表声明的数据范围挑的，那段本来就该有数。
        return UNHEALTHY, {**detail, "step": "结果",
                           "message": "返回 0 行 —— 时间窗取自注册表声明的数据范围，"
                                      "该段本应有数据。声明与实际对不上。"}
    return HEALTHY, detail


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True, type=Path)
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON，给采集用")
    args = ap.parse_args(argv)

    try:
        status, detail = run(args.profile)
    # SystemExit 要单独接：load_env 用 raise SystemExit 报「找不到 .env」，
    # 而 SystemExit 继承的是 BaseException，`except Exception` 接不住 ——
    # 那样「profile 路径写错了」会直接把进程带走，退出码是 1（不健康），
    # 而它其实该是 2（检查器自身出错）。两者在监控上必须分得开：
    # 前者是链路坏了，后者意味着前面那些绿都不算数。
    except SystemExit as exc:
        status, detail = CHECK_ERROR, {
            "profile": args.profile.name, "message": str(exc)}
    except Exception as exc:      # noqa: BLE001
        status, detail = CHECK_ERROR, {
            "profile": args.profile.name,
            "message": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-800:],
        }

    record = {"event": "bi_gate_healthcheck", "ts": int(time.time()),
              "status": status, **detail}
    if args.json:
        print(json.dumps(record, ensure_ascii=False, default=str))
    else:
        mark = {"healthy": "✓", "unhealthy": "✗"}.get(status, "!")
        print(f"[{mark}] {detail.get('profile','?'):12s} {status}")
        for k in ("metric", "time_window", "backend", "rows", "principal", "step", "message"):
            if k in detail:
                print(f"     {k:12s} {detail[k]}")

    return {HEALTHY: 0, UNHEALTHY: 1}.get(status, 2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
