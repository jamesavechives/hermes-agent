"""身份透传 —— 「谁在问」这件事，从会话上下文取到、判定、往下带。

为什么这是接真实数据之前必须先做的
----------------------------------
行列级权限本来就不在 agent 层强制（见插件头部说明）—— 谁能看哪些行、哪些列，
只能由数据层的账号与行级权限保证。那么把桩数据换成 StarRocks 的那一刻，问题
就变成：**这条查询以谁的身份到达数据层？**

如果答案是「一个共享服务账号」，那前面所有门禁都白做 —— 任何能让 agent 发起
查询的人，都拿到了那个账号的全部可见范围。这不是门禁能补的洞，因为门禁管的是
「查什么」，管不了「以谁的名义查」。所以顺序不能反：先身份，再真实数据。

这一层做什么、不做什么
----------------------
**做**：把「谁在问」从会话上下文取出来、判定它可不可信、拒绝不可信的调用、
把它带进工具参数、写进审计。

**不做**：证明这个人真的是他。这一层拿到的是**一个断言**，不是凭证。数据层
必须自己独立验证（短期令牌 / 每人独立库账号 / 网关侧鉴权），否则信任边界还是
停在 agent 进程上。**在数据层验证之前，这一层不是安全边界，是管道加 fail-closed
策略。** 这句话必须写在这里，否则「已经做了身份透传」会被当成「已经安全了」。

为什么不用 ``get_session_env``
------------------------------
Hermes 提供了 ``gateway.session_context.get_session_env()``，但它在 ContextVar
**没被设过**时会回落到 ``os.environ``。实测（2026-08-28，dev 机）：

    $ HERMES_SESSION_USER_ID=我随便写的 python probe.py
      HERMES_SESSION_USER_ID = '我随便写的'

也就是说任何能设环境变量的东西都能声称自己是任何人。那个回落对 CLI/cron 兼容
是合理的，对身份判定不是。所以这里**直接读 ContextVar**，并区分三种状态：

===============  ==========================================
ContextVar 状态   含义
===============  ==========================================
有值（非 _UNSET） 网关绑定过会话 —— 这是我们愿意采信的断言
``_UNSET``        本任务没有绑定会话 —— **没有身份**，
                  不管 ``os.environ`` 里写了什么
读不到 _VAR_MAP   上游结构变了 —— **判定不了，一律当不通过**
===============  ==========================================

第三行是这套东西一以贯之的那条：查不了 ≠ 通过。

用私有名 ``_VAR_MAP`` / ``_UNSET`` 是有意的：公开接口在这件事上不够严，而
「严」比「不碰私有」重要。上游改名的后果被兜住了 —— 读不到就拒绝，不是放行。
另有测试盯着这两个名字还在不在。

CLI 跑起来会怎样
----------------
实测（2026-08-28）：``hermes -z`` 这种一次性 CLI 进程
``session_context_engaged() == False``，**所有**会话变量都是空 —— 包括
``HERMES_SESSION_SOURCE``。所以它归到的是 ``unknown``，**不是** ``cli``。

``cli`` 这一档只在有东西真的绑了 ``HERMES_SESSION_SOURCE=cli/tui/desktop``
时才出现（交互式 CLI、TUI、桌面端会绑；一次性 ``-z`` 不会）。两者默认都被拒，
所以行为上没差别，但排查时看到 ``rejected_origin_not_allowed origin=unknown``
不要以为是哪里配漏了 —— 命令行本来就是这个样子。

（这段第一版写的是「CLI → cli」。真跑一次模型才看见审计里是 ``unknown``。
又是那个形状：按代码里存在的分支想当然，没去看真实进程里的值。）

前面所有的模型实测都是在无身份状态下跑的（用的桩数据，当时无所谓）。要在
命令行上验证真实数据，得显式给它一个「运维自查」主体，走和人一样的登记流程，
不能靠环境变量随手塞。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: 主体映射表。平台身份（如飞书 open_id）→ 数据层认识的主体。
#: 这张表由谁维护、怎么同步，是业务方/合规的事（§十一）。此刻先用文件 mock，
#: 但**映射不到就拒绝**，不 mock 成"放行"。
PRINCIPAL_MAP_ENV = "BI_GATE_PRINCIPAL_MAP"

#: 允许的发起来源。未声明 = 只允许人在聊天里发起。
#: cron / autonomous 这类「无人发起」的任务要不要有身份、有什么身份，
#: 是待拍板项（§十一第 6 条）—— 在定下来之前，它们被拒。
ALLOWED_ORIGINS_ENV = "BI_GATE_ALLOWED_ORIGINS"

#: 门禁放行时塞进工具参数的主体键。bi-query 读到才知道以谁的名义查。
PRINCIPAL_ARG = "_bi_principal"

# 判定码 —— 与 rules.py 的 REJECT_* 同一套命名。
REJECT_NO_IDENTITY = "rejected_no_identity"
REJECT_UNKNOWN_PRINCIPAL = "rejected_unknown_principal"
REJECT_ORIGIN_NOT_ALLOWED = "rejected_origin_not_allowed"
REJECT_IDENTITY_UNDECIDABLE = "rejected_identity_undecidable"


@dataclass(frozen=True)
class Principal:
    """数据层要认的那个主体。

    ``asserted_by`` 记的是「这个身份是谁说的」。现在只有一个来源（Hermes 会话
    上下文），但记下来是为了将来加了令牌交换之后，能一眼区分「断言」和「凭证」。
    """

    subject: str            #: 数据层认识的主体标识
    display: str            #: 人看的名字，只进审计不进查询
    platform_id: str        #: 平台侧原始身份（飞书 open_id 等）
    platform: str           #: 哪个平台
    origin: str             #: human / cron / cli / unknown
    asserted_by: str = "hermes-session-context"
    verified: bool = False  #: 数据层验证过了吗。现在恒为 False —— 见模块说明

    def to_audit(self) -> Dict[str, Any]:
        return {
            "subject": self.subject,
            "display": self.display,
            "platform": self.platform,
            "platform_id": self.platform_id,
            "origin": self.origin,
            "asserted_by": self.asserted_by,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class IdentityVerdict:
    """身份判定结果。``principal`` 为 None 时 ``code`` 说明为什么。"""

    principal: Optional[Principal]
    code: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.principal is not None


# ---------------------------------------------------------------------------
# 读会话上下文
# ---------------------------------------------------------------------------

def _read_bound(name: str) -> Tuple[Optional[str], bool]:
    """读一个会话变量。返回 ``(值, 是否读得了)``。

    值为 ``None`` 表示「本任务没绑定会话」——**不回落 os.environ**，理由见模块
    说明。``是否读得了 = False`` 表示上游结构变了，调用方必须当作判定不了。
    """
    try:
        from gateway import session_context as sc
        var_map = getattr(sc, "_VAR_MAP", None)
        unset = getattr(sc, "_UNSET", None)
        if var_map is None or unset is None:
            return None, False
        var = var_map.get(name)
        if var is None:
            return None, False
        value = var.get()
        if value is unset:
            return None, True     # 没绑定 —— 读得了，就是没有
        return ("" if value is None else str(value)), True
    except Exception as exc:      # noqa: BLE001
        logger.error("bi-gate: 读会话变量 %s 失败：%s", name, exc)
        return None, False


def read_session_identity() -> Dict[str, Any]:
    """把身份相关的会话变量读成一个字典，附带「读没读得了」。

    单独抽出来是为了能在不跑模型的情况下测，以及给排查用。
    """
    keys = {
        "user_id": "HERMES_SESSION_USER_ID",
        "user_id_alt": "HERMES_SESSION_USER_ID_ALT",
        "user_name": "HERMES_SESSION_USER_NAME",
        "platform": "HERMES_SESSION_PLATFORM",
        "source": "HERMES_SESSION_SOURCE",
        "cron": "HERMES_CRON_SESSION",
    }
    out: Dict[str, Any] = {"readable": True}
    for field, env_name in keys.items():
        value, readable = _read_bound(env_name)
        out[field] = value
        if not readable:
            out["readable"] = False
    return out


def classify_origin(sess: Dict[str, Any]) -> str:
    """这次调用是谁发起的：human / cron / cli / unknown。

    顺序有讲究：先看 cron —— 定时任务也可能带着一个平台和 chat_id（它要把结果
    发回某个群），光看 platform 会把它误判成人发起的。
    """
    if sess.get("cron"):
        return "cron"
    if sess.get("user_id"):
        return "human"
    if sess.get("source") in ("cli", "tui", "desktop"):
        return "cli"
    return "unknown"


# ---------------------------------------------------------------------------
# 主体映射
# ---------------------------------------------------------------------------

_principal_map: Optional[Dict[str, Dict[str, Any]]] = None
_principal_map_path: Optional[str] = None


def _load_principal_map() -> Optional[Dict[str, Dict[str, Any]]]:
    """载入平台身份 → 数据层主体的映射。

    返回 ``None`` 表示**载入不了**（没配、读不了、格式坏）。调用方必须把它当成
    判定不了而拒绝，不能当成"没有映射所以放行"。
    """
    path = os.environ.get(PRINCIPAL_MAP_ENV, "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.error("bi-gate: 载入主体映射 %s 失败（%s）—— 所有查询将被拒", path, exc)
        return None
    entries = raw.get("principals")
    if not isinstance(entries, dict):
        logger.error("bi-gate: 主体映射 %s 里没有 principals 字典 —— 所有查询将被拒", path)
        return None
    clean: Dict[str, Dict[str, Any]] = {}
    for key, item in entries.items():
        if not isinstance(item, dict) or not item.get("subject"):
            logger.error("bi-gate: 主体映射条目非法，已跳过：%r=%r", key, item)
            continue
        clean[str(key)] = item
    logger.info("bi-gate: 载入主体映射 %d 条（来自 %s）", len(clean), path)
    return clean


def principal_map_now() -> Optional[Dict[str, Dict[str, Any]]]:
    global _principal_map, _principal_map_path
    path = os.environ.get(PRINCIPAL_MAP_ENV, "").strip()
    if _principal_map is None or path != _principal_map_path:
        _principal_map = _load_principal_map()
        _principal_map_path = path
    return _principal_map


def reload_principal_map() -> Optional[Dict[str, Dict[str, Any]]]:
    global _principal_map
    _principal_map = None
    return principal_map_now()


def _allowed_origins() -> frozenset:
    """哪些发起来源允许查询。未声明 = 只允许 human。

    和工具白名单、action_max 同一条原则：漏声明的后果是做不了事，不是不受限。
    """
    raw = os.environ.get(ALLOWED_ORIGINS_ENV, "").strip()
    if not raw:
        return frozenset({"human"})
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def resolve_principal() -> IdentityVerdict:
    """解析当前调用的主体。这是本模块唯一的对外入口。

    任何一步不确定都返回拒绝 —— 包括「上游结构变了读不了」。
    """
    sess = read_session_identity()

    if not sess.get("readable"):
        return IdentityVerdict(
            None, REJECT_IDENTITY_UNDECIDABLE,
            "读不到会话身份变量（Hermes 的 session_context 结构可能变了）。"
            "判定不了一律当不通过 —— 查不了不等于通过。",
        )

    origin = classify_origin(sess)
    allowed = _allowed_origins()
    if origin not in allowed:
        return IdentityVerdict(
            None, REJECT_ORIGIN_NOT_ALLOWED,
            f"这次调用的发起来源是 {origin}，该人格只允许 {'、'.join(sorted(allowed))} 发起查询。"
            + ("定时/自动任务以谁的身份查数据还没定（待拍板），在定下来之前一律拒。"
               if origin == "cron" else
               "命令行没有会话身份，接上真实数据后这条路查不了 —— "
               "要自查请走登记过的运维主体。" if origin == "cli" else
               "认不出这次调用是谁发起的。最常见的原因是这次是从命令行"
               "（hermes -z 之类）发起的 —— 那种进程根本没有会话身份，"
               "不是哪里配漏了。要查数据请从聊天里发起，或用登记过的运维主体。")
            + "（身份来自会话，不是调用参数；补参数或向用户索要身份都没有用。）",
        )

    platform_id = sess.get("user_id") or ""
    if not platform_id:
        return IdentityVerdict(
            None, REJECT_NO_IDENTITY,
            "这个会话没有发起人身份，因此不能查数据（数据按人授权）。"
            # ⚠️ 下面这两句是必须的，删掉会退回一个真实发生过的失败：
            # 2026-08-28 第一版的措辞是「这次调用没有携带发起人身份」，模型把
            # 「携带」读成「该传个参数」，于是回头问用户「你的用户名是什么？
            # 或者你知道该用哪个参数名标识发起人吗？」——
            # 而「模型自己声称身份」正是这一层绝对不能发生的事。
            #
            # 拒因不只要说"为什么不行"，还要说"你做什么都不行"，否则模型会去找
            # 一条并不存在的出路，而它找的那条恰好是最危险的那条。
            "身份来自会话本身，不是调用参数 —— **你无法通过补参数解决这件事，"
            "也不要向用户索要身份信息再填进来**。"
            "正确的做法是告诉用户：请从聊天里发起这个问题（那里带得到身份）。",
        )

    pmap = principal_map_now()
    if pmap is None:
        return IdentityVerdict(
            None, REJECT_IDENTITY_UNDECIDABLE,
            f"主体映射表载入不了（{PRINCIPAL_MAP_ENV}）——"
            "无法把平台身份换成数据层认识的主体，判定不了一律当不通过。",
        )

    # 优先用 alt（飞书 union_id 等跨应用稳定 ID），它比 open_id 稳。
    entry = None
    matched_key = ""
    for key in (sess.get("user_id_alt") or "", platform_id):
        if key and key in pmap:
            entry, matched_key = pmap[key], key
            break
    if entry is None:
        return IdentityVerdict(
            None, REJECT_UNKNOWN_PRINCIPAL,
            f"发起人（{platform_id}）不在主体映射表里。"
            "需要先由业务方把这个人登记进来并确认数据范围，之后才能查。",
        )

    return IdentityVerdict(Principal(
        subject=str(entry["subject"]),
        display=str(entry.get("display") or sess.get("user_name") or ""),
        platform_id=matched_key,
        platform=str(sess.get("platform") or sess.get("source") or ""),
        origin=origin,
        verified=False,   # 数据层还没验 —— 见模块说明
    ))
