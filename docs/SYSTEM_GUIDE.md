---
type: guide
tags: [domain::meta, project::all, status::active, system-guide, handoff, agentic-os, self-learning]
created: 2026-05-25
last_updated: 2026-05-25
cross_refs: [[[AGENTIC_OS]], [[SELF_REGULATION]], [[HOW_THIS_ECOSYSTEM_WORKS]], [[MASTER_INDEX]], [[AGENTIC_OS_REGISTRY]]]
---

# System Guide — Self-Learning Agentic OS (read this first)

> The single end-to-end description of how this whole ecosystem works. Written so a
> new person (or a future session after `/clear`) can understand and run it without
> re-discovery. Deep detail lives in [[AGENTIC_OS]] and [[SELF_REGULATION]]; this is
> the map that connects everything.

---

## 1. What this is (in one paragraph)

A **self-learning Agentic OS** built on top of Claude Code. It turns daily work into
durable, reusable knowledge: every project keeps a wiki + knowledge graph + learnings;
a memory layer (Pinecone) gives cross-session recall; and a set of scheduled scripts
**autonomously** keeps it all fresh, promotes patterns, builds graphs, watches its own
health, and surfaces nudges — so the system gets smarter over time with minimal manual
effort. It runs on Windows via Task Scheduler; the brain is Claude Code.

---

## 2. Mental model — three stacked systems

```
┌─ AGENTIC OS ───────── what the system can DO ─────────────────────┐
│  domains → tasks → skills → automations  (agentic_os_registry)     │
│  observability dashboard (agentic_os_dashboard.py + agentic-os.bat)│
│  ROI tracking (roi_tracker) · dreaming 7/8 dims (stage3_dreaming)  │
├─ 4-LAYER MEMORY ───── what the system KNOWS ──────────────────────┤
│  L4 Pinecone     cross-session recall (419+ curated entries)       │
│  L3 Obsidian     wikis: concepts, summaries, learnings, ADRs       │
│  L2b GitNexus    "what breaks if I change X" (impact analysis)     │
│  L2a Graphify    knowledge_graph.json (~70x token savings)         │
│  L1 CLAUDE.md    per-session window + /clear discipline            │
├─ SELF-REGULATION ──── how it stays HEALTHY (the immune system) ───┤
│  detect (freshness/lint) → act (promote/graphify/sync) →           │
│  verify (backups/dedup) → MONITOR (selfreg_monitor grades itself)  │
└────────────────────────────────────────────────────────────────────┘
```

**Loop:** `Sense → Decide → Act → Verify → Monitor` + a **Boris loop** (corrections →
rule candidates). Nothing is destructive without a backup; the monitor grades the
regulator itself ("who watches the watchers").

---

## 3. The autonomous pipeline (Windows Task Scheduler)

Three scheduled tasks run `automation_dispatcher.py` (under `pythonw.exe`, UTF-8 forced):

| Task | When | Runs |
|------|------|------|
| **ClaudeAutomation_Daily** | 09:00 | `wiki_freshness_check` → `selfreg_monitor` |
| **ClaudeAutomation_Weekly** | Sun 10:00 | freshness → `learning_promoter` → `wiki_lint` → `promotion_auto` → `auto_graphify` → `super_graph_regen` → `cross_project_promoter` → `ai_video_check_new` → `pinecone_cleanup_expired` → **`knowledge_sync`** → `agentic_os_registry` → `roi_tracker` → `selfreg_monitor` |
| **ClaudeAutomation_Dreaming** | Sat 11:00 | `stage3_dreaming` (7-day) → `skills_audit` |

Plus two **hooks** (in `~/.claude/settings.json`):
- **Stop hook** → `auto_pinecone_save.py` — saves session learnings to Pinecone (secret-guarded, per-project namespace).
- **SessionStart hook** → `session_start_brief.py` — emits ONLY relevant alerts (pending promotions, stale wiki, graph-needs-enrichment, degraded health, Boris nudges). Token-conscious (0-40 tokens).

Manual override / catch-up: `python ~/.claude/scripts/automation_dispatcher.py {daily|weekly|dreaming}`.

---

## 4. Scripts reference (`~/.claude/scripts/`)

