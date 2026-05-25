# Dev Wiki — LLM-Maintained Development Knowledge Base

A system where the LLM builds and maintains a persistent wiki for your software projects. You direct the work; the LLM does all the bookkeeping — updating task statuses, logging decisions, recording gotchas, maintaining cross-references.

## The Problem

Most developers lose knowledge between sessions. You fix a tricky bug on Monday, forget the pattern by Thursday, and re-discover it next month. Meeting notes, architecture decisions, deployment gotchas — they scatter across chat histories, commit messages, and memory.

## The Solution

A structured wiki that the LLM maintains **for you**. Every session reads from it, every session writes back to it. Knowledge compounds over time instead of evaporating.

```
Session 1: Fix nginx routing bug → LLM records cause + solution in learnings
Session 2: Different bug → LLM checks learnings first, avoids known pitfalls
Session 5: New developer joins → reads learnings, avoids all previous mistakes
```

## Quick Start (5 minutes)

### 1. Install the skill

Copy the `dev-wiki/` folder to your Claude Code skills directory:

```
~/.claude/skills/dev-wiki/
```

### 2. Add to your global CLAUDE.md

Add this to `~/.claude/CLAUDE.md`:

```markdown
## Dev Wiki — Knowledge Base Protocol

Every project in `<your-projects-path>` may have a wiki in `<your-wiki-path>`.

**Session start (skip for trivial tasks — typo fix, single line):**
1. `{wiki}/wiki/SPRINT.md` — current tasks
2. `{wiki}/wiki/sources/learnings.md` — gotchas (if exists)
3. `{wiki}/wiki/log.md` — last ~30 lines

**Session end (after meaningful work):**
- Update SPRINT.md + log.md. New gotcha → learnings (universal → `_shared/patterns.md`).

**No wiki?** → suggest `python ~/.claude/skills/dev-wiki/scripts/init_dev_wiki.py`
```

### 3. Initialize your first project wiki

```bash
python ~/.claude/skills/dev-wiki/scripts/init_dev_wiki.py "/path/to/wiki" "Project Name" --lang en
```

This creates:
```
/path/to/wiki/
├── AGENTS.md                    # LLM agent config
├── wiki/
│   ├── SPRINT.md                # Current sprint
│   ├── DEVELOPMENT_PLAN.md      # All tasks + statuses
│   ├── DECISIONS.md             # Architecture Decision Records
│   ├── CLAUDE_CODE_INSTRUCTIONS.md  # Session protocol
│   ├── index.md                 # Page catalog
│   ├── log.md                   # Chronological log
│   ├── overview.md              # Project synthesis
│   ├── entities/                # Companies, products, people
│   ├── concepts/                # Technical/business concepts
│   └── sources/                 # Ingested document summaries
└── raw/                         # Original documents (read-only)
```

### 4. Start working

Open your project in Claude Code. The wiki protocol activates automatically.

---

## Two Modes

### CONCEIVE — New Project from Scratch

When you say "new project" or "start project", the LLM:

1. **Interviews you** — What problem? Who's the user? What exists? Business model?
2. **Generates a concept document** — Features, architecture, risks, timeline
3. **Initializes the wiki** — Populates SPRINT, DEVELOPMENT_PLAN, DECISIONS
4. **You start building** — The wiki tracks everything from day one

### TRACK — Ongoing Development

Every session follows this protocol:

```
START (1 minute):
  Read SPRINT.md → what are we working on?
  Read learnings.md → what gotchas exist?
  Read log.md (last 30 lines) → what happened recently?

WORK:
  Implement, fix bugs, deploy — normal development

END (30 seconds):
  Update SPRINT.md → mark progress
  Update log.md → append entry
  Update learnings.md → new gotcha (if found)
  Update DECISIONS.md → new ADR (if architectural choice made)
```

---

## The Five Key Files

