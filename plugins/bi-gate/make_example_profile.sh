#!/usr/bin/env bash
# 生成一个可以直接跑装配期检查的样例 profile。
#
# 用途有两个，缺一不可：
#   1. CI 里需要一份「真实存在、路径都能解析」的声明来跑 assemble_check.py。
#      拿仓库里的 *.example.json 当输入是不够的 —— 检查器读的是 profile 目录，
#      不是散落的样例文件。
#   2. 建新人格时照着抄。这也是它不放在 CI workflow 里写死的原因：写在
#      workflow 里，人建 profile 时就抄不到。
#
# 刻意不含任何密钥。模型凭据（DASHSCOPE_API_KEY 之类）由部署的人自己往
# .env 里补，不经过仓库、不经过 CI。
#
# 路径一律写绝对路径：运行时 (__init__.py::_load_registry) 是按进程 cwd 打开
# 这些文件的，相对路径会让「检查时能读到、跑起来读不到」成为可能，而那种
# 漂移的方向恰好最坏 —— 装配期放行了运行时读不到的东西。
#
# 用法：make_example_profile.sh <目标目录>
set -euo pipefail

DEST="${1:?用法: make_example_profile.sh <目标目录>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$DEST"

cat > "$DEST/.env" <<ENV
# ── 样例人格声明（不含任何密钥）────────────────────────────────
# 每一项未声明的后果都写在 plugins/bi-gate/__init__.py 的常量注释里。
# 简言之：漏声明的后果是做不了事，不是不受限。

# ② facts —— 绑哪一套受控事实层
BI_GATE_REGISTRY=$HERE/registry.example.json

# ③ tools + action_max —— 能调哪些工具、能做到哪一级动作
BI_GATE_TOOLS=query_metric
BI_GATE_ACTION_POLICY=$HERE/policy.example.json
BI_GATE_ACTION_MAX=L1

# 扫描预算：单次限额在注册表里按指标声明，这里是一次会话的累计上限。
# 一亿行这个数是随手取的，还没有业务依据 —— 见设计方案 §十一待拍板第 7 项。
BI_GATE_SESSION_SCAN_MAX=100000000

# 判定留痕。未声明则本次判定不留痕，事后无法对账。
BI_AUDIT_LOG=$DEST/audit.jsonl
ENV

cat > "$DEST/approvals.json" <<'JSON'
{
  "_comment": "样例签字。真实 profile 的 by/at/ref 必须指向真人和真 PR —— 装配期检查只验字段齐全，验不了签字是真的。",
  "authorization": {
    "by": ["技术负责人", "合规"],
    "at": "2026-08-27",
    "ref": "https://github.com/decodeex/hermes-agent/pull/0"
  },
  "facts": {
    "by": ["事实层责任人"],
    "at": "2026-08-27",
    "ref": "https://github.com/decodeex/hermes-agent/pull/0"
  }
}
JSON

cat > "$DEST/config.yaml" <<'YAML'
# 插件是 opt-in 的。两个都要：
#   bi-gate  少了它，门禁完全不存在，而且不会报任何错。
#   bi-query 少了它，门禁在、query_metric 没注册 —— 人格什么都干不了。
# 2026-08-28 在 dev 上真实踩过第二种：所有检查全绿，模型跑起来一个工具都调不到。
plugins:
  enabled:
    - bi-gate
    - bi-query
YAML

echo "样例 profile 已生成：$DEST"
