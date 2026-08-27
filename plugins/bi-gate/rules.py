"""bi-gate 的判定规则与拒因分类。

规则与执行分开放：这里只有纯函数和数据，没有 I/O、没有 hook 依赖，
所以每条规则都能单独跑单测，也能被门禁之外的地方复用（比如离线跑一遍
历史轨迹，看新规则会拦掉多少历史调用）。

拒因编号是对外契约的一部分——审计表、告警、以及给模型看的拒绝理由都引用它，
所以只增不改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# 拒因分类
# ---------------------------------------------------------------------------

# 与《可观测与自进化实施方案》的 gate_result 取值对齐，便于两边的审计表合并统计。
REJECT_UNKNOWN_METRIC = "rejected_unknown_metric"
REJECT_BAD_PARAM = "rejected_bad_param"
REJECT_NO_TIME_WINDOW = "rejected_no_time_window"
REJECT_SCAN = "rejected_scan"
#: 单次没超、但本会话累计超了。与 REJECT_SCAN 分开，因为处置方式完全不同：
#: 单次超限缩小时间窗就能过，累计超限缩不出来——已经拿走的数据不会退回去。
REJECT_SESSION_SCAN = "rejected_session_scan"
#: 动作级别超出该人格声明的 action_max。与"参数不合法"分开，是因为它是权限问题：
#: 调用本身没写错，是这个人格没被授权做这么重的动作，处理方式也不同（找审批，不是改参数）。
REJECT_ACTION_LEVEL = "rejected_action_level"
#: 门禁自身出错。单列一类是为了运维能分开看：这类的量上升说明门禁坏了，
#: 而不是调用方在乱调。它和其它拒因混在一起统计会互相淹没。
REJECT_GATE_ERROR = "rejected_gate_error"

#: 判定不了。与 True/False 并列的第三种结果 —— 动作分级里可能是「规则要求某
#: 参数是数字、实际传了字符串」，扫描预检里是「注册表没声明 rows_per_day」。
#: 两处的处理方向一致：**判定不了一律当不通过**。当成"不匹配"会把级别降下来，
#: 当成"没限制"会让填不全的注册表静默变成没有上限。
UNDECIDABLE = object()

#: evaluate() 里 estimated_rows 的默认哨兵：区分"没传"（自己算）和"显式传 None"
#: （跳过检查）。用 None 当默认值正是这条规则以前从来不生效的原因。
_DERIVE = object()
PASSED = "passed"

#: 拦截来源。必须出现在给模型看的拒绝理由里——否则模型会自己编一个归因。
#: 实测依据见《评估与 Reward v0.1》§2.4：两次实验模型都把 harness 的拦截
#: 说成了远端服务的行为。
GATE_SOURCE = "BI 门禁（bi-gate 插件，在调用发出前拦截）"


@dataclass(frozen=True)
class Verdict:
    """一次判定的结果。`code` 为 PASSED 时表示放行。"""

    code: str
    #: 给模型看的话。放行时为 None。
    reason: Optional[str] = None
    #: 给审计表的结构化补充，不进模型上下文。
    detail: Optional[Mapping[str, Any]] = None

    @property
    def blocked(self) -> bool:
        return self.code != PASSED


def _deny(code: str, message: str, **detail: Any) -> Verdict:
    """构造拒绝，统一带上拦截来源。"""
    return Verdict(code=code, reason=f"{GATE_SOURCE}：{message}", detail=detail or None)


ALLOW = Verdict(code=PASSED)


# ---------------------------------------------------------------------------
# 指标注册表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricSpec:
    """受控事实层里一个指标的门禁相关部分。

    完整的指标元模型（口径描述、责任人、新鲜度）在指标层，不在这里；
    门禁只需要够做确定性校验的那几项。
    """

    name: str
    #: 允许的维度名。请求里出现表外维度即拒——避免模型臆造维度。
    dimensions: frozenset[str]
    #: 是否必须带时间窗。绝大多数指标都要，留开关是因为存量类指标（如"当前持仓"）没有时间窗。
    requires_time_window: bool = True
    #: 单次查询允许的最大扫描行数；None 表示不限（仅用于已知极小的维表）。
    max_scan_rows: Optional[int] = None
    #: 该指标底表每天大约多少行。**由事实层责任人声明**，不是模型能填的东西——
    #: 让模型报预估等于把强制交给被约束方，和「配置里的清单不是强制」是同一类错误。
    #: 声明了 max_scan_rows 却没声明这个，会让扫描预检判不了（见 estimate_scan_rows）。
    rows_per_day: Optional[int] = None


class MetricRegistry:
    """指标注册表。首批只装 10–12 个核心指标，范围可缩、准出标准不降。"""

    def __init__(self, specs: Sequence[MetricSpec]) -> None:
        self._by_name = {s.name: s for s in specs}

    def get(self, name: str) -> Optional[MetricSpec]:
        return self._by_name.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)


# ---------------------------------------------------------------------------
# 单条规则
# ---------------------------------------------------------------------------

def check_metric_registered(metric: Any, registry: MetricRegistry) -> Verdict:
    """指标必须在注册表里。

    这条同时挡住两件事：模型臆造指标名，以及有人绕过指标层直接点名底表。
    """
    if not isinstance(metric, str) or not metric.strip():
        return _deny(REJECT_UNKNOWN_METRIC, "缺少 metric 参数。")
    spec = registry.get(metric)
    if spec is None:
        return _deny(
            REJECT_UNKNOWN_METRIC,
            f"指标 {metric!r} 不在受控事实层。当前可用：{'、'.join(registry.names) or '（注册表为空）'}。"
            "如果这个口径确实需要，走指标层登记流程，不要用 run_sql 绕过。",
            metric=metric,
        )
    return ALLOW


def check_dimensions(dimensions: Any, spec: MetricSpec) -> Verdict:
    """维度必须是该指标声明过的。"""
    if dimensions is None:
        return ALLOW
    if isinstance(dimensions, str):
        dimensions = [dimensions]
    if not isinstance(dimensions, (list, tuple)):
        return _deny(REJECT_BAD_PARAM, "dimensions 必须是字符串数组。", got=type(dimensions).__name__)
    unknown = [d for d in dimensions if d not in spec.dimensions]
    if unknown:
        return _deny(
            REJECT_BAD_PARAM,
            f"指标 {spec.name} 不支持维度 {'、'.join(map(str, unknown))}。"
            f"它支持的是：{'、'.join(sorted(spec.dimensions)) or '（无维度）'}。",
            metric=spec.name,
            unknown_dimensions=list(unknown),
        )
    return ALLOW


#: 只接受显式时间窗。相对表述（"最近"、"上个月"）必须在调用前解析成绝对区间，
#: 否则同一个问题在不同时刻问会得到不同的数，评估集就没法回归。
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")


def check_time_window(time_window: Any, spec: MetricSpec) -> Verdict:
    """时间窗必填且必须是绝对区间。"""
    if not spec.requires_time_window:
        return ALLOW
    if not isinstance(time_window, Mapping):
        return _deny(
            REJECT_NO_TIME_WINDOW,
            f"指标 {spec.name} 必须带时间窗，形如 "
            '{"start": "2026-08-01", "end": "2026-08-21"}。无界查询一律不放行。',
            metric=spec.name,
        )
    start, end = time_window.get("start"), time_window.get("end")
    for label, value in (("start", start), ("end", end)):
        if not isinstance(value, str) or not _DATE.match(value):
            return _deny(
                REJECT_NO_TIME_WINDOW,
                f"时间窗的 {label} 必须是绝对时间（YYYY-MM-DD 或 YYYY-MM-DD HH:MM），"
                f"当前是 {value!r}。相对表述请先解析成具体日期再调用。",
                metric=spec.name,
                field=label,
            )
    if start > end:
        return _deny(
            REJECT_NO_TIME_WINDOW,
            f"时间窗起止颠倒：start={start} 晚于 end={end}。",
            metric=spec.name,
        )
    return ALLOW


def estimate_scan_rows(args: Mapping[str, Any], spec: MetricSpec) -> Any:
    """预估这次查询要扫多少行。

    只用两样东西：**注册表里的声明**（``rows_per_day``）和**这次调用自己的
    参数**（时间窗跨度）。都在门禁手里，所以不用连库、不用跨插件回调、
    也绝不采信模型自报的预估值——采信了就等于把强制交给被约束方。

    这是个上界估算，不是测量。真实扫描行数由执行层在查完之后记进审计
    （``bi-query`` 的 ``scanned_rows``），两边事后能对账：预估长期偏得离谱，
    说明 ``rows_per_day`` 该修了。

    维度个数不进公式：列存下多切一个维度是多读一列，不是多读一批行。

    返回预估行数；无法预估时返回 :data:`UNDECIDABLE`。
    """
    if spec.rows_per_day is None:
        return UNDECIDABLE
    days = 1
    window = args.get("time_window")
    if isinstance(window, Mapping):
        start, end = window.get("start"), window.get("end")
        if isinstance(start, str) and isinstance(end, str):
            try:
                d0 = date.fromisoformat(start[:10])
                d1 = date.fromisoformat(end[:10])
            except ValueError:
                return UNDECIDABLE
            days = max(1, (d1 - d0).days)
        elif spec.requires_time_window:
            # 要求时间窗却给不出来 —— 这一步不该由扫描预检来拒，
            # check_time_window 会先拦下；走到这里说明调用方跳过了那一步。
            return UNDECIDABLE
    elif spec.requires_time_window:
        return UNDECIDABLE
    return spec.rows_per_day * days


def check_scan_budget(estimated_rows: Any, spec: MetricSpec) -> Verdict:
    """扫描量预检。

    ``estimated_rows`` 由 :func:`estimate_scan_rows` 算出，或由调用方用
    EXPLAIN 之类的真实预估覆盖。``None`` 表示调用方明确不做这项检查。

    三条分支，每条的方向都是有意选的：

    * 指标没声明 ``max_scan_rows``：没人要求限额，放行。
    * 声明了限额但预估不出来（:data:`UNDECIDABLE`）：**拒绝**。判定不了当作
      不通过，和动作分级里 UNDECIDABLE 必须拒是同一条原则——否则「注册表填
      得不全」会静默变成「这个指标没有扫描上限」。
    * 预估超限：拒绝，并告诉模型怎么改（缩时间窗），因为这一条确实是改参数
      就能过的，和越权类拒绝不一样。
    """
    if spec.max_scan_rows is None or estimated_rows is None:
        return ALLOW
    if estimated_rows is UNDECIDABLE:
        return _deny(
            REJECT_SCAN,
            f"指标 {spec.name} 声明了扫描上限 {spec.max_scan_rows:,} 行，"
            f"但注册表里没有 rows_per_day，预估不出这次要扫多少 —— 判定不了按不通过处理。"
            "请事实层责任人补上该指标的 rows_per_day 声明。",
            metric=spec.name,
            limit=spec.max_scan_rows,
            reason="undecidable",
        )
    if estimated_rows > spec.max_scan_rows:
        return _deny(
            REJECT_SCAN,
            f"预估扫描 {estimated_rows:,} 行，超过指标 {spec.name} 的上限 "
            f"{spec.max_scan_rows:,} 行。请缩小时间窗后重试。",
            metric=spec.name,
            estimated_rows=estimated_rows,
            limit=spec.max_scan_rows,
        )
    return ALLOW


def check_session_scan_budget(
    session_scanned: int,
    this_call: Any,
    session_limit: Optional[int],
) -> Verdict:
    """会话累计扫描量预检。

    为什么需要它：单次限额约束的是单次调用，不是一次会话总共拉走多少。
    2026-08-27 实测——半年的查询被单次限额拦下后，模型把它拆成 6 个月度查询
    逐月执行再自己求和：每一次都合规，7 次调用累计扫了 2.412 亿行，比那次被
    拦的 2.172 亿还多。模型不是在对抗，它在完成任务；规则守住了自己的字面，
    意图从旁边流走了。

    这条和单次限额是两个不同的问题，所以拒因也分开（见 REJECT_SESSION_SCAN）。

    ``session_limit`` 为 ``None`` 表示该人格没有声明会话预算，本项不检查——
    这一层是加在单次限额之上的额外约束，缺省值取 0 会让任何查询都做不了，
    与 ``action_max`` 未声明按 L0（最严但仍可用）的取舍不同。**没声明这件事
    本身要在部署自检里显式报出来**，不能变成静默缺失。

    ``this_call`` 判定不了（:data:`UNDECIDABLE`）时按不通过处理，与单次限额一致。
    """
    if session_limit is None:
        return ALLOW
    if this_call is UNDECIDABLE:
        return _deny(
            REJECT_SESSION_SCAN,
            "预估不出这次要扫多少行，无法计入会话累计预算 —— 判定不了按不通过处理。",
            session_scanned=session_scanned,
            session_limit=session_limit,
            reason="undecidable",
        )
    projected = session_scanned + (this_call or 0)
    if projected > session_limit:
        return _deny(
            REJECT_SESSION_SCAN,
            f"本次会话累计扫描将达 {projected:,} 行，超过该人格的会话上限 "
            f"{session_limit:,} 行（本次会话此前已扫 {session_scanned:,} 行）。"
            "缩小这一次的时间窗也解决不了 —— 累计额度是按会话算的。"
            "确需继续，请走授权变更流程调整会话扫描预算。",
            session_scanned=session_scanned,
            this_call=this_call,
            projected=projected,
            session_limit=session_limit,
        )
    return ALLOW


# ---------------------------------------------------------------------------
# 动作分级（action_max 的 L0–L3）
# ---------------------------------------------------------------------------
#
# 这一层回答的问题和前面几条不同。前面几条问"这个调用合不合法"，这一层问
# "这个人格被授权做到多重的动作"。同一个 query_metric，查汇总和导明细的风险
# 完全不是一回事，但工具名是同一个 —— 只按工具名授权在这里就不够用了。
#
# 分档标准由业务方与合规定，技术侧只提供形状：**输入是工具名 + 参数，输出是
# 一个级别**，再和人格声明的 action_max 比。所以规则写在配置里（policy JSON），
# 不写在代码里 —— 改分档不该要发版。
#
# 条件语言刻意做得很小。表达力再往上加一点就会变成"没有测试的代码"，而这份
# 配置是合规要审的东西，看不懂就等于没审。

#: 级别名与序号。只增不改 —— 审计表、告警、拒绝理由都引用它。
LEVEL_NAMES = ("L0", "L1", "L2", "L3")

#: 条件语言支持的全部算子。载入时会校验，出现表外算子直接判定策略不可用
#: （而不是跳过那条规则）—— 静默跳过一条规则等于悄悄放宽授权。
CONDITION_OPS = frozenset({
    "param_present",        # [名字...]：这些参数出现即匹配
    "param_absent",         # [名字...]：这些参数不出现即匹配
    "param_equals",         # {名字: 值}
    "param_in",             # {名字: [值...]}
    "param_gte",            # {名字: 数}：数值型，>= 即匹配
    "dimensions_count_gte", # 数：维度个数 >= N
})


def parse_level(value: Any) -> Optional[int]:
    """把 "L2" / 2 解析成序号；无法解析返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value < len(LEVEL_NAMES):
        return value
    if isinstance(value, str):
        v = value.strip().upper()
        if v in LEVEL_NAMES:
            return LEVEL_NAMES.index(v)
    return None