| File | What it answers | Updated when |
|------|----------------|-------------|
| **SPRINT.md** | "What am I working on RIGHT NOW?" | Every session |
| **DEVELOPMENT_PLAN.md** | "What's the full roadmap?" | Task added/completed |
| **DECISIONS.md** | "WHY did we choose X over Y?" | Architecture choice made |
| **log.md** | "WHAT happened and WHEN?" | Every session (append-only) |
| **learnings.md** | "What mistakes should I never repeat?" | New gotcha discovered |

---

## Workflows

### FEATURE — Building Something New
```
1. Read spec from wiki (if exists)
2. Add to DEVELOPMENT_PLAN.md → correct section
3. Move to SPRINT.md → "In Progress"
4. Implement (following learnings conventions)
5. Mark DONE + log entry
```

### BUGFIX — Fixing a Bug
```
1. Fix the bug
2. Add to DEVELOPMENT_PLAN.md section H (Bug Fixes)
3. If new pattern → add to learnings.md
4. Log entry: "## [date] fix | description"
```

### DEPLOY — Shipping to Production
```
1. Deploy
2. Verify (curl health check)
3. Log entry: "## [date] deploy | what + verification"
4. Update SPRINT.md completed items
```

### INGEST — New Document/Research
```
1. Read the raw document
2. Create summary in wiki/sources/
3. Update affected concepts/entities pages
4. Update index.md + log.md
```

### PLAN — Sprint Planning
```
1. Review DEVELOPMENT_PLAN.md priority matrix
2. Select 3-5 tasks for the sprint
3. Update SPRINT.md
4. Log entry: "## [date] plan | Sprint N"
```

### LINT — Wiki Health Check
```
Check for:
- Stale tasks in SPRINT.md (>2 weeks without movement)
- Drift between plan and actual code
- New code conventions not in learnings
- Orphan pages without links
```

---

## Multi-Project Setup

For multiple projects, create a shared knowledge layer:

```
/wiki-root/
├── _shared/                    # Cross-project knowledge
│   ├── patterns.md             # Universal patterns (read on-demand, NOT every session)
│   ├── antipatterns.md         # Mistakes from real projects
│   └── projects.md             # Project registry
├── ProjectA/                   # Project-specific wiki
│   ├── AGENTS.md
│   └── wiki/ ...
└── ProjectB/
    ├── AGENTS.md
    └── wiki/ ...
```

**Rule:** `_shared/` is read on-demand only (saves tokens). A gotcha goes to `_shared/` only if it applies to 2+ projects.

---

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

## Status Markers

```
✅ DONE (YYYY-MM-DD)
🔄 IN PROGRESS
📋 PLANNED
💡 BACKLOG
❌ BLOCKED — reason
```

---

## Token Optimization

The system is designed to minimize token usage:

- **Session start reads ~1,400 words** (SPRINT + learnings + log tail) — about 1,800 tokens
- **Trivial tasks skip the wiki read entirely** (typo fix, single line change)
- **Reference files are on-demand** — full API list, DB models, component tree load only when needed
- **`_shared/` is never auto-loaded** — saves tokens for every session that doesn't need cross-project knowledge
- **log.md reads only the last ~30 lines** — scales forever without bloat

---

## Principles

1. **The LLM writes the wiki, you direct.** You decide what to build. The LLM does the bookkeeping.
2. **Never modify `raw/`.** Original documents stay pristine.
3. **`log.md` is append-only.** Never delete entries.
4. **Every change gets logged.** No silent modifications.
5. **Learnings grow monotonically.** New gotcha = new entry immediately.
6. **Prefer updating over creating.** If a page exists, update it.
7. **Quality over quantity.** Five good pages beat twenty shallow ones.
8. **The wiki compounds.** Each session makes it more valuable than the last.

---

## Credits

Based on the [LLM Wiki pattern](https://github.com/nickarino/LLM-wiki) — adapted for software development with sprint tracking, ADRs, deployment logging, and multi-project support.
