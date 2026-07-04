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
GEMINI_TASKS = CLAUDE / "gemini-tasks"
RESEARCH_BASE = Path(r"{{RESEARCH_PATH}}\General Research")
RESEARCH_TRANSCRIPTS = RESEARCH_BASE / "raw" / "transcripts"

# hook_telemetry lives alongside this script; import defensively so a missing/
# broken sibling module never breaks the dashboard (tolerant-loader pattern).
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
try:
    from hook_telemetry import summarize as _summarize_hook_telemetry
except Exception:
    _summarize_hook_telemetry = None

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
    "integrity": ("Integrity check",      [PYEXE, str(SCRIPTS / "integrity_guard.py")]),
    "abeval":    ("A/B eval",             [PYEXE, str(SCRIPTS / "ab_eval.py")]),
}


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default if default is not None else {}


def _load_hook_telemetry() -> dict:
    """Per-hook 7d aggregates (count/error_rate/p50/p95) from hook-telemetry.jsonl.

    Tolerant: missing module, missing log, or corrupt lines all degrade to {}
    (hook_telemetry.summarize() already skips corrupt lines internally).
    """
    if _summarize_hook_telemetry is None:
        return {}
    try:
        return _summarize_hook_telemetry(window_days=7)
    except Exception:
        return {}


