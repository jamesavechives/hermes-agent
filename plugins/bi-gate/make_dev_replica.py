#!/usr/bin/env python3
"""在 dev StarRocks 上复刻一套开发用的 ads 表，数据自己造。

为什么要这么做
--------------
dev 上**没有数仓**：没有 `ods/dwd/dws/dim`，`ads` 只有 1 张个体级表。而指标
注册表必须从 `ads` 层的表注释/列注释生成（手写就会造出第二套口径）。所以在
dev 上开发这件事，本身缺一个前提。

三个刻意的选择
--------------
1. **只复制结构和注释，不搬生产数据。** 我们真正需要的是口径（注释），
   数据自己造就够。把生产数据搬到 dev 是一个数据流动的决定，不该顺手做 ——
   那些聚合表里有 BD 的人名，虽然不是 PII，但性质不同。
2. **建在自己的库里（默认 ``bi_agent_dev``），不往共享的 ``ads`` 写。**
   dev 的 `ads` 是数据团队的地盘，我们不去占。
3. **造的数据要一眼看得出是假的。** 维度值一律带「示例」字样，数值有明显的
   周期性。真实感不是目标 —— 目标是任何人看一眼就知道不能拿去用。
   （对照：`bi-query` 的桩数据也是靠 meta 里那句 note 让模型每次都主动声明，
   实测有效。）

日期范围
--------
默认从 ``--start``（默认 2026-01-01）到**昨天**。留一天的缺口是有意的：
这样问「最近 7 天」会落在「部分超出数据范围」那一档，正好把门禁的
`data_coverage: partial` 分支在真实链路上走一遍。

用法
----
    tsh proxy app dev-starrocks --port 19030 &
    python3 make_dev_replica.py --today 2026-08-28

连接走环境变量 ``SR_HOST`` / ``SR_PORT`` / ``SR_USER`` / ``SR_PASSWORD``，
默认连本地代理的 dev。**不往代码里写凭据。**
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
SCHEMA_SQL = HERE / "ads_schema.prod.sql"

#: 复刻到哪个库。默认不是 ``ads`` —— 那是数据团队的地盘。
DEFAULT_SCHEMA = "bi_agent_dev"

#: dev 集群规模小，副本数按 1；生产 DDL 里可能是 3，照抄会建不出来。
DEV_PROPERTIES = {"replication_num": "1"}

#: 造数用的维度取值。**一律带「示例」**，让人一眼知道是假的。
DIMENSION_VALUES: Dict[str, List[str]] = {
    "bd_name": ["示例BD-甲", "示例BD-乙", "示例BD-丙"],
    "contract_name": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "示例币/USDT"],
    "symbol_name": ["BTC/USDT", "ETH/USDT", "示例币/USDT"],
    "trade_type": ["contract", "spot", "forex_mt5", "polymarket", "tradfi"],
    "fee_coin": ["USDT", "BTC", "示例币"],
}

#: 认不出来的维度列用这个模板兜底 —— 造数不能因为多了一个列就整表跳过，
#: 但也不能悄悄用空值糊过去（那样按该维度拆会得到一行空的）。
FALLBACK_DIMENSION = ["示例值A", "示例值B"]


def sql(statement: str, *, conn: Dict[str, str], timeout: int = 120) -> Tuple[bool, str, str]:
    env = dict(os.environ)
    if conn.get("password"):
        env["MYSQL_PWD"] = conn["password"]
    cmd = ["mysql", "-h", conn["host"], "-P", conn["port"], "-u", conn["user"],
           "-N", "-B", "--connect-timeout=15", "-e", statement]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    if p.returncode != 0:
        return False, "", (p.stderr or p.stdout).strip()
    return True, p.stdout, ""


# ---------------------------------------------------------------------------
# 解析导出的 DDL
# ---------------------------------------------------------------------------

def parse_schema(text: str) -> List[Tuple[str, str]]:
    """把导出的 SQL 文件切成 ``[(表名, 建表语句)]``。"""
    out: List[Tuple[str, str]] = []
    for block in text.split("-- ═══ "):
        block = block.strip()
        if not block or "CREATE TABLE" not in block:
            continue
        name = block.split(" ", 1)[0].strip()
        # 头部注释里也会出现 "SHOW CREATE TABLE" 这几个字，第一版就把文件头当成
        # 了一张表，然后拿一句中文去建表。切块靠分隔符、认表靠表名格式，两者都要。
        if not re.match(r"^\w+$", name) or "CREATE TABLE `" + name + "`" not in block:
            continue
        stmt = block[block.index("CREATE TABLE"):].rstrip().rstrip(";")
        out.append((name, stmt))
    return out


def retarget(stmt: str, schema: str) -> str:
    """把建表语句改成建到我们自己的库，并按 dev 规模调属性。"""
    stmt = re.sub(r"CREATE TABLE `([^`]+)`", rf"CREATE TABLE `{schema}`.`\1`", stmt, count=1)
    for key, value in DEV_PROPERTIES.items():
        if f'"{key}"' in stmt:
            stmt = re.sub(rf'"{key}" = "[^"]*"', f'"{key}" = "{value}"', stmt)
        else:
            stmt = re.sub(r"PROPERTIES \(", f'PROPERTIES (\n"{key}" = "{value}",', stmt, count=1)
    return stmt


def columns_of(stmt: str) -> List[Tuple[str, str, str]]:
    """从建表语句里读 ``[(列名, 类型, 注释)]``。"""
    body = stmt[stmt.index("(") + 1: stmt.rindex(") ENGINE") if ") ENGINE" in stmt else len(stmt)]
    out = []
    for line in body.splitlines():
        m = re.match(r'\s*`(\w+)`\s+([a-z0-9_]+(?:\([^)]*\))?)', line, re.I)
        if not m:
            continue
        comment = ""
        c = re.search(r'COMMENT "([^"]*)"', line)
        if c:
            comment = c.group(1)
        out.append((m.group(1), m.group(2).lower(), comment))
    return out


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------

def _seeded(*parts: Any) -> float:
    """确定性伪随机，取值 [0,1)。

    不用 ``random`` 是为了**可重跑**：同样的表 + 同样的日期，永远造出同样的数。
    这样重建一次 dev 不会让所有数字都变一遍，对比差异时才看得出真正的变化。
    """
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(h[:6], "big") / float(1 << 48)


def _value(col: str, ctype: str, day: dt.date, dim: str) -> str:
    r = _seeded(col, day.isoformat(), dim)
    # 一点周期性：周末低、月初高。让画出来的图不是一条直线，便于肉眼查错。
    season = 1.0 + 0.25 * (0 if day.weekday() >= 5 else 1) + 0.15 * (1 if day.day <= 3 else 0)
    if ctype.startswith(("bigint", "int", "smallint", "tinyint")):
        base = 300 if col.lower().startswith(("dau", "dnu")) else 60
        return str(int(base * season * (0.5 + r)))
    if ctype.startswith("decimal") or ctype.startswith("double") or ctype.startswith("float"):
        base = 20000.0 if col.lower().endswith("_u") else 500.0
        return f"{base * season * (0.3 + r):.8f}"
    if ctype == "date":
        return f"'{day.isoformat()}'"
    if ctype.startswith("datetime"):
        return f"'{day.isoformat()} 19:40:02'"
    return "'示例'"


def rows_for(table: str, cols: List[Tuple[str, str, str]], days: List[dt.date],
             time_col: str) -> List[str]:
    dim_cols = [c for c, t, _ in cols
                if t.startswith(("varchar", "char", "string")) and c != time_col]
    combos: List[Tuple[str, ...]] = [()]
    for dc in dim_cols:
        values = DIMENSION_VALUES.get(dc, FALLBACK_DIMENSION)
        combos = [c + (v,) for c in combos for v in values]

    out = []
    for day in days:
        for combo in combos:
            vals = []
            di = 0
            tag = "|".join(combo)
            for name, ctype, _ in cols:
                if name == time_col:
                    vals.append(f"'{day.isoformat()}'" if ctype == "date"
                                else f"'{day.isoformat()} 08:00:00'")
                elif name in dim_cols:
                    vals.append("'" + combo[di].replace("'", "") + "'")
                    di += 1
                else:
                    vals.append(_value(name, ctype, day, tag))
            out.append("(" + ",".join(vals) + ")")
    return out


def time_column_of(cols: List[Tuple[str, str, str]]) -> Optional[str]:
    names = [c for c, _, _ in cols]
    for cand in ("bizdate", "part_date", "part_hour", "dt", "ds"):
        if cand in names:
            return cand
    return None


# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--today", required=True,
                    help="今天的日期。造数造到昨天为止 —— 留一天缺口，"
                         "好让「最近 7 天」落在部分超出那一档")
    ap.add_argument("--host", default=os.environ.get("SR_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=os.environ.get("SR_PORT", "19030"))
    ap.add_argument("--user", default=os.environ.get("SR_USER", "root"))
    ap.add_argument("--drop", action="store_true", help="先删库重建")
    args = ap.parse_args(argv)

    conn = {"host": args.host, "port": args.port, "user": args.user,
            "password": os.environ.get("SR_PASSWORD", "")}

    if not SCHEMA_SQL.exists():
        print(f"找不到 {SCHEMA_SQL} —— 先从生产导出建表语句", file=sys.stderr)
        return 2
    tables = parse_schema(SCHEMA_SQL.read_text(encoding="utf-8"))
    print(f"读到 {len(tables)} 张表的建表语句")

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.today) - dt.timedelta(days=1)
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    print(f"造数日期：{start} ~ {end}（{len(days)} 天，留了 1 天缺口）")

    if args.drop:
        sql(f"DROP DATABASE IF EXISTS `{args.schema}`", conn=conn)
    ok, _, err = sql(f"CREATE DATABASE IF NOT EXISTS `{args.schema}`", conn=conn)
    if not ok:
        print(f"建库失败：{err}", file=sys.stderr)
        return 1

    failed = 0
    for name, stmt in tables:
        ok, _, err = sql(retarget(stmt, args.schema), conn=conn)
        if not ok:
            print(f"  ✗ {name} 建表失败：{err[:160]}")
            failed += 1
            continue
        cols = columns_of(stmt)
        tcol = time_column_of(cols)
        if tcol is None:
            print(f"  ~ {name} 没有时间列，只建表不造数")
            continue
        # 小时表只造少量点 —— 它们在生产里本来也只有一个小时的数据。
        use_days = days if not tcol.endswith("hour") else days[-14:]
        values = rows_for(name, cols, use_days, tcol)
        col_list = ",".join(f"`{c}`" for c, _, _ in cols)
        inserted = 0
        for i in range(0, len(values), 500):
            chunk = values[i:i + 500]
            ok, _, err = sql(
                f"INSERT INTO `{args.schema}`.`{name}` ({col_list}) VALUES " + ",".join(chunk),
                conn=conn, timeout=180)
            if not ok:
                print(f"  ✗ {name} 写入失败：{err[:160]}")
                failed += 1
                break
            inserted += len(chunk)
        else:
            print(f"  ✓ {name}  {inserted} 行")

    print(f"\n完成，{failed} 处失败。库：{args.schema}")
    print("注意：这里面全是造出来的假数，维度值带「示例」字样。"
          "口径注释是真的（从生产结构复制），数值不是。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
