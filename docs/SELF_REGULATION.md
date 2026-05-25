---
type: guide
tags: [domain::meta, project::all, status::active, cross-project, self-regulation]
created: 2026-05-21
last_updated: 2026-05-21
applies_to: [project::all]
cross_refs: [[[HOW_THIS_ECOSYSTEM_WORKS]], [[MASTER_INDEX]], [[PROJECT_REGISTRY]]]
---

# Self-Regulation Layer

> How the ecosystem maintains itself **without manual intervention**.
> Built 2026-05-21. This document IS part of the system — keep it accurate.

---

## The closed loop (sense → decide → act → verify)

The old system had **sensors but no actuators** — it detected staleness and
suggested promotions, but nothing acted, so 12/13 projects went stale and
promotion suggestions piled up unreviewed. This layer closes the loop.

```
SENSE    wiki_freshness_check.py   → freshness.json (which active wikis are stale)
         wiki_lint.py              → wiki-lint.json (contract compliance)
         learning_promoter.py      → promotions-pending.md (cross-project patterns)

DECIDE   promotion_auto.py         → confidence-gated tiering

ACT      Tier 1 (auto): conf>=0.75 AND projects>=3 → apply with backup+dedup+provenance
         Tier 2 (queue): everything else → stays in promotions-pending.md
         super_graph_regen.py      → refresh inventory (preserve curated edges)

VERIFY   every auto-write is backed up (_shared/.backups/, *.bak-*)
         dedup guard prevents double-writes
         SessionStart surfaces Tier 2 + stale alerts to the human

MONITOR  selfreg_monitor.py → grades the regulator itself (cron/runs/errors/
         freshness/lint/hygiene), tracks trend, flags regressions. "Watch the watchers."

BORIS    stage3_dreaming attributes corrections per project → boris-candidates.json.
         SessionStart nudges (≥4 corrections) "consider a CLAUDE.md rule" for the
         active project — semi-automating the error→rule loop.
```

---

## Meta-monitor (who watches the watchers)

`selfreg_monitor.py` runs at the end of every daily AND weekly cycle. It grades
the self-regulation layer 0-100 across 5 components and records a trend line so
**silent degradation is caught early**:

| Component | Weight | Checks |
|-----------|--------|--------|
| cron | 20% | all 3 scheduled tasks registered & enabled |
| runs | 15% | daily ran ≤48h ago, weekly ≤8d ago |
| errors | 25% | no real script failures in last weekly block (freshness rc=1 is semantic, ignored) |
| freshness | 13% | share of active wikis that are fresh |
| lint | 7% | average wiki contract compliance |
| **hygiene** | **20%** | **deps on PATH (yt-dlp/ffmpeg/node/git/python) · no token leak in settings.json · dashboard CSRF guard present** |

The **hygiene** component (added after a critical audit) catches *regressions* the
other checks miss: a dependency dropping off PATH (would silently break the watch
skill), a secret leaking back into settings.json permissions, or the dashboard
CSRF guard being removed. On first run it immediately caught 2 leftover JWT tokens
in cached curl commands — proof the check earns its weight.

Outputs: `selfreg-health.json` (snapshot), `selfreg-history.jsonl` (trend),
`selfreg-digest.txt` (compact). **SessionStart only alerts when grade ≤ C or a
regression is detected** — zero token cost when healthy. Baseline: **A (93/100)**.

---

## Autonomy tiers

| Tier | What | Autonomy |
|------|------|----------|
| **0** | freshness check, registry sync, super_graph regen, lint | Fully auto (mechanical, reversible) |
| **1** | promote pattern in 3+ projects, confidence ≥ 0.75 | Auto + backup + provenance + dedup |
| **2** | 2-project / lower-confidence patterns | Queued → surfaced at SessionStart |
| **3** | dreaming analysis, EV recommendations | Insight only, never auto-acts |

Tune Tier-1 gate: `promotion_auto.py --min-confidence 0.75 --min-projects 3`.

---

## Schedule (Windows Task Scheduler)

| Task | When | Runs |
|------|------|------|
| `ClaudeAutomation_Daily` | daily 09:00 | freshness check |
| `ClaudeAutomation_Weekly` | weekly 10:00 | freshness · learning_promoter · **wiki_lint** · **promotion_auto** · **super_graph_regen** · cross_project_promoter · pinecone cleanup |
| `ClaudeAutomation_Dreaming` | Sat 11:00 | stage3_dreaming · skills_audit |

Entry point: `automation_dispatcher.py {daily|weekly|dreaming}` (never raises; always exit 0).

---

## Signal-quality fixes (why the loop now produces clean output)