def _find_entity_files() -> list[dict]:
    """Return [{slug, path, n_entities, n_relations}] for all entities.json files, newest first."""
    if not RESEARCH_TRANSCRIPTS.exists():
        return []
    out = []
    for p in sorted(RESEARCH_TRANSCRIPTS.glob("*.entities.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        slug = p.stem.replace(".entities", "")
        try:
            d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            ne, nr = len(d.get("entities", [])), len(d.get("relations", []))
        except Exception:
            ne, nr = 0, 0
        out.append({"slug": slug, "path": str(p), "n_entities": ne, "n_relations": nr})
    return out


def collect_state() -> dict:
    health = _load(LOGS / "selfreg-health.json")
    roi = _load(LOGS / "roi.json")
    freshness = _load(LOGS / "freshness.json")
    registry = _load(META / "domain-registry.json")
    graphq = _load(LOGS / "graphify-queue.json")
    dreaming = _load(REPORTS / "dreaming-latest.json")
    integrity = _load(LOGS / "integrity-report.json")
    abeval = _load(LOGS / "ab-eval.json")
    kpi = _load(LOGS / "outcome-kpi.json")
    incidents = _load(LOGS / "incidents.json")
    fixproposals = _load(LOGS / "fix-proposals.json")
    hook_telemetry = _load_hook_telemetry()
    # Discipline pulse (weekly producer) + Fable practice mine (dreaming
    # producer) + dispatcher health — the 2026-07-02 audit found the
    # DEGRADED canary signal reached no rendered card on either path.
    discipline = _load(LOGS / "discipline_stats.json")
    fable_mine = _load(LOGS / "fable-practice-candidates.json")
    autom_health = _load(LOGS / "health.json")
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
    # Gemini briefs: count + up to 5 newest filenames (missing dir → empty, never crash)
    gemini_briefs: dict = {"count": 0, "recent": []}
    try:
        if GEMINI_TASKS.is_dir():
            mds = sorted(GEMINI_TASKS.glob("*.md"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
            gemini_briefs = {"count": len(mds), "recent": [p.name for p in mds[:5]]}
    except Exception:
        pass
    research_files = _find_entity_files()
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "health": health, "roi": roi, "freshness": freshness,
        "registry": registry, "graphq": graphq, "dreaming": dreaming, "trend": trend,
        "integrity": integrity, "abeval": abeval, "kpi": kpi,
        "incidents": incidents, "fixproposals": fixproposals,
        "hook_telemetry": hook_telemetry,
        "discipline": discipline, "fable_mine": fable_mine,
        "autom_health": autom_health,
        "gemini_briefs": gemini_briefs,
        "research": {"files": research_files, "count": len(research_files)},
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

// Health — grade + the actual issue lines + dispatcher run status.
// The issues/status were collected but never rendered, so a DEGRADED daily
// run (e.g. the claude_auth_canary 401 streak) was invisible here.
const h=S.health||{};const g=h.grade||'?';
const issues=Object.entries(h.issues||{}).flatMap(([k,v])=>(v||[]).map(x=>String(x)));
const ah=S.autom_health||{};const ahBad=ah.status&&ah.status!=='OK';
card('Self-Regulation Health',
 `<div style="display:flex;align-items:center;gap:14px">
   <span class="grade" style="background:${gradeColor(g)}22;color:${gradeColor(g)}">${g}</span>
   <div><div class="big">${h.overall??'?'}<span class="mut" style="font-size:14px">/100</span></div>
   <div class="mut">${(h.components?Object.entries(h.components).map(([k,v])=>k+' '+v).join(' · '):'')}</div></div></div>
  ${ah.status?`<div class="row" style="margin-top:8px"><span class="mut">Dispatcher ${ah.run_type||''} ${ah.last_run||''}</span><b class="${ahBad?'bad':'ok'}">${ah.status}${(ah.failures&&ah.failures.length)?' — '+ah.failures.join(', '):''}</b></div>`:''}
  ${issues.length?issues.slice(0,5).map(i=>`<div class="row"><span class="warn" style="font-size:12px">⚠ ${i}</span></div>`).join(''):''}`);

// ROI + cost
const r=S.roi||{};const dc=(S.dreaming&&S.dreaming.cost)||{};const dcd=(S.dreaming&&S.dreaming.days)||7;
card('ROI & Cost',
 `<div class="row"><span class="mut">Time saved (${r.window_days||'?'}d)</span><b>${r.time_saved_hours??'?'}h</b></div>
  <div class="row"><span class="mut">Value</span><b class="ok">${r.currency||'$'} ${r.value_of_time??'?'}</b></div>
  <div class="row"><span class="mut">Net ROI</span><b class="ok">${r.currency||'$'} ${r.net_roi??'?'} (${r.roi_multiple??'?'}x)</b></div>
  <div class="row"><span class="mut">API-equiv cost (${dcd}d)</span><b>${dc.total_cost_usd!=null?'$'+dc.total_cost_usd:'?'}</b></div>
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

// 🧭 Дисциплина — Fable-gap pulse (weekly discipline_analyzer) + practice mine
// (dreaming fable_practice_miner). Wired 2026-07-02: тези продуценти нямаха
// нито един рендериран консуматор.
const dt=(S.discipline||{}).target||{}, db=(S.discipline||{}).baseline||{};
const fm=S.fable_mine||{};
const drow=(l,a,b)=>`<div class="row"><span class="mut">${l}</span><b>${a??'?'}%<span class="mut"> vs Fable ${b??'?'}%</span></b></div>`;
card('🧭 Дисциплина (vs Fable 5)',
 (dt.model?`<div class="mut" style="margin-bottom:6px">${dt.model} · ${dt.sessions??'?'} сесии</div>`+
  drow('Reason before act',dt.reason_before_action_pct,db.reason_before_action_pct)+
  drow('Re-eval after result',dt.reeval_after_result_pct,db.reeval_after_result_pct)+
  drow('Real test after edit',dt.real_test_after_edit_pct,db.real_test_after_edit_pct)+
  drow('Batch multi-tool',dt.batch_multi_tool_pct,db.batch_multi_tool_pct)
  :'<div class="mut">discipline_stats.json липсва — изчаква weekly run</div>')+
 `<div class="row" style="margin-top:6px"><span class="mut">Fable practice mine</span><b>${fm.records!=null?fm.records+' records · '+Object.keys(fm.theme_hits||{}).length+' теми':'изчаква dreaming'}</b></div>`);

// Integrity guard — "broken ruler" detector; crit/high must be impossible to miss
const ig=S.integrity||{};
if(ig.counts){
  const igc=ig.counts;
  const igTotal=(igc.critical||0)+(igc.high||0)+(igc.medium||0)+(igc.low||0);
  const igSev=(igc.critical||0)+(igc.high||0);
  const igCls=igSev?'bad':(igTotal?'warn':'ok');
  const igTop=(ig.violations||[]).filter(v=>v.severity==='critical'||v.severity==='high').slice(0,3);
  card('🧮 Integrity',
   `<div class="big ${igCls}">${igTotal}<span class="mut" style="font-size:14px"> violation${igTotal===1?'':'s'}</span></div>`+
   `<div class="row"><span class="mut">critical / high</span><b class="${igSev?'bad':'ok'}">${igc.critical||0} / ${igc.high||0}</b></div>`+
   `<div class="row"><span class="mut">medium / low</span><b>${igc.medium||0} / ${igc.low||0}</b></div>`+
   (igTop.length?'<div style="margin-top:8px">'+igTop.map(v=>`<div class="mut" style="font-size:12px">⚠ ${v.check}: ${(v.detail||'').slice(0,90)}</div>`).join('')+'</div>'
    :'<div class="ok" style="margin-top:8px">✓ no critical/high</div>'));
} else { card('🧮 Integrity','<div class="mut">Run Integrity check to populate</div>'); }

// Hook telemetry — per-hook call volume/error-rate/latency over the last 7d
// (hook-telemetry.jsonl was write-only until wired here; see hook_telemetry.py)
const hkt=S.hook_telemetry||{};
const hkEntries=Object.entries(hkt).sort((a,b)=>(b[1].count||0)-(a[1].count||0));
if(hkEntries.length){
  card('🪝 Hook Telemetry (7d)',
   hkEntries.slice(0,8).map(([name,m])=>
    `<div class="row"><span class="mut">${name}</span><b>${m.count} <span class="${m.error_rate?'bad':'mut'}" style="font-size:11px">(${Math.round((m.error_rate||0)*100)}% err · p95 ${m.p95_ms}ms)</span></b></div>`
   ).join(''));
} else { card('🪝 Hook Telemetry (7d)','<div class="mut">No hook events in window</div>'); }

// A/B eval — did auto-applied rules actually reduce their target complaint rate?
const ab=S.abeval||{};const absum=ab.summary||null;
if(absum){
  const eff=absum.effectiveness_pct;
  card('🧪 A/B Eval (rules)',
   `<div class="big ${eff==null?'mut':(eff>=50?'ok':'warn')}">${eff==null?'n/a':eff+'%'}<span class="mut" style="font-size:14px"> effective</span></div>`+
   `<div class="row"><span class="mut">decided</span><b>${absum.decided||0} / ${absum.total_rules||0}</b></div>`+
   `<div class="row"><span class="ok">effective</span><b class="ok">${absum.effective||0}</b></div>`+
   `<div class="row"><span class="mut">no effect / regressed</span><b>${absum.no_effect||0} / <span class="${absum.regressed?'bad':'mut'}">${absum.regressed||0}</span></b></div>`+
   `<div class="row"><span class="mut">pending / insuff.</span><b>${absum.pending||0} / ${absum.insufficient_data||0}</b></div>`+
   `<div class="mut" style="font-size:11px;margin-top:8px">window ${ab.window_days||'?'}d · ITS — confounded, small-n</div>`);
} else { card('🧪 A/B Eval (rules)','<div class="mut">Run A/B eval to populate</div>'); }

// Outcome KPI — does the system actually LEARN? (results, not activity)
const kp=S.kpi||{};const krc=kp.repeat_corrections||{};const kre=kp.recall_engagement||{};const kaf=kp.apply_funnel||{};
if(kp.repeat_corrections||kp.apply_funnel){
  const tr={improving:'📉 improving',degrading:'📈 DEGRADING',flat:'→ flat'}[kp.trend]||'· no data';
  const rcRate=krc.rate;
  card('📈 Outcome KPI',
   `<div class="row"><span class="mut">Repeat corrections</span><b>${krc.repeats??'?'}/${krc.total??'?'}${rcRate!=null?' ('+Math.round(rcRate*100)+'%)':''}</b></div>`+
   `<div class="row"><span class="mut">Trend</span><b>${tr}</b></div>`+
   (kre.surfaced!=null?`<div class="row"><span class="mut">Recall usefulness</span><b>${kre.engaged??'?'}/${kre.surfaced} (${Math.round((kre.rate||0)*100)}%)</b></div>`:'')+
   `<div class="row"><span class="mut">Applied (30d)</span><b>${kaf.applied_30d||0}</b></div>`+
   `<div class="row"><span class="mut">Open incidents</span><b class="${kaf.open_incidents?'warn':'ok'}">${kaf.open_incidents||0}</b></div>`);
} else { card('📈 Outcome KPI','<div class="mut">Run daily/weekly cycle to populate</div>'); }

// Incidents — repeated user corrections are unresolved bugs (strongest pain signal)
const esc=(s)=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const projShort=(p)=>(p||'?').replace('J--Antigraviti-','').replace('j--Antigraviti-','').replace('J--Obsidian-Resurch-','');
const incd=(S.incidents&&S.incidents.open)||[];
card('🚨 Incidents',
 incd.length
  ? `<div class="big bad">${incd.length}<span class="mut" style="font-size:14px"> open</span></div>`+
    incd.slice(0,5).map(i=>`<div style="margin:8px 0 0"><div class="mut" style="font-size:12px">${esc(projShort(i.project))} ×${i.count||'?'} · since ${String(i.first_seen||'').slice(0,10)}</div><div style="font-size:13px">${esc((i.title||'').slice(0,70))}</div></div>`).join('')
  : '<div class="ok">✓ no open incidents</div>', true);

// Fix proposals — drafted, human-gated fix sessions (never auto-run)
const fps=(S.fixproposals&&S.fixproposals.proposals)||[];
const fpBy={};fps.forEach(p=>{const s=(p&&p.status)||'?';fpBy[s]=(fpBy[s]||0)+1;});
const fpProposed=fps.filter(p=>p&&p.status==='proposed');
card('🔧 Fix Proposals',
 fps.length
  ? `<div class="row"><span class="warn">proposed (awaiting review)</span><b class="${fpProposed.length?'warn':'mut'}">${fpBy.proposed||0}</b></div>`+
    `<div class="row"><span class="mut">accepted / resolved</span><b>${fpBy.accepted||0} / ${fpBy.resolved||0}</b></div>`+
    `<div class="row"><span class="mut">suppressed</span><b>${fpBy.suppressed||0}</b></div>`+
    (fpProposed.length?'<div style="margin-top:8px">'+fpProposed.slice(0,4).map(p=>`<div style="font-size:12px;margin:4px 0"><span class="warn">→</span> ${esc(projShort(p.project))}: ${esc((p.title||'').slice(0,52))}</div>`).join('')+'</div>':'')+
    `<div class="mut" style="font-size:11px;margin-top:8px">Review: python ~/.claude/scripts/incident_fix_proposer.py --list</div>`
  : '<div class="mut">No fix proposals drafted</div>', true);

// Gemini briefs — pending outbound task files
const gb=S.gemini_briefs||{};const gbCount=gb.count||0;const gbRecent=gb.recent||[];
card('📤 Gemini briefs',
 gbCount
  ? `<div class="big ${gbCount?'warn':'ok'}">${gbCount}<span class="mut" style="font-size:14px"> pending</span></div>`+
    (gbRecent.length?'<div style="margin-top:8px">'+gbRecent.map(n=>`<div class="mut" style="font-size:12px">📄 ${esc(n)}</div>`).join('')+'</div>':'')+
    `<div class="mut" style="font-size:11px;margin-top:8px">Send: python ~/.claude/scripts/gemini_dispatch.py &lt;file&gt; · or copy from ~/.claude/gemini-tasks/</div>`
  : '<div class="ok">✓ no pending Gemini briefs</div>');

// Research entity graphs
const rs=S.research||{};const rsf=rs.files||[];
card('🔬 Research Graphs',
 rsf.length
  ? `<div class="mut" style="margin-bottom:8px">${rsf.length} document${rsf.length===1?'':'s'} · <a href="/research" style="color:var(--ac)">view all</a></div>`+
    rsf.slice(0,6).map(f=>`<div style="margin:5px 0"><a href="/research/${esc(f.slug)}" style="color:var(--ac);text-decoration:none">▸ ${esc(f.slug)}</a> <span class="mut">${f.n_entities} nodes · ${f.n_relations} edges</span></div>`).join('')
  : `<div class="mut">No research documents yet — run <code>gemini_research.py</code> to create them.<br><br>Then use <a href="/research" style="color:var(--ac)">/research</a> to browse entity graphs.</div>`);

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

// Skill Launcher — interactive, grouped by domain
(function(){
  const reg2=S.registry||{};const doms2=reg2.domains||{};
  const domKeys=Object.keys(doms2);
  if(!domKeys.length){
    card('🚀 AGENTIC OS — Skill Launcher','<div class="mut">Run \'Refresh registry\' to populate</div>',true);
    return;
  }
  // clipboard helper: tries navigator.clipboard, falls back to textarea execCommand
  function copyText(text,btn){
    function doFallback(){
      try{
        const ta=document.createElement('textarea');
        ta.value=text;ta.style.cssText='position:fixed;opacity:0;top:0;left:0';
        document.body.appendChild(ta);ta.focus();ta.select();
        document.execCommand('copy');document.body.removeChild(ta);
      }catch(e){console.warn('copy fallback failed',e);}
    }
    const orig=btn.textContent;
    function confirm(){btn.textContent='✓ copied';btn.style.color='var(--ok)';setTimeout(()=>{btn.textContent=orig;btn.style.color='';},1400);}
    if(navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){
      navigator.clipboard.writeText(text).then(confirm,()=>{doFallback();confirm();});
    } else { doFallback(); confirm(); }
  }
  let launcherHtml='<div style="font-size:11px;color:var(--mut);margin-bottom:10px">Click a skill/command to copy invocation → paste in Claude</div>';
  for(const [dn,dd] of Object.entries(doms2)){
    const skills=dd.skills||[];const cmds=dd.commands||[];
    if(!skills.length&&!cmds.length)continue;
    launcherHtml+=`<div class="dom" style="margin-bottom:10px">`;
    launcherHtml+=`<b style="color:var(--ac);font-size:12px;text-transform:uppercase;letter-spacing:.5px">${esc(dn)}</b>`;
    if((dd.projects||[]).length){launcherHtml+=`<span class="mut" style="font-size:11px;margin-left:6px">${esc(dd.projects.join(', '))}</span>`;}
    launcherHtml+='<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:6px">';
    for(const s of skills){
      launcherHtml+=`<button class="sl-btn" data-copy="${esc('Use the '+s+' skill')}" title="Copy: Use the ${esc(s)} skill">${esc(s)}</button>`;
    }
    for(const c of cmds){
      const raw=c.startsWith('/')?c:'/'+c;
      launcherHtml+=`<button class="sl-btn" data-copy="${esc(raw)}" title="Copy: ${esc(raw)}" style="opacity:.85">/${esc(c.replace(/^\//,''))}</button>`;
    }
    launcherHtml+='</div></div>';
  }
  card('🚀 AGENTIC OS — Skill Launcher',launcherHtml,true);
  // attach click handlers after card is in DOM
  document.querySelectorAll('.sl-btn').forEach(b=>{
    b.onclick=function(){copyText(b.dataset.copy,b);};
  });
})();

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
        health:'selfreg_monitor.py',freshness:'wiki_freshness_check.py --threshold 14',
        integrity:'integrity_guard.py',abeval:'ab_eval.py'}[k]||k);return;}
    const orig=b.textContent;b.disabled=true;b.textContent='Running…';out.textContent='Running '+k+'…';
    try{const res=await fetch('/run?action='+encodeURIComponent(k),{method:'POST'});
      const txt=await res.text();out.textContent=txt;}
    catch(e){out.textContent='Error: '+e;}
    b.disabled=false;b.textContent=orig;
  };
});
</script></body></html>"""


ENTITY_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="bg"><head><meta charset="utf-8"><title>Entity Graph — __SLUG__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;--ok:#3fb950;--warn:#d29922;--ac:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;display:flex;height:100vh;overflow:hidden}
.sidebar{width:270px;min-width:270px;background:var(--card);border-right:1px solid var(--bd);display:flex;flex-direction:column;padding:16px;gap:10px;overflow-y:auto}
.back{color:var(--ac);text-decoration:none;font-size:12px}.back:hover{text-decoration:underline}
h2{font-size:15px;font-weight:700;word-break:break-all}
.stats{color:var(--mut);font-size:12px}
#search{background:#010409;border:1px solid var(--bd);color:var(--fg);border-radius:6px;padding:6px 10px;width:100%;font-size:13px;outline:none}
#search:focus{border-color:var(--ac)}
.tog-row{display:flex;gap:6px;flex-wrap:wrap}
.tog{background:#21262d;color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:5px 11px;cursor:pointer;font-size:12px}
.tog.active{border-color:var(--ac);color:var(--ac)}
.tog:hover{border-color:var(--ac)}
#detail{background:#010409;border:1px solid var(--bd);border-radius:6px;padding:10px;font-size:12px;min-height:80px;flex:1}
#detail h3{font-size:13px;margin-bottom:6px;color:var(--fg)}
#detail .attr{color:var(--mut);margin:3px 0}
#detail .attr b{color:var(--fg)}
#detail .rels{margin-top:8px;font-size:11px}
#detail .rel{color:var(--mut);margin:2px 0}
.canvas-area{flex:1;position:relative;overflow:hidden}
svg{width:100%;height:100%;display:block;cursor:grab}
svg.panning{cursor:grabbing}
.node-circle{stroke:#30363d;stroke-width:1.5;cursor:pointer;transition:stroke .15s}
.node-circle:hover{stroke:var(--fg);stroke-width:2}
.node-circle.selected{stroke:var(--fg);stroke-width:2.5}
.node-circle.faded{opacity:.2}
.node-label{pointer-events:none;fill:var(--fg);font-size:11px;text-anchor:middle;dominant-baseline:middle;user-select:none}
.node-label.faded{opacity:.2}
.edge-line{stroke-opacity:.55;stroke-width:1.5;marker-end:url(#arrow);cursor:pointer;transition:stroke-opacity .15s}
.edge-line:hover{stroke-opacity:1}
.edge-line.faded{stroke-opacity:.06}
.edge-label{pointer-events:none;fill:var(--mut);font-size:10px;text-anchor:middle;dominant-baseline:middle}
.edge-label.faded{opacity:.06}
</style></head>
<body>
<div class="sidebar">
  <a href="/" class="back">← Agentic OS</a>
  <h2>__SLUG__</h2>
  <div class="stats" id="stats"></div>
  <input type="search" id="search" placeholder="Search entities…">
  <div class="tog-row">
    <button class="tog active" id="btnNodes">Labels</button>
    <button class="tog active" id="btnEdges">Edges</button>
    <button class="tog" id="btnReset">⟳ Reset</button>
  </div>
  <div id="detail"><div style="color:var(--mut);margin-top:4px">Click a node to see details</div></div>
</div>
<div class="canvas-area">
<svg id="svg" xmlns="http://www.w3.org/2000/svg">
<defs>
<marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
  <polygon points="0 0, 8 3, 0 6" fill="#58a6ff" fill-opacity=".7"/>
</marker>
</defs>
<g id="root"><g id="edgesG"></g><g id="nodesG"></g></g>
</svg>
</div>
<script>
const DATA = __DATA__;
const KIND_COLOR={person:'#7b8dff',org:'#ff9650',product:'#4ac94e',term:'#bf7fff',place:'#4acccf'};
const DEFAULT_COLOR='#8b949e';

const nodes=(DATA.entities||[]).map((e,i)=>({...e,id:i,color:KIND_COLOR[e.kind]||DEFAULT_COLOR,x:0,y:0,vx:0,vy:0,fixed:false}));
const nodeIdx={};nodes.forEach(n=>nodeIdx[n.name]=n.id);
const edges=(DATA.relations||[]).map((r,i)=>{
  const si=nodeIdx[r.source],ti=nodeIdx[r.target];
  return (si!=null&&ti!=null)?{...r,i,si,ti}:null;
}).filter(Boolean);

document.getElementById('stats').textContent=nodes.length+' entities · '+edges.length+' relations';

const svg=document.getElementById('svg');
const root=document.getElementById('root');
const edgesG=document.getElementById('edgesG');
const nodesG=document.getElementById('nodesG');

let showLabels=true,showEdges=true;
let selected=null,searchTerm='';
let alpha=1,running=true;

// SVG transform state
let tx=0,ty=0,scale=1;
function applyTransform(){root.setAttribute('transform',`translate(${tx},${ty}) scale(${scale})`);}

// Build SVG elements
const edgeEls=[],edgeLabelEls=[],nodeEls=[],nodeLabelEls=[];

for(const e of edges){
  const line=document.createElementNS('http://www.w3.org/2000/svg','line');
  line.classList.add('edge-line');line.setAttribute('stroke','#58a6ff');
  edgesG.appendChild(line);edgeEls.push(line);
  const lt=document.createElementNS('http://www.w3.org/2000/svg','text');
  lt.classList.add('edge-label');lt.textContent=e.type||'';
  edgesG.appendChild(lt);edgeLabelEls.push(lt);
}

for(const n of nodes){
  const g=document.createElementNS('http://www.w3.org/2000/svg','g');
  g.style.cursor='pointer';
  const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
  c.classList.add('node-circle');c.setAttribute('r','18');c.setAttribute('fill',n.color);
  const lbl=document.createElementNS('http://www.w3.org/2000/svg','text');
  lbl.classList.add('node-label');lbl.textContent=(n.name||'').slice(0,16);
  g.appendChild(c);g.appendChild(lbl);
  g.addEventListener('click',ev=>{ev.stopPropagation();selectNode(n);});
  // drag
  let dragging=false,ox=0,oy=0;
  g.addEventListener('mousedown',ev=>{
    if(ev.button!==0)return;
    dragging=true;n.fixed=true;alpha=0.5;running=true;
    const pt=svgPoint(ev);ox=pt.x-n.x;oy=pt.y-n.y;
    ev.stopPropagation();ev.preventDefault();
  });
  window.addEventListener('mousemove',ev=>{
    if(!dragging)return;
    const pt=svgPoint(ev);n.x=pt.x-ox;n.y=pt.y-oy;drawFrame();
  });
  window.addEventListener('mouseup',()=>{if(dragging){dragging=false;n.fixed=false;}});
  nodesG.appendChild(g);nodeEls.push(g);nodeLabelEls.push(lbl);
}

svg.addEventListener('click',()=>selectNode(null));

function svgPoint(ev){
  const r=svg.getBoundingClientRect();
  return {x:(ev.clientX-r.left-tx)/scale,y:(ev.clientY-r.top-ty)/scale};
}

// Pan
let panStart=null,txStart=0,tyStart=0;
svg.addEventListener('mousedown',ev=>{
  if(ev.target!==svg&&ev.target!==root)return;
  panStart={x:ev.clientX,y:ev.clientY};txStart=tx;tyStart=ty;
  svg.classList.add('panning');
});
window.addEventListener('mousemove',ev=>{
  if(!panStart)return;
  tx=txStart+(ev.clientX-panStart.x);ty=tyStart+(ev.clientY-panStart.y);applyTransform();
});
window.addEventListener('mouseup',()=>{panStart=null;svg.classList.remove('panning');});
svg.addEventListener('wheel',ev=>{
  ev.preventDefault();
  const r=svg.getBoundingClientRect();const mx=ev.clientX-r.left,my=ev.clientY-r.top;
  const factor=ev.deltaY<0?1.1:1/1.1;
  tx=mx-(mx-tx)*factor;ty=my-(my-ty)*factor;scale*=factor;applyTransform();
},{passive:false});

// Force simulation
function initPositions(){
  const W=svg.clientWidth||800,H=svg.clientHeight||600;
  const cx=W/2,cy=H/2;
  tx=-cx*(scale-1)+0;ty=-cy*(scale-1)+0;
  const r=Math.min(W,H)*0.28;
  nodes.forEach((n,i)=>{
    const a=(i/Math.max(nodes.length,1))*2*Math.PI;
    n.x=cx+r*Math.cos(a);n.y=cy+r*Math.sin(a);n.vx=0;n.vy=0;
  });
  tx=0;ty=0;scale=1;applyTransform();
}

function tick(){
  if(!running)return;
  const W=svg.clientWidth||800,H=svg.clientHeight||600;
  const cx=W/2,cy=H/2;
  // Repulsion
  for(let i=0;i<nodes.length;i++){
    for(let j=i+1;j<nodes.length;j++){
      const dx=nodes[i].x-nodes[j].x,dy=nodes[i].y-nodes[j].y;
      const d2=Math.max(dx*dx+dy*dy,1);const d=Math.sqrt(d2);
      const f=4000/(d2);const fx=f*dx/d,fy=f*dy/d;
      if(!nodes[i].fixed){nodes[i].vx+=fx;nodes[i].vy+=fy;}
      if(!nodes[j].fixed){nodes[j].vx-=fx;nodes[j].vy-=fy;}
    }
  }
  // Spring
  for(const e of edges){
    const ni=nodes[e.si],nj=nodes[e.ti];
    const dx=nj.x-ni.x,dy=nj.y-ni.y;const d=Math.sqrt(dx*dx+dy*dy)||1;
    const rest=120;const f=(d-rest)*0.12;const fx=f*dx/d,fy=f*dy/d;
    if(!ni.fixed){ni.vx+=fx;ni.vy+=fy;}
    if(!nj.fixed){nj.vx-=fx;nj.vy-=fy;}
  }
  // Center gravity
  for(const n of nodes){
    if(n.fixed)continue;
    n.vx+=(cx-n.x)*0.015*alpha;n.vy+=(cy-n.y)*0.015*alpha;
  }
  // Damping
  for(const n of nodes){
    if(n.fixed)continue;
    n.vx*=0.78;n.vy*=0.78;n.x+=n.vx;n.y+=n.vy;
  }
  alpha*=0.975;
  if(alpha<0.005&&nodes.every(n=>!n.fixed))running=false;
}

function drawFrame(){
  for(let i=0;i<edges.length;i++){
    const e=edges[i],ni=nodes[e.si],nj=nodes[e.ti];
    const dx=nj.x-ni.x,dy=nj.y-ni.y,d=Math.sqrt(dx*dx+dy*dy)||1;
    const r=18;const ex=nj.x-dx/d*r,ey=nj.y-dy/d*r;
    const sx=ni.x+dx/d*r,sy=ni.y+dy/d*r;
    edgeEls[i].setAttribute('x1',sx);edgeEls[i].setAttribute('y1',sy);
    edgeEls[i].setAttribute('x2',ex);edgeEls[i].setAttribute('y2',ey);
    edgeLabelEls[i].setAttribute('x',(sx+ex)/2);edgeLabelEls[i].setAttribute('y',(sy+ey)/2-6);
  }
  for(let i=0;i<nodes.length;i++){
    const n=nodes[i];
    nodeEls[i].setAttribute('transform',`translate(${n.x},${n.y})`);
  }
  applyVisibility();
}

function applyVisibility(){
  const q=searchTerm.toLowerCase().trim();
  const matchSet=new Set();
  if(q){
    nodes.forEach(n=>{if((n.name||'').toLowerCase().includes(q))matchSet.add(n.id);});
    // also include neighbours
    edges.forEach(e=>{if(matchSet.has(e.si))matchSet.add(e.ti);if(matchSet.has(e.ti))matchSet.add(e.si);});
  }
  for(let i=0;i<nodes.length;i++){
    const n=nodes[i],sel=(selected&&selected.id===n.id);
    const faded=q&&!matchSet.has(n.id);
    const c=nodeEls[i].querySelector('circle');const lbl=nodeEls[i].querySelector('text');
    c.classList.toggle('faded',faded);c.classList.toggle('selected',sel);
    lbl.classList.toggle('faded',faded);lbl.style.display=showLabels?'':'none';
  }
  for(let i=0;i<edges.length;i++){
    const e=edges[i];
    const faded=q&&(!matchSet.has(e.si)||!matchSet.has(e.ti));
    edgeEls[i].classList.toggle('faded',faded);
    edgeEls[i].style.display=showEdges?'':'none';
    edgeLabelEls[i].classList.toggle('faded',faded);
    edgeLabelEls[i].style.display=(showEdges&&showLabels)?'':'none';
  }
}

function selectNode(n){
  selected=n;
  const detail=document.getElementById('detail');
  if(!n){detail.innerHTML='<div style="color:var(--mut);margin-top:4px">Click a node to see details</div>';applyVisibility();return;}
  const rels=edges.filter(e=>e.si===n.id||e.ti===n.id);
  const attrs=Object.entries(n).filter(([k])=>!['id','x','y','vx','vy','fixed','color'].includes(k));
  detail.innerHTML=`<h3>${esc(n.name||'')}</h3>`+
    attrs.map(([k,v])=>`<div class="attr"><b>${esc(k)}:</b> ${esc(String(v||''))}</div>`).join('')+
    (rels.length?`<div class="rels"><div class="attr" style="margin-top:8px;font-weight:700">Relations (${rels.length}):</div>`+
      rels.map(r=>{const other=r.si===n.id?nodes[r.ti]:nodes[r.si];const dir=r.si===n.id?'→':'←';
        return `<div class="rel">${dir} <b>${esc(r.type||'')}</b> ${esc(other?other.name:'?')}</div>`;}).join('')+'</div>':'');
  applyVisibility();
}

function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// Controls
document.getElementById('btnNodes').onclick=function(){showLabels=!showLabels;this.classList.toggle('active',showLabels);applyVisibility();};
document.getElementById('btnEdges').onclick=function(){showEdges=!showEdges;this.classList.toggle('active',showEdges);applyVisibility();};
document.getElementById('btnReset').onclick=()=>{initPositions();alpha=1;running=true;};
document.getElementById('search').addEventListener('input',e=>{searchTerm=e.target.value;applyVisibility();});

// Animation loop
function loop(){tick();drawFrame();requestAnimationFrame(loop);}

initPositions();
drawFrame();
loop();
</script>
</body></html>"""


