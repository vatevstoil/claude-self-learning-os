#!/usr/bin/env python3
"""agentic_os_dashboard.py — The Agentic OS observability + control layer (Gap 3).

Chase/Jack's "step 3": one place to SEE everything (health, ROI, cost, the domain
backbone, dreaming actions) AND trigger automations with a click.

Two modes:
    --build   Generate a static HTML dashboard from all signal files, open browser.
              Read-only. Buttons become copy-to-clipboard commands.
    --serve   Run a localhost-only server (127.0.0.1) that renders the dashboard
              live AND executes WHITELISTED automations on button click. Safe:
              buttons send an action KEY → fixed argv (no arbitrary commands, no
              user input in the shell).

Security model:
    * Binds 127.0.0.1 only (never 0.0.0.0).
    * ACTIONS is a fixed allow-list; the browser sends a key, never a command.
    * subprocess called with an argv list (no shell=True).

Usage:
    python agentic_os_dashboard.py --build
    python agentic_os_dashboard.py --serve [--port 8723]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CLAUDE = Path.home() / ".claude"
SCRIPTS = CLAUDE / "scripts"
LOGS = CLAUDE / "logs"
REPORTS = CLAUDE / "reports"
META = Path(r"{{WIKI_PATH}}\_meta")
OUT_HTML = REPORTS / "agentic-os-dashboard.html"

PYEXE = sys.executable or "python"

# Whitelisted actions: key -> (label, argv). NO user input ever reaches the shell.
ACTIONS = {
    "daily":     ("Run daily cycle",      [PYEXE, str(SCRIPTS / "automation_dispatcher.py"), "daily"]),
    "weekly":    ("Run weekly cycle",     [PYEXE, str(SCRIPTS / "automation_dispatcher.py"), "weekly"]),
    "dreaming":  ("Run dreaming",         [PYEXE, str(SCRIPTS / "automation_dispatcher.py"), "dreaming"]),
    "registry":  ("Refresh registry",     [PYEXE, str(SCRIPTS / "agentic_os_registry.py")]),
    "roi":       ("Recompute ROI",        [PYEXE, str(SCRIPTS / "roi_tracker.py"), "--days", "30"]),
    "graphify":  ("Build missing graphs", [PYEXE, str(SCRIPTS / "auto_graphify.py")]),
    "health":    ("Health check",         [PYEXE, str(SCRIPTS / "selfreg_monitor.py")]),
    "freshness": ("Wiki freshness",       [PYEXE, str(SCRIPTS / "wiki_freshness_check.py"), "--threshold", "14"]),
}


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default if default is not None else {}


def collect_state() -> dict:
    health = _load(LOGS / "selfreg-health.json")
    roi = _load(LOGS / "roi.json")
    freshness = _load(LOGS / "freshness.json")
    registry = _load(META / "domain-registry.json")
    graphq = _load(LOGS / "graphify-queue.json")
    dreaming = _load(REPORTS / "dreaming-latest.json")
    # trend: last 10 health snapshots
    trend = []
    hist = LOGS / "selfreg-history.jsonl"
    if hist.exists():
        for line in hist.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]:
            try:
                d = json.loads(line)
                trend.append({"date": d.get("date"), "overall": d.get("overall")})
            except Exception:
                pass
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "health": health, "roi": roi, "freshness": freshness,
        "registry": registry, "graphq": graphq, "dreaming": dreaming, "trend": trend,
        "actions": {k: v[0] for k, v in ACTIONS.items()},
    }


HTML = r"""<!DOCTYPE html>
<html lang="bg"><head><meta charset="utf-8"><title>Agentic OS</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--ac:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:20px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:0 0 12px}
.big{font-size:30px;font-weight:700}.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #21262d}
.row:last-child{border:0}.mut{color:var(--mut)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.ac{color:var(--ac)}
.grade{display:inline-block;width:46px;height:46px;line-height:46px;text-align:center;border-radius:50%;font-size:22px;font-weight:700}
.pill{display:inline-block;background:#21262d;border-radius:12px;padding:2px 9px;margin:2px 3px 2px 0;font-size:12px}
.act{display:flex;flex-wrap:wrap;gap:8px}
button{background:#21262d;color:var(--fg);border:1px solid var(--bd);border-radius:7px;padding:9px 13px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--ac);color:var(--ac)}button:disabled{opacity:.5;cursor:wait}
.lev li{margin:6px 0}.spark{display:flex;align-items:flex-end;gap:3px;height:44px}
.spark div{flex:1;background:var(--ac);border-radius:2px 2px 0 0;min-height:3px}
pre{background:#010409;border:1px solid var(--bd);border-radius:7px;padding:11px;overflow:auto;max-height:240px;font-size:12px;white-space:pre-wrap}
.full{grid-column:1/-1}.dom{margin:8px 0}.dom b{color:var(--ac)}
#out{margin-top:14px;display:none}
</style></head><body><div class="wrap">
<h1>⚡ Agentic OS</h1><div class="sub" id="gen"></div>
<div class="grid" id="cards"></div>
<div class="card full" id="outcard" style="display:none"><h2>Action output</h2><pre id="out"></pre></div>
</div>
<script>
const S = __STATE__;
const SERVE = __SERVE__;
const $ = (h)=>{const d=document.createElement('div');d.innerHTML=h;return d.firstElementChild};
document.getElementById('gen').textContent = 'Generated '+S.generated+(SERVE?' · live (localhost)':' · static snapshot');
const gradeColor=(g)=>({A:'var(--ok)',B:'var(--ok)',C:'var(--warn)',D:'var(--bad)',F:'var(--bad)'}[g]||'var(--mut)');
const cards=document.getElementById('cards');
function card(title,inner,full){const c=$(`<div class="card ${full?'full':''}"><h2>${title}</h2>${inner}</div>`);cards.appendChild(c);}

// Health
const h=S.health||{};const g=h.grade||'?';
card('Self-Regulation Health',
 `<div style="display:flex;align-items:center;gap:14px">
   <span class="grade" style="background:${gradeColor(g)}22;color:${gradeColor(g)}">${g}</span>
   <div><div class="big">${h.overall??'?'}<span class="mut" style="font-size:14px">/100</span></div>
   <div class="mut">${(h.components?Object.entries(h.components).map(([k,v])=>k+' '+v).join(' · '):'')}</div></div></div>`);

// ROI + cost
const r=S.roi||{};const dc=(S.dreaming&&S.dreaming.cost)||{};
card('ROI & Cost',
 `<div class="row"><span class="mut">Time saved (${r.window_days||'?'}d)</span><b>${r.time_saved_hours??'?'}h</b></div>
  <div class="row"><span class="mut">Value</span><b class="ok">${r.currency||'$'} ${r.value_of_time??'?'}</b></div>
  <div class="row"><span class="mut">Net ROI</span><b class="ok">${r.currency||'$'} ${r.net_roi??'?'} (${r.roi_multiple??'?'}x)</b></div>
  <div class="row"><span class="mut">API-equiv cost (7d)</span><b>${dc.total_cost_usd!=null?'$'+dc.total_cost_usd:'?'}</b></div>
  <div class="row"><span class="mut">Cache-hit</span><b>${dc.cache_hit_pct!=null?dc.cache_hit_pct+'%':'?'}</b></div>`);

// Dreaming high-leverage
const dl=(S.dreaming&&S.dreaming.high_leverage)||[];
card('⚡ High-Leverage Actions',
 dl.length?`<ul class="lev">${dl.map(a=>`<li>${a}</li>`).join('')}</ul>`:'<div class="mut">Run dreaming to populate</div>',true);

// Memory / freshness
const f=S.freshness||{};const gq=(S.graphq&&S.graphq.queued_for_llm)||[];
card('Memory Health',
 `<div class="row"><span class="mut">Wikis stale</span><b>${f.stale_count??'?'}/${f.total??'?'}</b></div>
  <div class="row"><span class="mut">Graphs need enrichment</span><b>${gq.length}</b></div>
  ${gq.length?'<div style="margin-top:8px">'+gq.map(q=>`<span class="pill">${q.project||q}</span>`).join('')+'</div>':''}`);

// Trend sparkline
const t=S.trend||[];
card('Health Trend',
 t.length?`<div class="spark">${t.map(x=>`<div style="height:${Math.max(3,(x.overall||0))}%" title="${x.date}: ${x.overall}"></div>`).join('')}</div>
  <div class="mut" style="margin-top:6px">${t.length} snapshots · latest ${t[t.length-1].overall}</div>`:'<div class="mut">No trend yet</div>');

// Domain registry (backbone)
const reg=S.registry||{};const doms=reg.domains||{};const st=reg.stats||{};
let regHtml=`<div class="mut" style="margin-bottom:8px">${st.skills||0} skills · ${st.commands||0} cmds · ${st.automations||0} autos · ${st.active_projects||0} projects</div>`;
for(const[d,data]of Object.entries(doms)){
  const n=(data.skills||[]).length+(data.commands||[]).length;
  if(!n&&!(data.projects||[]).length)continue;
  regHtml+=`<div class="dom"><b>${d}</b> <span class="mut">${(data.projects||[]).join(', ')}</span><br>
   ${(data.skills||[]).map(s=>`<span class="pill">${s}</span>`).join('')}</div>`;
}
card('Domain Registry (backbone)',regHtml,true);

// Automation candidates (the codify-next loop, from dreaming)
const cand=(reg.automation_candidates||[]);
if(cand.length){
  const rows=cand.slice(0,8).map(c=>`<div class="row"><span>${c.action} <span class="mut">${c.count}×</span></span>`+
    `<b class="${c.covered?'ok':'warn'}">${c.covered?'covered':'codify →'}</b></div>`).join('');
  card('🔁 Automation Candidates',rows+'<div class="mut" style="margin-top:8px">Repeated work → skill (skill-creator)</div>',true);
}

// Action buttons
const acts=S.actions||{};
let btns='<div class="act">';
for(const[k,label]of Object.entries(acts)){btns+=`<button data-k="${k}">${label}</button>`;}
btns+='</div>';
card('Controls'+(SERVE?'':' (copy command)'),btns,true);

document.querySelectorAll('button[data-k]').forEach(b=>{
  b.onclick=async()=>{
    const k=b.dataset.k;
    const oc=document.getElementById('outcard'),out=document.getElementById('out');
    oc.style.display='block';out.style.display='block';
    if(!SERVE){out.textContent='Static mode — run in terminal:\n  python ~/.claude/scripts/'+
      ({daily:'automation_dispatcher.py daily',weekly:'automation_dispatcher.py weekly',dreaming:'automation_dispatcher.py dreaming',
        registry:'agentic_os_registry.py',roi:'roi_tracker.py --days 30',graphify:'auto_graphify.py',
        health:'selfreg_monitor.py',freshness:'wiki_freshness_check.py --threshold 14'}[k]||k);return;}
    const orig=b.textContent;b.disabled=true;b.textContent='Running…';out.textContent='Running '+k+'…';
    try{const res=await fetch('/run?action='+encodeURIComponent(k),{method:'POST'});
      const txt=await res.text();out.textContent=txt;}
    catch(e){out.textContent='Error: '+e;}
    b.disabled=false;b.textContent=orig;
  };
});
</script></body></html>"""


def render(serve: bool) -> str:
    state = collect_state()
    # Escape </ and <!-- so embedded JSON can never break out of the <script> tag
    payload = json.dumps(state, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    return (HTML.replace("__STATE__", payload)
                .replace("__SERVE__", "true" if serve else "false"))


def build():
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render(serve=False), encoding="utf-8")
    print(f"Dashboard: {OUT_HTML}")
    try:
        webbrowser.open(OUT_HTML.as_uri())
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/index.html"):
            self._send(200, render(serve=True))
        else:
            self._send(404, "not found")

    def _csrf_ok(self) -> bool:
        """Block CSRF / DNS-rebinding: reject cross-origin POSTs and foreign Host
        headers. A same-origin fetch from our own page passes; a malicious website
        in the user's browser cannot forge these."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            o = urlparse(origin).hostname
            if o not in ("127.0.0.1", "localhost"):
                return False
        return True

    def do_POST(self):
        if not self._csrf_ok():
            self._send(403, "forbidden (cross-origin)")
            return
        q = parse_qs(urlparse(self.path).query)
        action = (q.get("action", [""])[0])
        if urlparse(self.path).path != "/run" or action not in ACTIONS:
            self._send(400, "unknown action")
            return
        label, argv = ACTIONS[action]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=900,
                               encoding="utf-8", errors="replace")
            out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
            out = f"$ {label}\n(exit {p.returncode})\n\n{out.strip() or '(no output)'}"
            self._send(200, out, "text/plain; charset=utf-8")
        except subprocess.TimeoutExpired:
            self._send(200, f"{label}: timed out (still running in background)", "text/plain; charset=utf-8")
        except Exception as e:
            self._send(500, f"error: {e}", "text/plain; charset=utf-8")


def serve(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Agentic OS dashboard live at {url} (Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Agentic OS dashboard")
    ap.add_argument("--build", action="store_true", help="Generate static HTML and open it")
    ap.add_argument("--serve", action="store_true", help="Run localhost server with live buttons")
    ap.add_argument("--port", type=int, default=8723)
    args = ap.parse_args()
    if args.serve:
        serve(args.port)
    else:
        build()


if __name__ == "__main__":
    main()
