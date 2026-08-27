#!/usr/bin/env python3
"""探针调度器 —— 把 ``probe.py`` 定期跑起来，留痕，并推指标。

为什么单独一个文件，不把这些塞进 probe.py
------------------------------------------
``probe.py`` 的价值全在它的独立性：它刻意不 import 插件本身，只驱动真实派发
路径、观察结果。往里面加网络调用、文件写入、多 profile 循环，都会削弱这一点，
而且它一旦自己挂了，就再也回答不了「门禁还在吗」。所以那个文件保持不动。

这里做三件它不该做的事：调度多个 profile、把结果落盘、把状态推成指标。

为什么用子进程跑 probe.py 而不是 import 它
------------------------------------------
因为这样跑的正是**文档里写的那条命令**。调研 §二第六条就是这么栽的：探针命令
`python -m plugins.bi_gate.probe` 在 44 个测试全绿的情况下根本执行不了，因为
测试都是按文件路径 import 的，唯独没人真的按文档敲一次。子进程方式让调度器
每次都替我们敲一遍。

顺带它还隔离了崩溃：probe.py 段错误也只是这一个 profile 报 probe_error。

为什么调度不能用 Hermes 自己的 cron
-----------------------------------
用被测系统的调度器跑对它自己的检测，等于让它决定自己什么时候被测 —— Hermes
层面一挂，探针跟着静默，而静默和「一切正常」在只推异常的通道上长得一样。
所以用系统 systemd timer / cron，与被测对象无关。

用法
----
    probe_runner.py /data/profiles/bi /data/profiles/cs

退出码：全部 alive 为 0；任一不是 alive 为 1；调度器自身出错为 2。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PROBE = HERE / "probe.py"

#: 指标写入地址。VictoriaMetrics 的 Prometheus 导入端点，形如
#: ``https://<vm>/api/v1/import/prometheus``。**没配就不推**——不报错、不重试，
#: 只在结果里标明没推。这是有意的：指标推不出去不该让探针本身变成失败。
METRICS_URL_ENV = "BI_METRICS_URL"
#: 可选的 Bearer token。
METRICS_TOKEN_ENV = "BI_METRICS_TOKEN"

#: 探针结果落盘位置。缺省写到 profile 自己的目录下。
PROBE_LOG_ENV = "BI_PROBE_LOG"


# ---------------------------------------------------------------------------
# profile 的声明
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> Dict[str, str]:
    """读 profile 的 ``.env``。

    探针是独立进程，走不到 Hermes CLI 的启动路径，所以 CLI 那边的 .env 加载
    在这里不会发生 —— 调度器得自己来。实测确认：裸进程里 invoke_hook 读不到
    profile 的 .env（2026-08-27）。

    只认最简单的 ``KEY=VALUE``：不做变量展开、不处理引号嵌套、不 source shell。
    profile 的声明应该是能被静态读懂的东西，需要执行才能得出的配置本身就是
    审批不了的。
    """
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def build_env(profile: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(load_env_file(profile / ".env"))
    env["HERMES_HOME"] = str(profile)
    # 探针要导得到 model_tools，仓库根目录必须在 PYTHONPATH 上。
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{existing}" if existing else str(REPO)
    return env


# ---------------------------------------------------------------------------
# 跑一个 profile
# ---------------------------------------------------------------------------

def run_one(profile: Path, timeout: float = 60.0) -> Dict[str, Any]:
    """跑一次探针，返回一条可落盘的记录。"""
    started = time.time()
    record: Dict[str, Any] = {
        "event": "bi_gate_probe_run",
        "source": "bi-gate-probe-runner",
        "profile": profile.name,
        "profile_path": str(profile),
        "ts": int(started),
    }
    if not profile.is_dir():
        record.update(status="probe_error", exit_code=2,
                      detail=f"profile 目录不存在：{profile}")
        return record

    try:
        proc = subprocess.run(
            [sys.executable, str(PROBE)],
            env=build_env(profile),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO),
        )
    except subprocess.TimeoutExpired:
        # 超时按门禁失效处理，不按探针出错。理由：探针卡住时我们并不知道门禁
        # 是死是活，而「不知道」在这套体系里必须当成不安全（见 UNDECIDABLE 的
        # 一贯处理方向）。
        record.update(status="gate_down", exit_code=1,
                      detail=f"探针 {timeout:.0f}s 未返回，按门禁失效处理")
        record["duration_ms"] = int((time.time() - started) * 1000)
        return record
    except Exception as exc:  # pragma: no cover - 环境级故障
        record.update(status="probe_error", exit_code=2, detail=f"拉起探针失败：{exc}")
        return record

    record["exit_code"] = proc.returncode
    record["duration_ms"] = int((time.time() - started) * 1000)

    # probe.py 把结果打成一行 JSON —— **两种结果都走 stderr**：alive 走
    # logger.info（logging 默认 handler 就是 stderr），非 alive 直接 print 到
    # stderr。所以两个流都要看，不能只看 stdout。
    # 这处是 2026-08-27 第一次把 runner 和 probe 放一起跑才发现的：两边各自
    # 都对，接缝上错了，表现是每次都报「输出解析不了，按退出码推断」——
    # 状态碰巧还是对的，所以不看细节就发现不了。
    parsed = _last_json_line(proc.stdout) or _last_json_line(proc.stderr)
    if parsed:
        record["status"] = parsed.get("status", "probe_error")
        record["detail"] = parsed.get("detail", "")
    else:
        record["status"] = {0: "alive", 1: "gate_down"}.get(proc.returncode, "probe_error")
        record["detail"] = "探针输出解析不了，状态按退出码推断"
        record["stdout_tail"] = proc.stdout[-400:]
        record["stderr_tail"] = proc.stderr[-400:]
    if proc.returncode != 0 and proc.stderr.strip():
        record["stderr_tail"] = proc.stderr[-400:]
    return record


def _last_json_line(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def probe_log_path(profile: Path) -> Path:
    override = os.environ.get(PROBE_LOG_ENV, "").strip()
    return Path(override) if override else profile / "probe.jsonl"


def write_record(profile: Path, record: Dict[str, Any]) -> bool:
    """一行一条 JSON，与门禁审计同格式。

    即便指标还没接通，这份记录也能回答「过去 N 小时探针跑了几次、几次 alive」。
    这一点很重要：接监控之前，探针不该是完全没有历史的。
    """
    path = probe_log_path(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        return True
    except Exception as exc:
        print(f"[probe-runner] 落盘失败 {path}：{exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def render_metrics(records: List[Dict[str, Any]]) -> str:
    """Prometheus 文本格式。

    只推两个：
      * ``bi_gate_probe_alive``     1=门禁在拦，0=没拦或不知道
      * ``bi_gate_probe_exit_code`` 0/1/2 —— 区分「门禁失效」和「探针自身坏了」，
        这两种的处置完全不同，混成一个告警会互相淹没

    **不推「跑了多少次」这类计数器**：告警要的是「最近有没有新数据」，那件事
    由写入时间戳天然表达，不需要我们自己维护计数。
    """
    lines = ["# TYPE bi_gate_probe_alive gauge",
             "# TYPE bi_gate_probe_exit_code gauge"]
    for r in records:
        label = f'{{profile="{r.get("profile", "")}"}}'
        lines.append(f"bi_gate_probe_alive{label} {1 if r.get('status') == 'alive' else 0}")
        lines.append(f"bi_gate_probe_exit_code{label} {int(r.get('exit_code', 2))}")
    return "\n".join(lines) + "\n"


def push_metrics(payload: str) -> Tuple[bool, str]:
    """推到 VictoriaMetrics。没配地址就跳过。

    推送失败**不影响退出码**：探针的职责是回答门禁在不在，指标推不出去是监控
    链路的问题，把它算成门禁失效会制造假警报。但失败要打到 stderr，让 timer
    的日志里看得见。
    """
    url = os.environ.get(METRICS_URL_ENV, "").strip()
    if not url:
        return False, "未配置 " + METRICS_URL_ENV
    req = urllib.request.Request(url, data=payload.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain")
    token = os.environ.get(METRICS_TOKEN_ENV, "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    profiles = [Path(a).resolve() for a in argv[1:]]
    if not profiles:
        print(__doc__.strip().splitlines()[-3], file=sys.stderr)
        print("用法: probe_runner.py <profile 目录> [...]", file=sys.stderr)
        return 2

    records = [run_one(p) for p in profiles]
    for profile, record in zip(profiles, records):
        write_record(profile, record)
        mark = {"alive": "✓", "gate_down": "✗", "probe_error": "?"}.get(record["status"], "?")
        print(f"[{mark}] {record['profile']:<12} {record['status']:<12} "
              f"exit={record.get('exit_code')}  {record.get('detail', '')}")

    ok, note = push_metrics(render_metrics(records))
    if not ok:
        print(f"[probe-runner] 指标未推送：{note}", file=sys.stderr)

    if any(r["status"] != "alive" for r in records):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
