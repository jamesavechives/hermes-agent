#!/usr/bin/env python3
"""装配期静态检查 —— 第一层。

不通过不允许部署。回答的问题是「这份人格声明本身合不合法」，
而不是「它此刻在运行时生效吗」（那是 ``verify.py`` 的活）。

两者的分工
----------
================  ============================  ==========================
                  assemble_check.py（本文件）    verify.py
================  ============================  ==========================
何时跑            部署**之前**                   新 profile 上线或排障时
需要什么          只读文件                       要能 import Hermes、真派发
回答什么          声明合不合法、能不能部署        门禁此刻在不在拦
================  ============================  ==========================

**只有第 5 项例外**：它要真的执行一次文档里写的命令。这条来自调研 §二第六条
——探针命令 `python -m plugins.bi_gate.probe` 曾在 44 个测试全绿的情况下
根本执行不了，因为测试都按文件路径 import，唯独没人真的按文档敲一次。
静态检查抓不到这种事，只能真敲一遍。

为什么现在读 profile 目录而不是正式的声明文件
--------------------------------------------
声明格式还没落地：形状已定（设计方案 §3.4 —— 五块拆成五个文件，各设
CODEOWNERS），但生成工具还没写。照一个不
存在的文件写检查器，等于又造一个跑不到真实数据上的东西——而这正是本项目一路
在纠正的毛病。所以现在从 profile 目录里**实际存在的声明**读：``.env``、
``config.yaml``、注册表、动作策略。

声明文件落地后只需换 :func:`load_declaration`，检查项一条都不用动。

接在哪（设计方案 §4.7，2026-08-27 已定）
--------------------------------------
两个地方都要，检的不是同一件事：

- ``preflight.sh`` 在**每次启动前**跑，检这台机器上这份真声明合不合法。
  它是强制点 —— 检查不过就 exec 不到启动命令。
- CI（``.github/workflows/bi-gate.yaml``）在**每次 push** 跑，检这个检查器
  本身没写坏、样例声明合法。它还专门验一步「拿掉审批签字必须报非 0」——
  只验合法的能过是不够的，一个永远返回 0 的检查器也能过。

CI 检不了生产上那份 profile（不在仓库里，里面有凭据）；启动前置发现不了
「检查器被改成永远返回 0」。谁也替不了谁。

所以这里保持成一个退出码明确的独立脚本：0 可部署，1 有项未过，2 检查器自己出错。

用法
----
    assemble_check.py /data/profiles/bi [--skip-runtime]

退出码：0 = 可以部署；1 = 有检查未通过；2 = 检查器自身出错。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

#: Agent Profile 的五块字段（系统 B §8.1 图 7）与当前声明位置的对应。
#: 值为 None 表示这一块**目前没有任何声明位置**——不是"我们没查"，是"没地方写"。
FIELD_SOURCES: Dict[str, Tuple[str, Optional[str]]] = {
    "① persona":            ("身份与表达", None),  # 等 persona.yaml（§3.4）
    "② facts":              ("受控事实层绑定", "BI_GATE_REGISTRY"),
    "③ tools + action_max": ("工具与动作上限", "BI_GATE_TOOLS"),
    "④ skills":             ("技能集与自演化开关", None),
    "⑤ fallback":           ("交接与兜底", None),
}


class Result:
    """一项检查的结果。``ok=None`` 表示"查不了"，与"查了没过"分开记。"""

    def __init__(self, name: str, ok: Optional[bool], detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    @property
    def mark(self) -> str:
        return {True: "✓", False: "✗"}.get(self.ok, "?")

    def __str__(self) -> str:
        tail = f"  {self.detail}" if self.detail else ""
        return f"   [{self.mark}] {self.name}{tail}"


# ---------------------------------------------------------------------------
# 读声明
# ---------------------------------------------------------------------------

def _parse_env(path: Path) -> Dict[str, str]:
    """只认 ``KEY=VALUE``，不 source shell、不展开变量。

    与 ``probe_runner.load_env_file`` 同一条理由：需要执行才能得出的配置
    本身就是审批不了的。两处各写一份而不共用，是因为这两个脚本要能独立
    分发——装配期检查器在 CI 里跑时，probe_runner 可能根本不在。
    """
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_declaration(profile: Path) -> Dict[str, Any]:
    """把一个 profile 的声明读成一个字典。

    profile.yaml 落地后只改这个函数：返回同样形状的字典，下面的检查项
    一条都不用动。
    """
    env = _parse_env(profile / ".env")
    config_text = ""
    cfg = profile / "config.yaml"
    if cfg.exists():
        config_text = cfg.read_text(encoding="utf-8")
    return {
        "profile": profile.name,
        "path": profile,
        "env": env,
        "config_text": config_text,
        "approvals": profile / "approvals.json",
    }


_rules_cache: Any = None


def _rules() -> Any:
    """按文件路径加载 ``rules.py``。

    目录名带连字符不是合法包名，正常 import 走不通——仓库里的测试、verify.py、
    Hermes 自身都是这个做法。这里也一样，好处是装配期用的解析逻辑和运行时
    **是同一份代码**：另写一套的话，装配期说"能解析"而运行时说"不能"，
    漂移的方向恰好是最坏的那个。
    """
    global _rules_cache
    if _rules_cache is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("bi_gate_rules_for_check", HERE / "rules.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _rules_cache = mod
    return _rules_cache


def _load_json(path_str: str) -> Tuple[Optional[Any], str]:
    if not path_str:
        return None, "未声明路径"
    p = Path(path_str)
    if not p.exists():
        return None, f"文件不存在：{p}"
    try:
        return json.loads(p.read_text(encoding="utf-8")), ""
    except ValueError as exc:
        return None, f"JSON 解析失败：{exc}"


# ---------------------------------------------------------------------------
# ① 五块字段齐全
# ---------------------------------------------------------------------------

def check_fields(decl: Dict[str, Any]) -> List[Result]:
    """五块字段各自有没有落点。

    没有落点的字段**不是"检查通过"也不是"检查失败"，是"没地方写"**——
    这个区别重要：前者说明这个 profile 配漏了，后者说明我们的声明格式
    还缺一块。混成一个结论会让人以为补配置就能解决。
    """
    out: List[Result] = []
    env = decl["env"]
    for field, (what, key) in FIELD_SOURCES.items():
        if key is None:
            out.append(Result(f"{field}（{what}）", None,
                              "当前声明格式里没有这一块的位置 —— 等五个声明文件（设计方案 §3.4）"))
        elif env.get(key, "").strip():
            out.append(Result(f"{field}（{what}）", True, f"{key} 已声明"))
        else:
            out.append(Result(f"{field}（{what}）", False, f"{key} 缺失"))
    return out


# ---------------------------------------------------------------------------
# ② 审批签字齐全
# ---------------------------------------------------------------------------

def check_approvals(decl: Dict[str, Any]) -> List[Result]:
    """``authorization`` 与 ``facts`` 两段必须有签字和 PR 引用。

    这两块是「改它＝权限变更」的那两块（系统 B §8.1 判据）。没有审批记录时
    **必须判失败，不能判"查不了"**：一个没人批准过的人格声明被部署，和一个
    格式写错的声明，后果完全不同——后者跑不起来，前者跑得好好的。
    """
    path: Path = decl["approvals"]
    if not path.exists():
        return [Result("审批签字", False,
                       f"没有 {path.name} —— 无法确认这份声明被谁批准过")]
    data, err = _load_json(str(path))
    if data is None:
        return [Result("审批签字", False, err)]

    out: List[Result] = []
    for seg in ("authorization", "facts"):
        rec = (data or {}).get(seg)
        if not isinstance(rec, dict):
            out.append(Result(f"审批签字 · {seg}", False, "缺这一段"))
            continue
        missing = [k for k in ("by", "at", "ref") if not rec.get(k)]
        if missing:
            out.append(Result(f"审批签字 · {seg}", False, f"缺字段：{'、'.join(missing)}"))
        else:
            by = rec["by"]
            who = "、".join(by) if isinstance(by, list) else str(by)
            out.append(Result(f"审批签字 · {seg}", True, f"{who} @ {rec['at']}"))
    return out


# ---------------------------------------------------------------------------
# ③ tools 与 action_max 自相容
# ---------------------------------------------------------------------------

def check_self_consistency(decl: Dict[str, Any]) -> List[Result]:
    """声明之间不能互相矛盾。

    这一项抓的是「每条单看都合法、合起来做不了事」——那种配置不会报错，
    只会让人格安静地什么都干不成，然后被当成"模型不行"。
    """
    rules = _rules()
    parse_level, level_name = rules.parse_level, rules.level_name

    env = decl["env"]
    out: List[Result] = []

    max_raw = env.get("BI_GATE_ACTION_MAX", "").strip()
    action_max = parse_level(max_raw) if max_raw else None
    if max_raw and action_max is None:
        out.append(Result("action_max 取值", False, f"{max_raw!r} 不是合法级别"))
        return out
    if action_max is None:
        out.append(Result("action_max 取值", None, "未声明 —— 运行时按 L0（最严）处理"))
        action_max = 0

    policy, err = _load_json(env.get("BI_GATE_ACTION_POLICY", ""))
    if policy is None:
        out.append(Result("action_policy 与 action_max 相容", None, err))
    else:
        default_raw = policy.get("default_level", "L0")
        default_level = parse_level(str(default_raw))
        if default_level is None:
            out.append(Result("action_policy.default_level", False,
                              f"{default_raw!r} 不是合法级别"))
        elif default_level > action_max:
            out.append(Result("action_policy 与 action_max 相容", False,
                              f"默认级别 {level_name(default_level)} 高于 action_max "
                              f"{level_name(action_max)} —— 这个人格连最普通的调用都做不了"))
        else:
            out.append(Result("action_policy 与 action_max 相容", True,
                              f"默认 {level_name(default_level)} ≤ 上限 {level_name(action_max)}"))

        hr = policy.get("human_review_from")
        if hr is not None:
            hr_level = parse_level(str(hr))
            if hr_level is None:
                out.append(Result("human_review_from", False, f"{hr!r} 不是合法级别"))
            elif hr_level <= (default_level or 0):
                out.append(Result("human_review_from", False,
                                  f"{level_name(hr_level)} 不高于默认级别 —— 每次调用都要人审"))
            else:
                out.append(Result("human_review_from", True, level_name(hr_level)))

    # 注册表：声明了扫描上限却没有 rows_per_day 的指标，任何查询都会被拒。
    registry, err = _load_json(env.get("BI_GATE_REGISTRY", ""))
    if registry is None:
        out.append(Result("注册表可读", False, err))
    else:
        metrics = registry.get("metrics", [])
        broken = [m.get("name") for m in metrics
                  if m.get("max_scan_rows") is not None and m.get("rows_per_day") is None]
        if broken:
            out.append(Result("注册表扫描量声明完整", False,
                              f"这些指标声明了 max_scan_rows 却没有 rows_per_day，"
                              f"任何查询都会被拒：{'、'.join(str(b) for b in broken)}"))
        else:
            out.append(Result("注册表扫描量声明完整", True, f"{len(metrics)} 个指标"))

    return out


# ---------------------------------------------------------------------------
# ④ action_policy 能被解析
# ---------------------------------------------------------------------------

def check_policy_parses(decl: Dict[str, Any]) -> List[Result]:
    """用运行时那份解析器来解析，不另写一套。

    另写一套的话，装配期说"能解析"而运行时说"不能"，两边就会漂移——而
    漂移的方向恰好是最坏的：装配期放行了运行时拒绝的东西。
    """
    path = decl["env"].get("BI_GATE_ACTION_POLICY", "").strip()
    if not path:
        return [Result("action_policy 可解析", None, "未声明 —— 动作分级不启用")]
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bi_gate_plugin_for_check", HERE / "__init__.py",
            submodule_search_locations=[str(HERE)])
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        # _load_policy 从环境变量读路径，不收参数 —— 用运行时那套完整逻辑，
        # 包括「载入失败返回 unavailable 策略」这条，装配期才能提前看到
        # 运行时会不会把整份策略判死。
        os.environ["BI_GATE_ACTION_POLICY"] = path
        policy = mod._load_policy()
    except Exception as exc:
        return [Result("action_policy 可解析", False, f"解析器抛异常：{exc}")]

    if getattr(policy, "unavailable", False):
        return [Result("action_policy 可解析", False,
                       "运行时会判整份策略不可用 —— 届时所有调用都会被拒")]
    return [Result("action_policy 可解析", True, f"{len(policy.rules)} 条规则")]


# ---------------------------------------------------------------------------
# ⑤ 文档里写的启动命令真的能跑
# ---------------------------------------------------------------------------

def check_documented_command(decl: Dict[str, Any], timeout: float = 60.0) -> List[Result]:
    """真敲一遍探针命令。

    静态检查抓不到「文档写的命令根本执行不了」——那次 44 个测试全绿，
    因为测试都按文件路径 import。只有真执行才发现得了。

    这里只关心**命令能不能跑起来**，不关心门禁判定结果：退出码 1（门禁失效）
    也算命令跑通了。跑不起来（退出码 2 / 找不到文件 / import 失败）才算失败。
    """
    probe = HERE / "probe.py"
    if not probe.exists():
        return [Result("文档里的探针命令可执行", False, f"找不到 {probe}")]

    env = dict(os.environ)
    env.update(decl["env"])
    env["HERMES_HOME"] = str(decl["path"])
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{existing}" if existing else str(REPO)

    try:
        proc = subprocess.run([sys.executable, str(probe)], env=env,
                              capture_output=True, text=True,
                              timeout=timeout, cwd=str(REPO))
    except subprocess.TimeoutExpired:
        return [Result("文档里的探针命令可执行", False, f"{timeout:.0f}s 未返回")]
    except Exception as exc:
        return [Result("文档里的探针命令可执行", False, f"拉不起来：{exc}")]

    if proc.returncode in (0, 1):
        note = "门禁在拦" if proc.returncode == 0 else "命令能跑，但门禁此刻不生效"
        return [Result("文档里的探针命令可执行", True, note)]
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return [Result("文档里的探针命令可执行", False,
                   f"退出码 {proc.returncode}：{tail[-1][:120] if tail else '无输出'}")]


# ---------------------------------------------------------------------------

SECTIONS = [
    ("① 五块字段齐全", check_fields),
    ("② 审批签字齐全", check_approvals),
    ("③ 声明之间自相容", check_self_consistency),
    ("④ action_policy 可解析", check_policy_parses),
]


def run(profile: Path, skip_runtime: bool = False) -> Tuple[int, List[Result]]:
    decl = load_declaration(profile)
    all_results: List[Result] = []
    print(f"装配期检查：{profile}")
    for title, fn in SECTIONS:
        print(f"\n{title}")
        try:
            results = fn(decl)
        except Exception as exc:  # 单项炸了不牵连整体，但要算失败
            results = [Result(title, False, f"检查项自身出错：{exc}")]
        for r in results:
            print(r)
        all_results.extend(results)

    print("\n⑤ 文档里写的命令真的跑一遍")
    if skip_runtime:
        r = Result("文档里的探针命令可执行", None, "--skip-runtime，本次跳过")
        print(r)
        all_results.append(r)
    else:
        for r in check_documented_command(decl):
            print(r)
            all_results.append(r)

    failed = [r for r in all_results if r.ok is False]
    unknown = [r for r in all_results if r.ok is None]
    print()
    if failed:
        print(f"结论：不允许部署 ✗（{len(failed)} 项未通过）")
    else:
        print("结论：可以部署 ✓")
    if unknown:
        print(f"另有 {len(unknown)} 项查不了 —— 它们不是通过，只是当前声明格式还没有"
              f"对应位置或未启用。查不了的东西不算安全。")
    return (1 if failed else 0), all_results


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="bi-gate 装配期静态检查")
    ap.add_argument("profile", help="profile 目录，例如 /data/profiles/bi")
    ap.add_argument("--skip-runtime", action="store_true",
                    help="跳过第 5 项（需要能 import Hermes 的环境）")
    args = ap.parse_args(argv[1:])

    profile = Path(args.profile).resolve()
    if not profile.is_dir():
        print(f"profile 目录不存在：{profile}", file=sys.stderr)
        return 2
    code, _ = run(profile, skip_runtime=args.skip_runtime)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
