"""身份透传 —— 「以谁的名义查」这件事的守卫测试。

这个文件里的用例大多带 ``@pytest.mark.no_bi_identity``，退出 conftest 里那个
自动绑身份的夹具。原因很直接：那个夹具让另外 270 多个测试能继续测它们本来要
测的东西，但如果没有这里这几条，把身份检查整个删掉，那 270 多个测试照样全绿。

**守的是三件事**：

1. 没有身份 → 拒绝。不是警告，不是记一笔然后放行。
2. 环境变量里塞一个身份 → 不算数。Hermes 的 ``get_session_env`` 在 ContextVar
   没绑定时会回落 ``os.environ``，那条回落对 CLI 兼容是合理的，对身份判定不是。
3. 判定不了（上游结构变了、映射表载不进来）→ 拒绝。查不了 ≠ 通过。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ident():
    return _load("bi_gate_identity_under_test", PLUGIN_DIR / "identity.py")


@pytest.fixture()
def gate(monkeypatch):
    monkeypatch.setenv("BI_GATE_TOOLS", "query_metric")
    monkeypatch.setenv("BI_GATE_REGISTRY", str(PLUGIN_DIR / "registry.example.json"))
    mod = _load("bi_gate_identity_gate", PLUGIN_DIR / "__init__.py")
    mod.reload_registry()
    return mod


def _map(tmp_path, entries: dict) -> str:
    p = tmp_path / "principals.json"
    p.write_text(json.dumps({"principals": entries}, ensure_ascii=False), encoding="utf-8")
    return str(p)


WINDOW = {"start": "2026-08-01", "end": "2026-08-05", "timezone": "UTC+8"}


def _query(**over):
    args = {"metric": "daily_active_users", "time_window": dict(WINDOW)}
    args.update(over)
    return args


# ---------------------------------------------------------------------------
# 一、没有身份 = 拒绝
# ---------------------------------------------------------------------------

@pytest.mark.no_bi_identity
def test_no_session_identity_is_rejected(gate, monkeypatch, tmp_path):
    """没绑会话 → 查询被拒，而且拒因说得出是身份问题。

    这是接真实数据的前提。桩数据时期无所谓，接上 StarRocks 之后，一条不知道
    是谁发起的查询就是用共享账号查全量。
    """
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP", _map(tmp_path, {"ou_x": {"subject": "s"}}))
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))

    v = gate._on_pre_tool_call(tool_name="query_metric", args=_query())
    assert v is not None and v.get("action") == "block", "无身份的查询被放行了"

    # **不能只断言 block。** 门禁自身崩溃时兜底也返回 block —— 第一版就是这样：
    # 代码里 Verdict(blocked=True) 抛 TypeError，被兜底转成 rejected_gate_error，
    # 这条测试照样绿。真跑模型才看出来，模型收到的是「门禁故障，请联系值班」。
    # 所以要断到拒因上：确实是因为身份被拒，而不是因为门禁炸了。
    records = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert records, "没写审计"
    code = records[-1]["gate_result"]
    assert code != "rejected_gate_error", (
        f"门禁是崩了不是拒了 —— 拒因应该说清是身份问题。detail={records[-1].get('detail')!r}"
    )
    assert code in ("rejected_no_identity", "rejected_origin_not_allowed",
                    "rejected_unknown_principal", "rejected_identity_undecidable"), code
    assert "身份" in v["message"] or "谁" in v["message"], v["message"]


@pytest.mark.no_bi_identity
def test_env_var_cannot_forge_an_identity(ident, monkeypatch, tmp_path):
    """往环境变量里塞身份不算数。

    实测过（2026-08-28，dev 机）：``get_session_env`` 在 ContextVar 未绑定时
    回落 ``os.environ``，``HERMES_SESSION_USER_ID=我随便写的`` 直接被读到。
    identity.py 因此直接读 ContextVar，这条守住那个决定不被"顺手简化"掉。
    """
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "我随便写的")
    monkeypatch.setenv("HERMES_SESSION_USER_NAME", "冒充的人")
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP",
                       _map(tmp_path, {"我随便写的": {"subject": "不该拿到的主体"}}))
    ident.reload_principal_map()

    sess = ident.read_session_identity()
    # 断言的是「环境变量的值没有变成身份」，不是「值恰好是 None」。
    # 两种状态都算没有身份，效果一样：
    #   None  —— ContextVar 从没被设过（干净进程）
    #   ""    —— 被 clear_session_vars 显式清成空串（前面跑过别的测试之后）
    # 第一版写的是 `is None`，单独跑绿、跑全量红 —— 因为同进程里先跑过绑过会话
    # 的测试。断言绑到了实现细节上，而不是绑到那条性质上。
    assert not sess["user_id"], (
        f"环境变量被当成了身份：{sess['user_id']!r} —— 任何能设环境变量的东西"
        f"都能冒充任何人"
    )
    assert sess["user_id"] != "我随便写的"
    assert sess["user_name"] != "冒充的人"
    verdict = ident.resolve_principal()
    assert not verdict.ok
    assert verdict.code in (ident.REJECT_NO_IDENTITY, ident.REJECT_ORIGIN_NOT_ALLOWED)


@pytest.mark.no_bi_identity
def test_unregistered_person_is_rejected(ident, monkeypatch, tmp_path):
    """人在，但没登记进主体映射 → 拒绝，且说清楚要业务方先登记。"""
    from gateway.session_context import set_session_vars, clear_session_vars
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP", _map(tmp_path, {"ou_known": {"subject": "s"}}))
    ident.reload_principal_map()
    tokens = set_session_vars(platform="feishu", user_id="ou_不认识的人")
    try:
        v = ident.resolve_principal()
    finally:
        clear_session_vars(tokens)
    assert not v.ok and v.code == ident.REJECT_UNKNOWN_PRINCIPAL
    assert "登记" in v.reason


# ---------------------------------------------------------------------------
# 二、判定不了 = 拒绝（不是放行）
# ---------------------------------------------------------------------------

@pytest.mark.no_bi_identity
def test_missing_principal_map_is_undecidable_not_allowed(ident, monkeypatch):
    """映射表没配 → 拒绝。「没有映射所以不限制」是最容易写出来的那种放宽。"""
    from gateway.session_context import set_session_vars, clear_session_vars
    monkeypatch.delenv("BI_GATE_PRINCIPAL_MAP", raising=False)
    ident.reload_principal_map()
    tokens = set_session_vars(platform="feishu", user_id="ou_someone")
    try:
        v = ident.resolve_principal()
    finally:
        clear_session_vars(tokens)
    assert not v.ok and v.code == ident.REJECT_IDENTITY_UNDECIDABLE


@pytest.mark.no_bi_identity
def test_broken_principal_map_is_undecidable(ident, monkeypatch, tmp_path):
    """映射表格式坏了 → 拒绝，不是"当成空表"然后继续。"""
    from gateway.session_context import set_session_vars, clear_session_vars
    bad = tmp_path / "bad.json"
    bad.write_text("{这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP", str(bad))
    ident.reload_principal_map()
    tokens = set_session_vars(platform="feishu", user_id="ou_someone")
    try:
        v = ident.resolve_principal()
    finally:
        clear_session_vars(tokens)
    assert not v.ok and v.code == ident.REJECT_IDENTITY_UNDECIDABLE


@pytest.mark.no_bi_identity
def test_upstream_rename_is_undecidable_not_a_pass(ident, monkeypatch):
    """上游把 ``_VAR_MAP`` 改名了 → 判定不了 → 拒绝。

    identity.py 有意读私有名。这条守的是「读不到的后果是拒绝，不是放行」——
    也就是那个决定的代价被兜住了。
    """
    from gateway import session_context as sc
    monkeypatch.delattr(sc, "_VAR_MAP", raising=False)
    sess = ident.read_session_identity()
    assert sess["readable"] is False
    v = ident.resolve_principal()
    assert not v.ok and v.code == ident.REJECT_IDENTITY_UNDECIDABLE


def test_private_names_still_exist_upstream():
    """``_VAR_MAP`` / ``_UNSET`` 还在不在。

    在的话什么都不用做；不在了这条会红 —— 那时门禁会开始一律拒绝（fail-closed，
    方向是对的），但业务全停。红在这里比红在生产上好。
    """
    from gateway import session_context as sc
    assert hasattr(sc, "_VAR_MAP"), "上游改了 _VAR_MAP —— 门禁将一律拒绝，需要跟进"
    assert hasattr(sc, "_UNSET"), "上游改了 _UNSET —— 同上"
    for key in ("HERMES_SESSION_USER_ID", "HERMES_SESSION_USER_ID_ALT",
                "HERMES_SESSION_USER_NAME", "HERMES_SESSION_PLATFORM",
                "HERMES_SESSION_SOURCE", "HERMES_CRON_SESSION"):
        assert key in sc._VAR_MAP, f"上游把 {key} 从 _VAR_MAP 里去掉了"


# ---------------------------------------------------------------------------
# 三、发起来源
# ---------------------------------------------------------------------------

@pytest.mark.no_bi_identity
@pytest.mark.parametrize("sess,expect", [
    ({"cron": "1", "user_id": "ou_x", "source": ""}, "cron"),
    ({"cron": "", "user_id": "ou_x", "source": ""}, "human"),
    ({"cron": "", "user_id": "", "source": "cli"}, "cli"),
    ({"cron": "", "user_id": "", "source": ""}, "unknown"),
])
def test_origin_classification(ident, sess, expect):
    """cron 排在 human 前面：定时任务也可能带 platform/chat_id（它要把结果发回群），
    光看 user_id 会把它误判成人发起的。"""
    assert ident.classify_origin(sess) == expect


@pytest.mark.no_bi_identity
def test_cron_is_rejected_until_someone_decides(ident, monkeypatch, tmp_path):
    """定时任务默认被拒 —— 它以谁的身份查数据还没定（待拍板 §十一第 6 条）。"""
    from gateway.session_context import set_session_vars, clear_session_vars
    monkeypatch.delenv("BI_GATE_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP", _map(tmp_path, {"ou_x": {"subject": "s"}}))
    ident.reload_principal_map()
    tokens = set_session_vars(platform="feishu", user_id="ou_x", cron_session="nightly")
    try:
        v = ident.resolve_principal()
    finally:
        clear_session_vars(tokens)
    assert not v.ok and v.code == ident.REJECT_ORIGIN_NOT_ALLOWED


@pytest.mark.no_bi_identity
def test_unset_allowed_origins_means_human_only(ident, monkeypatch):
    """漏声明 = 只允许人发起，不是"不限制"。

    和工具白名单未声明 = 空、action_max 未声明 = L0 同一条原则。
    """
    monkeypatch.delenv("BI_GATE_ALLOWED_ORIGINS", raising=False)
    assert ident._allowed_origins() == frozenset({"human"})


# ---------------------------------------------------------------------------
# 四、放行时把主体带下去
# ---------------------------------------------------------------------------

def test_passing_call_carries_the_principal_downstream(gate, tmp_path, monkeypatch):
    """门禁放行时必须把主体塞进工具参数。

    不塞的话 bi-query 不知道以谁的名义去连数据层 —— 而它才是真正要连的那一侧。
    conftest 的夹具已经绑了合法身份，所以这条走的是通过路径。
    """
    v = gate._on_pre_tool_call(tool_name="query_metric", args=_query())
    assert v is not None and v.get("action") == "modify", f"没放行：{v!r}"
    injected = v["args"]
    assert injected.get(gate.PRINCIPAL_ARG), "放行了但没把主体带下去"
    assert injected[gate.PRINCIPAL_ARG]["subject"], "主体没有 subject"


def test_principal_lands_in_the_audit(gate, tmp_path, monkeypatch):
    """审计里要看得见「这次是以谁的名义查的」。"""
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    gate._on_pre_tool_call(tool_name="query_metric", args=_query())
    records = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert records, "没写审计"
    assert records[-1].get("principal"), f"审计里没有主体：{records[-1]!r}"


@pytest.mark.no_bi_identity
def test_rejected_record_says_principal_is_null_not_absent(gate, tmp_path, monkeypatch):
    """没身份被拒的记录里，``principal`` 要写成 null 而不是这个键不存在。

    不写这个键的话，「这次调用没有身份」和「这条记录是加身份功能之前写的」
    在审计里长得一模一样，事后分不开。
    """
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP", _map(tmp_path, {"ou_x": {"subject": "s"}}))
    gate._on_pre_tool_call(tool_name="query_metric", args=_query())
    records = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert "principal" in records[-1], "拒绝记录里连 principal 这个键都没有"
    assert records[-1]["principal"] is None


# ---------------------------------------------------------------------------
# 五、bi-query 那一侧：没主体不执行
# ---------------------------------------------------------------------------

def test_bi_query_refuses_without_a_principal(tmp_path, monkeypatch):
    """bi-query 拿不到主体就不许执行，而且要留痕。

    和 call_id 的处理**故意不同**：call_id 缺了照样执行、记成 None，因为它是
    用来发现"绕过门禁"的探针。主体缺了必须拒 —— 这一侧是真要去连数据层的。
    拒了同样写审计（principal=null），所以绕过依旧看得见。
    """
    q = _load("bi_query_identity_under_test", REPO / "plugins" / "bi-query" / "__init__.py")
    q.reload_fixtures()
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("BI_AUDIT_LOG", str(target))

    out = json.loads(q.handle_query_metric(
        {"metric": "daily_active_users", "time_window": dict(WINDOW)}))
    assert "error" in out, f"没主体却执行了：{out!r}"
    assert "身份" in out["error"]

    records = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert records[-1]["event"] == "rejected_by_tool"
    assert records[-1]["reason"] == "principal_missing"
    assert records[-1]["principal"] is None


def test_two_plugins_agree_on_the_principal_arg_name():
    """两个插件里的主体参数名必须一致。

    故意各写一份（两边能分别部署），所以靠这条守住相等 —— 同 GATE_CALL_ID_ARG。
    不一致的后果最难查：门禁塞了、bi-query 读不到，于是每一次查询都被拒，
    而两边的代码单看都对。
    """
    ident = _load("bi_gate_identity_names", PLUGIN_DIR / "identity.py")
    q = _load("bi_query_identity_names", REPO / "plugins" / "bi-query" / "__init__.py")
    assert ident.PRINCIPAL_ARG == q.GATE_PRINCIPAL_ARG


@pytest.mark.no_bi_identity
def test_oneshot_cli_lands_in_unknown_not_cli(ident):
    """一次性 CLI（``hermes -z``）归到 unknown，不是 cli。

    实测得来，不是读代码得来：那种进程 ``session_context_engaged()`` 是 False，
    **所有**会话变量都空，包括 HERMES_SESSION_SOURCE。所以走不到 cli 那一档。

    钉住它是为了排查时不困惑 —— 看到 ``origin=unknown`` 不要以为是配漏了。
    """
    assert ident.classify_origin(
        {"cron": None, "user_id": None, "source": None}) == "unknown"
    assert ident.classify_origin(
        {"cron": "", "user_id": "", "source": ""}) == "unknown"
    # cli 这一档只在真有东西绑了 source 时才出现（交互式 CLI / TUI / 桌面端）
    assert ident.classify_origin({"cron": "", "user_id": "", "source": "cli"}) == "cli"
