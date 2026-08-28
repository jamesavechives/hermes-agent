"""装配期静态检查的测试。

这一层回答「这份人格声明本身合不合法」。它判错的代价是**放行一个不该部署的
声明**——而那种错误不会在运行时报警，配置会跑得好好的，只是没人批准过、
或者悄悄什么都干不成。

所以测试的重点是三类：
1. **「查不了」不能被算成「通过」**（``ok=None`` 与 ``ok=True`` 严格分开）
2. **没有审批记录必须判失败**，不能判「查不了」
3. **自相矛盾的声明要被抓住**——每条单看都合法、合起来做不了事的那种
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate"


def _load():
    spec = importlib.util.spec_from_file_location(
        "bi_gate_assemble_under_test", PLUGIN_DIR / "assemble_check.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ac():
    return _load()


def _registry(tmp_path: Path, **over) -> Path:
    metric = {"name": "dau", "dimensions": ["market"], "requires_time_window": True,
              "max_scan_rows": 50_000_000, "rows_per_day": 1_000_000}
    metric.update(over)
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"metrics": [metric]}), encoding="utf-8")
    return p


def _policy(tmp_path: Path, **over) -> Path:
    doc = {"version": "t", "default_level": "L0", "rules": [
        {"level": "L2", "when": {"param_equals": {"export": True}}, "label": "导出"}]}
    doc.update(over)
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _profile(tmp_path: Path, env_lines: str, *, approvals: dict | None = None) -> Path:
    p = tmp_path / "prof"
    p.mkdir(exist_ok=True)
    (p / ".env").write_text(env_lines, encoding="utf-8")
    (p / "config.yaml").write_text(
        # bi-query 必须在 —— 只有 bi-gate 的话门禁开着、query_metric 没注册，
        # 那是块砖，不该被当成"合法样本"。2026-08-28 在 dev 上真实发生过。
        "plugins:\n  enabled: [bi-gate, bi-query]\n", encoding="utf-8")
    if approvals is not None:
        (p / "approvals.json").write_text(json.dumps(approvals), encoding="utf-8")
    return p


GOOD_APPROVALS = {
    "authorization": {"by": ["技术负责人", "合规"], "at": "2026-08-27", "ref": "PR#1"},
    "facts": {"by": ["事实层责任人"], "at": "2026-08-27", "ref": "PR#1"},
}


def _by_name(results, needle):
    return [r for r in results if needle in r.name]


# ── .env 解析 ───────────────────────────────────────────────────────────

def test_env_parsing_does_not_execute(ac, tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=$(touch /tmp/nope_assemble)\nB=${HOME}\n# c=1\n", encoding="utf-8")
    got = ac._parse_env(f)
    assert got["A"] == "$(touch /tmp/nope_assemble)"
    assert got["B"] == "${HOME}"
    assert "c" not in got
    assert not Path("/tmp/nope_assemble").exists()


# ── 「查不了」不是「通过」 ───────────────────────────────────────────────

def test_fields_without_a_home_are_unknown_not_pass(ac, tmp_path):
    """persona / skills / fallback 当前没有声明位置。

    这三块要报「查不了」（ok=None），不能报通过。区别很重要：报通过会让人
    以为这个 profile 的五块字段都齐了，而实际上是我们的声明格式还缺三块。
    """
    prof = _profile(tmp_path, "BI_GATE_REGISTRY=/x\nBI_GATE_TOOLS=query_metric\n")
    results = ac.check_fields(ac.load_declaration(prof))
    unknown = {r.name for r in results if r.ok is None}
    assert any("persona" in n for n in unknown)
    assert any("skills" in n for n in unknown)
    assert any("fallback" in n for n in unknown)
    assert all(r.ok is not True for r in results if "persona" in r.name)


def test_declared_field_missing_is_a_failure(ac, tmp_path):
    """有声明位置却没填 —— 这是配漏了，判失败。"""
    prof = _profile(tmp_path, "BI_GATE_TOOLS=query_metric\n")   # 少了 REGISTRY
    results = ac.check_fields(ac.load_declaration(prof))
    facts = _by_name(results, "facts")[0]
    assert facts.ok is False


def test_unknown_results_do_not_make_the_run_pass_silently(ac, tmp_path, capsys):
    """查不了的项要在结论里被单独点名，不能被吞掉。"""
    prof = _profile(tmp_path, "BI_GATE_REGISTRY=/x\nBI_GATE_TOOLS=t\n",
                    approvals=GOOD_APPROVALS)
    ac.run(prof, skip_runtime=True)
    out = capsys.readouterr().out
    assert "查不了" in out
    assert "查不了的东西不算安全" in out


# ── 审批 ────────────────────────────────────────────────────────────────

def test_missing_approvals_file_is_a_failure_not_unknown(ac, tmp_path):
    """没有审批记录必须判失败。

    一个没人批准过的声明被部署，和一个格式写错的声明，后果完全不同：
    后者跑不起来，前者跑得好好的。所以不能判「查不了」。
    """
    prof = _profile(tmp_path, "BI_GATE_TOOLS=query_metric\n")
    r = ac.check_approvals(ac.load_declaration(prof))[0]
    assert r.ok is False
    assert "无法确认" in r.detail


@pytest.mark.parametrize("missing", ["by", "at", "ref"])
def test_incomplete_signature_is_a_failure(ac, tmp_path, missing):
    """签字、日期、PR 引用缺一不可 —— 少了任何一项都追溯不到。"""
    ap = json.loads(json.dumps(GOOD_APPROVALS))
    ap["authorization"][missing] = ""
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n", approvals=ap)
    auth = _by_name(ac.check_approvals(ac.load_declaration(prof)), "authorization")[0]
    assert auth.ok is False
    assert missing in auth.detail


def test_both_segments_are_required(ac, tmp_path):
    """authorization 和 facts 都要 —— 只批了一块不算批过。"""
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n",
                    approvals={"authorization": GOOD_APPROVALS["authorization"]})
    results = ac.check_approvals(ac.load_declaration(prof))
    assert _by_name(results, "facts")[0].ok is False


def test_complete_approvals_pass(ac, tmp_path):
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n", approvals=GOOD_APPROVALS)
    assert all(r.ok is True for r in ac.check_approvals(ac.load_declaration(prof)))


# ── 自相矛盾 ────────────────────────────────────────────────────────────

def test_default_level_above_action_max_is_caught(ac, tmp_path):
    """默认级别高于上限 = 这个人格连最普通的调用都做不了。

    这种配置不会报错，只会让人格安静地什么都干不成，然后被当成「模型不行」。
    """
    prof = _profile(tmp_path, f"""BI_GATE_ACTION_MAX=L0
