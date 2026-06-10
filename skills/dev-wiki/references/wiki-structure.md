# Dev Wiki — Full Reference

## Wiki Structure

```
<project-wiki>/
  AGENTS.md                      # LLM agent config (read every session)
  wiki/
    CLAUDE_CODE_INSTRUCTIONS.md  # Session protocol + critical rules
    DEVELOPMENT_PLAN.md          # All tasks with statuses
    SPRINT.md                    # Current sprint (active/done/next)
    DECISIONS.md                 # Architecture Decision Records
    index.md                     # Catalog of all pages
    log.md                       # Chronological append-only log
    overview.md                  # Living synthesis of the project
    entities/                    # Companies, products, people
    concepts/                    # Technical/business concepts
    sources/                     # Summaries of ingested documents
  raw/                           # Original documents (read-only)
```

## File Purposes

| File | Purpose | Updated When |
|------|---------|-------------|
| `SPRINT.md` | What is being worked on NOW | Every session |
| `DEVELOPMENT_PLAN.md` | ALL tasks with status | Task completed/added |
| `DECISIONS.md` | WHY we built X this way | Architectural choice made |
| `log.md` | WHAT happened WHEN | Every session (append-only) |
| `learnings` source page | Gotchas and patterns | New pattern discovered |
| `overview.md` | Big picture synthesis | Major milestone |
| `index.md` | Page catalog | New page created |

## Status Markers

```
Done:        DONE (YYYY-MM-DD)
In Progress: IN PROGRESS
Planned:     PLANNED
Backlog:     BACKLOG
Blocked:     BLOCKED (reason)
```

## Log Entry Formats

```
## [YYYY-MM-DD] session | Description of work done
## [YYYY-MM-DD] fix | Bug description and solution
## [YYYY-MM-DD] deploy | What was deployed + verification
## [YYYY-MM-DD] feature | New feature + status
## [YYYY-MM-DD] ingest | Document title
## [YYYY-MM-DD] plan | Sprint N description
## [YYYY-MM-DD] decision | ADR title
## [YYYY-MM-DD] lint | Issues found
```

## ADR Format

```markdown
## ADR-NNN | YYYY | Title
**Status:** Accepted / Rejected / Superseded

**Context:** What problem are we solving?
**Decision:** What did we choose?
**Alternatives:** What else was considered?
**Consequences:** What are the trade-offs?
```

## CONCEIVE Mode — Discovery Interview Questions

1. **What problem does it solve?** One sentence.
2. **Who is the target user?** Be specific.
3. **What exists today?** Competitors, workarounds.
4. **What's the unique angle?** Why switch?
5. **What's the business model?** SaaS, one-time, freemium?
6. **What tech stack?** Or leave open.
7. **What's the MVP scope?** v1 vs later.

## Concept Document Template

```markdown
# [Project Name] — Concept

## Problem Statement (1 paragraph)
## Target Users (personas with needs)
## Competitive Landscape (table)
## Core Features — MVP (numbered with priority)
## Features — Post-MVP (backlog)
## Architecture Overview (tech stack, data flow)
## Data Model (key entities and relationships)
## Business Model (pricing, monetization)
## Risk Assessment (technical, market, regulatory)
## Timeline Estimate (phases with hours/weeks)
```

## Page Templates

### Entity Page (`wiki/entities/`)
```yaml
---
type: entity
tags: []
created: YYYY-MM-DD
last_updated: YYYY-MM-DD
source_count: 0
---
```

### Concept Page (`wiki/concepts/`)
```yaml
---
type: concept
tags: []
created: YYYY-MM-DD
last_updated: YYYY-MM-DD
source_count: 0
---
```

### Source Page (`wiki/sources/`)
```yaml
---
type: source
tags: []
created: YYYY-MM-DD
last_updated: YYYY-MM-DD
original_file: raw/...
---
```
