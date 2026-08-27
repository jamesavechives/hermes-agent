#!/usr/bin/env bash
# 装配期检查的强制点 —— 检查不过就不启动。
#
# 为什么需要一个包装器，而不是「记得先跑一下 assemble_check」
# ----------------------------------------------------------
# 这套门禁一路在纠正同一件事：写在文档里、清单里、配置里给人看的规矩，
# 不是强制；强制只能做在**必经的路径**上。装配期检查也一样 —— 放在
# CI 里、写在 DEPLOY.md 里，都属于「看的人照做才生效」。真正的强制是让
# 启动这件事本身必须穿过它。
#
# 所以启动命令改成：
#     preflight.sh /data/profiles/bi -- hermes chat
# 而不是直接 hermes chat。检查不过就 exec 不到后面那半截。
#
# CI 与本脚本的分工
# -----------------
#   CI        检查器本身没写坏、样例声明合法      每次 push
#   本脚本    这台机器上这份真声明合法            每次启动
# CI 检的是代码，它验不了生产上那份 profile —— 那份根本不在仓库里。
# 两个都要，谁也替不了谁。
#
# 退出码：沿用 assemble_check.py（1 = 有检查未通过，2 = 检查器自身出错）。
# 两种都不启动：「检查器坏了」和「声明不合法」在后果上没区别，都是
# 「我们不知道这份声明合不合法」，而查不了的东西不算安全。
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
用法: preflight.sh <profile 目录> [--skip-runtime] -- <启动命令...>

例:
  preflight.sh /data/profiles/bi -- hermes chat
  preflight.sh /data/profiles/bi --skip-runtime -- python -m something

环境变量:
  BI_PREFLIGHT_PYTHON   跑检查器用的解释器，默认 <仓库>/.venv/bin/python
EOF
  exit 2
}

PROFILE=""
SKIP_RUNTIME=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --) shift; break ;;
    --skip-runtime) SKIP_RUNTIME=(--skip-runtime); shift ;;
    -h|--help) usage ;;
    *) [[ -n "$PROFILE" ]] && usage; PROFILE="$1"; shift ;;
  esac
done
[[ -n "$PROFILE" && $# -gt 0 ]] || usage

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PYTHON="${BI_PREFLIGHT_PYTHON:-$REPO/.venv/bin/python}"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

echo "── 装配期检查（不过不启动）─────────────────────────────"
set +e
PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON" "$HERE/assemble_check.py" "$PROFILE" "${SKIP_RUNTIME[@]+"${SKIP_RUNTIME[@]}"}"
CODE=$?
set -e

if [[ $CODE -ne 0 ]]; then
  echo "────────────────────────────────────────────────────────" >&2
  if [[ $CODE -eq 2 ]]; then
    echo "装配期检查器自身出错（退出码 2）—— 不启动。" >&2
    echo "「检查器坏了」和「声明不合法」在后果上是一回事：都不知道这份声明合不合法。" >&2
  else
    echo "装配期检查未通过 —— 不启动。" >&2
    echo "修好上面标 ✗ 的项再来。标 ? 的是查不了，不拦启动，但也不代表通过。" >&2
  fi
  exit $CODE
fi

echo "── 检查通过，启动：$* ─────────────────────────────────"
exec "$@"