BI_GATE_ACTION_POLICY={_policy(tmp_path, default_level="L2")}
BI_GATE_REGISTRY={_registry(tmp_path)}
BI_GATE_TOOLS=query_metric
""", approvals=GOOD_APPROVALS)
    r = _by_name(ac.check_self_consistency(ac.load_declaration(prof)), "相容")[0]
    assert r.ok is False
    assert "连最普通的调用都做不了" in r.detail


def test_compatible_levels_pass(ac, tmp_path):
    prof = _profile(tmp_path, f"""BI_GATE_ACTION_MAX=L1
BI_GATE_ACTION_POLICY={_policy(tmp_path)}
BI_GATE_REGISTRY={_registry(tmp_path)}
BI_GATE_TOOLS=query_metric
""", approvals=GOOD_APPROVALS)
    r = _by_name(ac.check_self_consistency(ac.load_declaration(prof)), "相容")[0]
    assert r.ok is True


def test_human_review_at_or_below_default_is_caught(ac, tmp_path):
    """人审门槛不高于默认级别 = 每次调用都要人审，等于这个人格不能自动跑。"""
    prof = _profile(tmp_path, f"""BI_GATE_ACTION_MAX=L2
BI_GATE_ACTION_POLICY={_policy(tmp_path, default_level="L1", human_review_from="L1")}
BI_GATE_REGISTRY={_registry(tmp_path)}
BI_GATE_TOOLS=query_metric
""", approvals=GOOD_APPROVALS)
    r = _by_name(ac.check_self_consistency(ac.load_declaration(prof)), "human_review")[0]
    assert r.ok is False


def test_bad_action_max_value_is_caught(ac, tmp_path):
    prof = _profile(tmp_path, "BI_GATE_ACTION_MAX=L9\nBI_GATE_TOOLS=t\n",
                    approvals=GOOD_APPROVALS)
    r = _by_name(ac.check_self_consistency(ac.load_declaration(prof)), "action_max 取值")[0]
    assert r.ok is False


def test_unset_action_max_is_unknown_and_says_it_means_L0(ac, tmp_path):
    """未声明不是通过，而且要说清楚运行时按 L0 处理。"""
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n", approvals=GOOD_APPROVALS)
    r = _by_name(ac.check_self_consistency(ac.load_declaration(prof)), "action_max 取值")[0]
    assert r.ok is None
    assert "L0" in r.detail


def test_scan_limit_without_rows_per_day_is_caught(ac, tmp_path):
    """声明了扫描上限却没有 rows_per_day —— 该指标任何查询都会被拒。

    上线才发现，不如装配期就发现。
    """
    reg = _registry(tmp_path, rows_per_day=None)
    prof = _profile(tmp_path, f"""BI_GATE_ACTION_MAX=L1