def level_name(level: int) -> str:
    return LEVEL_NAMES[level] if 0 <= level < len(LEVEL_NAMES) else f"L?({level})"


@dataclass(frozen=True)
class ActionRule:
    """一条分级规则：参数满足 ``when`` 时，这次调用至少是 ``level``。"""

    level: int
    when: Mapping[str, Any]
    #: 给人看的说明，会进审计和拒绝理由。写清楚"为什么这算这一级"。
    label: str = ""


def _match_one(op: str, spec: Any, args: Mapping[str, Any]) -> Any:
    """单个算子。返回 True / False / :data:`UNDECIDABLE`。"""
    if op == "param_present":
        return all(name in args and args[name] is not None for name in spec)
    if op == "param_absent":
        return all(name not in args or args[name] is None for name in spec)
    if op == "param_equals":
        return all(args.get(name) == want for name, want in spec.items())
    if op == "param_in":
        return all(args.get(name) in tuple(want) for name, want in spec.items())
    if op == "param_gte":
        for name, want in spec.items():
            got = args.get(name)
            if got is None:
                return False
            if isinstance(got, bool) or not isinstance(got, (int, float)):
                return UNDECIDABLE  # 该是数字却不是 —— 判定不了，不能当成不匹配
            if got < want:
                return False
        return True
    if op == "dimensions_count_gte":
        dims = args.get("dimensions")
        if dims is None:
            return 0 >= spec
        if isinstance(dims, str):
            dims = [dims]
        if not isinstance(dims, (list, tuple)):
            return UNDECIDABLE
        return len(dims) >= spec
    return UNDECIDABLE  # 表外算子。载入时本该拦住，走到这里说明校验漏了。


