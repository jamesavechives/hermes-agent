#!/bin/bash
# 谁打开过体验页但不在名单里 —— 拿这个结果去补 principals.json
python3 - "$@" <<'PY'
import json, sys, collections, time
path = sys.argv[1] if len(sys.argv) > 1 else "/data/audit/bi.jsonl"
seen = collections.OrderedDict()
for line in open(path, encoding="utf-8"):
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("gate_result") != "rejected_unknown_principal": continue
    p = d.get("principal") or {}
    who = p.get("claimed")
    if not who: continue
    seen[who] = (p.get("asserted_by"), p.get("verified"), d.get("ts"))
if not seen:
    print("没有人被拒 —— 名单是全的（或者还没人来过）"); raise SystemExit
print(f"{len(seen)} 个身份试过但不在名单里：\n")
for who, (by, ver, ts) in seen.items():
    when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"
    mark = "✓验过" if ver else "自称"
    print(f"  {who:24s} {mark:6s} {by:24s} 最近 {when}")
print("\n加进 /data/profiles/bi/principals.json，形如：")
print('  "<飞书 open_id 或任一主标识>": {')
print('    "subject": "bi_<英文名>", "display": "<中文名>",')
print(f'    "aliases": [{", ".join(json.dumps(w, ensure_ascii=False) for w in list(seen)[:2])}]')
print("  }")
PY
