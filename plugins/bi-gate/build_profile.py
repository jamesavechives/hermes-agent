#!/usr/bin/env python3
"""从五个声明文件生成运行时 profile。

为什么要有这个工具
------------------
设计方案 §3.3 写了「散落的运行时文件由工具从声明生成，不允许手改 —— 手改能绕过
审批，那这一层就白做了」。但那句话到现在为止**只是一句话**。没有工具，就没有
「生成」这个动作；没有 manifest，「不许手改」就只能靠自觉。

这个项目一路在纠正的正是这类事：写在文档里给人看的规矩不是强制，强制只能做在
必经的路径上。所以这里做两件事：

1. 从五个声明文件生成运行时文件（.env / SOUL.md / config.yaml / 注册表 / 动作策略）
2. 同时写一份 ``.generated.json``：每个生成物的 sha256 + 它来自哪个声明文件

第 2 件才是重点。``assemble_check.py`` 会校验这些 hash —— 对不上就是有人手改过
生成物，**判不允许部署**。这样「不许手改」就从一句话变成了部署前拦得住的东西。

五个声明文件（设计方案 §3.4，各设 CODEOWNERS）
----------------------------------------------
    persona.yaml         ① 业务方自助
    facts.yaml           ② 事实层责任人
    authorization.yaml   ③ 技术负责人 + 合规双签
    skills.yaml          ④ 技术负责人
    fallback.yaml        ⑤ 业务方 + 合规
    approvals.json       签字记录（装配期检查读它）

关于依赖
--------
门禁**运行时**（rules / hooks / probe）仍然是纯标准库、零第三方依赖（§9.1），
那条不变。这个文件是**构建期工具**，不在派发路径上，用了 ``yaml`` —— 宿主
Hermes 本身就精确锁了 pyyaml，不是新增的供应链面。

刻意**没有**自己写一个 YAML 子集解析器：那种东西看着聪明，实际会和真 YAML 在
边角上解析不一致，而这类不一致恰好最难发现 —— 声明看起来是一个意思，生成出来
是另一个意思。

三种状态，处理不一样
--------------------
==========  ==============================  ========================
 状态        怎么写                          工具怎么处理
==========  ==============================  ========================
 空着        漏写这个字段                     **报错，不生成任何文件**
 待定        写 ``null``                     生成，逐条报「待定」
 mock        写值 + 列进 ``_mock_fields``     生成，逐条报「mock，未确认」
==========  ==============================  ========================

三者的区别是**谁知道它没定**：空着是没人知道；待定是有人知道它没定；
mock 是我们先填了一个值让开发不阻塞，但**没有人确认过它**。

mock 最危险，因为它看起来是定了的。所以它不能只写在注释里 —— 注释不会被
``assemble_check`` 读到、不会出现在任何报告里，几周之后没人分得清哪个值是拍过板的。
``_mock_fields`` 让「这是 mock」变成一条查得到的事实。

用法
----
    build_profile.py <声明目录> <运行时目录>
    build_profile.py <声明目录> <运行时目录> --check   # 只比对，不写

退出码：0 生成成功；1 声明有问题（未生成）；2 工具自身出错。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent

#: 五个声明文件。顺序即报告顺序，与设计方案 §3.4 的编号一致。
DECLARATIONS = ("persona", "facts", "authorization", "skills", "fallback")

#: manifest 文件名。放在运行时目录里，跟着 profile 走。
MANIFEST = ".generated.json"

#: 工具名 → 提供它的插件。生成 config.yaml 时据此把插件也写进 plugins.enabled。
#:
#: 2026-08-28 实测发现的坑：原先这里硬编码只写 ``- bi-gate``，于是生成出来的
#: 人格门禁开着、``query_metric`` 根本没注册 —— **什么都干不了，而 preflight
#: 和 assemble_check 都判它合法**。真跑一次模型才看出来，它自己诊断的原话是
#: 「声明该人格只能使用 query_metric，但 query_metric 并未实际注册」。
#:
#: 方向是安全的（fail-closed，泄不了），但「检查全过、跑起来是块砖」和
#: 「44 个测试全绿但文档写的命令执行不了」是同一类失效。
TOOL_PROVIDERS: Dict[str, str] = {
    "query_metric": "bi-query",
}

#: ``.env`` 里的分界线。以上由本工具生成、参与 hash；以下由部署方维护。
#:
#: 为什么需要它：模型凭据（DASHSCOPE_API_KEY 之类）**必须**由部署的人往 .env 里
#: 追加 —— 它不能经过声明文件、不能进仓库。但追加会改变文件内容，于是「生成物
#: 没被手改」那条检查会当场判失败。
#:
#: 这是 2026-08-28 真去部署一个新 profile 时撞出来的：DEPLOY.md 和生成的 .env
#: 头部都写着"凭据请另行追加"，而照做就通不过检查 —— 两个我们自己建的东西互相
#: 打架。分界线把「生成的」和「部署方的」在同一个文件里划开。
ENV_MARKER = "# ==== 以下由部署方维护（模型凭据等），不参与 hash ===="

#: 分界线**以下**不许出现的键前缀。不设这条，分界线就成了后门 ——
#: ``.env`` 是后出现的键覆盖先出现的，在下面写一行 ``BI_GATE_TOOLS=...``
#: 就能绕过整套审批，而且 hash 还是对的。
ENV_TAIL_FORBIDDEN_PREFIXES = ("BI_GATE_", "BI_AUDIT_", "HERMES_HOME")


def split_env(text: str) -> Tuple[str, str]:
    """把 ``.env`` 拆成 (生成的部分, 部署方维护的部分)。

    没有分界线时整份都算生成的 —— 老 profile 兼容。
    """
    idx = text.find(ENV_MARKER)
    if idx < 0:
        return text, ""
    end = idx + len(ENV_MARKER)
    return text[:end], text[end:]


def env_tail_violations(tail: str) -> List[str]:
    """分界线以下有没有本该由声明决定的键。"""
    out = []
    for raw in tail.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key.startswith(ENV_TAIL_FORBIDDEN_PREFIXES):
            out.append(key)
    return out

LEVELS = ("L0", "L1", "L2", "L3")


class DeclError(Exception):
    """声明本身有问题 —— 不生成任何东西，退出码 1。"""


# ---------------------------------------------------------------------------
# 读声明
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise DeclError(f"缺少声明文件：{path.name}")
    try:
        import yaml  # 构建期依赖，见模块 docstring
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise DeclError(f"读不了 YAML（{exc}）—— 这是构建期工具，需要 pyyaml") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DeclError(f"{path.name} 解析失败：{exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise DeclError(f"{path.name} 顶层必须是键值对，实际是 {type(data).__name__}")
    return data


def load_declarations(src: Path) -> Dict[str, Dict[str, Any]]:
    return {name: _load_yaml(src / f"{name}.yaml") for name in DECLARATIONS}


def collect_mock_fields(decl: Dict[str, Dict[str, Any]]) -> List[str]:
    """把五个声明文件里的 ``_mock_fields`` 汇总成一张表。

    每一项形如 ``facts.default_timezone`` —— 前缀是哪个声明文件，好知道该找谁改。
    """
    out: List[str] = []
    for name, data in decl.items():
        fields = data.get("_mock_fields") or []
        if not isinstance(fields, list):
            raise DeclError(f"{name}.yaml 的 _mock_fields 必须是列表")
        out.extend(f"{name}.{f}" for f in fields)
    return sorted(out)


def _require(d: Dict[str, Any], key: str, where: str) -> Any:
    """字段必须**出现**。值可以是 null（明确待定），但不能不写。

    这个区别是有意的：漏写说明没人想过这一项；写 null 说明有人想过、还没定。
    前者要报错，后者只要报出来。
    """
    if key not in d:
        raise DeclError(f"{where} 缺字段 {key!r} —— 若尚未确定，请显式写 {key}: null")
    return d[key]


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def render_soul(persona: Dict[str, Any]) -> str:
    """① persona → SOUL.md（system prompt 的身份段）。"""
    name = _require(persona, "name", "persona.yaml")
    self_ref = _require(persona, "self_reference", "persona.yaml")
    address = _require(persona, "address_user", "persona.yaml")
    tone = _require(persona, "tone", "persona.yaml")
    banned = _require(persona, "banned_words", "persona.yaml") or []
    if not isinstance(banned, list):
        raise DeclError("persona.yaml 的 banned_words 必须是列表")

    lines = [
        "<!-- 本文件由 build_profile.py 从 persona.yaml 生成，不要手改。",
        "     手改会被装配期检查拦下（生成物 hash 与 .generated.json 对不上）。-->",
        "",
        f"你是{name or '（名字待定）'}。",
        "",
        f"- 自称：{self_ref or '（待定）'}",
        f"- 称呼用户：{address or '（待定）'}",
        f"- 语气：{tone or '（待定）'}",
    ]
    if banned:
        lines.append(f"- 禁用词：{'、'.join(str(w) for w in banned)}")
    lines.append("")
    return "\n".join(lines)


def render_registry(facts: Dict[str, Any]) -> Tuple[str, List[str]]:
    """② facts → 指标注册表 JSON。返回 (内容, 待定项)。"""
    metrics = _require(facts, "metrics", "facts.yaml") or []
    if not isinstance(metrics, list):
        raise DeclError("facts.yaml 的 metrics 必须是列表")

    pending: List[str] = []
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, m in enumerate(metrics):
        if not isinstance(m, dict):
            raise DeclError(f"facts.yaml metrics[{i}] 必须是键值对")
        name = m.get("name")
        if not name:
            raise DeclError(f"facts.yaml metrics[{i}] 缺 name")
        if name in seen:
            raise DeclError(f"facts.yaml 里指标 {name!r} 重复登记")
        seen.add(name)

        spec: Dict[str, Any] = {
            "name": name,
            "dimensions": list(m.get("dimensions") or []),
            "requires_time_window": bool(m.get("requires_time_window", True)),
        }
        for opt in ("rows_per_day", "max_scan_rows"):
            if m.get(opt) is not None:
                spec[opt] = int(m[opt])
        # 口径来源不进运行时判定，但要留在注册表里 —— 出了口径争议好追溯。
        for meta in ("label", "owner", "source"):
            if m.get(meta):
                spec[meta] = m[meta]
            else:
                pending.append(f"指标 {name} 的 {meta} 未填")
        out.append(spec)

    tz = _require(facts, "default_timezone", "facts.yaml")
    if tz is None:
        pending.append("default_timezone 待定（业务方定，见《待确认事项》第三节）")

    # 红线：永远不该进这个助手的数据。和"没登记"不是一回事 ——
    # 没登记只是暂时查不了，红线是明令禁止。写进注册表是为了让装配期检查能验：
    # 防的是以后有人顺手把它们登记进来，而那时候没有任何环节会说话。
    forbidden = _require(facts, "forbidden_patterns", "facts.yaml") or []
    if not isinstance(forbidden, list):
        raise DeclError("facts.yaml 的 forbidden_patterns 必须是列表")
    if not forbidden:
        pending.append("forbidden_patterns 是空的 —— 没有任何数据被明令禁止")

    body = {
        "_comment": "由 build_profile.py 从 facts.yaml 生成，不要手改。",
        "default_timezone": tz,
        "forbidden_patterns": [str(x) for x in forbidden],
        "metrics": out,
    }
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=False) + "\n", pending


def render_policy(auth: Dict[str, Any]) -> Tuple[str, List[str]]:
    """③ authorization 的一半 → 动作分级策略 JSON。"""
    policy = _require(auth, "action_policy", "authorization.yaml")
    if policy is None:
        return "", ["action_policy 待定 —— 动作分级这一层不会启用"]
    if not isinstance(policy, dict):
        raise DeclError("authorization.yaml 的 action_policy 必须是键值对或 null")
    body = dict(policy)
    body.setdefault("_comment", "由 build_profile.py 从 authorization.yaml 生成，不要手改。")
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=False) + "\n", []


def render_env(auth: Dict[str, Any], runtime: Path) -> Tuple[str, List[str]]:
    """③ authorization → .env 里的 BI_GATE_*。

    路径一律写**绝对路径**：``.env`` 里的变量没有任何一层会展开（Hermes 自己的
    ``load_env()`` 原样返回，我们的 ``_parse_env`` 也刻意不展开）。DEPLOY.md §七
    原先写 ``$HERMES_HOME/...``，照着建出来的人格读不到注册表、什么都查不了。
    """
    pending: List[str] = []

    tools = _require(auth, "tools", "authorization.yaml") or []
    if not isinstance(tools, list):
        raise DeclError("authorization.yaml 的 tools 必须是列表")
    if not tools:
        pending.append("tools 是空的 —— 该人格任何工具都调不了")

    action_max = _require(auth, "action_max", "authorization.yaml")
    if action_max is None:
        pending.append("action_max 待定 —— 运行时按 L0（最严）处理")
    elif action_max not in LEVELS:
        raise DeclError(f"authorization.yaml 的 action_max={action_max!r} 不是 {'/'.join(LEVELS)}")

    session_max = _require(auth, "session_scan_max", "authorization.yaml")
    if session_max is None:
        pending.append("session_scan_max 待定 —— 只有单次限额，拆调用绕不过的那条不生效")

    lines = [
        "# 由 build_profile.py 生成，不要手改。",
        "# 手改会被装配期检查拦下（hash 与 .generated.json 对不上）。",
        "# 模型凭据请另行追加，不经过声明文件、不进仓库。",
        "",
        f"BI_GATE_REGISTRY={runtime / 'bi_registry.json'}",
    ]
    if (runtime / "action_policy.json").name:
        lines.append(f"BI_GATE_ACTION_POLICY={runtime / 'action_policy.json'}")
    if action_max:
        lines.append(f"BI_GATE_ACTION_MAX={action_max}")
    lines.append(f"BI_GATE_TOOLS={','.join(str(t) for t in tools)}")
    if session_max is not None:
        lines.append(f"BI_GATE_SESSION_SCAN_MAX={int(session_max)}")
    lines.append(f"BI_AUDIT_LOG={runtime / 'audit.jsonl'}")
    lines.append("")
    lines.append(ENV_MARKER)
    return "\n".join(lines), pending


def render_config(skills: Dict[str, Any], declared_tools: List[Any]) -> Tuple[str, List[str]]:
    """④ skills + authorization.tools → config.yaml。

    ``plugins.enabled`` 必须同时含门禁和**提供被声明工具的那些插件**，
    缺任何一边都不会报错，只会安静地做不了事。见 :data:`TOOL_PROVIDERS`。
    """
    pending: List[str] = []
    self_evo = _require(skills, "self_evolution", "skills.yaml")
    if self_evo is None:
        pending.append("skills.self_evolution 待定")
    elif self_evo:
        pending.append("skills.self_evolution 为 true —— 自演化开着，画像写入限制还没做（§八）")
    # 门禁本身永远要在。其余按 authorization.yaml 声明的 tools 反查提供方 ——
    # 声明了某个工具却不把提供它的插件启用，人格就是块砖（见 TOOL_PROVIDERS）。
    enabled = ["bi-gate"]
    unknown: List[str] = []
    for tool in declared_tools:
        provider = TOOL_PROVIDERS.get(str(tool))
        if provider is None:
            unknown.append(str(tool))
        elif provider not in enabled:
            enabled.append(provider)

    if unknown:
        # 不认识的工具名不静默略过。它要么是宿主内置（那就不需要启用插件），
        # 要么是拼错了（那生成出来的人格会以为自己有这个工具、实际调不到）。
        # 两种都得让人看见，所以进 pending 而不是进日志。
        pending.append(
            "authorization.yaml 声明了 " + "、".join(unknown) +
            " —— build_profile 不知道哪个插件提供它。若是宿主内置工具可忽略；"
            "若是插件工具，请补进 build_profile.py 的 TOOL_PROVIDERS，"
            "否则生成的人格会声明一个自己根本调不到的工具"
        )

    lines = [
        "# 由 build_profile.py 从 skills.yaml + authorization.yaml 生成，不要手改。",
        "# plugins.enabled 少了 bi-gate，门禁完全不存在，而且不会报任何错。",
        "# 少了提供工具的那个插件（如 bi-query），门禁在、工具不在 —— 人格什么都干不了。",
        "plugins:",
        "  enabled:",
    ] + [f"    - {name}" for name in enabled] + [""]
    return "\n".join(lines), pending


def collect_fallback_pending(fallback: Dict[str, Any]) -> List[str]:
    """⑤ fallback 不生成运行时文件（还没有落点），只报待定项。

    不生成不等于不检查：这四项空着的时候，助手会自己编一套说法 —— 实测过两次，
    模型把系统拦截说成「远端数据服务异常」。所以要显式点名。
    """
    out = []
    for key in ("on_uncertain", "on_timeout", "refusal_script", "escalation"):
        if _require(fallback, key, "fallback.yaml") is None:
            out.append(f"fallback.{key} 待定")
    return out


# ---------------------------------------------------------------------------
# manifest —— 「不许手改」的强制点
# ---------------------------------------------------------------------------

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(files: Dict[str, str], src: Path,
                   mocks: Optional[List[str]] = None) -> str:
    """记录每个生成物的 hash 和它来自哪个声明。

    ``assemble_check.py`` 校验这份 manifest。对不上 = 有人手改了生成物 =
    绕过了审批 = 不允许部署。这才是 §3.3「不允许手改」的落点。
    """
    body = {
        "_comment": "由 build_profile.py 生成。装配期检查会校验下面的 hash；"
                    "对不上说明生成物被手改过，不允许部署。",
        "source_dir": str(src),
        # mock 值也进 manifest：assemble_check 读它，好在部署前把「这些值没人确认过」
        # 摆出来。只写在 yaml 注释里的话，装配期检查看不见，报告里也不会有。
        "mock_fields": list(mocks or []),
        # .env 只 hash 分界线以上的部分，见 ENV_MARKER。
        "files": {name: {"sha256": sha256(split_env(text)[0] if name == ".env" else text)}
                  for name, text in sorted(files.items())},
    }
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------

def build(src: Path, runtime: Path) -> Tuple[Dict[str, str], List[str], List[str]]:
    """返回 (文件名 → 内容, 待定项)。不写盘 —— 写盘由调用方决定，好做 --check。"""
    decl = load_declarations(src)

    soul = render_soul(decl["persona"])
    registry, p_reg = render_registry(decl["facts"])
    policy, p_pol = render_policy(decl["authorization"])
    env, p_env = render_env(decl["authorization"], runtime)
    config, p_cfg = render_config(decl["skills"], decl["authorization"].get("tools") or [])
    p_fb = collect_fallback_pending(decl["fallback"])

    files = {
        "SOUL.md": soul,
        "bi_registry.json": registry,
        ".env": env,
        "config.yaml": config,
    }
    if policy:
        files["action_policy.json"] = policy

    pending = p_reg + p_pol + p_env + p_cfg + p_fb
    return files, pending, collect_mock_fields(decl)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="从五个声明文件生成运行时 profile")
    ap.add_argument("source", help="声明目录（五个 yaml + approvals.json）")
    ap.add_argument("runtime", help="运行时目录")
    ap.add_argument("--check", action="store_true",
                    help="只比对，不写盘。生成物与声明不一致时退出码 1")
    args = ap.parse_args(argv[1:])

    src, runtime = Path(args.source).resolve(), Path(args.runtime).resolve()
    if not src.is_dir():
        print(f"声明目录不存在：{src}", file=sys.stderr)
        return 2

    try:
        files, pending, mocks = build(src, runtime)
    except DeclError as exc:
        print(f"声明有问题，未生成任何文件：\n  {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"生成器自身出错：{exc}", file=sys.stderr)
        return 2

    manifest = build_manifest(files, src, mocks)

    if args.check:
        drift = []
        for name, text in sorted(files.items()):
            cur = runtime / name
            if not cur.exists():
                drift.append(f"{name}：不存在")
            else:
                actual = cur.read_text(encoding="utf-8")
                if name == ".env":
                    actual, tail = split_env(actual)
                    text = split_env(text)[0]
                    bad = env_tail_violations(tail)
                    if bad:
                        drift.append(f".env：分界线以下出现了本该由声明决定的键："
                                     f"{'、'.join(bad)} —— 那是在绕过审批")
                if actual != text:
                    drift.append(f"{name}：与声明生成的结果不一致（被手改过，或声明变了没重新生成）")
        mpath = runtime / MANIFEST
        if not mpath.exists():
            drift.append(f"{MANIFEST}：不存在")
        elif mpath.read_text(encoding="utf-8") != manifest:
            drift.append(f"{MANIFEST}：与声明对不上")
        for d in drift:
            print(f"  [✗] {d}")
        print(f"\n结论：{'有漂移 ✗' if drift else '生成物与声明一致 ✓'}")
        _report_pending(pending)
        _report_mocks(mocks)
        return 1 if drift else 0

    runtime.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(files.items()):
        if name == ".env" and (runtime / name).exists():
            # 重新生成时**保留部署方那半截** —— 否则每次 regen 都会把凭据抹掉，
            # 而抹掉之后的表现是"模型调不通"，跟门禁一点关系都没有，很难查。
            _, tail = split_env((runtime / name).read_text(encoding="utf-8"))
            if tail.strip():
                text = text + tail
                print(f"  [✓] {name}（保留了部署方维护的 {len(tail.strip().splitlines())} 行）")
                (runtime / name).write_text(text, encoding="utf-8")
                continue
        (runtime / name).write_text(text, encoding="utf-8")
        print(f"  [✓] {name}")
    (runtime / MANIFEST).write_text(manifest, encoding="utf-8")
    print(f"  [✓] {MANIFEST}（装配期检查按它校验有没有被手改）")

    ap_src, ap_dst = src / "approvals.json", runtime / "approvals.json"
    if ap_src.exists():
        ap_dst.write_text(ap_src.read_text(encoding="utf-8"), encoding="utf-8")
        print("  [✓] approvals.json（原样拷贝，不参与 hash —— 签字改了不算生成物漂移）")
    else:
        print("  [!] 声明目录里没有 approvals.json —— 装配期检查会判不允许部署")

    _report_pending(pending)
    _report_mocks(mocks)
    return 0


def _report_mocks(mocks: List[str]) -> None:
    if not mocks:
        return
    print(f"\nmock 值 {len(mocks)} 项（**已在跑，但没有人确认过**）：")
    for m in mocks:
        print(f"  [~] {m}")
    print("mock 比待定更需要盯：待定是空的、一眼看得出；mock 有值，看起来像定了。")


def _report_pending(pending: List[str]) -> None:
    if not pending:
        return
    print(f"\n待定项 {len(pending)} 条（**不是错误**，但这些约束此刻不生效）：")
    for p in pending:
        print(f"  [ ] {p}")
    print("待定和空着不是一回事：待定是有人知道它没定，空着是没人知道。")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
