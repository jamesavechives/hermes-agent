"""五个声明文件 → 运行时 profile 的生成器，以及「不许手改」的强制点。

这个文件真正要守住的是一条：**设计方案 §3.3 说「生成物不允许手改」，
而在 manifest 校验做出来之前，那只是一句话。** 谁都能直接改 ``.env`` 往
``BI_GATE_TOOLS`` 里加一个工具，没有任何环节会发现 —— CODEOWNERS 管的是声明
文件，管不到运行时目录。

所以下面那条 :func:`test_hand_edited_env_is_caught_before_deploy` 是本文件的重点：
它模拟的正是「绕过审批」这个动作本身。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"
SOURCE_EXAMPLE = PLUGIN_DIR / "profile.source.example"

pytest.importorskip("yaml", reason="build_profile 是构建期工具，需要 pyyaml")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bp():
    return _load("bi_gate_build_profile", PLUGIN_DIR / "build_profile.py")


#: 声明目录里该有的文件。写死不遍历目录 —— 遍历会把 macOS 的 ``._*`` 扩展属性
#: 文件、编辑器备份之类也拷进去，然后在读取时炸掉。第一次跑就是这么红的。
SOURCE_FILES = ("persona.yaml", "facts.yaml", "authorization.yaml",
                "skills.yaml", "fallback.yaml", "approvals.json")


def _copy_source(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for name in SOURCE_FILES:
        src = SOURCE_EXAMPLE / name
        assert src.exists(), f"样例声明目录里缺 {name}"
        (dest / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _run_build(src: Path, runtime: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "build_profile.py"), str(src), str(runtime), *extra],
        capture_output=True, text=True, timeout=60)


def _run_check(runtime: Path):
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{env.get('PYTHONPATH','')}".rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "assemble_check.py"), str(runtime), "--skip-runtime"],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(REPO))


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def test_example_source_generates_a_deployable_profile(tmp_path):
    """样例声明必须能生成出一个通得过装配期检查的 profile。

    过不了的话，「照着这个建新人格」这句话就是错的 —— 而它同时也是 CI 的输入。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    proc = _run_build(src, runtime)
    assert proc.returncode == 0, proc.stderr

    for name in (".env", "SOUL.md", "config.yaml", "bi_registry.json",
                 "action_policy.json", ".generated.json", "approvals.json"):
        assert (runtime / name).exists(), f"没生成 {name}"

    check = _run_check(runtime)
    assert check.returncode == 0, f"生成物没通过装配期检查：\n{check.stdout}"


def test_build_is_idempotent(tmp_path):
    """同样的声明跑两次，结果必须一模一样。

    不幂等的话「生成物 hash」这个判据就没意义了 —— 每次重新生成都会被判成漂移。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    first = {f.name: f.read_text(encoding="utf-8") for f in runtime.iterdir() if f.is_file()}
    assert _run_build(src, runtime).returncode == 0
    second = {f.name: f.read_text(encoding="utf-8") for f in runtime.iterdir() if f.is_file()}
    assert first == second


def test_missing_field_produces_nothing_at_all(tmp_path):
    """声明缺字段 → 退出码 1，且**一个文件都不产出**。

    产出半成品比什么都不产出更糟：目录里会留下一份看起来正常、实际不完整的
    运行时配置，而门禁读的就是那份。
    """
    src = _copy_source(tmp_path / "src")
    # 把 action_max 整行删掉（不是设成 null）—— 模拟「没人想过这一项」
    p = src / "authorization.yaml"
    p.write_text("\n".join(l for l in p.read_text(encoding="utf-8").splitlines()
                           if not l.startswith("action_max:")), encoding="utf-8")

    runtime = tmp_path / "rt"
    proc = _run_build(src, runtime)
    assert proc.returncode == 1, proc.stdout
    assert "action_max" in proc.stderr
    assert not runtime.exists() or not list(runtime.iterdir()), "缺字段却产出了文件"


def test_null_is_pending_not_an_error(tmp_path):
    """写 null = 明确待定：照常生成，但要逐条报出来。

    待定和空着不是一回事 —— 待定是有人知道它没定，空着是没人知道。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    proc = _run_build(src, runtime)
    assert proc.returncode == 0
    assert "待定项" in proc.stdout
    for expected in ("default_timezone", "fallback.on_uncertain", "fallback.escalation"):
        assert expected in proc.stdout, f"{expected} 是 null，却没被报成待定"


