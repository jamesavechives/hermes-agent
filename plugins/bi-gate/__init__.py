"""bi-gate — 系统 B 的门禁插件。

在 ``query_metric`` 真正派发之前做一轮确定性校验：指标是否在受控事实层、
维度是否是该指标声明过的、时间窗是否是绝对区间、预估扫描量是否超限。
任何一条不过就拦下，并把判定写进审计。

为什么做成插件而不是改核心
--------------------------
仓库是 nousresearch/hermes-agent 的 fork，上游非常活跃。门禁只用
``pre_tool_call`` / ``post_tool_call`` 这两个既有扩展点，核心文件一行不动，
同步上游时不会冲突。

这一层不做什么
--------------
**行列级权限（ACL）不在这里强制。** 谁能看哪些行、哪些列，必须由数据层的
独立库账号与行级权限保证；放在 agent 层的权限本质上是提示词级约束，绕过
一个 hook 就没了。本插件在拒绝理由里可以提示权限问题，但它不是防线。

拒绝理由为什么要写明来源
------------------------
实测过两次：拦截理由只写"命中规则"时，模型会自行编造归因（把 harness 的
拦截说成远端服务的行为），最后给用户一个错误的解释。所以每条拒绝都由
``rules.GATE_SOURCE`` 带上拦截方。见《评估与 Reward v0.1》§2.4。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping, Optional

from .rules import (
    GATE_SOURCE,
    PASSED,
    REJECT_GATE_ERROR,
    MetricRegistry,
    MetricSpec,
    Verdict,
    evaluate,
)

logger = logging.getLogger(__name__)

#: 门禁只管这个工具。run_sql 的降级路径有另一套围栏，不在本插件范围内。
GATED_TOOL = "query_metric"

#: 注册表来源。留成环境变量是因为首批指标还在对齐口径，落地前会反复改；
#: 指标层稳定后应改为从指标服务拉取并带版本号。
REGISTRY_PATH_ENV = "BI_GATE_REGISTRY"


# ---------------------------------------------------------------------------
# 指标注册表加载
# ---------------------------------------------------------------------------

def _load_registry() -> MetricRegistry:
    """从 JSON 载入指标注册表；载入失败时返回空表。

    空表意味着所有 query_metric 调用都会被 ``rejected_unknown_metric`` 拦下。
    这是有意的 fail-closed：门禁配置坏掉时应该停摆，而不是放行。
    """
    path = os.environ.get(REGISTRY_PATH_ENV)
    if not path:
        logger.warning("bi-gate: 未设置 %s，注册表为空，所有 query_metric 将被拦截", REGISTRY_PATH_ENV)
        return MetricRegistry([])
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        # 读不到或解析失败都按空表处理 —— 见上面的 fail-closed 说明。
        logger.error("bi-gate: 载入注册表 %s 失败（%s），注册表按空处理", path, exc)
        return MetricRegistry([])

    specs = []
    for item in raw.get("metrics", []):
        try:
            specs.append(
                MetricSpec(
                    name=item["name"],
                    dimensions=frozenset(item.get("dimensions", ())),
                    requires_time_window=bool(item.get("requires_time_window", True)),
                    max_scan_rows=item.get("max_scan_rows"),
                )
            )
        except (KeyError, TypeError) as exc:
            # 单条坏掉不牵连整表，但要吵出来 —— 静默跳过等于悄悄放宽门禁。
            logger.error("bi-gate: 注册表条目非法，已跳过：%r（%s）", item, exc)
    logger.info("bi-gate: 载入 %d 个指标", len(specs))
    return MetricRegistry(specs)


_registry: Optional[MetricRegistry] = None


def _registry_now() -> MetricRegistry:
    global _registry
    if _registry is None:
        _registry = _load_registry()
    return _registry


def reload_registry() -> MetricRegistry:
    """强制重载。指标层改口径后调用，避免重启进程。"""
    global _registry
    _registry = _load_registry()
    return _registry


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def _audit(verdict: Verdict, args: Mapping[str, Any], **context: Any) -> None:
    """记录一次判定。

    当前只写结构化日志。接 ai_cs.agent_audit 时替换这里的实现即可 ——
    调用点不用改。写入必须不阻塞主链路：审计挂了也不能让查询失败。
    """
    record = {
        "event": "bi_gate_verdict",
        "gate_result": verdict.code,
        "tool": GATED_TOOL,
        "metric": args.get("metric"),
        "dimensions": args.get("dimensions"),
        "time_window": args.get("time_window"),
        "detail": dict(verdict.detail) if verdict.detail else None,
        **{k: v for k, v in context.items() if v},
    }
    try:
        logger.info("bi-gate verdict %s", json.dumps(record, ensure_ascii=False, default=str))
    except Exception:  # pragma: no cover - 审计不能反过来打断业务
        logger.exception("bi-gate: 审计写入失败（已忽略，不影响本次调用）")


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **context: Any,
) -> Optional[Dict[str, str]]:
    """在 query_metric 派发前判定；不通过则返回 block。

    返回 ``None`` 表示放行（其它工具一律不管）。

    整个函数体包在兜底里：**任何未预料的异常都转成拦截，而不是让它抛出去**。
    因为 Hermes 侧对 pre_tool_call 的调用包在 ``except Exception`` 中且只记
    debug 日志（见 ``model_tools.py`` 里 ``_dispatch_pre_tool_call_hooks``
    的调用处），异常逃出去就等于门禁静默消失 —— 没人看得见，调用照常放行。
    这个失败方向对资金/口径类系统是不可接受的，所以在插件内部就把它扭回来。
    """
    if tool_name != GATED_TOOL:
        return None

    try:
        if not isinstance(args, Mapping):
            args = {}

        # EXPLAIN 预估行数由执行层在派发前填进来；当前阶段还没接，先传 None
        # （check_scan_budget 会放行并留给执行层记录）。
        verdict = evaluate(args, _registry_now(), estimated_rows=None)
        _audit(
            verdict,
            args,
            session_id=context.get("session_id"),
            task_id=context.get("task_id"),
            tool_call_id=context.get("tool_call_id"),
        )
        if not verdict.blocked:
            return None
        return {"action": "block", "message": verdict.reason or "BI 门禁拦截。"}
    except Exception:
        return _gate_error_block(args, context)


def _gate_error_block(args: Any, context: Mapping[str, Any]) -> Dict[str, str]:
    """门禁自身出错时的兜底返回：拦截。

    这里不再抛任何异常 —— 兜底自己崩掉就等于没兜。日志与审计都各自吞掉自己的
    失败，最后无论如何都会返回一个 block。
    """
    try:
        logger.exception("bi-gate: 判定过程异常，按拦截处理（args=%r）", args)
    except Exception:  # pragma: no cover - 日志坏掉不能影响拦截
        pass
    try:
        _audit(
            Verdict(
                code=REJECT_GATE_ERROR,
                reason="门禁自身异常",
                detail={"args": repr(args)[:500]},
            ),
            args if isinstance(args, Mapping) else {},
            session_id=context.get("session_id"),
            task_id=context.get("task_id"),
            tool_call_id=context.get("tool_call_id"),
        )
    except Exception:  # pragma: no cover - 审计坏掉不能影响拦截
        pass
    return {
        "action": "block",
        "message": (
            f"{GATE_SOURCE}：门禁在判定这次调用时自身出错，按拦截处理。"
            "这不是你的调用有问题，是门禁故障 —— 请联系值班，不要绕过。"
        ),
    }


def _is_gate_block(result: Any) -> bool:
    """这次调用是不是被本门禁拦下的。

    只认「结果里出现了门禁来源」这一种形态，和存活探针用同一个判据 ——
    文案会改，"被本门禁拦了"这件事的形态不会。同时匹配 ``ensure_ascii=True``
    的转义形态，理由同 :func:`probe._looks_blocked`。
    """
    if result is None:
        return False
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return GATE_SOURCE in text or json.dumps(GATE_SOURCE)[1:-1] in text


def _on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **context: Any,
) -> None:
    """放行后的回执，用来把「拦没拦」和「查出什么」对上。

    只观察，不改结果。

    注意：Hermes 在 ``pre_tool_call`` 拦下调用之后，**仍然会触发**
    ``post_tool_call``（本地实测，2026-08-24：探针的 canary 调用被拦，
    这里照样被调到一次）。所以必须先认出「这是被门禁拦下的调用」并跳过，
    否则每条拒绝都会再配一条 ``gate_result: passed``，审计日志会自相矛盾 ——
    而审计日志正是事后唯一能查的东西。
    """
    if tool_name != GATED_TOOL:
        return
    if _is_gate_block(result):
        # pre 钩子已经把判定写进审计了，这里再记一条只会让日志说谎。
        return
    logger.info(
        "bi-gate passed-call %s",
        json.dumps(
            {
                "event": "bi_gate_passed_call",
                "gate_result": PASSED,
                "metric": (args or {}).get("metric") if isinstance(args, Mapping) else None,
                "duration_ms": context.get("duration_ms"),
                "session_id": context.get("session_id"),
            },
            ensure_ascii=False,
            default=str,
        ),
    )


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