1. **Freshness future-date clamp** — `check_log` used `max()` of ALL dates incl.
   future deadlines (e.g. tax date "2026-08-15"), producing negative ages that
   made stale projects look fresh. Now ignores dates > today and prefers
   `## [YYYY-MM-DD]` log headers.
2. **Promoter label stripping** — matched template scaffolding (`**Result:**`,
   `**Status:**`) as if it were lesson content, producing garbage like a
   "Result:" promotion. Now strips leading bold labels, drops structural-label
   stopwords, enforces a min-content gate, and scores confidence from REAL
   cross-project jaccard, not just counts.
3. **Dedup guard** — `promotion_apply` appended even if the entry already
   existed (caused the static-routes double-block). Now checks before writing.
4. **Status awareness** — dormant projects (Video, WiFi, GDEnergie, MiroFish)
   are no longer nagged as "stale"; missing-wiki projects (InvestPro,
   VatevWebsite, Filipoffwines) are a separate "needs scaffolding" concern.

---

## Project status (source of truth: `_shared/wiki-map.json` → metadata)

- **active** (held to wiki contract): Fakturka.bg, StroyOffice, CasinoScore,
  Cinemind, Higgsfield, Reed, Autoagency
- **dormant** (intentionally frozen): Video, WiFi, GDEnergie, MiroFish
- **missing-wiki** (code exists, no wiki): InvestPro, VatevWebsite, Filipoffwines

---

## Wiki contract (enforced by wiki_lint.py for active wikis)

1. nav hub — `index.md` or `overview.md`
2. session entry — `COMPACT_SNAPSHOT.md` or `POCKET_GUIDE.md` (token-cheap start)
3. learnings — `sources/learnings.md` or `PLAYBOOK.md`
4. activity log — `wiki/log.md`
5. frontmatter on nav hub  *(current ecosystem gap — index files lack YAML frontmatter)*

Compliance report: `~/.claude/logs/wiki-lint.json`. Current avg: ~80%.

---

## Scripts (all in `~/.claude/scripts/`)

| Script | Role |
|--------|------|
| `wiki_freshness_check.py` | SENSE — stale active wikis (status-aware, future-date safe) |
| `wiki_lint.py` | SENSE — wiki contract compliance |
| `learning_promoter.py` | SENSE — cross-project pattern detection (clean signal) |
| `promotion_auto.py` | DECIDE+ACT — confidence-gated auto-promotion |
| `promotion_apply.py` | ACT — apply one candidate (dedup + backup + provenance) |
| `knowledge_sync.py` | ACT — continuous L4 sync: embed new/changed learnings/decisions/_shared/_meta into Pinecone (incremental, content-hash manifest) |
| `auto_graphify.py` | ACT — build MISSING graphs structurally; queue stale for LLM (never overwrites quality) |
| `super_graph_regen.py` | ACT — refresh super_graph inventory, preserve curated edges |
| `selfreg_monitor.py` | MONITOR — grade the regulator, track trend, flag regressions |
| `automation_dispatcher.py` | ORCHESTRATE — daily/weekly/dreaming entry point |

---

## Auto-graphify (knowledge graphs)

Full graphify quality (critical_rules, semantic clusters) is an LLM task, so
`auto_graphify.py` does only the SAFE deterministic part automatically:

- **Missing graph** (active project) → builds a structural graph from code
  (stack + entry point + router/dir/script clusters). `critical_rules` left
  empty — never fabricated. Pure gain.
- **Existing graph** → compares graph date vs newest source mtime. Overwrites
  NOTHING. If code is newer, or the graph is a thin structural one, it is
  **queued for LLM enrichment** (`graphify-queue.json`), surfaced at SessionStart
  for that project as: `🧠 <project> graph needs enrichment — run /graphify`.

This is why staleness here is smarter than the calendar-age freshness check: a
40-day-old graph for code that hasn't changed is still fresh; only real drift
or missing graphs trigger action.

---

## What still needs a human

- **Tier 2 promotions** — review `~/.claude/logs/promotions-pending.md`, then
  `promotion_apply.py --candidate N` or `--skip N`.
- **Missing-wiki scaffolding** — run `dev-wiki/scripts/init_dev_wiki.py` for
  InvestPro / VatevWebsite / Filipoffwines if they become active.
- **COMPACT_SNAPSHOT drafts** — Autoagency/CasinoScore/Reed snapshots are marked
  `status: draft`; review and promote to `status: active`.
- **Semantic patterns** — lexical jaccard catches reworded duplicates poorly;
  the dreaming pass + periodic agent audits catch the semantic ones.
