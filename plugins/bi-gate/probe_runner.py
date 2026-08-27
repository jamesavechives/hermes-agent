#!/usr/bin/env python3
"""探针调度器 —— 把 ``probe.py`` 定期跑起来，留痕，并上报。

为什么单独一个文件，不把这些塞进 probe.py
------------------------------------------
``probe.py`` 的价值全在它的独立性：它刻意不 import 插件本身，只驱动真实派发
路径、观察结果。往里面加网络调用、文件写入、多 profile 循环，都会削弱这一点，
而且它一旦自己挂了，就再也回答不了「门禁还在吗」。所以那个文件保持不动。

这里做三件它不该做的事：调度多个 profile、把结果落盘、把结果上报到遥测。

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

#: 遥测汇聚点。VictoriaLogs 的 JSON Lines 写入端点，形如
#: ``https://<vlogs>/insert/jsonline``（查询串由本模块补，操作者只需给基址）。
#:
#: 为什么是日志而不是指标：2026-08-27 实测，dev 机在 K8s 集群外，
#: VictoriaMetrics 只有集群内 service 地址、没有对外 ingress，扫了 19 个候选
#: 域名都不通；VictoriaLogs 有 ingress 且可写、无需鉴权。而「长时间没有数据」
#: 这件事在 Grafana 里由告警规则的 No Data 状态表达，日志侧同样做得到 ——
#: 原先「指标才好告警」的顾虑不成立，为它去动网络拓扑不划算。
#:
#: **没配就不推**：不报错、不重试，只在 stderr 说一句。这是有意的——上报失败是
#: 遥测链路的问题，把它算成门禁失效会制造假警报。
SINK_URL_ENV = "BI_PROBE_SINK_URL"
#: 可选的 Bearer token（当前 dev 环境不需要）。
SINK_TOKEN_ENV = "BI_PROBE_SINK_TOKEN"

#: 日志流的 app 标签。Grafana 那边按它筛。
SINK_APP = "bi-gate-probe"

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

def render_sink_payload(records: List[Dict[str, Any]]) -> str:
    """JSON Lines —— 一行一条，就是落盘的那条记录本身。

    落盘和上报用**同一份数据**，不维护两套格式：两套格式意味着两处可能漂移，
    而事后对账时「盘上写的」和「报上去的」对不上是最难查的一类问题。

    只补两个纯路由字段：``app``（Grafana 按它筛）和 ``time``（VictoriaLogs 的
    时间字段要 RFC3339，而记录里的 ``ts`` 是整数 epoch，留着好做算术）。
    """
    lines = []
    for r in records:
        doc = dict(r)
        doc["app"] = SINK_APP
        doc["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.get("ts") or time.time()))
        lines.append(json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(lines) + "\n"


def _sink_url() -> Optional[str]:
    """补齐 VictoriaLogs 需要的查询串。

    操作者只需要在配置里给基址。参数由代码补，是因为这几个字段名是**我们和
    Grafana 查询之间的契约**——写在配置里的话，改一处忘一处就会出现「数据进去
    了但查不到」，那种故障和「没数据」在告警上长得一模一样。
    """
    raw = os.environ.get(SINK_URL_ENV, "").strip()
    if not raw:
        return None
    if "?" in raw:  # 操作者显式给了参数就照他的来
        return raw
    return raw + "?_stream_fields=app,profile&_msg_field=detail&_time_field=time"


def push_records(payload: str) -> Tuple[bool, str]:
    """上报。没配地址就跳过，失败不影响退出码。"""
    url = _sink_url()
    if not url:
        return False, "未配置 " + SINK_URL_ENV
    req = urllib.request.Request(url, data=payload.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/stream+json")
    token = os.environ.get(SINK_TOKEN_ENV, "").strip()
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

    ok, note = push_records(render_sink_payload(records))
    if not ok:
        print(f"[probe-runner] 遥测未上报：{note}", file=sys.stderr)

    if any(r["status"] != "alive" for r in records):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