def _match(when: Mapping[str, Any], args: Mapping[str, Any]) -> Any:
    """一条规则的全部条件（AND）。任一条判定不了，整条就判定不了。"""
    undecidable = False
    for op, spec in when.items():
        got = _match_one(op, spec, args)
        if got is UNDECIDABLE:
            undecidable = True
        elif not got:
            return False          # 明确不匹配，优先于"判定不了"
    return UNDECIDABLE if undecidable else True


@dataclass(frozen=True)
class ActionPolicy:
    """一份动作分级策略。

    :param rules: 分级规则。命中多条时取**最高**的级别 —— 就高不就低。
    :param default_level: 一条都没命中时的级别。
    :param human_review_from: 达到该级别一律走人工审批，无论 action_max 是多少。
        对齐系统 B 方案 §7.2「L3（不可逆或涉资金）一律人审」。``None`` 表示不启用。
        注意本层只负责**拦下并说明**，真正转给谁属于 Profile 的 fallback 字段。
    :param unavailable: 策略载入失败。为真时一切调用都拒 —— 与注册表载入失败
        同样的 fail-closed 方向：授权配置坏掉时应该停摆，不是放行。
    """

    rules: tuple = ()
    default_level: int = len(LEVEL_NAMES) - 1
    human_review_from: Optional[int] = None
    unavailable: bool = False
    #: 策略版本，只为审计留痕，回答"这次判定用的是哪一版分档"。
    version: str = ""


