#!/usr/bin/env python3
"""BI 助手体验页 —— 让业务方自己点着试。

这是什么、不是什么
------------------
**是**：一个让人亲手试的入口。选个身份、选个指标、选个时间窗，看助手能查到
什么、以及**被拒时是怎么拒的** —— 拒绝理由才是这套东西真正要给业务方看的
部分，因为它决定了助手在真实场景里好不好用。

**不是**：一个正式的产品界面，也**不是一个可以对外的服务**。默认只绑
``127.0.0.1``，要对外必须显式加 ``--host``，并且那时会在页面上挂一条醒目提示。

身份：两种模式，差别很大
------------------------
门禁的规矩是「身份来自会话，不能由调用方自称」（见 identity.py）。一个网页没有
会话，所以这里有两条路，**默认是严的那条**：

**① Teleport 模式（``--teleport``，推荐）**
    把这个页面注册成 Teleport 应用之后，Teleport 会在每个请求里注入一个
    ``Teleport-Jwt-Assertion`` 头，里面是集群签发的 JWT，``sub`` 就是登录的人。
    我们**验签**（拉集群的 JWKS，ES256），验过才认。

    这是真身份，不是自称。所以这个模式下允许查真实数据。

    ⚠️ 必须验签：页面要绑 ``0.0.0.0`` 才能让 Teleport 连进来，那也意味着
    **任何能连到这个端口的人都能自己伪造一个头**。不验签的话，"Teleport 模式"
    比自称模式还危险 —— 因为它看起来像是有鉴权的。

**② 自称模式（``--allow-self-declared``）**
    页面上让人自己选身份。这就是自称，不装作不是。三条硬约束：

    1. 审计里如实记成 ``asserted_by="web-ui-selfdeclared"``、``verified=false``；
    2. **只允许查「造出来的数据」** —— 注册表的 ``data_notice`` 没标明是复刻/造数
       就直接拒绝启动。假身份配假数据是安全的组合，假身份配真数据不是；
    3. 页面最显眼的位置写清楚。

    第 2 条做在启动路径上，是强制不是提醒。

两个模式都不给的话，页面起不来 —— **没有默认身份**，和门禁「未声明按最严处理」
是同一条原则。

用法
----
    # 自称模式（本机试，只能配造出来的数据）
    ./.venv/bin/python plugins/bi-gate/webui.py --profile /data/profiles/bi \
        --allow-self-declared

    # Teleport 模式（同事都能用）
    ./.venv/bin/python plugins/bi-gate/webui.py --profile /data/profiles/bi \
        --teleport --host 0.0.0.0

本机试的时候端口转发过去看：

    tsh ssh -L 8800:127.0.0.1:8800 ubuntu@dev-frontend-Hermes
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, directory: Path):
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


#: Teleport 应用访问注入的头。名字由 Teleport 决定，不是我们能选的。
TELEPORT_JWT_HEADER = "Teleport-Jwt-Assertion"


class App:
    def __init__(self, profile: Path, *, mode: str, jwks_url: str = "",
                 audience: str = ""):
        self.profile = profile
        self.mode = mode          # "teleport" | "self-declared"
        self.jwks_url = jwks_url
        self.audience = audience
        self._jwks_client = None
        self._load_env()
        self.gate = _load("webui_bi_gate", REPO / "plugins" / "bi-gate")
        self.query = _load("webui_bi_query", REPO / "plugins" / "bi-query")
        self.gate.reload_registry()
        self.query.reload_fixtures()
        self.registry = json.loads(
            Path(os.environ["BI_GATE_REGISTRY"]).read_text(encoding="utf-8"))
        self.principals = json.loads(
            Path(os.environ.get("BI_GATE_PRINCIPAL_MAP", "/dev/null")).read_text(
                encoding="utf-8")).get("principals", {}) if os.environ.get(
            "BI_GATE_PRINCIPAL_MAP") else {}

        notice = self.registry.get("data_notice") or ""
        if self.mode == "self-declared" and not notice:
            raise SystemExit(
                "拒绝启动：注册表没有 data_notice，说明它指向的是**真实数据**。\n"
                "自称身份只允许查造出来的数据。\n"
                "（假身份配假数据是安全的；假身份配真数据不是。见模块说明。）\n"
                "要查真实数据请用 --teleport，那是验过签的真身份。")
        self.notice = notice or (
            "本页面数据来自生产数仓。身份由 Teleport 验证，每次查询都会留审计。")

    def _load_env(self) -> None:
        """读 profile 的 .env。和 Hermes 一样不做变量展开。"""
        path = self.profile / ".env"
        if not path.exists():
            raise SystemExit(f"找不到 {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()
        os.environ["HERMES_HOME"] = str(self.profile)
        # 网页是自称身份，来源记成 web_demo —— 和 human / cron / cli 分开，
        # 这样审计里能按来源筛出「哪些是体验页发起的」。
        os.environ["BI_GATE_ALLOWED_ORIGINS"] = "human"

    # ── 身份 ────────────────────────────────────────────────────────────
    def identify(self, headers: Any, body: Dict[str, Any]) -> Dict[str, Any]:
        """确定这次请求是谁发的。返回 ``{"ok":..., "subject":..., ...}``。

        Teleport 模式下**只认验过签的 JWT**，请求体里写什么都不看 —— 那正是
        "自称"和"真身份"的全部区别所在。
        """
        if self.mode == "self-declared":
            return {"ok": True, "claimed": str(body.get("principal") or ""),
                    "asserted_by": "web-ui-selfdeclared", "verified": False}

        raw = headers.get(TELEPORT_JWT_HEADER) or ""
        if not raw:
            return {"ok": False, "message":
                    f"这次请求没有带 {TELEPORT_JWT_HEADER} 头。\n"
                    f"本页面在 Teleport 模式下运行，只接受经 Teleport 访问的请求。\n"
                    f"直接连端口是不行的 —— 那样就没有身份了。"}
        try:
            claims = self._verify_jwt(raw)
        except Exception as exc:      # noqa: BLE001
            # 验签失败一律拒。**不回落到"那就当自称吧"** —— 那会让伪造一个头
            # 就能绕过整套鉴权，而且看起来还像是有鉴权的。
            return {"ok": False, "message":
                    f"Teleport 身份验签没通过：{type(exc).__name__}: {exc}\n"
                    f"不放行、也不回落成自称身份。"}
        sub = str(claims.get("sub") or "")
        if not sub:
            return {"ok": False, "message": "JWT 验过了但里面没有 sub，认不出是谁。"}
        return {"ok": True, "claimed": sub, "asserted_by": "teleport-jwt",
                "verified": True, "claims": {k: claims.get(k)
                                             for k in ("sub", "roles", "exp", "aud")}}

    def _verify_jwt(self, token: str) -> Dict[str, Any]:
        import jwt                     # PyJWT
        from jwt import PyJWKClient
        if self._jwks_client is None:
            self._jwks_client = PyJWKClient(self.jwks_url)
        key = self._jwks_client.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, key,
            algorithms=["ES256", "RS256"],
            audience=self.audience or None,
            # aud 没配就不校验受众；其余（签名、exp）照常校验。
            options={"verify_aud": bool(self.audience)},
        )

    # ── 一次查询 ────────────────────────────────────────────────────────
    def run_query(self, body: Dict[str, Any],
                  ident: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ident = ident or {"ok": True, "claimed": str(body.get("principal") or ""),
                          "asserted_by": "web-ui-selfdeclared", "verified": False}
        subject = str(ident.get("claimed") or "")
        entry = None
        for key, item in self.principals.items():
            # key / subject / aliases 都认 —— Teleport 给的是用户名，
            # 飞书给的是 open_id，同一个人挂在同一条下面。
            if (item.get("subject") == subject or key == subject
                    or subject in (item.get("aliases") or ())):
                entry, platform_id = item, key
                break
        if entry is None:
            return {"ok": False, "stage": "身份",
                    "message": f"「{subject}」不在主体名单里。\n\n"
                               f"名单由业务方维护 —— 要加人得先登记，"
                               f"助手不认没登记的身份。\n"
                               f"（如果你是通过 Teleport 登录的，"
                               f"需要把你的 Teleport 用户名加进名单的 aliases 里。）"}

        args: Dict[str, Any] = {
            "metric": body.get("metric"),
            "time_window": {"start": body.get("start"), "end": body.get("end"),
                            "timezone": body.get("timezone") or "UTC+8"},
        }
        if body.get("dimensions"):
            args["dimensions"] = list(body["dimensions"])

        from gateway.session_context import set_session_vars, clear_session_vars
        tokens = set_session_vars(platform="web_demo", user_id=platform_id,
                                  user_name=entry.get("display", ""),
                                  session_id="webui")
        try:
            verdict = self.gate._on_pre_tool_call(tool_name="query_metric",
                                                  args=dict(args))
            if verdict and verdict.get("action") == "block":
                return {"ok": False, "stage": "门禁", "message": verdict["message"],
                        "args": args}
            args.update((verdict or {}).get("args") or {})
            # 自称身份如实标注 —— 事后能和真实会话身份分开。
            p = args.get(self.gate.PRINCIPAL_ARG)
            if isinstance(p, dict):
                # 身份是怎么来的，如实写进审计。Teleport 验过签的和自称的，
                # 事后必须一眼分得开。
                p["asserted_by"] = ident.get("asserted_by", "unknown")
                p["verified"] = bool(ident.get("verified"))
            out = json.loads(self.query.handle_query_metric(args))
        finally:
            clear_session_vars(tokens)

        return {"ok": not out.get("error"), "stage": "执行", "result": out, "args": args}

    # ── 页面数据 ────────────────────────────────────────────────────────
    def catalog(self) -> Dict[str, Any]:
        metrics = []
        for m in self.registry.get("metrics", []):
            f = m.get("freshness") or {}
            metrics.append({
                "name": m["name"],
                "description": m.get("description", ""),
                "unit": m.get("unit", ""),
                "dimensions": m.get("dimensions", []),
                "table": (m.get("source") or {}).get("table", ""),
                "table_description": (m.get("source") or {}).get("table_description", ""),
                "data_start": (f.get("data_start") or "")[:10],
                "data_end": (f.get("data_end") or "")[:10],
            })
        return {
            "notice": self.notice,
            "mode": self.mode,
            "principals": [{"subject": v.get("subject"), "display": v.get("display", "")}
                           for v in self.principals.values()],
            "metrics": sorted(metrics, key=lambda m: m["name"]),
        }


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

# 用 raw 字符串：页面里 JS 的 '\n' 必须原样交给浏览器。
# 第一版用的普通字符串，Python 先把 \n 变成了真换行，塞进 JS 的单引号字符串里
# 就成了 SyntaxError —— 页面能出来，但下拉框全空，boot() 根本没跑。
PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>BI 助手体验</title>
<style>
:root{--bg:#fbfbfa;--fg:#22201d;--dim:#6b6660;--line:#e3e0da;--warn:#8a4b00;
      --warnbg:#fff6e6;--ok:#1f6f43;--bad:#a02020;--card:#fff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--dim);margin:0 0 20px}
.notice{background:var(--warnbg);border:1px solid #f0d9a8;color:var(--warn);
        border-radius:8px;padding:12px 14px;margin:0 0 22px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:18px;margin-bottom:18px}
label{display:block;font-size:13px;color:var(--dim);margin:0 0 5px}
.row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.row>div{flex:1;min-width:180px}
select,input{width:100%;padding:8px 10px;border:1px solid var(--line);
             border-radius:6px;background:#fff;font:inherit;color:inherit}
button{background:var(--fg);color:#fff;border:0;border-radius:6px;
       padding:10px 20px;font:inherit;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.hint{font-size:13px;color:var(--dim);margin-top:6px;min-height:20px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left}
th{color:var(--dim);font-weight:500;font-size:13px}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bad{border-left:3px solid var(--bad);padding-left:14px;white-space:pre-wrap}
.ok{border-left:3px solid var(--ok);padding-left:14px}
.meta{font-size:13px;color:var(--dim);margin-top:14px;white-space:pre-wrap}
.chip{display:inline-block;background:#f1efec;border-radius:4px;padding:1px 7px;
      font-size:12px;margin-right:5px}
details{margin-top:14px}summary{cursor:pointer;color:var(--dim);font-size:13px}
pre{background:#f6f5f3;border-radius:6px;padding:12px;overflow-x:auto;font-size:12px}
.scroll{overflow-x:auto}
</style>
<div class="wrap">
<h1>BI 助手体验</h1>
<p class="sub">选一个身份和指标，看它能查到什么 —— 以及被拒时是怎么拒的。</p>
<div class="notice" id="notice"></div>

<div class="card">
  <div class="row">
    <div><label>你是谁（名单由业务方维护）</label><select id="principal"></select></div>
    <div><label>时区</label><select id="tz">
      <option>UTC+8</option><option>UTC+0</option><option>UTC-5</option></select></div>
  </div>
  <div class="row">
    <div style="flex:2"><label>指标</label><select id="metric"></select>
      <div class="hint" id="metricHint"></div></div>
  </div>
  <div class="row">
    <div><label>按什么拆（可不选）</label><select id="dim"></select></div>
    <div><label>开始</label><input id="start" type="date"></div>
    <div><label>结束</label><input id="end" type="date"></div>
  </div>
  <button id="go">查询</button>
  <span class="hint" id="status" style="margin-left:12px"></span>
</div>

<div id="out"></div>
</div>
<script>
let CAT = null;
const $ = id => document.getElementById(id);

async function boot(){
  CAT = await (await fetch('api/catalog')).json();
  $('notice').textContent = CAT.notice;
  if(CAT.mode === 'teleport'){
    // 真身份模式下不给选 —— 身份来自 Teleport 的 JWT，页面上选什么都不算数。
    // 留一个不可点的框，是为了让人看见"身份不是你说了算"这件事。
    $('principal').innerHTML = '<option>由 Teleport 登录身份决定（不可选）</option>';
    $('principal').disabled = true;
    document.querySelector('label[for],#principal').previousElementSibling.textContent =
      '你是谁（来自 Teleport 登录，已验签）';
  } else {
    $('principal').innerHTML = CAT.principals
      .map(p => `<option value="${p.subject}">${p.display || p.subject}</option>`).join('');
  }
  $('metric').innerHTML = CAT.metrics
    .map(m => `<option value="${m.name}">${m.name} — ${m.description || '(无口径说明)'}</option>`).join('');
  $('metric').onchange = onMetric;
  onMetric();
}

function onMetric(){
  const m = CAT.metrics.find(x => x.name === $('metric').value);
  if(!m) return;
  $('dim').innerHTML = '<option value="">不拆，按时间看</option>' +
    m.dimensions.map(d => `<option value="${d}">${d}</option>`).join('');
  $('metricHint').innerHTML =
    `<span class="chip">${m.table_description || m.table}</span>` +
    (m.unit ? `<span class="chip">单位 ${m.unit}</span>` : '') +
    `数据范围 ${m.data_start} ~ ${m.data_end}`;
  // 默认给一个落在数据范围内的窗口，省得第一次点就被拒
  if(!$('start').value){
    const end = m.data_end, d = new Date(end);
    d.setDate(d.getDate() - 6);
    $('end').value = end;
    $('start').value = d.toISOString().slice(0,10);
  }
}

$('go').onclick = async () => {
  $('go').disabled = true; $('status').textContent = '查询中…';
  const dim = $('dim').value;
  const body = {principal:$('principal').value, metric:$('metric').value,
                start:$('start').value, end:$('end').value, timezone:$('tz').value,
                dimensions: dim ? [dim] : []};
  let r;
  try{ r = await (await fetch('api/query',{method:'POST',
        headers:{'content-type':'application/json'}, body:JSON.stringify(body)})).json(); }
  catch(e){ r = {ok:false, stage:'页面', message:String(e)}; }
  $('go').disabled = false; $('status').textContent = '';
  render(r);
};

function esc(s){ return String(s??'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function render(r){
  const out = $('out');
  if(!r.ok){
    out.innerHTML = `<div class="card"><div class="bad"><b>被拒（${esc(r.stage)}）</b>\n\n${
      esc(r.message || (r.result && r.result.error) || '未知')}</div>
      <details><summary>这次调用的参数</summary><pre>${esc(JSON.stringify(r.args, null, 2))}</pre></details>
      </div>`;
    return;
  }
  const res = r.result, rows = res.rows || [];
  const keys = rows.length ? Object.keys(rows[0]) : [];
  const meta = res.meta || {};
  let m = '';
  if(meta.note) m += '⚠️ ' + meta.note + '\n\n';
  if(meta.aggregation_caveat) m += meta.aggregation_caveat + '\n\n';
  if(meta.freshness) m += `数据范围 ${meta.freshness.data_start} ~ ${meta.freshness.data_end}`
    + (meta.freshness.etl_time ? `（数仓最后更新 ${meta.freshness.etl_time}）` : '');
  out.innerHTML = `<div class="card"><div class="ok"><b>${rows.length} 行</b>　${
      esc(meta.metric_description || '')}</div>
    <div class="scroll"><table><thead><tr>${keys.map(k=>`<th>${esc(k)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(row=>`<tr>${keys.map(k=>
      `<td class="${typeof row[k]==='number'?'num':''}">${esc(row[k])}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div>
    <div class="meta">${esc(m)}</div>
    <details><summary>门禁与后端的完整返回</summary><pre>${esc(JSON.stringify(res, null, 2))}</pre></details>
    </div>`;
}
boot();
</script>
"""


