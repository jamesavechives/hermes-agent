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
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .identity import (
    PRINCIPAL_ARG,
    IdentityVerdict,
    Principal,
    resolve_principal,
)
from .rules import (
    CONDITION_OPS,
    GATE_SOURCE,
    PASSED,
    REJECT_GATE_ERROR,
    ActionPolicy,
    ActionRule,
    MetricRegistry,
    MetricSpec,
    Verdict,
    check_session_scan_budget,
    classify_action,
    estimate_scan_rows,
    evaluate,
    level_name,
    parse_level,
)

logger = logging.getLogger(__name__)

#: 门禁只管这个工具。run_sql 的降级路径有另一套围栏，不在本插件范围内。
GATED_TOOL = "query_metric"

#: 门禁放行时塞进工具参数的配对键。bi-query 读到就记进自己的执行记录，
#: 这样「判定」和「执行」两行才连得起来 —— 对账不用靠计数。
CALL_ID_ARG = "_bi_gate_call_id"


def _profile_name() -> str:
    """当前是哪个人格。取 ``HERMES_HOME`` 的目录名。

    审计记录原先没有这一项 —— 一旦有第二个人格，就回答不了「这条判定是谁做的」。
    探针记录一直有 profile，审计没有（和时间戳是同一天发现的同一类疏漏）。

    这个值同时用于会话预算的播种过滤：两个 profile 共用一个审计文件时，
    只按 session_id 过滤会把对方的扫描量算进自己的额度。
    """
    home = os.environ.get("HERMES_HOME", "").strip()
    return Path(home).name if home else ""

#: 注册表来源。留成环境变量是因为首批指标还在对齐口径，落地前会反复改；
#: 指标层稳定后应改为从指标服务拉取并带版本号。
REGISTRY_PATH_ENV = "BI_GATE_REGISTRY"

#: 动作分级策略（L0–L3 怎么分档）。分档标准由业务方与合规定，所以放配置不放代码 ——
#: 改分档不该要发版。未设置时这一层不启用。
ACTION_POLICY_ENV = "BI_GATE_ACTION_POLICY"

#: 该人格声明的动作上限，取值 L0–L3。属于 Profile 的字段 ③，跟着人格走。
#: 未声明按 L0 处理（最严），不是"不限制"。
ACTION_MAX_ENV = "BI_GATE_ACTION_MAX"

#: 人格声明的工具白名单（逗号分隔）。对应 Agent Profile 字段 ③ 的 ``tools``。
#:
#: **未声明按空白名单处理，即任何工具都不许派发。** 这与 ``action_max``
#: 未声明按 L0 是同一条原则：漏声明的后果应该是做不了事，而不是不受限。
#: 反过来做（未声明就不限制）会让「忘了配」和「配成全开」在行为上无法区分，
#: 那正是这套门禁要消灭的失效方式。
TOOLS_ENV = "BI_GATE_TOOLS"

#: 该人格单次会话允许的累计扫描行数。未声明表示不做这项检查——这一层加在
#: 单次限额之上，缺省取 0 会让任何查询都做不了。**没声明这件事由 verify.py
#: 显式报出来**，不能变成静默缺失。
SESSION_SCAN_MAX_ENV = "BI_GATE_SESSION_SCAN_MAX"


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
                    rows_per_day=item.get("rows_per_day"),
                )
            )
        except (KeyError, TypeError) as exc:
            # 单条坏掉不牵连整表，但要吵出来 —— 静默跳过等于悄悄放宽门禁。
            logger.error("bi-gate: 注册表条目非法，已跳过：%r（%s）", item, exc)
    # 统计时区。None = 业务方还没定 —— 那样需要时间窗的指标必须在调用里显式带
    # 时区，否则被 rejected_timezone 拦下。见 rules.resolve_timezone。
    default_tz = raw.get("default_timezone")
    if default_tz is None:
        logger.warning(
            "bi-gate: 注册表没有 default_timezone —— 需要时间窗的指标必须在调用里"
            "显式指定时区，否则会被拦。这一项待业务方定。")
    logger.info("bi-gate: 载入 %d 个指标，默认时区 %r", len(specs), default_tz)
    return MetricRegistry(specs, default_timezone=default_tz)


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
# 动作分级策略加载
# ---------------------------------------------------------------------------