def render_entity_viewer(slug: str) -> str | None:
    """Return HTML for the entity viewer for the given slug, or None if not found."""
    p = RESEARCH_TRANSCRIPTS / f"{slug}.entities.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        data = {}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    return (ENTITY_VIEWER_HTML
            .replace("__SLUG__", slug)
            .replace("__DATA__", payload))


RESEARCH_LIST_HTML = r"""<!DOCTYPE html>
<html lang="bg"><head><meta charset="utf-8"><title>Research — Agentic OS</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;--ac:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:32px}
a{color:var(--ac);text-decoration:none}.back{font-size:12px}.back:hover{text-decoration:underline}
h1{font-size:20px;margin:12px 0 4px}.sub{color:var(--mut);font-size:12px;margin-bottom:24px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;text-decoration:none;display:block;transition:border-color .15s}
.card:hover{border-color:var(--ac)}.card h2{font-size:15px;color:var(--ac);margin-bottom:6px}
.meta{color:var(--mut);font-size:12px}.empty{color:var(--mut);margin-top:32px}
</style></head>
<body>
<a href="/" class="back">← Agentic OS</a>
<h1>🔬 Research Entity Graphs</h1>
<div class="sub">__BASE__</div>
<div class="grid" id="grid">__CARDS__</div>
</body></html>"""


