#!/usr/bin/env python3
"""审计上报 + 对账。

为什么审计不能像探针那样直接上报
--------------------------------
探针是独立进程，慢一点无所谓。**审计是同步写在派发路径上的** —— 每次
``query_metric`` 判定都会写一行。在那条路径上做网络调用意味着：模型每次调用都
多等一个 RTT，遥测端挂了还可能把业务拖住。

所以分工不变：门禁只管落本地 JSONL（快、不依赖网络），这个独立进程定期把新增
的行推上去。和 ``probe_runner.py`` 是同一个形状。

为什么对账要在这里做，不放到 Grafana 里
----------------------------------------
对账要做的是**集合差**：执行了但没有对应放行判定的调用有哪些。用 LogsQL 表达
勉强能写，但那样判定逻辑就活在一条查询字符串里 —— 没法单测、改错了没人知道。
这套东西一路在纠正的正是这类事。所以对账在这里算，Grafana 只对结果告警。

对账在查什么
------------
门禁写「判定」，bi-query 写「执行」，两边用同一个 ``call_id``（门禁放行时通过
宿主的 modify 通道塞进工具参数）。于是：

    执行了、但没有对应的放行判定  →  **这次调用没经过门禁**。最严重的一类，
                                     §5.3 第一例（模型换工具写文件）就是靠
                                     「审计里什么都没有」才发现的。
    放行了、但没有执行            →  次要。模型放弃了、工具报错了都会这样。

**没有 call_id 的旧记录不参与对账**，单独计数报出来。把它们算成"绕过门禁"会让
告警一上线就全是假警报 —— 而假警报会直接毁掉这条告警的可信度。

对账窗口为什么要留宽限
----------------------
判定和执行是同一次调用里前后脚发生的，但上报有延迟。窗口取
``[now - window - grace, now - grace]``，跳过最近 grace 秒 —— 否则每次都会看到
一批"判定了还没执行"的调用，而它们只是还在路上。

用法
----
    audit_shipper.py /data/audit/bi.jsonl

退出码：0 正常（含对账通过）；1 对账不平；2 上报器自身出错。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 遥测汇聚点。和探针共用一套配置，见 /etc/default/bi-gate-probe。
SINK_URL_ENV = "BI_PROBE_SINK_URL"
SINK_TOKEN_ENV = "BI_PROBE_SINK_TOKEN"

SINK_APP_AUDIT = "bi-gate-audit"
SINK_APP_RECON = "bi-gate-reconcile"

#: 断点文件。存"读到哪个字节"和文件 inode —— inode 变了说明日志被轮转过，
#: 那时候必须从头读，否则会漏掉一整个文件的记录。
CHECKPOINT_SUFFIX = ".shipped"

#: 对账窗口与宽限（秒）。宽限跳过最近这段时间，见模块 docstring。
WINDOW_SECONDS = int(os.environ.get("BI_AUDIT_RECONCILE_WINDOW", "1800"))
GRACE_SECONDS = int(os.environ.get("BI_AUDIT_RECONCILE_GRACE", "300"))

RECON_OK = "ok"
RECON_MISMATCH = "mismatch"
RECON_NO_DATA = "no_data"


# ---------------------------------------------------------------------------
# 增量读
# ---------------------------------------------------------------------------

def checkpoint_path(audit: Path) -> Path:
    return audit.with_suffix(audit.suffix + CHECKPOINT_SUFFIX)


def read_checkpoint(audit: Path) -> Tuple[int, Optional[int]]:
    p = checkpoint_path(audit)
    if not p.exists():
        return 0, None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return int(d.get("offset", 0)), d.get("inode")
    except (OSError, ValueError):
        # 断点文件坏了 → 从头读。宁可重复上报，也不要漏 ——
        # 重复的记录在查询侧能靠 call_id 去重，漏掉的没人知道。
        return 0, None


def write_checkpoint(audit: Path, offset: int, inode: int) -> None:
    try:
        checkpoint_path(audit).write_text(
            json.dumps({"offset": offset, "inode": inode}), encoding="utf-8")
    except OSError as exc:
        print(f"[audit-shipper] 断点写不进去 {checkpoint_path(audit)}：{exc}", file=sys.stderr)


def read_new_lines(audit: Path) -> Tuple[List[Dict[str, Any]], int, int]:
    """从断点往后读。返回 (记录, 新 offset, inode)。"""
    st = audit.stat()
    offset, prev_inode = read_checkpoint(audit)

    if prev_inode is not None and prev_inode != st.st_ino:
        print(f"[audit-shipper] 文件 inode 变了（轮转过），从头读", file=sys.stderr)
        offset = 0
    elif offset > st.st_size:
        # 文件变短了 —— 被截断或重建。同样从头读。
        print(f"[audit-shipper] 文件比断点还短（被截断），从头读", file=sys.stderr)
        offset = 0

    out: List[Dict[str, Any]] = []
    with open(audit, "r", encoding="utf-8") as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                # 半行（正在写）或坏行。不推进 offset 到它后面会导致重复读，
                # 推进又会丢。选丢：审计行是一次 write 写完的，出现半行说明
                # 有别的问题，报出来比静默重试有用。
                print(f"[audit-shipper] 跳过读不懂的一行：{line[:120]}", file=sys.stderr)
        new_offset = fh.tell()
    return out, new_offset, st.st_ino


def read_window(audit: Path, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """读时间窗内的全部记录（对账用，不看断点）。"""
    out = []
    try:
        with open(audit, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("ts")
                if isinstance(ts, int) and start_ts <= ts < end_ts:
                    out.append(rec)
    except OSError as exc:
        print(f"[audit-shipper] 读审计文件失败：{exc}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------

def reconcile(records: List[Dict[str, Any]], start_ts: int, end_ts: int) -> Dict[str, Any]:
    """按 call_id 把「判定」和「执行」配对。"""
    passed: Dict[str, Dict[str, Any]] = {}
    executed: Dict[str, Dict[str, Any]] = {}
    legacy = 0

    for rec in records:
        cid = rec.get("call_id")
        event = rec.get("event")
        if event == "bi_gate_verdict" and rec.get("gate_result") == "passed":
            if cid:
                passed[cid] = rec
            else:
                legacy += 1
        elif event == "executed":
            if cid:
                executed[cid] = rec
            else:
                legacy += 1

    # 执行了但没有放行判定 —— 这条调用没经过门禁。最严重的一类。
    orphan_exec = sorted(set(executed) - set(passed))
    # 放行了但没执行 —— 模型放弃、工具报错都会这样，次要。
    unused_pass = sorted(set(passed) - set(executed))

    if not passed and not executed and not legacy:
        status = RECON_NO_DATA
    elif orphan_exec:
        status = RECON_MISMATCH
    else:
        status = RECON_OK

    detail = "对账通过"
    if status == RECON_MISMATCH:
        detail = (f"{len(orphan_exec)} 次执行找不到对应的门禁放行记录 —— "
                  f"这些调用可能没经过门禁：{'、'.join(orphan_exec[:5])}")
    elif status == RECON_NO_DATA:
        detail = "窗口内没有任何判定或执行记录"

    return {
        "event": "bi_gate_reconcile",
        "source": "bi-gate-audit-shipper",
        "ts": int(time.time()),
        "status": status,
        "window_start": start_ts,
        "window_end": end_ts,
        "passed": len(passed),
        "executed": len(executed),
        "paired": len(set(passed) & set(executed)),
        "executed_without_verdict": len(orphan_exec),
        "executed_without_verdict_ids": orphan_exec[:20],
        "passed_without_execution": len(unused_pass),
        # 没有 call_id 的旧记录。**不算成绕过门禁** —— 把查不了的算成出事了，
        # 会让这条告警一上线就全是假警报，而假警报直接毁掉它的可信度。
        "legacy_records": legacy,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 上报
# ---------------------------------------------------------------------------

def _sink_url(app: str) -> Optional[str]:
    raw = os.environ.get(SINK_URL_ENV, "").strip()
    if not raw:
        return None
    if "?" in raw:
        return raw
    return raw + "?_stream_fields=app,event&_msg_field=detail&_time_field=time"


def render(records: List[Dict[str, Any]], app: str) -> str:
    lines = []
    for r in records:
        doc = dict(r)
        doc["app"] = app
        doc["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(r.get("ts") or time.time()))
        doc.setdefault("detail", doc.get("gate_result") or doc.get("event") or "")
        # detail 有时是 dict（门禁的结构化补充），但 VictoriaLogs 的 _msg 要字符串。
        if not isinstance(doc["detail"], str):
            doc["detail"] = json.dumps(doc["detail"], ensure_ascii=False, default=str)
        lines.append(json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(lines) + "\n" if lines else ""


def push(payload: str, app: str) -> Tuple[bool, str]:
    if not payload:
        return True, "无新记录"
    url = _sink_url(app)
    if not url:
        return False, "未配置 " + SINK_URL_ENV
    req = urllib.request.Request(url, data=payload.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/stream+json")
    token = os.environ.get(SINK_TOKEN_ENV, "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("用法: audit_shipper.py <审计 JSONL 路径>", file=sys.stderr)
        return 2
    audit = Path(argv[1]).resolve()
    if not audit.exists():
        print(f"审计文件不存在：{audit}", file=sys.stderr)
        return 2

    try:
        new_records, offset, inode = read_new_lines(audit)
    except OSError as exc:
        print(f"读审计文件失败：{exc}", file=sys.stderr)
        return 2

    ok, note = push(render(new_records, SINK_APP_AUDIT), SINK_APP_AUDIT)
    print(f"[audit-shipper] 新增 {len(new_records)} 条，上报：{note}")
    # 只有上报成功才推进断点。失败就下次重来 —— 宁可重复，不要漏。
    if ok:
        write_checkpoint(audit, offset, inode)
    elif new_records:
        print("[audit-shipper] 上报失败，断点不推进，下次重试", file=sys.stderr)

    now = int(time.time())
    end_ts = now - GRACE_SECONDS
    start_ts = end_ts - WINDOW_SECONDS
    result = reconcile(read_window(audit, start_ts, end_ts), start_ts, end_ts)
    mark = {RECON_OK: "✓", RECON_MISMATCH: "✗", RECON_NO_DATA: "?"}[result["status"]]
    print(f"[{mark}] 对账 {result['status']}：放行 {result['passed']} / 执行 "
          f"{result['executed']} / 配上 {result['paired']}"
          + (f" / 旧记录 {result['legacy_records']}" if result["legacy_records"] else ""))
    if result["status"] != RECON_OK:
        print(f"    {result['detail']}")

    ok2, note2 = push(render([result], SINK_APP_RECON), SINK_APP_RECON)
    if not ok2:
        print(f"[audit-shipper] 对账结果未上报：{note2}", file=sys.stderr)

    return 1 if result["status"] == RECON_MISMATCH else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