def _parse_policy(raw: Mapping[str, Any]) -> ActionPolicy:
    """把 policy JSON 解析成 :class:`ActionPolicy`。

    任何一处不合法都抛异常，由调用方转成 ``unavailable=True``（全拒）。
    **不做"跳过坏规则、其余照用"** —— 一条规则被静默跳过，就是一档授权被
    静默放宽，而这正是最难在事后发现的那类问题。
    """
    rules = []
    for item in raw.get("rules", []):
        level = parse_level(item.get("level"))
        if level is None:
            raise ValueError(f"规则的 level 非法：{item.get('level')!r}，应为 L0–L3")
        when = item.get("when")
        if not isinstance(when, Mapping) or not when:
            raise ValueError(f"规则缺少 when 或 when 为空：{item!r}")
        unknown_ops = set(when) - CONDITION_OPS
        if unknown_ops:
            raise ValueError(
                f"规则用了不支持的条件算子 {sorted(unknown_ops)}；"
                f"支持的是 {sorted(CONDITION_OPS)}"
            )
        rules.append(ActionRule(level=level, when=dict(when), label=str(item.get("label", ""))))

    default_level = parse_level(raw.get("default_level", "L0"))
    if default_level is None:
        raise ValueError(f"default_level 非法：{raw.get('default_level')!r}")

    hrf = raw.get("human_review_from")
    human_review_from = None
    if hrf is not None:
        human_review_from = parse_level(hrf)
        if human_review_from is None:
            raise ValueError(f"human_review_from 非法：{hrf!r}")

    return ActionPolicy(
        rules=tuple(rules),
        default_level=default_level,
        human_review_from=human_review_from,
        version=str(raw.get("version", "")),
    )


def _load_policy() -> Optional[ActionPolicy]:
    """载入动作分级策略。

    * 未设置 ``BI_GATE_ACTION_POLICY`` —— 返回 ``None``，这一层不启用。
      分档标准要业务方与合规一起定，没定之前不该由技术侧塞一套默认值进去
      假装有授权控制；其余几条规则照常生效。
    * 设置了但载入失败 —— 返回 ``unavailable`` 策略，**所有调用都拒**。
      声明了要管却管不了，只能停摆，不能放行。
    """
    path = os.environ.get(ACTION_POLICY_ENV)
    if not path:
        logger.info("bi-gate: 未设置 %s，动作分级未启用（其余门禁规则不受影响）", ACTION_POLICY_ENV)
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        policy = _parse_policy(raw)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.error("bi-gate: 载入动作分级策略 %s 失败（%s），所有调用将被拒", path, exc)
        return ActionPolicy(unavailable=True, version="<载入失败>")
    logger.info(
        "bi-gate: 载入动作分级策略 version=%s，%d 条规则，默认 %s，人审起点 %s",
        policy.version or "(未标版本)", len(policy.rules), level_name(policy.default_level),
        level_name(policy.human_review_from) if policy.human_review_from is not None else "未启用",
    )
    return policy


def _load_action_max() -> Optional[int]:
    """读该人格声明的动作上限。

    读不到或写错都返回 ``None``；``check_action_level`` 会把 ``None`` 当 L0 处理
    （最严），而不是"不限制" —— 没声明就当没授权。
    """
    raw = os.environ.get(ACTION_MAX_ENV)
    if raw is None:
        return None
    level = parse_level(raw)
    if level is None:
        logger.error("bi-gate: %s=%r 不是合法级别（L0–L3），按未声明处理（即 L0）", ACTION_MAX_ENV, raw)
        return None
    return level