def render_research_list() -> str:
    files = _find_entity_files()
    if not files:
        cards = '<div class="empty">No research documents yet.<br>Run: <code>python ~/.claude/scripts/gemini_research.py "..." --domain web</code></div>'
    else:
        cards = "".join(
            f'<a class="card" href="/research/{f["slug"]}">'
            f'<h2>{f["slug"]}</h2>'
            f'<div class="meta">{f["n_entities"]} entities · {f["n_relations"]} relations</div>'
            f'</a>'
            for f in files
        )
    return (RESEARCH_LIST_HTML
            .replace("__BASE__", str(RESEARCH_TRANSCRIPTS))
            .replace("__CARDS__", cards))


def render(serve: bool) -> str:
    state = collect_state()
    # Escape </ and <!-- so embedded JSON can never break out of the <script> tag
    payload = json.dumps(state, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    return (HTML.replace("__STATE__", payload)
                .replace("__SERVE__", "true" if serve else "false"))


def build(no_open: bool = False):
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(render(serve=False), encoding="utf-8")
    print(f"Dashboard: {OUT_HTML}")
    if no_open:
        return
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
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, render(serve=True))
        elif path == "/research":
            self._send(200, render_research_list())
        elif path.startswith("/research/"):
            slug = path[len("/research/"):]
            # Basic sanitization: alphanumeric + dash + underscore only
            import re
            if not re.match(r'^[\w\-]+$', slug):
                self._send(400, "invalid slug")
                return
            html = render_entity_viewer(slug)
            if html is None:
                self._send(404, f"No entity file found for slug: {slug}")
            else:
                self._send(200, html)
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
    ap.add_argument("--no-open", action="store_true",
                     help="With --build: skip opening the browser (unattended/scheduled runs)")
    args = ap.parse_args()
    if args.serve:
        serve(args.port)
    else:
        build(no_open=args.no_open)


if __name__ == "__main__":
    main()
