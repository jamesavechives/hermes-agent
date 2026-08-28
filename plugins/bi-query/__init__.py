"""bi-query — 受控指标层的查询工具。

它是 ``bi-gate`` 要保护的那个东西，**不是门禁的一部分**。两者分开是有意的：
判定只能有一处，两边各写一套规则必然漂移。所以这里**不重新校验指标是否注册、
维度合不合法、时间窗要不要绝对** —— 那是 ``pre_tool_call`` 上门禁的职责。

那这里做什么
------------
1. 执行查询，返回确定性结果；
2. **把自己真的执行了什么记进审计**。

第 2 条是这个插件存在的第二个理由。门禁在 ``pre_tool_call`` 留一条判定记录，
工具在执行后留一条执行记录，两边对账：**出现了没有对应判定记录的执行，就说明
这次调用没经过门禁** —— 要么门禁没启用，要么走的是别的工具名。这正是
《人格门禁设计方案》图 5 里那几条绕过路径唯一能被发现的方式。

阶段一为什么是桩数据，不接 StarRocks
------------------------------------
不是偷懒，是顺序问题。方案 §6.2 要求「查询以发起人身份执行，agent 自己没有
独立的数据权限」。现在既没有发起人身份透传，也没定「无人发起的任务用谁的身份」
（§十一待拍板第 6 条）。此刻用一个共享服务账号连上 StarRocks，等于亲手造出
§6.2 明令禁止的东西，而且一旦跑起来就很难退回去。

所以先用桩数据把「模型 → 门禁 → 工具 → 审计」这条链闭合，等身份透传定了再换
真实后端 —— 换的时候只动 ``_backend_stub``，工具契约和审计格式都不变。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOL_NAME = "query_metric"

#: 门禁放行时塞进参数的配对键（bi-gate 的 CALL_ID_ARG）。
#: 这里写死字符串而不是从 bi-gate import —— 两个插件要能各自独立部署，
#: 一方没装的时候另一方不该崩。代价是这个常量在两处，改名要同时改；
#: 有一条测试守住它们一致。
GATE_CALL_ID_ARG = "_bi_gate_call_id"

#: 门禁放行时塞进来的主体（以谁的名义查）。和 GATE_CALL_ID_ARG 一样，
#: **故意在两个插件里各写一份** —— 两边可以分别部署，靠测试守住相等。
GATE_PRINCIPAL_ARG = "_bi_principal"
TOOLSET = "bi"

#: 审计里标记执行方，和门禁的 GATE_SOURCE 对应，便于对账。
EXEC_SOURCE = "bi-query"

#: 桩数据路径，可用环境变量覆盖（部署时指向 profile 自己的那份）。
FIXTURES_ENV = "BI_QUERY_FIXTURES"

_DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures.json"


# ---------------------------------------------------------------------------
# 工具 schema —— 这就是门禁与执行层的接口契约
# ---------------------------------------------------------------------------
#
# 字段与门禁 rules.py 的判定输入一一对应，改这里必须同步改那边：
#   metric        → check_metric_registered
#   dimensions    → check_dimensions / dimensions_count_gte 规则
#   time_window   → check_time_window（必须绝对区间）
#   granularity   → 动作分级：row 判 L1
#   export        → 动作分级：true 判 L2
SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "查询受控指标层。只能查注册表里已登记的指标，维度必须是该指标声明过的，"
        "时间窗必须是绝对日期区间（不接受 last_7d / now 这类相对写法）。"
        "不要自己编造指标名或数值——查不到就如实说查不到。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "description": "指标名，必须与注册表一致，例如 daily_active_users",
            },
            "dimensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按哪些维度切分；不传表示只要总计",
            },
            "time_window": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "起始日期 YYYY-MM-DD，含"},
                    "end": {"type": "string", "description": "结束日期 YYYY-MM-DD，不含"},
                },
                "required": ["start", "end"],
                "description": "绝对日期区间",
            },
            "granularity": {
                "type": "string",
                "enum": ["agg", "row"],
                "description": "agg=聚合结果（默认），row=按行返回明细",
            },
            "export": {
                "type": "boolean",
                "description": "是否导出到受控环境之外；默认 false",
            },
        },
        "required": ["metric"],
    },
}


# ---------------------------------------------------------------------------
# 桩后端
# ---------------------------------------------------------------------------

_fixtures_cache: Optional[Dict[str, Any]] = None


def _fixtures_path() -> Path:
    override = os.environ.get(FIXTURES_ENV, "").strip()
    return Path(override) if override else _DEFAULT_FIXTURES


def reload_fixtures() -> None:
    """清空缓存，下次查询时重新读盘。测试与热更新用。"""
    global _fixtures_cache
    _fixtures_cache = None


def _fixtures() -> Dict[str, Any]:
    global _fixtures_cache
    if _fixtures_cache is None:
        with open(_fixtures_path(), "r", encoding="utf-8") as fh:
            _fixtures_cache = json.load(fh)
    return _fixtures_cache


def _dimension_key(dimensions: Optional[List[str]]) -> str:
    """维度列表 → 桩数据里的键。顺序无关，所以排序后拼接。"""
    if not dimensions:
        return "__total__"
    return "+".join(sorted(str(d) for d in dimensions))


def _days_in_window(window: Any) -> int:
    """时间窗天数。算不出来时按 1 天——宁可低估，也不要因为格式问题抛异常。"""
    if not isinstance(window, dict):
        return 1
    try:
        start = _dt.date.fromisoformat(str(window.get("start", "")))
        end = _dt.date.fromisoformat(str(window.get("end", "")))
    except ValueError:
        return 1
    return max(1, (end - start).days)


def _backend_stub(metric: str, dimensions: Optional[List[str]], window: Any) -> Dict[str, Any]:
    """确定性桩后端。换成真实 StarRocks 时只替换这一个函数。"""
    fx = _fixtures()
    table = fx.get("data", {}).get(metric)
    if table is None:
        return {"error": "no_fixture", "rows": [], "scanned_rows": 0}

    rows = table.get(_dimension_key(dimensions))
    if rows is None:
        return {
            "error": "no_fixture_for_dimensions",
            "rows": [],
            "scanned_rows": 0,
            "available_dimension_sets": sorted(table),
        }

    per_day = int(fx.get("rows_per_day", {}).get(metric, 1))
    scanned = per_day * _days_in_window(window) * max(1, len(rows))
    return {"rows": rows, "scanned_rows": scanned}


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

# --- 审计落盘 -----------------------------------------------------------
#
# 不依赖宿主的日志配置。2026-08-26 在 dev 机上实测：会话跑完，agent.log 里
# 只有插件注册那几行，审计一条没有——宿主的 handler 在什么条件下收哪些
# logger，不是我们能保证的东西。而审计是合规唯一会查的东西，落不了盘等于
# 没有。所以这里直接写一个我们自己控制的 JSONL 文件。
#
# 换成正式审计表时只改这一个函数，调用点不动。

AUDIT_PATH_ENV = "BI_AUDIT_LOG"


def _audit_path() -> Optional[Path]:
    override = os.environ.get(AUDIT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME", "").strip()
    return Path(home) / "audit.jsonl" if home else None


def _write_audit_line(record: Dict[str, Any]) -> bool:
    """追加一行 JSON。成功返回 True。

    只用 append 模式 + 单次 write，让并发进程各自的整行不会交错
    （POSIX 下 O_APPEND 的小写入是原子的；一条记录远小于 PIPE_BUF）。
    """
    path = _audit_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception:
        logger.exception("bi-query 审计落盘失败: %s", path)
        return False


def _audit(record: Dict[str, Any]) -> bool:
    """执行侧留痕。返回是否成功落盘。

    格式与门禁那边同构：一行一条 JSON，靠 ``source`` 区分是谁写的。
    门禁写判定、这里写执行，两边对账能发现「执行了但没判定」的调用。
    """
    record.setdefault("source", EXEC_SOURCE)
    ok = _write_audit_line(record)
    try:
        logger.info("bi-query audit %s", json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        pass
    return ok


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def handle_query_metric(args: Dict[str, Any], **_kwargs: Any) -> str:
    """执行一次指标查询。

    走到这里意味着门禁已经放行——或者门禁根本不在（见模块开头第 2 条）。
    这里不再判定，只执行 + 留痕。
    """
    metric = str(args.get("metric") or "").strip()
    dimensions = args.get("dimensions")
    if isinstance(dimensions, str):
        dimensions = [dimensions]
    window = args.get("time_window")
    granularity = str(args.get("granularity") or "agg")
    export = bool(args.get("export") or False)

    if not metric:
        _audit({"event": "rejected_by_tool", "reason": "metric_missing"})
        return json.dumps({"error": "metric 是必填的"}, ensure_ascii=False)

    # ── 没有主体不执行 ──────────────────────────────────────────────
    # 这一条和 call_id 的处理**故意不同**。call_id 缺了照样执行、记成 None ——
    # 因为它是用来发现"这次调用绕过了门禁"的探针，抹平了就发现不了。
    # 主体缺了必须拒：这一侧是真正要去连数据层的，接上 StarRocks 之后一条
    # 不知道以谁的名义发出的查询，就是用共享账号查全量。那正是整件事要防的。
    #
    # 拒了同样留痕（记 principal=None），所以"绕过门禁"依旧看得见 ——
    # 既拒绝执行，又不丢失这次异常。
    principal = args.get(GATE_PRINCIPAL_ARG)
    if not isinstance(principal, dict) or not principal.get("subject"):
        _audit({
            "event": "rejected_by_tool",
            "reason": "principal_missing",
            "profile": Path(os.environ.get("HERMES_HOME", "")).name,
            "ts": int(time.time()),
            "call_id": args.get(GATE_CALL_ID_ARG),
            "metric": metric,
            "principal": None,
        })
        return json.dumps({
            "error": "这次查询没有携带发起人身份，不能执行。"
                     "数据按人授权，不知道是谁就查不了。",
        }, ensure_ascii=False)

    result = _backend_stub(metric, dimensions, window)
    fx = _fixtures()

    audit: Dict[str, Any] = {
        "event": "executed",
        # 哪个人格。和门禁那侧同一个取法（HERMES_HOME 的目录名），
        # 这样对账时能按 profile 分开看，而不是把所有人格混成一摊。
        "profile": Path(os.environ.get("HERMES_HOME", "")).name,
        # 时间戳与配对键。两个原先都没有：
        #   没时间戳 → 审计按时间窗查不了，也没法回答"这次执行发生在什么时候"
        #   没配对键 → 「判定」和「执行」两行连不起来，对账只能靠计数，
        #              而计数在时间窗边界上必然对不齐
        # 配对键由门禁在放行时塞进 args（宿主的 modify 通道），这里原样记下。
        # 取不到说明这次调用**没经过门禁** —— 那本身就是最该被发现的情况，
        # 所以记成 None 而不是自己生成一个，别把异常抹平成正常。
        "ts": int(time.time()),
        "call_id": args.get(GATE_CALL_ID_ARG),
        # 以谁的名义查。审计里记全量（含 display），传给后端时只用 subject。
        "principal": principal,
        "tool": TOOL_NAME,
        "metric": metric,
        "dimensions": sorted(dimensions) if dimensions else [],
        "time_window": window,
        "granularity": granularity,
        "export": export,
        "row_count": len(result.get("rows", [])),
        "scanned_rows": result.get("scanned_rows", 0),
        "backend": "stub",
        "fixtures_version": fx.get("version", ""),
    }
    if result.get("error"):
        audit["error"] = result["error"]
    _audit(audit)

    payload: Dict[str, Any] = {
        "metric": metric,
        "dimensions": audit["dimensions"],
        "time_window": window,
        "rows": result.get("rows", []),
        "meta": {
            "backend": "stub",
            "fixtures_version": fx.get("version", ""),
            "scanned_rows": result.get("scanned_rows", 0),
            "note": "阶段一桩数据，非真实业务数值，不可用于对外结论",
        },
    }
    if result.get("error"):
        payload["error"] = result["error"]
        if result.get("available_dimension_sets"):
            payload["available_dimension_sets"] = result["available_dimension_sets"]
    return json.dumps(payload, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=handle_query_metric,
        description=SCHEMA["description"],
        emoji="\U0001f4ca",  # bar chart
    )