_policy: Optional[ActionPolicy] = None
_policy_loaded = False
_action_max: Optional[int] = None
_action_max_loaded = False


def _policy_now() -> Optional[ActionPolicy]:
    global _policy, _policy_loaded
    if not _policy_loaded:
        _policy = _load_policy()
        _policy_loaded = True
    return _policy


def _action_max_now() -> Optional[int]:
    global _action_max, _action_max_loaded
    if not _action_max_loaded:
        _action_max = _load_action_max()
        _action_max_loaded = True
    return _action_max


def reload_policy():
    """强制重载分级策略与 action_max。改完授权后调用，避免重启进程。"""
    global _policy, _policy_loaded, _action_max, _action_max_loaded
    _policy_loaded = False
    _action_max_loaded = False
    return _policy_now(), _action_max_now()


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

AUDIT_PATH_ENV = "BI_AUDIT_LOG"


def _audit_path() -> Optional[Path]:
    """审计文件路径。显式配置优先，否则落在该 profile 的 HERMES_HOME 下。"""
    override = os.environ.get(AUDIT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME", "").strip()
    return Path(home) / "audit.jsonl" if home else None


def _write_audit_line(record: Mapping[str, Any]) -> bool:
    """追加一行 JSON，成功返回 True。

    只用 append + 单次 write，让并发进程各自的整行不会交错（POSIX 下 O_APPEND
    的小写入是原子的，一条记录远小于 PIPE_BUF）。

    **写不进去时不阻断本次调用**，但会打 ERROR。这是一个有意的取舍：
    审计挂了就拒绝放行，会把一个记录问题变成业务不可用；而放行不记录，
    等于留下一次查不到的调用。两害相权，目前选后者 + 告警。
    这条要不要改成"记不上就拒绝"，见《人格门禁设计方案》待拍板。
    """
    path = _audit_path()
    if path is None:
        logger.error("bi-gate: 审计路径未知（HERMES_HOME 与 %s 都没设），本次判定没有留痕", AUDIT_PATH_ENV)
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception:
        logger.exception("bi-gate: 审计落盘失败 %s —— 本次判定没有留痕", path)
        return False


def _audit(
    verdict: Verdict,
    args: Mapping[str, Any],
    action_level: Optional[str] = None,
    action_max: Optional[str] = None,
    **context: Any,
) -> None:
    """记录一次判定。

    落到我们自己控制的 JSONL 文件，**不依赖宿主的日志配置**。2026-08-26 在
    dev 机上实测：一次完整会话跑完，``agent.log`` 里只有插件注册那几行，
    审计一条都没有——宿主的 handler 在什么条件下收哪些 logger，不是我们能
    保证的。而审计是合规唯一会查的东西，落不了盘等于没有。

    接 ai_cs.agent_audit 时替换 ``_write_audit_line`` 即可，调用点不用改。
    写入失败不阻塞主链路（见 ``_write_audit_line`` 里的说明）。
    """
    record = {
        "event": "bi_gate_verdict",
        # 哪个人格。有第二个人格之后，没有这一项就回答不了「这条判定是谁做的」。
        "profile": _profile_name(),
        # 时间戳。原先整个没有 —— 审计是合规唯一会查的东西，没有时间戳
        # 基本等于不可用：既回答不了"这次拦截什么时候发生的"，也没法按时间窗对账。
        # 2026-08-28 做审计上报时才发现（探针记录一直有 ts，审计没有）。
        "ts": int(time.time()),
        # 对账靠这个字段区分是谁写的——门禁写判定、bi-query 写执行。
        # 漏了它，两边记录混在一起就分不开（2026-08-26 首次落盘时就漏了）。
        "source": GATE_SOURCE,
        "gate_result": verdict.code,
        "tool": GATED_TOOL,
        "metric": args.get("metric"),
        "dimensions": args.get("dimensions"),
        "time_window": args.get("time_window"),
        # 动作级别对放行的调用同样要记 —— 事后要能回答"这个人格实际都做到了几级"，
        # 而不是只能看到被拦的那些。只记拒绝等于只看得见失败的越权尝试。
        "action_level": action_level,
        "action_max": action_max,
        # 统计时区提到顶层，不留在 detail 里。事后对账最常问的一句就是
        # 「这个数按哪个时区算的」—— 嵌在 detail 里要先解一层 JSON 才查得到。
        # timezone_source 同样要记：调用自己指定的，和吃了注册表默认值的，
        # 出了口径争议时责任完全不同。
        "timezone": (verdict.detail or {}).get("timezone"),
        "timezone_source": (verdict.detail or {}).get("timezone_source"),
        "detail": dict(verdict.detail) if verdict.detail else None,
        # 以谁的名义查。**没有主体时也要写成 null 而不是不写这个键** ——
        # 下面那行 `if v` 会把假值整个丢掉，于是"这次调用没有身份"和"这条记录
        # 是加身份之前写的"在审计里长得一模一样。写成 null 才分得开。
        "principal": None,
        **{k: v for k, v in context.items() if v},
    }
    _write_audit_line(record)
    try:
        logger.info("bi-gate verdict %s", json.dumps(record, ensure_ascii=False, default=str))
    except Exception:  # pragma: no cover - 审计不能反过来打断业务
        pass


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 会话累计扫描量
# ---------------------------------------------------------------------------
#
# 状态放在这一层而不是 rules.py：rules.py 是纯判定，不碰 I/O 也不持有状态，
# 这样每条规则都能单独测、也能拿去离线重放历史轨迹。

#: session_id → 本会话已放行的累计预估扫描行数。
_session_scanned: Dict[str, int] = {}

#: 已经从审计文件播过种的 session_id，避免重复扫盘。
_session_seeded: set = set()


def _session_scan_max() -> Optional[int]:
    raw = os.environ.get(SESSION_SCAN_MAX_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.error("bi-gate: %s=%r 不是整数，会话扫描预算按未声明处理", SESSION_SCAN_MAX_ENV, raw)
        return None
    return value if value >= 0 else None


def _seed_session_from_audit(session_id: str) -> int:
    """从审计文件把该会话此前已放行的扫描量读回来。

    为什么要播种：计数器在进程内存里，而一个会话可能跨进程（resume、
    gateway 重启、cron 续跑）。不播种的话，重启一次额度就清零——那等于给出
    一条「重启即可绕过」的路径，和我们一路在堵的失效方式是同一类。

    审计文件就是账本，不另外维护一份状态：多一份状态就多一处会跟账本不一致
    的地方。代价是每个会话首次判定要扫一遍文件，只发生一次。
    """
    path = _audit_path()
    if path is None or not session_id:
        return 0
    profile = _profile_name()
    total = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or session_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                # 同时按 profile 过滤。只按 session_id 的话，两个人格共用一个
                # 审计文件时，A 的会话播种会把 B 放行过的扫描量算进 A 的额度 ——
                # 表现是"莫名其妙额度就满了"，而且查不出为什么。
                #
                # 历史记录没有 profile 字段（2026-08-28 之前写的）。对它们放行，
                # 不然升级那一刻所有会话的历史额度会突然归零 —— 那是把一个
                # 兼容问题变成一次静默的约束放宽。
                rec_profile = rec.get("profile")
                if rec_profile not in (None, "", profile):
                    continue
                if (
                    rec.get("session_id") == session_id
                    and rec.get("gate_result") == PASSED
                    and isinstance(rec.get("estimated_rows"), int)
                ):
                    total += rec["estimated_rows"]
    except FileNotFoundError:
        return 0
    except Exception:
        # 播种失败按 0 计。这是有意选的方向：读不出账本时把额度算少，
        # 会放宽约束而不是误伤业务；同时打 ERROR，让运维看得见。
        logger.exception("bi-gate: 从审计播种会话扫描量失败，本会话按 0 起算")
        return 0
    return total


def _session_scanned_now(session_id: str) -> int:
    if session_id and session_id not in _session_seeded:
        _session_scanned[session_id] = _seed_session_from_audit(session_id)
        _session_seeded.add(session_id)
    return _session_scanned.get(session_id, 0)


def _session_add(session_id: str, rows: Any) -> None:
    """放行后累加。只有真正放行的调用才计入——被拦的没有真的去扫。"""
    if not session_id or not isinstance(rows, int):
        return
    _session_scanned[session_id] = _session_scanned.get(session_id, 0) + rows


def reset_session_counters() -> None:
    """清空会话计数。测试用。"""
    _session_scanned.clear()
    _session_seeded.clear()


def _audit_tool_denied(tool_name: str, allowed: frozenset, context: Mapping[str, Any]) -> None:
    """白名单拒绝的留痕。

    和指标判定分开记：这条回答的是「这个人格试图用一个它没被授权的工具」，
    和「它查了一个不该查的指标」不是一回事，事后统计要分得开。
    """
    # ts / profile / call_id 必须和 _audit 写的记录**同构**。
    # 这一条原先三个都没有：加时间戳和 profile 那次只改了 _audit，漏了这个
    # 单独的函数。后果是白名单拒绝在按时间查、按人格分、对账时全都看不见 ——
    # 而「这个人格试图用没被授权的工具」恰恰是最该被看见的一类记录。
    # 2026-08-28 真跑一次模型、翻审计才发现（那次前两条 ts 是 None）。
    _write_audit_line({
        "event": "bi_gate_verdict",
        "source": GATE_SOURCE,
        "profile": _profile_name(),
        "ts": int(time.time()),
        "gate_result": "rejected_tool_not_allowed",
        "tool": tool_name,
        "allowed_tools": sorted(allowed),
        "session_id": context.get("session_id"),
        "task_id": context.get("task_id"),
        "tool_call_id": context.get("tool_call_id"),
        # 被拒的调用不会有执行记录，配对不上是正常的；记 call_id 是为了能和
        # 宿主转录对上，查"模型当时到底想干什么"。
        "call_id": str(context.get("tool_call_id") or "") or None,
    })


def _allowed_tools_now() -> frozenset:
    """当前人格声明的工具白名单。

    每次调用都重读环境变量，不做缓存 —— 白名单是安全边界，宁可多读一次，
    也不要在配置变更后还按旧值放行。
    """
    raw = os.environ.get(TOOLS_ENV, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def _tool_not_allowed_block(tool_name: str, allowed: frozenset) -> Dict[str, str]:
    """工具不在白名单时的拦截消息。

    理由里写明来源与白名单内容 —— 实测过两次，只说「被拦截」时模型会自行
    编造归因（把 harness 的拦截说成远端服务的行为），给用户一个错误的解释。
    """
    if allowed:
        listed = "、".join(sorted(allowed))
        detail = f"该人格声明可用的工具只有：{listed}。"
    else:
        detail = (
            f"该人格没有声明工具白名单（环境变量 {TOOLS_ENV} 为空），"
            "按最严处理：任何工具都不允许派发。"
        )
    return {
        "action": "block",
        "message": (
            f"{GATE_SOURCE}：工具 {tool_name!r} 不在该人格的授权范围内。{detail}"
            "换个说法或改参数都没用 —— 需要新增工具请走授权变更流程修改人格声明。"
        ),
    }


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **context: Any,
) -> Optional[Dict[str, str]]:
    """在 query_metric 派发前判定；不通过则返回 block。

    返回 ``None`` 表示放行（其它工具一律不管）。

    整个函数体包在兜底里：**任何未预料的异常都转成拦截，而不是让它抛出去**。

    因为宿主那边**两层**都会把异常吃掉，且都当作「无人拦截」：

    - ``hermes_cli/plugins.py:5140`` —— ``invoke_hook`` 每个回调各自套
      try/except，抛了就记一条 ``logger.warning`` 并**不往结果里追加**。
      返回空列表 = 没有插件要拦。
    - ``agent/tool_executor.py`` ``_resolve_pre_tool_block`` 外面还有一层
      ``except Exception: return None`` —— 这层连日志都没有。

    2026-08-28 实测（注册一个必抛的回调）：``invoke_hook`` 返回 ``[]``，
    ``_dispatch_pre_tool_call_hooks`` 返回 ``block_message=None`` —— 放行。

    **也就是说门禁在宿主眼里是「失效开门」的**：崩了等于没意见。这和这套东西
    一路在讲的「判定不了一律当不通过」正好相反，而且我们改不了宿主。所以只能
    在插件内部把方向扭回来 —— 异常在我们这儿就转成 block，绝不让它逃出去。

    （原先这段注释把出处写成 ``model_tools.py``。那是 2026-08-27 桥接工具那次
    看错入口的余波 —— 结论侥幸是对的，引的位置是错的。08-28 更正。）
    """
    try:
        # ── 第一道：工具白名单 ──────────────────────────────────────
        # 这一段必须在 GATED_TOOL 判断之前。原来的写法是「不是
        # query_metric 就直接 return None」，等于门禁只盯一个工具名，
        # 其余全部放行 —— 2026-08-26 实测中模型被拦下 export 之后，
        # 转手用文件工具把同样的数据写到了磁盘上，审计里一条记录都没有。
        # 白名单要成为强制，就只能做在这里，不能交给 Hermes 的 toolsets
        # 配置（那只决定模型看得到什么，不决定能执行什么）。
        allowed = _allowed_tools_now()
        if tool_name not in allowed:
            _audit_tool_denied(tool_name, allowed, context)
            return _tool_not_allowed_block(tool_name, allowed)

        if tool_name != GATED_TOOL:
            return None

        if not isinstance(args, Mapping):
            args = {}

        policy = _policy_now()
        action_max = _action_max_now()

        # 扫描量预估不再传 None —— 那行写死的 None 让这条规则在生产路径上
        # 从来没生效过（规则在 rules.py 里、有测试、永远拿不到值）。
        # 现在不传，由 evaluate 自己按注册表声明 + 时间窗跨度算。
        registry = _registry_now()
        verdict = evaluate(
            args,
            registry,
            policy=policy,
            action_max=action_max,
        )

        # ── 身份 ──────────────────────────────────────────────────────
        # 「查什么」判完再判「以谁的名义查」。顺序这样定是因为拒因不该互相盖住：
        # 一条越权的查询，即使发起人合法，也该报越权而不是报身份问题。
        #
        # 但**身份不通过一定拦**，哪怕查询本身完全合法 —— 接上真实数据之后，
        # 一条不知道是谁发起的查询，等于用共享账号查全量。这正是门禁管不了的
        # 那类洞（门禁管「查什么」，管不了「以谁的名义查」）。
        id_verdict = resolve_principal()
        if not id_verdict.ok:
            # 拒绝的构造方式必须和 rules 里其它拒绝**完全一样**：code 决定
            # blocked（它是只读属性，传不进去），reason 前面带 GATE_SOURCE 和
            # 全角冒号。第一版写成 Verdict(blocked=True, ...) —— TypeError，
            # 被兜底转成 rejected_gate_error。方向没错（还是拦），但模型看到的是
            # 「门禁故障，请联系值班」而不是「这次调用没有身份」，排查会跑偏。
            verdict = Verdict(
                code=id_verdict.code,
                reason=f"{GATE_SOURCE}：{id_verdict.reason}",
                detail={"identity_code": id_verdict.code},
            )

        # 单次预估：审计要记（放行的也记，事后才能对账「预估 vs 实际」），
        # 会话累计也要用它。评估一次，两处共用。
        spec = registry.get(args.get("metric"))
        this_call = estimate_scan_rows(args, spec) if spec is not None else None
        session_id = str(context.get("session_id") or "")

        # ── 会话累计预算 ──────────────────────────────────────────────
        # 只有单次判定已经通过才轮到它：先回答「这次调用本身合不合法」，
        # 再回答「这个会话还有没有额度」，拒因不会互相盖住。
        session_limit = _session_scan_max()
        if not verdict.blocked and session_limit is not None:
            session_verdict = check_session_scan_budget(
                _session_scanned_now(session_id), this_call, session_limit
            )
            # 会话检查也放行时，**保留原判定的 detail**。
            # 原先这里直接 `verdict = check_session_scan_budget(...)`，而那个函数
            # 放行时返回的是不带 detail 的 ALLOW —— evaluate 好不容易带出来的
            # 时区就这么丢了。审计里 timezone 永远是 null，而我们还写了文档说
            # 「事后能查这个数按哪个时区算的」。2026-08-28 看真实审计数据才发现：
            # time_window.timezone 是 UTC+8，顶层 timezone 却是 null。
            verdict = session_verdict if session_verdict.blocked else verdict

        # 配对键。判定和执行分别写自己的审计行，之间原先没有任何能连起来的东西，
        # 对账只能靠计数 —— 而计数在时间窗边界上必然对不齐。
        # 优先用宿主的 tool_call_id（这样还能和宿主转录对上），没有才自己生成。
        call_id = str(context.get("tool_call_id") or "") or uuid.uuid4().hex[:16]

        _audit(
            verdict,
            args,
            action_level=_describe_level(args, policy),
            action_max=level_name(action_max) if action_max is not None else None,
            estimated_rows=this_call if isinstance(this_call, int) else None,
            session_scanned_before=_session_scanned_now(session_id) if session_limit else None,
            session_id=context.get("session_id"),
            task_id=context.get("task_id"),
            tool_call_id=context.get("tool_call_id"),
            call_id=call_id,
            principal=id_verdict.principal.to_audit() if id_verdict.principal else None,
        )
        if not verdict.blocked:
            # 放行之后才累加 —— 被拦的调用没有真的去扫，不该占额度。
            _session_add(session_id, this_call)
            # 把配对键塞进工具参数，让 bi-query 记下同一个 id。
            # 宿主的 pre_tool_call 支持 {"action": "modify", "args": {...}}，
            # 这条通道就是干这个用的；不走它的话，两边只能靠时间近似配对。
            # 下划线开头表示这是元数据，不是查询语义的一部分。
            # 主体和配对键一起塞进去。bi-query 拿不到主体就不许执行 ——
            # 它是真正要去连数据层的那一侧，不该相信"没人给我身份就是没关系"。
            return {"action": "modify", "args": {
                CALL_ID_ARG: call_id,
                PRINCIPAL_ARG: id_verdict.principal.to_audit() if id_verdict.principal else None,
            }}
        return {"action": "block", "message": verdict.reason or "BI 门禁拦截。"}
    except Exception:
        return _gate_error_block(args, context)


def _describe_level(args: Mapping[str, Any], policy: Optional[ActionPolicy]) -> Optional[str]:
    """给审计用的动作级别字符串。分级未启用返回 None，判定不了返回 "undecidable"。

    它自己吞掉异常：审计取不到级别是小事，把主链路带崩是大事。
    """
    if policy is None:
        return None
    try:
        level, _why = classify_action(args, policy)
    except Exception:  # pragma: no cover - 审计取值不能反过来打断业务
        return "error"
    return level_name(level) if level is not None else "undecidable"


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
