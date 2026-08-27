"""启动前置包装器 —— 真敲一遍，不是读一遍。

这个文件里的每条测试都用 subprocess 跑真实脚本。理由和 probe_runner 用子进程
跑 probe.py 是同一条：**文档里写的那条命令，得有人真的敲**。调研 §二第六条
栽的就是这里 —— 44 个测试全绿，而文档写的命令根本执行不了，因为测试全是按
文件路径 import 的，唯独没人按文档敲一次。

装配期检查（assemble_check.py）本身的单元测试在 test_bi_gate_assemble_check.py。
这里只管**接法**：包装器在检查不过时到底会不会拦住启动。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "plugins" / "bi-gate"
MAKE = PLUGIN_DIR / "make_example_profile.sh"
PREFLIGHT = PLUGIN_DIR / "preflight.sh"

MARKER = "【启动了】"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="包装器是 bash 脚本，Windows 上不适用")


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["BI_PREFLIGHT_PYTHON"] = sys.executable
    return env


def _make_profile(dest: Path) -> Path:
    # 直接调，不写成 ``bash <script>`` —— 那样连可执行位丢了都测不出来，
    # 而 CI 和 DEPLOY.md 里都是直接调的。
    proc = subprocess.run([str(MAKE), str(dest)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"生成样例 profile 失败：{proc.stderr}"
    return dest


def _preflight(profile: Path, *extra: str):
    return subprocess.run(
        [str(PREFLIGHT), str(profile), "--skip-runtime", *extra,
         "--", "echo", MARKER],
        capture_output=True, text=True, timeout=180, env=_env(), cwd=str(REPO))


# ---------------------------------------------------------------------------

def test_generated_profile_is_deployable(tmp_path):
    """样例 profile 必须自己就能过检查。

    过不了的话，「照着这个建新人格」这句话就是错的 —— 而它同时也是 CI 的输入，
    一份过不了的样例会让 CI 永远红，然后被人加 `|| true` 绕过去。
    """
    profile = _make_profile(tmp_path / "p")
    for name in (".env", "approvals.json", "config.yaml"):
        assert (profile / name).exists(), f"少了 {name}"

    proc = subprocess.run(
        [sys.executable, str(PLUGIN_DIR / "assemble_check.py"), str(profile), "--skip-runtime"],
        capture_output=True, text=True, timeout=120, env=_env(), cwd=str(REPO))
    assert proc.returncode == 0, f"样例 profile 没通过装配期检查：\n{proc.stdout}\n{proc.stderr}"


def test_example_profile_contains_no_secrets(tmp_path):
    """样例里不许出现任何凭据键。

    样例是要进仓库、进 CI 日志的。真实 profile 的 .env 里躺着模型 API key，
    抄样例时最容易顺手把那一行也抄进模板。
    """
    profile = _make_profile(tmp_path / "p")
    env_text = (profile / ".env").read_text(encoding="utf-8")
    for forbidden in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "DASHSCOPE"):
        assert forbidden not in env_text.upper(), f"样例 .env 里出现了 {forbidden}"


def test_paths_in_example_are_absolute(tmp_path):
    """样例里的路径必须是绝对路径。

    ``.env`` 里的 ``$VAR`` 没有任何一层会展开 —— Hermes 自己的
    ``hermes_cli.config.load_env()`` 原样返回，我们的 ``_parse_env`` 也刻意不展开。
    DEPLOY.md §七 原先写的就是 ``$HERMES_HOME/bi_registry.json``，照着建出来的
    人格读不到注册表、什么都查不了（2026-08-27 实测更正）。
    """
    profile = _make_profile(tmp_path / "p")
    for line in (profile / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        assert "$" not in value, f"{key} 的值里有变量，不会被展开：{value}"
        if value.endswith(".json") or value.endswith(".jsonl"):
            assert value.startswith("/"), f"{key} 不是绝对路径：{value}"


def test_preflight_starts_the_command_when_checks_pass(tmp_path):
    profile = _make_profile(tmp_path / "p")
    proc = _preflight(profile)
    assert proc.returncode == 0, f"检查通过却没启动：\n{proc.stdout}\n{proc.stderr}"
    assert MARKER in proc.stdout, "命令没有被 exec"


def test_preflight_refuses_to_start_without_approvals(tmp_path):
    """没有审批签字 —— 不启动。

    这是包装器存在的全部理由。检查器本身早就会报错了；问题在于报了错之后
    有没有人拦住启动。「记得先跑一下检查」不是强制。
    """
    profile = _make_profile(tmp_path / "p")
    (profile / "approvals.json").unlink()
    proc = _preflight(profile)
    assert proc.returncode == 1, f"退出码应为 1，实际 {proc.returncode}"
    assert MARKER not in proc.stdout, "检查没过，命令却还是被 exec 了"


def test_preflight_refuses_on_self_contradictory_declaration(tmp_path):
    """默认级别高于 action_max —— 这种人格连最普通的调用都做不了，不许启动。

    这类配置不会报错、不会拦人，只会让人格安静地什么都干不成，然后被当成
    「模型不行」。抓它正是装配期这一层的意义。
    """
    profile = _make_profile(tmp_path / "p")
    env_path = profile / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "BI_GATE_ACTION_MAX=L1", "BI_GATE_ACTION_MAX=L0"),
        encoding="utf-8")

    policy = json.loads((PLUGIN_DIR / "policy.example.json").read_text(encoding="utf-8"))
    policy["default_level"] = "L2"
    bad_policy = profile / "policy_bad.json"
    bad_policy.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    env_path.write_text(
        "\n".join(
            f"BI_GATE_ACTION_POLICY={bad_policy}" if ln.startswith("BI_GATE_ACTION_POLICY=") else ln
            for ln in env_path.read_text(encoding="utf-8").splitlines()),
        encoding="utf-8")

    proc = _preflight(profile)
    assert proc.returncode == 1, f"自相矛盾的声明被放行了：\n{proc.stdout}"
    assert MARKER not in proc.stdout


def test_preflight_refuses_when_the_checker_itself_errors(tmp_path):
    """检查器自身出错（退出码 2）也不启动。

    「检查器坏了」和「声明不合法」在后果上是一回事：都不知道这份声明合不合法。
    而查不了的东西不算安全 —— 这条判断在整个门禁里是一贯的（见 UNDECIDABLE）。
    """
    missing = tmp_path / "根本不存在的 profile"
    proc = _preflight(missing)
    assert proc.returncode == 2, f"退出码应为 2，实际 {proc.returncode}"
    assert MARKER not in proc.stdout
