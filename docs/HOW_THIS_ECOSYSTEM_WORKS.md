---
type: guide
tags: [domain::meta, project::all, status::active, cross-project]
created: 2026-04-28
last_updated: 2026-04-28
applies_to: [project::all]
cross_refs: [[[MASTER_INDEX]], [[PROJECT_REGISTRY]], [[TAG_TAXONOMY]]]
---

# How This Ecosystem Works

> Practical guide. Read once, then keep handy. Not philosophical.

---

## 4-Layer Memory Architecture (the big picture)

Source of truth: `~/.claude/CLAUDE.md`. Brief recap:

```
L4 Pinecone      → cross-session recall (decisions, conversations, emails)
L3 Obsidian Wiki → THIS LAYER — concepts, patterns, ADRs, learnings
L2b GitNexus MCP → "what will break" — impact() before any change
L2a Graphify     → "what is the project" — knowledge_graph.json (~70x token savings)
L1 CLAUDE.md     → session window control + /clear triggers
```

The wiki is the **knowledge** layer. The other layers are session/code-state — they read from this layer when starting work.

---

## When to use `_shared/` vs project wiki vs `_meta/`

| Location | Goes here | Doesn't go here |
|----------|-----------|-----------------|
| `_meta/` | Documentation **about** the ecosystem itself: registry, taxonomy, this guide | Project content, code patterns, gotchas |
| `_shared/` | Patterns valid for **2+ projects**, bridges, universal antipatterns | Single-project gotchas (those go in the project wiki) |
| `<Project>/wiki/` | Anything specific to that project: ADRs, sprint, learnings, modules | Patterns the next project would also need (promote to `_shared/`) |

**Promotion rule:** when the same gotcha hits a 2nd project, move it from project wiki → `_shared/patterns.md` (or a new `_shared/<topic>-patterns.md` for substantial bodies of knowledge).

---

## How Pinecone, GitNexus, Graphify integrate with the wiki

### Pinecone (L4) — cross-session recall
- **Save with:** `python3 ~/.claude/scripts/pinecone.py save <NS> <id> "<summary>"`
- **Query with:** `python3 ~/.claude/scripts/pinecone.py query <NS> "<question>"`
- Existing namespaces: `Fakturka.bg`, `StroyOffice`, `Trading`. Per-project NS proposed in [[PROJECT_REGISTRY]].
- Wiki link: at session end, save a Pinecone summary that references the wiki path it came from.

### GitNexus (L2b) — impact analysis
- Before changing code: `impact({target: "<symbol>", minConf: 0.8})`
- 1 `impact()` call ≈ 500 tokens, replaces 10 Grep + Read calls (~50k tokens). Always prefer.
- Findings worth keeping → write to `<project>/wiki/sources/learnings.md`.

### Graphify (L2a) — JSON project map
- Lives at `<project>/graph/knowledge_graph.json`. 5/10 projects have one (see [[PROJECT_REGISTRY]]).
- Read **first** when entering a project, before scanning source.
- When architecture changes, update the JSON (Graphify scripts in `~/.claude/skills/graphify/`).

### Obsidian wiki (L3) — this layer
- Each project: `<project>/wiki/index.md` is the entry. SPRINT.md, DEVELOPMENT_PLAN.md, DECISIONS.md, log.md, sources/learnings.md.
- Cross-project: `_shared/`, `_meta/`.

---

## Workflow examples

### Starting work on Project X
1. Open [[MASTER_INDEX]] → find project → click wiki link.
2. Read `<project>/wiki/CLAUDE_CODE_INSTRUCTIONS.md` (if exists) and `SPRINT.md`.
3. If knowledge graph exists: read `<project>/graph/knowledge_graph.json`.
4. Glance at `sources/learnings.md` for active gotchas.
5. Run `pinecone.py query <NS> "<topic>"` if returning to a topic touched in past sessions.

### Found a gotcha — where to put it
- **Single project, novel** → `<project>/wiki/sources/learnings.md` (append, dated).
- **Hits 2nd project** → promote to `_shared/patterns.md` (or new `_shared/<topic>-patterns.md`).
- **Architectural decision** → ADR in `<project>/wiki/DECISIONS.md`.
- **Worth recall across sessions** → also save to Pinecone (with wiki path in summary).

### Looking for an existing pattern
1. Check `_shared/patterns.md` first (universal).
2. Check `_shared/antipatterns.md` for known bad ideas.
3. Check `_shared/<topic>-patterns.md` for deep-dives (e.g., `expense-extraction-patterns.md`, `invoicing-patterns.md`).
4. Check the most-similar project's `sources/learnings.md`.
5. Last resort: Pinecone query → `pinecone.py query <NS> "<keywords>"`.

### Adding a new project to the ecosystem
1. Add to `_shared/wiki-map.json` (folder → wiki name mapping).
2. Run `~/.claude/skills/dev-wiki/scripts/init_dev_wiki.py` to scaffold.
3. Update [[PROJECT_REGISTRY]] (this `_meta/` file).
4. Add tag to [[TAG_TAXONOMY]] under "Project prefix tags".
5. Decide Pinecone NS; create on first save.
6. Update [[MASTER_INDEX]] table.

---

## Self-development mechanics (NOW AUTOMATED — see [[SELF_REGULATION]] + [[AGENTIC_OS]])

These were aspirational in Apr 2026; as of May 2026 they run autonomously via
`automation_dispatcher.py` (Windows Task Scheduler: Daily 09:00 / Weekly 10:00 /
Dreaming Sat 11:00):

- **auto_pinecone_save** — Stop hook saves session summaries to Pinecone.
- **learning_promoter + promotion_auto** — scan learnings, confidence-gated auto-promote.
- **wiki_freshness_check** — daily; flags stale wikis (>14d) → SessionStart alert.
- **auto_graphify** — builds missing knowledge graphs, queues stale for LLM enrichment.
- **agentic_os_registry** — auto-discovers the domain backbone (skills/automations).
- **roi_tracker** — value of time saved by automations.
- **selfreg_monitor** — grades the regulator itself (the "immune system").
- **stage3_dreaming** — 7/8-dimension weekly self-improvement analysis.

Control panel: double-click `~/.claude/agentic-os.bat` (or `agentic_os_dashboard.py`).
Manual ops are now the exception, not the rule — the system maintains itself and
only surfaces issues at SessionStart when something is degraded.

---

## Token-cost guidance (cheapest → most expensive)

When you need information about a project, prefer in this order:

1. **POCKET_GUIDE / COMPACT_SNAPSHOT** (~500 tokens) — if the project has one (e.g., Higgsfield). Read at session start.
2. **CROSS_PROJECT_BRIDGES** (`_shared/<topic>-patterns.md`) — when working at the seam between two projects.
3. **super_graph.json** (planned, `_meta/`) — cross-project knowledge graph queries.
4. **Project knowledge_graph.json** (~2-5k tokens) — full project map.
5. **Project wiki index + targeted reads** (~5-15k tokens) — task-specific.
6. **Full-wiki scan** (50k+ tokens) — only when truly exploring.

Never grep source files when an `impact()` call or graph query answers the question. The CLAUDE.md rule is **tools first, grep last**.

---

## Maintenance schedule (informal)

- Every session end: append to project log + learnings if needed; Pinecone save.
- Weekly: skim active project `SPRINT.md` for completed-but-undocumented work.
- Monthly: run wiki freshness check; promote duplicated gotchas to `_shared/`.
- Per architectural change: update Graphify JSON + DECISIONS ADR.

---

*Last updated: 2026-04-28*
*If anything in this guide is wrong or stale, edit it. The guide is part of the ecosystem.*
