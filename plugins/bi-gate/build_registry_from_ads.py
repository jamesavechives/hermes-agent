#!/usr/bin/env python3
"""从 StarRocks 的 ads 层元数据生成指标注册表。

为什么必须生成，不能手写
------------------------
第一版注册表 15 个指标是我们凭空编的：``daily_active_users``、
``spot_trade_volume``…… 而 2026-08-28 查生产才发现，**真实世界里已经有一整套
口径**，写在 ``ads`` 层的表注释和列注释里，它们叫 ``dau``、``spot_trade_amt_u``。

手写注册表 = 造第二套指标定义。对 BI 助手来说，**口径不一致比查不到更糟** ——
查不到用户会去问人，口径不一致他会直接拿去用。所以注册表的每一条口径都必须
来自数仓，且能重新生成、能对比差异。

三条筛选规则（都是"不确定就不收"）
----------------------------------
1. **有个体标识列的表不收。** v1 只做全平台聚合。``user_id`` / ``top_uid`` /
   ``login_id`` 这类列一出现就整表排除 —— 因为 StarRocks 这个版本没有行级安全
   （实测：``CREATE ROW ACCESS POLICY`` 语法都不存在），个体级数据一旦进注册表，
   谁能看哪些行就没有任何东西管得住。见《StarRocks 权限模型现状调研 v1.0》。
2. **没有列注释的不收。** 没注释 = 口径不明 = 我们又要开始猜。
   视图在 StarRocks 里不带列注释，所以视图整类都进不来（这是副作用，不是目标，
   但方向是对的）。
3. **查不动的不收。** 目录里有 ≠ 查得了。实测发现
   ``ads_sopt_trade_amt_fee_daily_token_stat_view`` 引用了一个不存在的库
   ``bifu_user``，一查就报错。所以每张候选表都实地探一次。

数据新鲜度是一等字段
--------------------
实测：整条数仓链路（dwd/dws/dim/ads）的 ``etl_time`` 停在 **2026-07-17 01:10**，
而业务库 ``unimargin_history`` 是活的（当天还在写）。也就是说**照着 ads 回答
「最近 7 天」会拿到六周前的数字**。

一个把 7 月的数说成上周的助手，比一个说"查不了"的助手糟得多。所以每个指标都带
``data_start`` / ``data_end`` / ``etl_time``，由门禁强制：问的时间窗整段超出
数据范围就拒绝，部分超出要在回答里说清截止到哪天。

用法
----
    tsh proxy app new-live-starrocks --port 19031 &
    python3 build_registry_from_ads.py -o bi_registry.json

连接参数走环境变量（``SR_HOST`` / ``SR_PORT`` / ``SR_USER`` / ``SR_PASSWORD``），
默认连本地代理。**不往代码里写任何凭据。**
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

#: 出现这些列名前缀，整张表判为个体级，v1 不收。
#: 宁可漏收也不错收 —— 错收一张个体级表，等于把"谁能看哪些行"这个问题
#: 悄悄绕过去了，而没有任何机制会发现。
INDIVIDUAL_COLUMN = re.compile(
    r"^(uid|user_id|top_uid|invite_uid|account_id|customer_id|login|"
    r"email|phone|mobile|id_card|passport|name|user_name|nick)",
    re.IGNORECASE,
)

#: 时间列候选，按优先级。找不到任何一个的表不收 —— 没有时间列就没法做时间窗强制。
TIME_COLUMNS = ("bizdate", "part_date", "part_hour", "dt", "ds")

#: 这些列既不是维度也不是度量，不进注册表。
META_COLUMNS = {"etl_time"}

#: 维度列的类型。数值列一律当度量 —— 但见下面 ID_COLUMN 的例外。
DIMENSION_TYPES = {"varchar", "char", "string", "text"}

#: 标识符列。**永远不当度量**，只可能当维度。
#: 第一版漏了这条：``contract_id`` / ``symbol_id`` 是 bigint，于是被当成度量
#: 收进注册表 —— 「把合约 ID 加起来」是个没有意义的数，而助手会照做。
#: 类型判断不出语义，名字能。
ID_COLUMN = re.compile(r"(^id$|_id$|_code$|^code$)", re.IGNORECASE)

#: 维度基数上限。超过这个数的列不当维度 —— 按它拆会返回上千行，
#: 那已经不是"看指标"而是"拉明细"了。
MAX_DIMENSION_CARDINALITY = 500


def mysql(sql: str, *, host: str, port: str, user: str, password: str,
          timeout: int = 60) -> Tuple[bool, List[List[str]], str]:
    """跑一条只读 SQL，返回 (成功, 行, 错误信息)。

    用 mysql 客户端而不是 Python 驱动：部署机上不一定装得了 pymysql，而
    StarRocks 走 MySQL 协议，客户端本来就有。**密码只经过 argv 之外的环境变量**。
    """
    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = password
    cmd = ["mysql", "-h", host, "-P", str(port), "-u", user,
           "-N", "-B", "--connect-timeout=15", "-e", sql]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, [], f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return False, [], (proc.stderr or proc.stdout).strip()
    rows = [line.split("\t") for line in proc.stdout.splitlines() if line]
    return True, rows, ""


class Builder:
    def __init__(self, conn: Dict[str, str], schema: str):
        self.conn = conn
        self.schema = schema
        self.notes: List[str] = []      # 给人看的：收了什么、没收什么、为什么

    def q(self, sql: str, timeout: int = 60):
        return mysql(sql, timeout=timeout, **self.conn)

    # ── 元数据 ──────────────────────────────────────────────────────────
    def read_metadata(self) -> Dict[str, Dict[str, Any]]:
        ok, rows, err = self.q(f"""
            SELECT c.TABLE_NAME, t.TABLE_TYPE, t.TABLE_COMMENT,
                   c.COLUMN_NAME, c.DATA_TYPE, c.COLUMN_COMMENT
              FROM information_schema.columns c
              JOIN information_schema.tables t
                ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
             WHERE c.TABLE_SCHEMA = '{self.schema}'
             ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION;
        """)
        if not ok:
            raise SystemExit(f"读元数据失败：{err}")
        tables: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"type": "", "comment": "", "columns": []})
        for r in rows:
            if len(r) < 6:
                continue
            name, ttype, tcomment, col, dtype, ccomment = r[:6]
            t = tables[name]
            t["type"], t["comment"] = ttype, _clean(tcomment)
            t["columns"].append({"name": col, "type": dtype.lower(),
                                 "comment": _clean(ccomment)})
        return dict(tables)

    # ── 筛选 ────────────────────────────────────────────────────────────
    def select_tables(self, tables: Dict[str, Dict[str, Any]]) -> List[str]:
        keep: List[str] = []
        for name, t in sorted(tables.items()):
            cols = t["columns"]

            individual = [c["name"] for c in cols
                          if INDIVIDUAL_COLUMN.match(c["name"])]
            if individual:
                self.notes.append(
                    f"排除 {name}：个体级（{'、'.join(individual[:3])}）。"
                    f"StarRocks 无行级安全，个体数据进注册表就没人管得住谁看哪些行")
                continue

            time_col = _time_column(cols)
            if time_col is None:
                self.notes.append(f"排除 {name}：找不到时间列，做不了时间窗强制")
                continue

            uncommented = [c["name"] for c in cols
                           if not c["comment"] and c["name"] not in META_COLUMNS]
            if uncommented:
                self.notes.append(
                    f"排除 {name}：{len(uncommented)}/{len(cols)} 列没有注释，"
                    f"口径不明（{'、'.join(uncommented[:4])}）")
                continue

            keep.append(name)
        return keep

    def probe(self, name: str) -> Tuple[bool, str]:
        """实地查一次。目录里有 ≠ 查得了。"""
        ok, _rows, err = self.q(
            f"SELECT COUNT(*) FROM {self.schema}.{name} LIMIT 1;", timeout=45)
        return ok, err

    # ── 量数据 ──────────────────────────────────────────────────────────
    def measure(self, name: str, time_col: str) -> Optional[Dict[str, Any]]:
        ok, rows, err = self.q(f"""
            SELECT COUNT(*), COUNT(DISTINCT {time_col}),
                   MIN({time_col}), MAX({time_col})
              FROM {self.schema}.{name};
        """, timeout=90)
        if not ok or not rows or len(rows[0]) < 4:
            self.notes.append(f"排除 {name}：量不出行数/日期范围（{err[:120]}）")
            return None
        total, days, lo, hi = rows[0][:4]
        total, days = int(total or 0), int(days or 0)
        if total == 0 or days == 0:
            self.notes.append(f"排除 {name}：表是空的")
            return None

        etl = None
        ok2, rows2, _ = self.q(
            f"SELECT MAX(etl_time) FROM {self.schema}.{name};", timeout=45)
        if ok2 and rows2 and rows2[0][0] not in ("NULL", ""):
            etl = rows2[0][0]

        return {"rows": total, "periods": days, "data_start": lo,
                "data_end": hi, "etl_time": etl,
                # 向上取整。宁可高估扫描量 —— 低估会让会话预算形同虚设。
                "rows_per_period": max(1, -(-total // days))}

    def dimensions(self, name: str, cols: List[Dict[str, Any]],
                   time_col: str) -> List[Dict[str, Any]]:
        out = []
        for c in cols:
            if c["name"] == time_col or c["name"] in META_COLUMNS:
                continue
            if c["type"] not in DIMENSION_TYPES and not ID_COLUMN.search(c["name"]):
                continue
            ok, rows, err = self.q(
                f"SELECT COUNT(DISTINCT {c['name']}) FROM {self.schema}.{name};",
                timeout=60)
            if not ok:
                self.notes.append(
                    f"{name}.{c['name']}：基数量不出来（{err[:80]}），不当维度")
                continue
            n = int(rows[0][0] or 0) if rows and rows[0] else 0
            if n > MAX_DIMENSION_CARDINALITY:
                self.notes.append(
                    f"{name}.{c['name']}：{n} 个不同值，超过 {MAX_DIMENSION_CARDINALITY}，"
                    f"不当维度（按它拆等于拉明细）")
                continue
            out.append({"name": c["name"], "description": c["comment"],
                        "cardinality": n})
        return out

    # ── 生成 ────────────────────────────────────────────────────────────
    def build(self) -> Dict[str, Any]:
        tables = self.read_metadata()
        self.notes.append(f"{self.schema} 库共 {len(tables)} 张表/视图")
        candidates = self.select_tables(tables)
        self.notes.append(f"通过筛选 {len(candidates)} 张，逐张探活")

        metrics: List[Dict[str, Any]] = []
        by_column: Dict[str, List[str]] = defaultdict(list)
        specs: List[Tuple[str, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], str]] = []

        for name in candidates:
            ok, err = self.probe(name)
            if not ok:
                self.notes.append(f"排除 {name}：查不动 —— {err[:140]}")
                continue
            t = tables[name]
            time_col = _time_column(t["columns"])
            stats = self.measure(name, time_col)
            if stats is None:
                continue
            dims = self.dimensions(name, t["columns"], time_col)
            dim_names = {d["name"] for d in dims}
            for c in t["columns"]:
                if c["name"] in (time_col,) or c["name"] in META_COLUMNS:
                    continue
                if c["name"] in dim_names:
                    continue
                # 只用于报告：同一个列名出现在多张表里，说明口径可能有多个版本，
                # 值得让人看一眼。命名本身已经全限定，不依赖这个。
                by_column[c["name"]].append(name)
            specs.append((name, t, stats, dims, time_col))

        for name, t, stats, dims, time_col in specs:
            dim_names = {d["name"] for d in dims}
            for c in t["columns"]:
                if c["name"] == time_col or c["name"] in META_COLUMNS:
                    continue
                if c["name"] in dim_names:
                    continue
                if ID_COLUMN.search(c["name"]):
                    # 基数太大没当成维度的 ID 列，到这里也不能变成度量。
                    self.notes.append(
                        f"{name}.{c['name']}：标识符列，基数超限当不了维度，"
                        f"也不收成指标（把 ID 加起来没有意义）")
                    continue
                # 指标名一律 `<表简称>.<列名>`。
                #
                # 第一版是「唯一就用裸列名，撞名才加限定」，结果 44 次撞名，
                # 出来一堆 `deposit@ads_overall_daily_stat_di`，而且同一张表里
                # 有的指标裸名、有的带限定 —— 模型没法从一个推出另一个。
                # 全部限定虽然长一点，但可预测：知道表就知道指标叫什么。
                metric_name = f"{_short(name)}.{c['name']}"
                metrics.append({
                    "name": metric_name,
                    "description": c["comment"],
                    "unit": _unit_of(c["name"], c["type"]),
                    "dimensions": sorted(dim_names),
                    "requires_time_window": True,
                    "rows_per_day": stats["rows_per_period"],
                    # 单次上限取 30 天的量，向上取整到百。够看一个月趋势，
                    # 又不至于一次把整表拉走。
                    "max_scan_rows": _round_up(stats["rows_per_period"] * 30),
                    "source": {
                        "catalog": "default_catalog",
                        "schema": self.schema,
                        "table": name,
                        "column": c["name"],
                        "time_column": time_col,
                        "table_description": t["comment"],
                    },
                    "freshness": {
                        "data_start": stats["data_start"],
                        "data_end": stats["data_end"],
                        "etl_time": stats["etl_time"],
                    },
                })

        for col, tabs in sorted(by_column.items()):
            if len(tabs) > 1:
                self.notes.append(
                    f"同名列 {col} 出现在 {len(tabs)} 张表：{'、'.join(tabs)} —— "
                    f"指标名已按表限定，不会混；但值得确认这几处是不是同一个口径")

        return {
            # 统计时区仍然待业务方定 —— 生成器不替他们决定。
            "default_timezone": None,
            "generated_from": f"{self.schema}（StarRocks 元数据）",
            "metrics": sorted(metrics, key=lambda m: m["name"]),
            "dimensions_catalog": {
                name: dims for name, _t, _s, dims, _tc in specs if dims
            },
            "build_notes": self.notes,
        }


def _short(table: str) -> str:
    """表名 → 指标名前缀。去掉 ``ads_`` 前缀和分层后缀。

    ``ads_overall_daily_stat_di`` → ``overall_daily_stat``
    保留可读性，不做进一步缩写 —— 缩写要建一张对照表，那就又是一份会漂移的东西。
    """
    s = table
    if s.startswith("ads_"):
        s = s[4:]
    for suf in ("_di", "_da", "_ha", "_hi", "_view", "_tmp"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def _clean(s: str) -> str:
    return "" if s in (None, "NULL") else s.strip()


def _time_column(cols: List[Dict[str, Any]]) -> Optional[str]:
    names = {c["name"] for c in cols}
    for cand in TIME_COLUMNS:
        if cand in names:
            return cand
    return None


def _unit_of(col: str, dtype: str) -> str:
    """单位。**只按明确的后缀判断，判断不了就留空** —— 猜错单位比没有单位糟：
    没有单位用户会去问，猜错了他直接按错的量级用。"""
    low = col.lower()
    if low.endswith("_u") or low in ("deposit", "withdrawal", "net_deposit"):
        return "USDT"
    if dtype in ("bigint", "int", "smallint", "tinyint"):
        return "人/笔"
    return ""


def _round_up(n: int) -> int:
    step = 100
    return max(step, -(-n // step) * step)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", default="-", help="输出文件，默认 stdout")
    ap.add_argument("--schema", default="ads")
    ap.add_argument("--host", default=os.environ.get("SR_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=os.environ.get("SR_PORT", "19031"))
    ap.add_argument("--user", default=os.environ.get("SR_USER", "root"))
    args = ap.parse_args(argv)

    conn = {"host": args.host, "port": args.port, "user": args.user,
            "password": os.environ.get("SR_PASSWORD", "")}
    b = Builder(conn, args.schema)
    registry = b.build()

    text = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)

    n = len(registry["metrics"])
    print(f"\n生成 {n} 个指标，来自 {len(registry['dimensions_catalog'])} 张表。",
          file=sys.stderr)
    print("构建说明（这些不是错误，是「为什么没收」）：", file=sys.stderr)
    for note in registry["build_notes"]:
        print(f"  · {note}", file=sys.stderr)
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