def test_generated_env_uses_absolute_paths_and_no_variables(tmp_path):
    """``.env`` 里不许出现变量。

    没有任何一层会展开它们 —— Hermes 自己的 ``load_env()`` 原样返回，我们的
    ``_parse_env`` 也刻意不展开。DEPLOY.md §七 原先写 ``$HERMES_HOME/...``，
    照着建出来的人格读不到注册表、什么都查不了（2026-08-27 实测更正）。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0

    for line in (runtime / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        assert "$" not in value, f"{key} 的值里有变量，不会被展开：{value}"
        if value.endswith((".json", ".jsonl")):
            assert value.startswith("/"), f"{key} 不是绝对路径：{value}"


def test_generated_files_contain_no_credentials(tmp_path):
    """生成物要进仓库、进 CI 日志，不许带凭据。"""
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    text = (runtime / ".env").read_text(encoding="utf-8").upper()
    for forbidden in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "DASHSCOPE"):
        assert forbidden not in text, f"生成的 .env 里出现了 {forbidden}"


# ---------------------------------------------------------------------------
# 「不许手改」的强制点 —— 本文件的重点
# ---------------------------------------------------------------------------

def test_hand_edited_env_is_caught_before_deploy(tmp_path):
    """手改 ``.env`` 往白名单里塞一个工具 —— 必须在部署前被拦下。

    这条模拟的就是「绕过审批」这个动作本身：改声明文件要过 CODEOWNERS，
    但直接改运行时的 ``.env`` 不用过任何人。在 manifest 校验做出来之前，
    这条路是通的，而且**不留痕迹**。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    assert _run_check(runtime).returncode == 0, "干净的生成物应该能过"

    env_path = runtime / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "BI_GATE_TOOLS=query_metric", "BI_GATE_TOOLS=query_metric,terminal"),
        encoding="utf-8")

    check = _run_check(runtime)
    assert check.returncode == 1, f"手改后仍判可部署：\n{check.stdout}"
    assert "手改" in check.stdout and ".env" in check.stdout


def test_build_check_mode_also_reports_drift(tmp_path):
    """``--check`` 不写盘，只报漂移 —— 给 CI 用。"""
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    assert _run_build(src, runtime, "--check").returncode == 0

    (runtime / "SOUL.md").write_text("我是被手改过的\n", encoding="utf-8")
    proc = _run_build(src, runtime, "--check")
    assert proc.returncode == 1
    assert "SOUL.md" in proc.stdout


def test_missing_manifest_is_undecidable_not_a_pass(tmp_path):
    """没有 manifest → 报「查不了」，不是「通过」。

    手工建的老 profile 就没有 manifest。查不了不该拦部署，但也绝不能算通过 ——
    结论里要单独点名，这是 §4.3 那条判断的又一次应用。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    (runtime / ".generated.json").unlink()

    check = _run_check(runtime)
    assert check.returncode == 0, "缺 manifest 不该拦部署"
    assert "查不了" in check.stdout
    assert "不是 build_profile.py 生成的" in check.stdout


def test_manifest_listing_a_missing_file_fails(tmp_path):
    """manifest 里记了、目录里却没有 → 判失败。

    这种情况说明有人把生成物删了，而门禁读的就是那些文件。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    (runtime / "SOUL.md").unlink()

    check = _run_check(runtime)
    assert check.returncode == 1
    assert "SOUL.md" in check.stdout


def test_approvals_are_not_part_of_the_hash(tmp_path, bp):
    """签字记录改了不算生成物漂移。

    ``approvals.json`` 是原样拷贝的，不由声明生成 —— 补一个签字不该让整个
    profile 被判成「被手改过」。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0

    data = json.loads((runtime / "approvals.json").read_text(encoding="utf-8"))
    data["facts"]["by"] = ["事实层责任人", "又一个人"]
    (runtime / "approvals.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert _run_check(runtime).returncode == 0
    manifest = json.loads((runtime / ".generated.json").read_text(encoding="utf-8"))
    assert "approvals.json" not in manifest["files"]


# ---------------------------------------------------------------------------
# mock 值与红线（2026-08-28 加）
# ---------------------------------------------------------------------------

def test_mock_fields_are_reported_and_land_in_the_manifest(tmp_path):
    """mock 值必须被点名，而且要进 manifest。

    mock 比待定更需要盯：待定是空的、一眼看得出；**mock 有值，看起来像定了**。
    只写在 yaml 注释里的话，装配期检查读不到、报告里不会有，几周之后没人分得清
    哪个值是拍过板的。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    proc = _run_build(src, runtime)
    assert proc.returncode == 0, proc.stderr
    assert "mock 值" in proc.stdout and "没有人确认过" in proc.stdout

    manifest = json.loads((runtime / ".generated.json").read_text(encoding="utf-8"))
    mocks = manifest.get("mock_fields")
    assert mocks, "manifest 里没有 mock_fields —— 装配期检查就看不到了"
    assert any(m.startswith("facts.") for m in mocks)
    assert any(m.startswith("authorization.") for m in mocks)