def classify_action(args: Mapping[str, Any], policy: ActionPolicy) -> Any:
    """把一次调用归到一个级别。

    :returns: ``(level, 命中说明)``；判定不了时返回 ``(None, 说明)``。
    """
    if policy.unavailable:
        return None, "动作分级策略载入失败"

    best: Optional[int] = None
    best_label = ""
    for rule in policy.rules:
        got = _match(rule.when, args)
        if got is UNDECIDABLE:
            return None, f"规则 {rule.label or rule.when!r} 判定不了（参数类型与规则不符）"
        if got and (best is None or rule.level > best):
            best, best_label = rule.level, rule.label or repr(dict(rule.when))

    if best is None:
        return policy.default_level, "未命中任何规则，按默认级别"
    return best, best_label


def check_action_level(
    args: Mapping[str, Any],
    policy: ActionPolicy,
    action_max: Optional[int],
) -> Verdict:
    """动作级别不得超过该人格声明的 action_max。

    :param action_max: 人格声明的上限。``None`` 表示没声明 —— 按 L0 处理（最严），
        不是"不限制"。没声明就当没授权，是这一层唯一安全的默认值。
    """
    level, why = classify_action(args, policy)
    if level is None:
        return _deny(
            REJECT_ACTION_LEVEL,
            f"无法判定这次调用的动作级别（{why}），按拦截处理。"
            "这不是你的调用有问题，是授权策略配置有问题 —— 请联系值班。",
            reason_detail=why,
            policy_version=policy.version,
        )

    if policy.human_review_from is not None and level >= policy.human_review_from:
        return _deny(
            REJECT_ACTION_LEVEL,
            f"这次调用被判定为 {level_name(level)}（{why}），"
            f"{level_name(policy.human_review_from)} 及以上一律需要人工审批，不能由 agent 直接执行。",
            action_level=level_name(level),
            action_max=level_name(action_max) if action_max is not None else None,
            needs_human_review=True,
            policy_version=policy.version,
        )

    effective_max = 0 if action_max is None else action_max
    if level > effective_max:
        hint = "该人格未声明 action_max，按 L0 处理" if action_max is None else ""
        return _deny(
            REJECT_ACTION_LEVEL,
            f"这次调用被判定为 {level_name(level)}（{why}），"
            f"超出该人格的动作上限 {level_name(effective_max)}"
            f"{'（' + hint + '）' if hint else ''}。"
            "改参数没用 —— 要么换一个更轻的问法，要么走授权变更流程提升 action_max。",
            action_level=level_name(level),
            action_max=level_name(effective_max),
            declared_action_max=level_name(action_max) if action_max is not None else None,
            policy_version=policy.version,
        )
    return ALLOW


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------