def make_server(app: App, host: str, port: int):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):   # 安静点，别刷屏
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/catalog":
                self._send(200, json.dumps(app.catalog(), ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):                 # noqa: N802
            if self.path != "/api/query":
                self._send(404, b"not found", "text/plain")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                ident = app.identify(self.headers, body)
                if not ident.get("ok"):
                    result = {"ok": False, "stage": "身份", "message": ident["message"]}
                else:
                    result = app.run_query(body, ident)
            except Exception as exc:       # noqa: BLE001
                # 体验页崩了就把栈显示出来 —— 这是给我们自己看的工具，
                # 藏起错误只会让排查更慢。
                result = {"ok": False, "stage": "体验页自身",
                          "message": f"{type(exc).__name__}: {exc}\n\n"
                                     + traceback.format_exc()[-1500:]}
            self._send(200, json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", required=True, type=Path)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认只绑回环。对外开放是一个显式的决定。")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--teleport", action="store_true",
                    help="用 Teleport 注入的 JWT 做身份（验签）。真身份，可查真数据。")
    ap.add_argument("--allow-self-declared", action="store_true",
                    help="让用户自己选身份。只允许配造出来的数据。")
    ap.add_argument("--jwks-url",
                    default="https://teleport.decodebackoffice.com/.well-known/jwks.json",
                    help="Teleport 集群的 JWKS 地址，用来验签")
    ap.add_argument("--audience", default="",
                    help="JWT 的 aud（Teleport 里就是这个应用的 URI）。不填则不校验受众")
    args = ap.parse_args(argv)

    if args.teleport == args.allow_self_declared:
        # 两个都给或都不给都不行。**没有默认身份模式** —— 和门禁「未声明按最严
        # 处理」同一条原则：漏配的后果应该是起不来，不是悄悄用一个宽松的默认值。
        raise SystemExit(
            "必须明确选一种身份模式：--teleport 或 --allow-self-declared，二选一。\n"
            "  --teleport            Teleport 验签的真身份，可查真数据\n"
            "  --allow-self-declared 用户自称，只能查造出来的数据\n"
            "不给默认值是有意的：身份这件事漏配的后果应该是起不来。")

    mode = "teleport" if args.teleport else "self-declared"
    app = App(args.profile, mode=mode, jwks_url=args.jwks_url, audience=args.audience)
    if args.host not in ("127.0.0.1", "localhost") and mode != "teleport":
        print(f"⚠️  绑在 {args.host}，而且是自称身份模式 —— "
              f"任何能连到它的人都能用，且能任选身份。\n"
              f"    只在受控内网这么做，并且确认数据是造出来的。", file=sys.stderr)
    server = make_server(app, args.host, args.port)
    print(f"BI 助手体验页：http://{args.host}:{args.port}")
    print(f"  profile   {args.profile}")
    print(f"  指标      {len(app.registry.get('metrics', []))} 个")
    print(f"  身份模式  {mode}" + ("（Teleport JWT 验签）" if mode == "teleport"
                                     else "（用户自称，只配造出来的数据）"))
    print(f"  数据性质  {app.notice[:60]}…")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