def test_assemble_check_reports_mocks_without_blocking(tmp_path):
    """mock 不拦部署，但必须出现在报告里。

    拦部署是错的：mock 就是为了不被业务方排期阻塞。但不报出来更错 ——
    那等于替一个没人确认过的值作保。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    check = _run_check(runtime)
    assert check.returncode == 0, check.stdout
    assert "未经确认" in check.stdout


def test_a_metric_hitting_a_red_line_blocks_deploy(tmp_path):
    """登记了红线数据 → 不允许部署。

    红线和「没登记」不是一回事：没登记只是暂时查不了，红线是明令禁止。
    这条防的是**以后有人顺手把它登记进来** —— 红线写在声明里，
    但如果没有环节去验，它就只是一段文字。
    """
    src = _copy_source(tmp_path / "src")
    facts = src / "facts.yaml"
    facts.write_text(facts.read_text(encoding="utf-8").replace(
        "metrics:\n",
        "metrics:\n"
        "  - name: user_balance_detail\n"
        "    label: 单用户资金明细\n"
        "    dimensions: [coin]\n"
        "    requires_time_window: true\n"
        "    rows_per_day: 1000\n"
        "    owner: null\n"
        "    source: null\n\n", 1), encoding="utf-8")

    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0, "红线是装配期的事，生成本身不该失败"
    check = _run_check(runtime)
    assert check.returncode == 1
    assert "user_balance_detail" in check.stdout and "user_balance" in check.stdout


def test_forbidden_patterns_reach_the_generated_registry(tmp_path):
    """红线要写进生成的注册表 —— 装配期检查读的是那份，不是声明。"""
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    reg = json.loads((runtime / "bi_registry.json").read_text(encoding="utf-8"))
    assert reg.get("forbidden_patterns"), "注册表里没有 forbidden_patterns"
    assert "kyc" in reg["forbidden_patterns"]


# ---------------------------------------------------------------------------
# .env 的分界线：凭据要能追加，但不能变成后门
# ---------------------------------------------------------------------------

def _append_env(runtime: Path, line: str) -> None:
    with open(runtime / ".env", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def test_credentials_can_be_appended_below_the_marker(tmp_path):
    """模型凭据必须能往 .env 里追加而不破坏 hash。

    这是 2026-08-28 真去部署一个新 profile 时撞出来的：DEPLOY.md 和生成的 .env
    头部都写着"凭据请另行追加"，而照做就通不过「生成物没被手改」——
    **两个我们自己建的东西互相打架**。分界线把两半在同一个文件里划开。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    _append_env(runtime, "DASHSCOPE_API_KEY=sk-fake-for-test")
    assert _run_check(runtime).returncode == 0, "追加凭据不该让检查失败"


def test_the_marker_is_not_a_backdoor(tmp_path):
    """分界线以下不许出现本该由声明决定的键。

    ``.env`` 是**后出现的键覆盖先出现的**，所以在分界线下面写一行
    ``BI_GATE_TOOLS=query_metric,terminal`` 就能给自己加一个工具，
    而上半截的 hash 还是对的 —— 分界线就成了绕过整套审批的后门。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    _append_env(runtime, "BI_GATE_TOOLS=query_metric,terminal")
    check = _run_check(runtime)
    assert check.returncode == 1, f"夹带没被拦：\n{check.stdout}"
    assert "绕过审批" in check.stdout and "BI_GATE_TOOLS" in check.stdout


def test_editing_above_the_marker_is_still_caught(tmp_path):
    """分界线**以上**照旧受 hash 保护 —— 分界线只放开下半截。"""
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    env = runtime / ".env"
    env.write_text(env.read_text(encoding="utf-8").replace(
        "BI_GATE_ACTION_MAX=L1", "BI_GATE_ACTION_MAX=L3"), encoding="utf-8")
    assert _run_check(runtime).returncode == 1


def test_regenerating_keeps_the_operator_section(tmp_path):
    """重新生成不能抹掉部署方追加的内容。

    抹掉之后的表现是"模型调不通"——跟门禁一点关系都没有，很难查到是重新生成
    干的。
    """
    src = _copy_source(tmp_path / "src")
    runtime = tmp_path / "rt"
    assert _run_build(src, runtime).returncode == 0
    _append_env(runtime, "DASHSCOPE_API_KEY=sk-fake-for-test")
    assert _run_build(src, runtime).returncode == 0
    assert "sk-fake-for-test" in (runtime / ".env").read_text(encoding="utf-8")
    assert _run_check(runtime).returncode == 0


def test_env_marker_matches_on_both_sides(tmp_path, bp):
    """两处各写了一份分界线常量（为了能独立分发），必须一致。

    不一致的后果最坏：生成器认为下半截不参与 hash、检查器认为整份都算，
    于是每次部署都失败；或者反过来，检查器认不出分界线，夹带就没人管。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ac_for_marker", PLUGIN_DIR / "assemble_check.py")
    ac = importlib.util.module_from_spec(spec)
    sys.modules["ac_for_marker"] = ac
    spec.loader.exec_module(ac)
    assert bp.ENV_MARKER == ac.ENV_MARKER