| Script | Role |
|--------|------|
| `automation_dispatcher.py` | ORCHESTRATE — entry point; runs each step in a subprocess, never throws, logs to `logs/automation.log` |
| `wiki_freshness_check.py` | SENSE — flag wikis/graphs not updated in >14d |
| `learning_promoter.py` | DECIDE — find gotchas in 2+ projects → promotion candidates |
| `promotion_auto.py` | ACT — confidence-gated: auto-promote (≥0.8) / queue for review |
| `cross_project_promoter.py` | DECIDE — cross-project links + orphan `_shared` detection |
| `wiki_lint.py` | SENSE — wiki contract compliance (frontmatter, structure) score |
| `auto_graphify.py` | ACT — build MISSING graphs structurally; queue real drift (>14d) for LLM; never overwrites hand-made graphs |
| `super_graph_regen.py` | ACT — refresh ecosystem super-graph (preserves curated edges) |
| `knowledge_sync.py` | ACT — **continuous L4 sync**: embed new/changed learnings/decisions/_shared/_meta into Pinecone (incremental) |
| `bulk_files_import.py` | TOOL — embed flat .md → Pinecone (section-chunked ≤1400 chars, content-hash manifest) |
| `wiki_bulk_import.py` | TOOL — embed a wiki's concepts/summaries → Pinecone (idempotent) |
| `ns_util.py` | TOOL — Pinecone namespace sanitizer (Cyrillic→ASCII: Клошар→Kloshar) |
| `agentic_os_registry.py` | ACT — auto-discover skills/commands/automations/projects → domain backbone + automation candidates |
| `roi_tracker.py` | MONITOR — value of time saved by automations (config: `roi-config.json`) |
| `stage3_dreaming.py` | LEARN — 7/8-dimension weekly analysis → 4 high-leverage actions + `boris-candidates.json` |
| `skills_audit.py` | MONITOR — skills compliance/health audit |
| `selfreg_monitor.py` | MONITOR — grades the regulator (cron/runs/errors/freshness/lint/**hygiene**); trend + regressions |
| `auto_pinecone_save.py` | CAPTURE — Stop-hook session-learning saver |
| `session_start_brief.py` | SURFACE — SessionStart alerts (incl. Boris nudges) |
| `pinecone.py` | TOOL — CLI: `save|query|list|delete <ns>` (auto-sanitizes ns) |
| `agentic_os_dashboard.py` | OBSERVE — localhost control panel (`--serve` / `--build`) |

Signal files in `~/.claude/logs/`: `automation.log`, `freshness.json`, `selfreg-health.json`,
`selfreg-history.jsonl`, `roi.json`, `graphify-queue.json`, `boris-candidates.json`,
`promotions-pending.md`, `bulk_import_manifest.json`.

---

## 5. How to operate it (day to day)

- **See everything:** double-click `~/.claude/agentic-os.bat` → http://127.0.0.1:8723 (health, ROI, cost, registry, dreaming actions, automation candidates; click to run automations). Static snapshot: `agentic_os_dashboard.py --build`.
- **Recall knowledge:** `/recall <query>` or `python ~/.claude/scripts/pinecone.py query <namespace> "<question>"`.
- **Read a project fast:** open its `{{WIKI_PATH}}\<Project>\graph\knowledge_graph.json` (critical_rules first) before scanning source.
- **Before changing code:** GitNexus `impact({target, minConf:0.8})` (1 call ≈ 500 tk vs ~50k grepping). Use the `fix-flow` skill for the full safe-fix loop.
- **Health at a glance:** `cat ~/.claude/logs/selfreg-digest.txt` (or SessionStart alerts when degraded).

---

## 6. Onboard a NEW project (propagation checklist)

1. Add to `{{WIKI_PATH}}\_shared\wiki-map.json` → `mapping` (code-folder → wiki name) + `metadata` (`status: active`, `stack`). Cyrillic name? It auto-sanitizes for Pinecone (Клошар→Kloshar).
2. Scaffold the wiki: `python ~/.claude/skills/dev-wiki/scripts/init_dev_wiki.py` (or `four-layer-init` skill).
3. Add a project `CLAUDE.md` with: Knowledge-Graph RAG pointer + **Self-Learning Loop (Boris) section**.
4. Build the graph: `/graphify` (or let `auto_graphify` scaffold a structural one).
5. The weekly pipeline now covers it automatically: freshness, `knowledge_sync` (learnings→L4), registry, dreaming, monitor.

The whole stack auto-propagates — the only manual steps are the wiki-map entry + the CLAUDE.md self-learn section.

---

## 7. Memory layers & namespaces (Pinecone)

- Index `memory` (1024-dim, multilingual-e5-large). Plan: Builder ($20/mo, 10M embed tokens/mo).
- **Embedding budget rule:** never bulk-embed raw transcripts (~60M tokens). Embed only CURATED content (summaries, concepts, learnings, patterns) — the full ecosystem is ~1M tokens.
- Namespaces (ASCII): per project (`Fakturka.bg`, `StroyOffice`, `Kloshar`…), per research wiki (`Trading`, `ClaudeCode`, `AI-Video`…), plus `_shared` and `_meta`.
- Saves AND recall go through `pinecone.py` / the import scripts → all use `ns_util.sanitize_ns` so they hit the same namespace.

---

## 8. Safety, troubleshooting, restore

- Every auto-write is backed up (`*.bak-*`, `~/.claude/backups/`). Dedup guards prevent double-writes.
- **Restore guide:** `~/.claude/backups/WIKI_SELFREG_RESTORE.md` (full file list + rollback).
- Scripts never raise (dispatcher always exits 0 so the scheduler stays green).
- If health degrades, `selfreg_monitor` flags it at SessionStart (grade ≤ C / regression / hygiene issue like a dep off PATH or a secret leaked into settings.json).
- Common gotchas: model-switch / pause >1h breaks the prompt cache ([[Prompt-Caching-Session-Limits]]); Cyrillic Pinecone namespaces need sanitizing (handled); e5-large only embeds ~512 tokens (chunks capped at 1400 chars).

---

## 9. Handoff notes (for a new person)

To run this system you need: Claude Code (paid plan), Python 3.11, the `~/.claude/`
folder (scripts + skills + settings), the Obsidian vaults at `{{WIKI_PATH}}` &
`{{RESEARCH_PATH}}`, a Pinecone account (Builder plan), and the 3 Windows scheduled
tasks. Start here: read this guide → [[AGENTIC_OS]] (capabilities) → [[SELF_REGULATION]]
(the immune system) → open the dashboard (`agentic-os.bat`). Everything is markdown +
Python; nothing is a black box. The design principle throughout: **codify behaviour so
it's repeatable, trackable, and transferable** — and let the system maintain itself,
surfacing to a human only what genuinely needs a decision.

---

*This guide is part of the ecosystem. If something here is wrong or stale, fix it.*
*Last updated 2026-05-25 — covers: Agentic OS, 4-layer memory, self-regulation, Boris loop, continuous L4 sync.*
