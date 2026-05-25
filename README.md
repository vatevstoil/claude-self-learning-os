# Claude Self-Learning OS

A **self-learning Agentic OS** for [Claude Code](https://docs.claude.com/claude-code) that turns your daily work into durable, compounding knowledge — maintained autonomously. Every correction you give Claude, every tool sequence you repeat, every pattern that proves useful: the system detects it, codifies it, and makes it available in the next session without manual effort.

The system runs entirely as scheduled Python scripts (Windows Task Scheduler or cron). No servers, no daemons, no LLM API calls in the automation layer — pure Python, zero extra token cost.

> Inspired by Andrej Karpathy's Obsidian-RAG idea, Chase AI's "Agentic OS", and Jack Roberts' "dreaming" concept — assembled into one working, self-maintaining system.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    LAYER 1: KNOWLEDGE & SELF-REGULATION      │
│                                                             │
│  ┌─────────────┐   ┌───────────────┐   ┌────────────────┐  │
│  │  Pinecone   │   │ Obsidian wikis│   │  Graphify JSON │  │
│  │ (L4: cross- │◄──│ (L3: concepts │◄──│  (L2a: "what   │  │
│  │  session)   │   │  & patterns)  │   │  is this proj")│  │
│  └──────▲──────┘   └───────▲───────┘   └────────────────┘  │
│         │                  │                                 │
│  knowledge_sync      auto_graphify                          │
│  (incremental,       (weekly rebuild)                       │
│   hash-manifest)                                            │
│                                                             │
│  selfreg_monitor ──► health grade (A–F) ──► alerts         │
│  Boris loop ────────► correction → rule candidate           │
│  SessionStart hook ─► top 2 suggestions + 🔮 anticipation   │
└───────────────────────────────┬─────────────────────────────┘
                                │ feeds
┌───────────────────────────────▼─────────────────────────────┐
│              LAYER 2: BRAIN-INSPIRED SELF-LEARNING OS        │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  HABIT ENGINE  (procedural memory / basal ganglia)   │  │
│  │  habit_miner → tool-sequence n-grams (IDF + reward)  │  │
│  │  habit_ledger → detected → suggested → codified       │  │
│  │  habit_to_skill → SKILL.md scaffolds on accept        │  │
│  └─────────────────────────┬────────────────────────────┘  │
│                             │                               │
│  ┌──────────────────────────▼────────────────────────────┐  │
│  │  UNIFIED ATTENTION QUEUE  (self_improvement_queue)    │  │
│  │  single ranked inbox — habits, Boris rules, dreaming  │  │
│  │  dismiss → exponential back-off                        │  │
│  │  accept  → triggers action (scaffold / patch CLAUDE)  │  │
│  └─────────────────────────┬────────────────────────────┘  │
│                             │                               │
│  ┌──────────────────────────▼────────────────────────────┐  │
│  │  HEBBIAN CONSOLIDATION  (declarative memory)          │  │
│  │  recall_tracker + hebbian_consolidation               │  │
│  │  frequently-recalled memories → extended TTL          │  │
│  │  salience.py → security/money/prod events → boost     │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  metacognition: effectiveness_tracker + anticipate          │
│  Boris draft generator: boris_draft → CLAUDE.md patches     │
└─────────────────────────────────────────────────────────────┘
```

---

## What you get

**Layer 1 — Knowledge & Self-Regulation**

- **4-layer memory** — Pinecone (cross-session recall) → Obsidian wikis → Graphify knowledge graphs → CLAUDE.md session windows
- **Autonomous pipeline** — 36 scripts, scheduled daily/weekly/dreaming; pure Python, 0 LLM tokens in the automation loop
- **Self-regulation immune system** — `selfreg_monitor` grades itself across 7 dimensions (cron health, run errors, freshness, lint, hygiene, dispatcher, queue), tracks grade trends, flags regressions before they degrade quality
- **Boris loop** — detects repeated corrections per project, surfaces rule candidates, auto-patches CLAUDE.md with backup on accept
- **SessionStart hook** — surfaces top 2 ranked suggestions + anticipation prediction at session open (0–40 tokens, never bloats context)
- **Incremental knowledge sync** — content-hash manifest ensures only changed content is re-embedded; never bulk-syncs raw transcripts

**Layer 2 — Brain-Inspired Self-Learning OS**

- **Habit Engine** — mines JSONL transcripts for frequent tool-sequence n-grams using IDF distinctiveness + reward ratio + time-gap segmentation; reduces noise from ~2961 raw sequences to ~14 high-signal items; cross-project detection with distinctiveness bonus
- **Habit graduation ladder** — detected → suggested_skill → codified; `habit_to_skill.py` scaffolds SKILL.md drafts automatically on accept
- **Hebbian consolidation** — frequently-recalled + high-salience Pinecone memories get extended TTL; `salience.py` tags security/money/prod/error events
- **Unified Attention Queue** — single ranked inbox replacing 5 separate alert systems; dismiss triggers exponential back-off, implicit auto-suppress after 5 ignores
- **Metacognition** — `effectiveness_tracker` grades which suggestions get acted on, auto-tunes promotion thresholds; `anticipate.py` predicts next routine per project
- **Boris Draft Generator** — `boris_draft.py` generates CLAUDE.md rule drafts from repeated corrections; human confirms before patching

---

## Requirements

- **Claude Code** (paid plan)
- **Python 3.11+**
- **[Pinecone](https://pinecone.io)** — Builder plan (10M embedding tokens/month; more than sufficient)
- **[Obsidian](https://obsidian.md)** vault, or any folder of markdown files
- **Git**
- Windows (Task Scheduler) or macOS/Linux (cron) — all scripts are cross-platform Python
- Optional: `ffmpeg` + `yt-dlp` for video-analysis skill; GitNexus MCP for impact analysis

---

## Quick start

```bash
git clone https://github.com/vatevstoil/claude-self-learning-os.git
cd claude-self-learning-os
```

**Step 1 — Configure paths and secrets (interactive)**

```bash
python configure.py
```

Fills all `{{PLACEHOLDERS}}` in scripts with your paths and Pinecone key. Creates `~/.claude/.env` (git-ignored).

**Step 2 — Install**

```powershell
# Windows
.\install.ps1
```

```bash
# macOS / Linux
./install.sh
```

Copies scripts and skills to `~/.claude/`, sets up the hook for SessionStart.

**Step 3 — Create your Pinecone index**

In the Pinecone console: create an index named `memory`, dimension `1024`, model `multilingual-e5-large`.

**Step 4 — Schedule the automation**

See [`config/wiki-map.example.json`](config/wiki-map.example.json) for the wiki-map format, then register the scheduled tasks:

```powershell
# Windows — install.ps1 registers the 3 Task Scheduler tasks
.\install.ps1
```

```bash
# macOS/Linux — add these to crontab (crontab -e)
0 9 * * *   python ~/.claude/scripts/automation_dispatcher.py daily
0 10 * * 0  python ~/.claude/scripts/automation_dispatcher.py weekly
0 11 * * 6  python ~/.claude/scripts/automation_dispatcher.py dreaming
```

**Step 5 — Verify**

```bash
python scripts/automation_dispatcher.py daily
```

Check `~/.claude/logs/selfreg_health.json` — should show a grade of A or B.

---

## Configuration

`configure.py` replaces these placeholders throughout all scripts:

| Placeholder | Meaning |
|---|---|
| `{{WIKI_PATH}}` | Obsidian vault for project wikis (e.g. `~/Obsidian`) |
| `{{RESEARCH_PATH}}` | Vault for research / knowledge wikis (can be same) |
| `{{CODE_PATH}}` | Root directory where your code projects live |
| `{{HOME}}` | Your home directory (`~`) |
| `{{PINECONE_API_KEY}}` | Pinecone API key (written to `.env`, never committed) |
| `{{PINECONE_INDEX_HOST}}` | Your Pinecone index host URL |

Secrets live in `~/.claude/.env`. Nothing sensitive is committed.

---

## How the pipeline works

```
daily (09:00)
  wiki_freshness_check       → logs/freshness.json
  habit_miner --days 2       → incremental habit update
  habit_to_skill --process-accepted  → scaffold accepted habit skills
  boris_draft --process-accepted     → patch accepted Boris rules into CLAUDE.md
  selfreg_monitor            → logs/selfreg-health.json (grade A–F)

  hooks (per session, not scheduled):
  auto_pinecone_save [Stop]  → save session learnings to Pinecone
  session_start_brief [SessionStart] → surface top-2 suggestions + 🔮 anticipation

weekly (Sunday 10:00)
  learning_promoter          → promote patterns to Pinecone
  wiki_lint                  → flag stale / malformed wiki pages
  auto_graphify              → rebuild Graphify knowledge graphs
  semantic_merge             → deduplicate near-duplicate Pinecone memories
  salience --days 7          → tag high-stakes session events
  self_improvement_queue     → rebuild ranked suggestion inbox
  knowledge_sync             → embed changed wiki content to Pinecone
  roi_tracker                → usage/cost ROI report
  selfreg_monitor            → weekly health grade

dreaming (Saturday 11:00)
  stage3_dreaming --days 7   → 7-dimension analysis → 4 high-leverage actions
  skills_audit               → skills compliance check
  habit_miner --days 14      → full habit mining window
  habit_ledger               → advance graduation ladder
  habit_to_skill             → scaffold suggested_skill entries
  habit_to_skill --process-accepted  → process accepted queue
  boris_draft                → generate CLAUDE.md rule drafts
  boris_draft --process-accepted     → apply accepted rules
  effectiveness_tracker      → grade suggestions, auto-tune thresholds
  anticipate                 → update per-project routine predictions
  hebbian_consolidation --apply → extend TTL for recalled+salient memories
```

All jobs are pure Python. No Claude API calls in the automation layer.

---

## Brain-inspired OS — detailed

### Habit Engine

`habit_miner.py` scans JSONL session transcripts for repeated tool-call sequences. It applies:

- **IDF distinctiveness** — sequences that appear in only 1–2 projects score higher than universal patterns
- **Reward ratio** — sequences that cluster near positive outcomes (task completion, no error follow-up)
- **Time-gap segmentation** — long pauses reset the sequence window, preventing false cross-task patterns
- **Cross-project detection** — same routine detected in N projects → `_cross_project` aggregate entry with distinctiveness bonus

Raw signal: ~2961 n-grams → 14 items after filtering. These are surfaced in the Unified Attention Queue for review.

`habit_ledger.py` tracks each habit through: `detected` → `suggested_skill` → `codified`. `habit_to_skill.py` scaffolds a `SKILL.md` draft when you accept a suggestion.

### Hebbian Consolidation

`recall_tracker.py` logs every Pinecone query hit (which memory IDs were returned). `hebbian_consolidation.py` runs weekly: memories recalled frequently or tagged high-salience by `salience.py` (security / money / prod / errors) get their TTL extended. Memories never recalled expire normally. This mirrors Hebbian synaptic strengthening: neurons that fire together wire together.

### Unified Attention Queue

`self_improvement_queue.py` is a single ranked inbox aggregating:
- Habit suggestions from the habit engine
- Boris rule candidates from repeated corrections
- Dreaming high-leverage actions
- Selfreg regressions

`suggestion_feedback.py` handles response:
- **Dismiss** → exponential back-off (shown again after 2x the suppression window)
- **Accept** → triggers the appropriate action (scaffold habit skill / patch CLAUDE.md / etc.)
- **5 ignores with no feedback** → auto-suppress

`effectiveness_tracker.py` measures what fraction of accepted suggestions get acted on, and tunes promotion thresholds automatically.

### Boris Loop

When Claude makes a repeated mistake and you correct it, `boris_draft.py` detects the pattern across session transcripts and generates a candidate CLAUDE.md rule. The rule lands in the Unified Attention Queue for your review. On accept, `suggestion_feedback.py` patches the relevant CLAUDE.md with a backup — no manual editing required.

### Anticipation

`anticipate.py` builds a per-project model of tool-call sequences and predicts the most likely next routine when a session opens. Shown at session start as `🔮 anticipated: <routine>`. Accuracy is tracked against actual habits to auto-calibrate.

---

## Key output files

| File | What it tells you |
|---|---|
| `~/.claude/logs/selfreg_health.json` | Self-regulation grade (A–F) + dimension breakdown |
| `~/.claude/logs/freshness.json` | Which wikis are stale and by how many days |
| `~/.claude/logs/habit_ledger.json` | All detected habits and their graduation status |
| `~/.claude/logs/self_improvement_queue.json` | Current ranked suggestion inbox |
| `~/.claude/logs/recall_tracker.json` | Pinecone memory access frequency |
| `~/.claude/reports/dreaming-{date}.json` | Latest dreaming analysis + 4 high-leverage actions |
| `~/.claude/reports/roi_latest.json` | Cost, session count, memory health over 30 days |

---

## Cost discipline

- **Never embeds raw transcripts** — only curated content (summaries, learnings, patterns); Pinecone quota is not a concern on the Builder plan
- **Incremental sync via content-hash manifest** — re-embeds only what changed since last run
- **Chunks sized to embedding model window** — no silently truncated content
- **Zero LLM calls in the automation loop** — all scheduled scripts are pure Python; dreaming analysis is local heuristics, not Claude API
- **Prompt-cache-aware guidance** — built-in rules against mid-session model switching (breaks cache → full re-cache cost)

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, adapt it. PRs welcome.

*Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.*