BI_GATE_REGISTRY={reg}
BI_GATE_TOOLS=query_metric
""", approvals=GOOD_APPROVALS)
    r = _by_name(ac.check_self_consistency(ac.load_declaration(prof)), "扫描量声明")[0]
    assert r.ok is False
    assert "dau" in r.detail


# ── 策略解析 ────────────────────────────────────────────────────────────

def test_policy_uses_the_runtime_parser(ac, tmp_path, monkeypatch):
    """用运行时那份解析器，不另写一套。

    另写一套会漂移，而漂移的方向恰好最坏：装配期放行了运行时拒绝的东西。
    """
    prof = _profile(tmp_path, f"BI_GATE_ACTION_POLICY={_policy(tmp_path)}\n",
                    approvals=GOOD_APPROVALS)
    r = ac.check_policy_parses(ac.load_declaration(prof))[0]
    assert r.ok is True
    assert "1 条规则" in r.detail


def test_broken_policy_is_caught_before_deploy(ac, tmp_path, monkeypatch):
    """运行时会判整份策略不可用的，装配期就要拦下。"""
    bad = tmp_path / "bad_policy.json"
    bad.write_text(json.dumps({"rules": [{"level": "L9", "when": {"param_present": ["x"]}}]}),
                   encoding="utf-8")
    prof = _profile(tmp_path, f"BI_GATE_ACTION_POLICY={bad}\n", approvals=GOOD_APPROVALS)
    r = ac.check_policy_parses(ac.load_declaration(prof))[0]
    assert r.ok is False


def test_no_policy_is_unknown_not_pass(ac, tmp_path):
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n", approvals=GOOD_APPROVALS)
    assert ac.check_policy_parses(ac.load_declaration(prof))[0].ok is None


# ── 退出码 ──────────────────────────────────────────────────────────────

def test_any_failure_blocks_deploy(ac, tmp_path):
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n")   # 没有 approvals
    code, _ = ac.run(prof, skip_runtime=True)
    assert code == 1


def test_all_pass_allows_deploy(ac, tmp_path):
    prof = _profile(tmp_path, f"""BI_GATE_ACTION_MAX=L1
BI_GATE_ACTION_POLICY={_policy(tmp_path)}
BI_GATE_REGISTRY={_registry(tmp_path)}
BI_GATE_TOOLS=query_metric
""", approvals=GOOD_APPROVALS)
    code, _ = ac.run(prof, skip_runtime=True)
    assert code == 0


def test_missing_profile_dir_is_checker_error(ac, tmp_path):
    """profile 不存在返回 2，与「检查没过」的 1 分开 —— 前者是调用方搞错了。"""
    assert ac.main(["x", str(tmp_path / "不存在")]) == 2


def test_a_crashing_check_counts_as_failure_not_pass(ac, tmp_path, monkeypatch):
    """某项检查自己炸了要算失败。

    检查器出错时放行，等于把「我不知道」当成「没问题」——这套东西存在的
    全部理由就是不这么做。
    """
    def boom(_decl):
        raise RuntimeError("故意炸")
    monkeypatch.setattr(ac, "SECTIONS", [("① 会炸的检查", boom)])
    prof = _profile(tmp_path, "BI_GATE_TOOLS=t\n", approvals=GOOD_APPROVALS)
    code, results = ac.run(prof, skip_runtime=True)
    assert code == 1
    assert any(r.ok is False and "故意炸" in r.detail for r in results)


# ---------------------------------------------------------------------------
# ⑧ 声明了的工具真的调得到
# ---------------------------------------------------------------------------

def _profile_with(tmp_path, tools: str, config_body: str):
    prof = tmp_path / "p"
    prof.mkdir()
    (prof / ".env").write_text(f"BI_GATE_TOOLS={tools}\n", encoding="utf-8")
    (prof / "config.yaml").write_text(config_body, encoding="utf-8")
    return prof


def test_declared_tool_without_its_plugin_is_caught(ac, tmp_path):
    """声明 query_metric 但 config 里没有 bi-query —— 必须判不过。

    这条守的是 2026-08-28 在 dev 上真实发生过的那个 profile：门禁开着、
    工具没注册，模型跑起来一个工具都调不到，而当时所有检查都是绿的。
    """
    prof = _profile_with(tmp_path, "query_metric", "plugins:\n  enabled:\n    - bi-gate\n")
    results = ac.check_declared_tools_are_reachable(ac.load_declaration(prof))
    bad = [r for r in results if r.ok is False]
    assert bad, "没抓住 —— 这正是当时漏过去的那个 profile 的形状"
    assert any("bi-query" in r.detail for r in bad)


def test_both_plugins_present_passes(ac, tmp_path):
    prof = _profile_with(tmp_path, "query_metric",
                         "plugins:\n  enabled:\n    - bi-gate\n    - bi-query\n")
    results = ac.check_declared_tools_are_reachable(ac.load_declaration(prof))
    assert all(r.ok is not False for r in results), [str(r) for r in results]


def test_missing_gate_itself_is_caught(ac, tmp_path):
    """config 里没有 bi-gate —— 门禁完全不存在，且运行时不报任何错。"""
    prof = _profile_with(tmp_path, "query_metric",
                         "plugins:\n  enabled:\n    - bi-query\n")
    results = ac.check_declared_tools_are_reachable(ac.load_declaration(prof))
    assert any(r.ok is False and "bi-gate" in r.name for r in results)


def test_unknown_tool_is_undecidable_not_a_pass(ac, tmp_path):
    """不认识的工具名记成"查不了"（ok=None），不能记成通过。

    「查不了」是第三种结果 —— 假装查过是这套东西一路在堵的失效方式。
    """
    prof = _profile_with(tmp_path, "某个宿主内置工具",
                         "plugins:\n  enabled:\n    - bi-gate\n")
    results = ac.check_declared_tools_are_reachable(ac.load_declaration(prof))
    assert any(r.ok is None for r in results), [str(r) for r in results]
