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
