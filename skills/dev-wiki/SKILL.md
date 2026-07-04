---
name: dev-wiki
description: "LLM-maintained development wiki for software projects. Two modes: CONCEIVE (develop software concept from idea to architecture) and TRACK (manage ongoing development with sprints, decisions, deployments, learnings). Creates and maintains an Obsidian-compatible wiki that accumulates project knowledge across sessions. Triggers on: 'dev wiki', 'wiki', 'init wiki', 'start project', 'new project', 'project wiki', 'knowledge base', 'track development', 'development plan', 'sprint planning', or when starting a new software project. ⛔ NOT: за sync на СЪЩЕСТВУВАЩ codebase wiki от source (ползвай wiki-sync)."
---

# Dev Wiki

Persistent, structured wiki maintained by the LLM for any software project. Two modes:

- **CONCEIVE** — Develop software concept (idea to architecture to plan)
- **TRACK** — Manage ongoing development (sprints, bugs, deploys, decisions)

For full structure, templates, and formats: read `references/wiki-structure.md`.

## Init New Project

```bash
python scripts/init_dev_wiki.py "<wiki_path>" "<project_name>" [--lang bg|en]
```

Creates: AGENTS.md, SPRINT.md, DEVELOPMENT_PLAN.md, DECISIONS.md, log.md, overview.md, index.md + directories.

## CONCEIVE Mode

1. **Discovery interview** — ask: problem, user, competitors, angle, business model, tech stack, MVP scope (details in `references/wiki-structure.md`)
2. **Generate concept document** — problem, users, features, architecture, model, risks, timeline
3. **Init wiki** — run init script
4. **Populate** — transfer concept into overview.md, DEVELOPMENT_PLAN.md, SPRINT.md, DECISIONS.md

## TRACK Mode — Session Protocol

**Start** (skip for trivial tasks):
1. `SPRINT.md` — active tasks
2. `sources/learnings.md` — gotchas (if exists)
3. `log.md` — last ~30 lines

**End** (after meaningful work):
1. Mark status in `DEVELOPMENT_PLAN.md`
2. Update `SPRINT.md`
3. Append to `log.md`
4. New gotcha → learnings. Universal → `_shared/patterns.md`
5. Architecture decision → `DECISIONS.md` as ADR

## Workflows

| Workflow | Key actions |
|----------|------------|
| **FEATURE** | Read spec → add to plan → SPRINT "In Progress" → implement → DONE + log |
| **BUGFIX** | Add to section H in plan → learnings if new pattern → log |
| **DEPLOY** | Log with verification results → update SPRINT completed |
| **INGEST** | Read raw → create source page → update concepts/entities → index + log |
| **PLAN** | Read priority matrix → select 3-5 tasks → update SPRINT → log |
| **LINT** | Check stale tasks, drift, orphans, outdated claims |

## Principles

1. LLM writes the wiki, human directs.
2. Never modify `raw/`.
3. `log.md` is append-only.
4. Every change updates `log.md`.
5. Learnings grow monotonically.
6. Prefer updating over creating.
