"""真实后端 —— 按注册表拼 SQL，查 StarRocks。

SQL 从哪来（这是这个文件最要紧的一条）
--------------------------------------
**模型不提供 SQL，也不提供任何进入 SQL 的标识符。**

它只提供三样东西：指标名、维度名列表、时间窗。前两样在注册表里查得到才算数，
查不到就没有对应的表名/列名可用；时间窗的日期已经被门禁用正则校过是
``YYYY-MM-DD``。所以最终拼进 SQL 的每一个标识符都来自**注册表**，
每一个字面量都是校验过的日期。

这不是"我们小心地转义了"，是**模型的输入根本到不了 SQL 的语法位置**。
两者差别很大：前者依赖每次都记得转义，后者是结构上就没有那条路。

注册表在这里再读一遍，不是复用门禁的判定
----------------------------------------
门禁已经判过一次「这个指标在不在注册表里」。这里**独立再查一次**是有意的冗余：
如果哪天门禁被绕过（那正是审计对账要发现的情况），这一侧仍然只会查注册过的
指标 —— 而不是拿着一个模型编的名字去问数据库。

代价是两处都要能读到注册表，好处是绕过门禁并不等于绕过一切。

身份：记下来，但不假装它是权限
------------------------------
每条 SQL 前面挂一段注释 ``/* bi-agent principal=... call_id=... */``。
StarRocks 的查询日志里会带上它，于是数据库侧的审计能和我们的审计对上号。

**但这不是权限控制。** StarRocks 这个版本没有行级安全（实测：
``CREATE ROW ACCESS POLICY`` 语法都不存在），注释也不会被任何东西核对。
它的作用是事后追溯，不是事前拦截。见《StarRocks 权限模型现状调研 v1.0》。

连接账号
--------
凭据只走环境变量，**代码里不写任何账号密码**。而且默认值是空 —— 没配就报错，
不会悄悄退回桩数据（那会让"以为在查真实数据、其实在看假数"成为可能，
那是这套东西最不能接受的一类失效）。

目前可用的账号都是全库读的共享账号。应该建一个只读 ``ads`` 的专用账号：

.. code-block:: sql

    CREATE USER 'bi_agent_ro'@'10.%.%.%' IDENTIFIED BY '<由运维设置>';
    CREATE ROLE bi_agent_ro;
    GRANT SELECT ON ALL TABLES IN DATABASE ads TO ROLE bi_agent_ro;
    GRANT bi_agent_ro TO USER 'bi_agent_ro'@'10.%.%.%';

这条 DDL 需要在生产上执行，不是我们能自己动的。在它建好之前，用现有只读账号
跑，但**爆炸半径是全库**，这一点必须写在部署说明里，不能靠"反正只查 ads"。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 后端选择。``stub``（默认）用桩数据，``starrocks`` 连真库。
#: 默认留 stub 是因为：配错了退回假数据比查不到更糟，所以切换必须是显式动作。
BACKEND_ENV = "BI_QUERY_BACKEND"

HOST_ENV = "BI_SR_HOST"
PORT_ENV = "BI_SR_PORT"
USER_ENV = "BI_SR_USER"
PASSWORD_ENV = "BI_SR_PASSWORD"

#: 注册表路径。和门禁读的是**同一个文件** —— 两份注册表必然漂移。
REGISTRY_ENV = "BI_GATE_REGISTRY"

#: 单次查询的行数硬上限。门禁已经按 max_scan_rows 判过一次，这里是第二道：
#: 门禁判的是"预估扫描量"，这里限的是"实际返回行数"，两者会不一致。
HARD_ROW_LIMIT = int(os.environ.get("BI_QUERY_ROW_LIMIT", "1000"))

#: 标识符白名单。注册表里的表名/列名必须长这样才拼进 SQL。
#: 注册表是我们自己生成的、可信的，但**可信不等于不用校验** ——
#: 生成器有 bug、或有人手改了注册表，这里是最后一道。
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 日期字面量。门禁已经校过一次，这里再校一次，理由同上。
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BackendError(RuntimeError):
    """后端不可用或查询失败。**不降级到桩数据** —— 见模块说明。"""


def backend_name() -> str:
    return (os.environ.get(BACKEND_ENV) or "stub").strip().lower()


def _conn() -> Dict[str, str]:
    host = os.environ.get(HOST_ENV, "").strip()
    user = os.environ.get(USER_ENV, "").strip()
    if not host or not user:
        raise BackendError(
            f"真实后端已启用（{BACKEND_ENV}=starrocks），但连接信息不全："
            f"需要 {HOST_ENV} 和 {USER_ENV}。"
            f"**不会退回桩数据** —— 让人以为在看真实数据、其实是假数，"
            f"比查不到严重得多。")
    return {"host": host, "port": os.environ.get(PORT_ENV, "9030").strip() or "9030",
            "user": user, "password": os.environ.get(PASSWORD_ENV, "")}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_registry_cache: Optional[Dict[str, Dict[str, Any]]] = None
_registry_path: Optional[str] = None
#: 注册表顶层的数据性质声明。见 build_registry_from_ads 里 data_notice 的说明 ——
#: 声明挂在数据上，不挂在后端上，否则换个后端它就静默消失了。
_registry_notice: Optional[str] = None


def _load_registry() -> Dict[str, Dict[str, Any]]:
    global _registry_cache, _registry_path, _registry_notice
    path = os.environ.get(REGISTRY_ENV, "").strip()
    if _registry_cache is not None and path == _registry_path:
        return _registry_cache
    if not path:
        raise BackendError(f"没有设置 {REGISTRY_ENV}，查不到指标对应的底表")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise BackendError(f"读注册表 {path} 失败：{exc}") from exc
    out = {}
    for item in raw.get("metrics", []):
        name = item.get("name")
        if name:
            out[name] = item
    _registry_cache, _registry_path = out, path
    _registry_notice = raw.get("data_notice")
    return out


def data_notice() -> Optional[str]:
    """注册表声明的数据性质（复刻库/造数等）。载入前返回 None。"""
    _load_registry()
    return _registry_notice


def reload_registry() -> None:
    global _registry_cache, _registry_notice
    _registry_cache = None
    _registry_notice = None


def _ident(value: Any, what: str) -> str:
    s = str(value or "")
    if not _IDENT.match(s):
        raise BackendError(f"注册表里的{what} {s!r} 不是合法标识符 —— 拒绝拼进 SQL")
    return s


def _date(value: Any, what: str) -> str:
    s = str(value or "")
    if not _DATE.match(s):
        raise BackendError(f"{what} {s!r} 不是 YYYY-MM-DD —— 拒绝拼进 SQL")
    return s


# ---------------------------------------------------------------------------
# 拼 SQL
# ---------------------------------------------------------------------------

def build_sql(metric: str, dimensions: Optional[List[str]], window: Any,
              principal: Optional[Dict[str, Any]] = None,
              call_id: str = "") -> Tuple[str, Dict[str, Any]]:
    """把一次指标查询拼成 SQL。返回 ``(sql, 用到的注册表条目)``。

    单独抽出来是为了能在不连数据库的情况下测 —— SQL 长什么样是这一层最该被
    钉死的东西，不该只能通过"跑一次真查询"来验证。
    """
    reg = _load_registry()
    spec = reg.get(metric)
    if spec is None:
        # 门禁本该已经拦下，走到这里说明这次调用没经过门禁 —— 那是最该被发现的
        # 情况，所以这里报错而不是放行。
        raise BackendError(
            f"指标 {metric!r} 不在注册表里。（门禁本该拦下这次调用 —— "
            f"走到执行层说明它可能被绕过了，请查审计对账。）")

    src = spec.get("source") or {}
    schema = _ident(src.get("schema"), "库名")
    table = _ident(src.get("table"), "表名")
    column = _ident(src.get("column"), "列名")
    time_col = _ident(src.get("time_column"), "时间列")

    allowed = set(spec.get("dimensions") or ())
    dims: List[str] = []
    for d in (dimensions or []):
        if d not in allowed:
            # 同上：门禁判过一次，这里独立再判一次。
            raise BackendError(
                f"维度 {d!r} 不在指标 {metric} 声明的维度里（{sorted(allowed)}）。")
        dims.append(_ident(d, "维度列"))

    if not isinstance(window, dict):
        raise BackendError("缺时间窗 —— 无界查询不执行")
    start = _date(window.get("start"), "时间窗 start")
    end = _date(window.get("end"), "时间窗 end")

    # 度量一律 SUM。**这是一个假设，而且对某些指标是错的** ——
    # 比如 dau（去重人数）按天求和会重复计人，价格类指标求和更没有意义。
    # 正确做法是让注册表带上聚合方式（sum / avg / last / 不可聚合）。
    # 生成器现在还给不出这个信息（数仓的列注释里没有），所以：
    #   * 只按 SUM 拼，并且
    #   * 在返回结果里把这个假设明确标出来，让回答里能说清楚。
    # 不标的话，"7 天 dau 加起来"会被当成"7 天活跃人数"，那是错的数。
    agg = "SUM"

    select = [f"`{time_col}`"] if not dims else [f"`{d}`" for d in dims]
    group = list(select)
    select.append(f"{agg}(`{column}`) AS `value`")

    marker = _comment(principal, call_id)
    sql = (
        f"{marker}SELECT {', '.join(select)} "
        f"FROM `{schema}`.`{table}` "
        f"WHERE `{time_col}` >= '{start}' AND `{time_col}` <= '{end}' "
        f"GROUP BY {', '.join(group)} "
        f"ORDER BY {', '.join(group)} "
        f"LIMIT {HARD_ROW_LIMIT}"
    )
    return sql, spec


def _comment(principal: Optional[Dict[str, Any]], call_id: str) -> str:
    """挂在 SQL 前面的追溯注释。

    **只放能安全出现在 SQL 注释里的字符**，其余一律丢掉 —— 主体是从会话来的，
    不是模型编的，但"来源可信"不该被当成"内容一定安全"。
    """
    safe = re.compile(r"[^A-Za-z0-9_.:@-]")
    subject = safe.sub("", str((principal or {}).get("subject") or "unknown"))[:64]
    cid = safe.sub("", str(call_id or ""))[:32]
    return f"/* bi-agent principal={subject} call_id={cid} */ "


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def run(metric: str, dimensions: Optional[List[str]], window: Any,
        principal: Optional[Dict[str, Any]] = None,
        call_id: str = "") -> Dict[str, Any]:
    """执行一次真实查询。失败抛 :class:`BackendError`，**不返回假数据**。"""
    sql, spec = build_sql(metric, dimensions, window, principal, call_id)
    conn = _conn()

    env = dict(os.environ)
    if conn["password"]:
        env["MYSQL_PWD"] = conn["password"]
    cmd = ["mysql", "-h", conn["host"], "-P", conn["port"], "-u", conn["user"],
           "-N", "-B", "--connect-timeout=15", "-e", sql]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError(f"连 StarRocks 失败：{type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise BackendError(f"查询失败：{(proc.stderr or proc.stdout).strip()[:400]}")

    src = spec.get("source") or {}
    dims = list(dimensions or [])
    keys = dims or [src.get("time_column", "time")]
    rows: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != len(keys) + 1:
            continue
        row = {k: _null(v) for k, v in zip(keys, parts[:-1])}
        row["value"] = _num(parts[-1])
        rows.append(row)

    return {
        "rows": rows,
        "scanned_rows": len(rows),
        "sql": sql,
        "aggregation": "SUM",
        # 这条必须跟着结果走 —— 见 build_sql 里关于 SUM 的说明。
        "aggregation_caveat":
            "本次按 SUM 聚合。注册表还没有声明每个指标的正确聚合方式，"
            "所以对「去重人数」（dau/uv 类）和价格类指标，跨天求和是错的 —— "
            "这类指标请按天看，不要看合计。",
        "freshness": spec.get("freshness"),
        "metric_description": spec.get("description"),
        "data_notice": _registry_notice,
        "schema": (spec.get("source") or {}).get("schema"),
    }


def _null(v: str) -> Any:
    return None if v == "NULL" else v


def _num(v: str) -> Any:
    if v == "NULL":
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return int(f) if f.is_integer() else f