def evaluate(
    args: Mapping[str, Any],
    registry: MetricRegistry,
    estimated_rows: Any = _DERIVE,
    policy: Optional[ActionPolicy] = None,
    action_max: Optional[int] = None,
) -> Verdict:
    """跑完整条门禁，返回第一条不通过的判定。

    顺序是有意的：

    1. 先确认指标存在 —— 后面几条都依赖 spec，而且"指标不存在"是最可操作的报错；
    2. 再判动作级别 —— 这是**权限**问题，比参数格式问题更该优先告诉调用方，
       因为处理方式不同：参数错了改参数，越权了改参数没用，得走审批；
    3. 然后校验参数；
    4. 最后才是代价最高的扫描量预检。

    :param args: query_metric 的调用参数。
    :param registry: 当前生效的指标注册表。
    :param estimated_rows: 扫描行数预估。默认由 :func:`estimate_scan_rows` 从
        注册表声明与调用参数算出；调用方有更准的数（例如真做了一次 EXPLAIN）
        可以直接传进来覆盖；显式传 ``None`` 表示跳过这项检查。

        默认值刻意不是 ``None``：原来钩子里写死 ``estimated_rows=None``，
        导致这条规则在 rules.py 里有、有测试、而生产路径上永远不生效。
        改成默认自己算，漏传的后果就变成"照常检查"而不是"静默跳过"。
    :param policy: 动作分级策略。``None`` 表示这一层未启用（不判级别，直接跳过）。
    :param action_max: 该人格声明的动作上限序号。``None`` 且 policy 启用时按 L0 处理。
    :returns: 放行为 ``ALLOW``，否则是带拒因与理由的 :class:`Verdict`。
    """
    metric = args.get("metric")
    verdict = check_metric_registered(metric, registry)
    if verdict.blocked:
        return verdict

    spec = registry.get(metric)
    assert spec is not None  # check_metric_registered 已经保证

    if policy is not None:
        verdict = check_action_level(args, policy, action_max)
        if verdict.blocked:
            return verdict

    for verdict in (
        check_dimensions(args.get("dimensions"), spec),
        check_time_window(args.get("time_window"), spec),
        check_scan_budget(
            estimate_scan_rows(args, spec) if estimated_rows is _DERIVE else estimated_rows,
            spec,
        ),
    ):
        if verdict.blocked:
            return verdict
    return ALLOW
