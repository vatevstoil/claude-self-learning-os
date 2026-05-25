# Claude Self-Learning OS

A **self-learning Agentic OS** for [Claude Code](https://docs.claude.com/claude-code) — turns your daily work into durable, compounding knowledge that the system maintains autonomously.

Every project keeps a wiki + knowledge graph + learnings; a memory layer (Pinecone) gives cross-session recall; and a set of scheduled scripts keep it all fresh, promote patterns, build graphs, watch their own health, and surface nudges — so the system gets smarter over time with almost no manual effort.

> Inspired by Andrej Karpathy's Obsidian-RAG idea, Chase AI's "Agentic OS", and Jack Roberts' "dreaming" concept — assembled into one working, self-maintaining system.

---

## What you get

- **4-layer memory** — Pinecone (cross-session recall) · Obsidian wikis · GitNexus impact analysis · Graphify knowledge graphs · CLAUDE.md
- **Autonomous pipeline** — daily/weekly/dreaming scheduled jobs that maintain the whole ecosystem
- **Self-regulation immune system** — `selfreg_monitor` grades the regulator itself (cron, errors, freshness, lint, hygiene), tracks trends, flags regressions
- **Agentic OS layer** — a domain registry of everything you can do + an observability dashboard (skills/automations as clickable buttons)
- **Dreaming engine** — weekly 7-dimension analysis → 4 high-leverage actions (cost, memory health, session hygiene, repeated-work detection)
- **Boris loop** — detects repeated corrections per project and nudges you to codify a rule
- **Continuous knowledge sync** — new learnings flow into Pinecone automatically (incremental, token-efficient)

See [`docs/SYSTEM_GUIDE.md`](docs/SYSTEM_GUIDE.md) for the full end-to-end description.

---

## Requirements

- Claude Code (paid plan) · Python 3.11+ · Git
- A [Pinecone](https://pinecone.io) account (the **Builder** plan gives 10M embedding tokens/month — plenty)
- An [Obsidian](https://obsidian.md) vault (or any folder of markdown) for your wikis
- Optional: `ffmpeg` + `yt-dlp` (for the video-analysis skill), GitNexus MCP (for impact analysis)
- Windows (Task Scheduler) or macOS/Linux (cron) — the scripts are cross-platform Python

---

## Quick start

```bash
git clone https://github.com/vatevstoil/claude-self-learning-os.git
cd claude-self-learning-os

# 1. Configure paths + Pinecone key (interactive — fills the {{PLACEHOLDERS}})
python configure.py

# 2. Install into ~/.claude (scripts + skills) and create .env
#    Windows:  .\install.ps1     |     macOS/Linux:  ./install.sh

# 3. Create your Pinecone index (1024-dim, model multilingual-e5-large)

# 4. Schedule the automation (Windows Task Scheduler / cron) — see docs/SYSTEM_GUIDE.md §3

# 5. Run once to verify
python ~/.claude/scripts/automation_dispatcher.py daily
```

Then open the dashboard: `python ~/.claude/scripts/agentic_os_dashboard.py --serve` → http://127.0.0.1:8723

---

## Configuration

`configure.py` replaces these placeholders throughout the scripts with your values:

| Placeholder | Meaning |
|-------------|---------|
| `{{WIKI_PATH}}` | Your Obsidian vault for project wikis (e.g. `~/Obsidian`) |
| `{{RESEARCH_PATH}}` | Vault for research/knowledge wikis (can be the same) |
| `{{CODE_PATH}}` | Where your code projects live |
| `{{HOME}}` | Your home dir (`~`) |
| `{{PINECONE_API_KEY}}` | Your Pinecone API key (stored in `.env`, never committed) |
| `{{PINECONE_INDEX_HOST}}` | Your Pinecone index host |

Secrets live in `~/.claude/.env` (git-ignored). Nothing sensitive is ever committed.

---

## How it works (30-second version)

```
Sense → Decide → Act → Verify → Monitor   (+ Boris: corrections → rule candidates)

daily   : freshness check + health monitor
weekly  : promote patterns · lint · build graphs · sync knowledge to Pinecone ·
          refresh domain registry · ROI · self-health grade
dreaming: 7-dimension analysis → 4 high-leverage actions + Boris candidates
```

Full detail: [`docs/SYSTEM_GUIDE.md`](docs/SYSTEM_GUIDE.md) · [`docs/AGENTIC_OS.md`](docs/AGENTIC_OS.md) · [`docs/SELF_REGULATION.md`](docs/SELF_REGULATION.md)

---

## Cost discipline built in

- Never bulk-embeds raw transcripts (would blow any quota) — only **curated** content (summaries, learnings, patterns)
- Incremental sync (content-hash manifest) — re-runs embed only what changed
- Chunks sized to the embedding model's window (no silently-truncated content)
- Prompt-cache-aware guidance (don't switch model mid-session, etc.)

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, adapt it. PRs welcome.

*Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.*
