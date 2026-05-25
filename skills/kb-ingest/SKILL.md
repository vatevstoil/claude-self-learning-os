---
name: kb-ingest
description: >
  Generic knowledge base ingest skill for any Karpathy-method research wiki.
  Automatically processes new RAW files (raw/transcripts/*.md) into wiki summaries,
  updates index.md, hot.md, knowledge_graph.json, NotebookLM, and processed.json.
  Use when: "обнови wiki", "провери за нови файлове", "ingest", "нови raw файлове",
  or when user adds .md files to a raw/transcripts/ folder.
  Configure per-project via KB_CONFIG.md in the wiki base directory.
  Works for: Claude Trading, Claude Code Research, AI Video, or any new research wiki.
---

# KB Ingest — Generic Research Wiki Auto-Ingest

Processes new RAW files in any Karpathy-method knowledge base and integrates them
into the wiki. Requires a `KB_CONFIG.md` in the base directory (or falls back to
project detection from path).

## Paths (configurable)

```
Base:         <from KB_CONFIG.md or argument>
Raw:          raw/transcripts/*.md
Registry:     raw/processed.json
Summaries:    wiki/summaries/<Name>.md
Index:        wiki/index.md
Hot cache:    wiki/hot.md
Graph:        graph/knowledge_graph.json
```

---

## Step 0: Locate Project Config

Read `KB_CONFIG.md` in the base directory to get:

```
base_path:        {{RESEARCH_PATH}}\<Project>\
notebooklm_id:    <UUID>
categories:       <routing table — what content maps to which category>
```

If no `KB_CONFIG.md` exists → look for `CLAUDE.md` with a similar structure.

If the user provides a path argument → use that as base.

**Known projects (auto-detect from path):**

| Path pattern | Project | NLM Notebook |
|---|---|---|
| `Claude Trading` | Trading wiki | `15d280af-2689-4ff8-ab6a-096f34415a83` |
| `Claude Code Resurch` | Claude Code wiki | *(read from KB_CONFIG.md)* |
| `AI Video` | AI Video wiki | *(read from KB_CONFIG.md)* |

---

## Step 1: Detect Delta

```python
import json, os

registry = json.load(open(base + "raw/processed.json"))
processed = {e["raw_file"] for e in registry["processed"]}
all_raw   = {f for f in os.listdir(base + "raw/transcripts/") if f.endswith(".md")}
delta     = sorted(all_raw - processed)
```

**Report to user:**
- 0 files: "Всички файлове са обработени. Wiki-то е актуално."
- 1-3 files: Process all automatically
- 4+ files: List and ask which to process

---

## Step 2: For Each New File

### 2a. Read the file
```
Read("<base>/raw/transcripts/<filename>")
```
If >10,000 tokens: read in parts (offset + limit 200 lines).

### 2b. Determine category

Read the category routing from `KB_CONFIG.md` → `references/category-routing.md`.

Generic defaults (override in KB_CONFIG):

| Content keywords | Category |
|---|---|
| TradingView, MCP, CDP | TradingView Setup |
| API, Railway, bot, execution | Automated Trading |
| AutoAgent, optimization | Agent Optimization |
| ML, backtest, LightGBM | Strategy & Risk |
| TAO, Bittensor, crypto, chain | Crypto & Blockchain |
| Polymarket, prediction | Prediction Markets |
| HMM, regime, Alpaca | Advanced Bot Architecture |
| tutorial, how-to, guide | Tutorials |
| research, analysis | Research |

### 2c. Create Summary

Template: `wiki/summaries/<PascalCaseName>.md`

```markdown
---
type: summary
tags: [tag1, tag2, tag3]
cluster: <cluster_key>
created: <today>
last_updated: <today>
---

# <Descriptive Title>

> Type: YouTube Transcript Summary / Research Summary / Instruction File
> Date: <today>
> Author/Channel: <author>
> Source: `raw/transcripts/<filename>`

---

## TL;DR (3-layer scan)

**Summary** — one clean, concise paragraph (5-second read; what this is + why it matters).

**Headlines**
- <top key point 1>
- <top key point 2>
- <3-6 bullets total — the 15-second read>

**Things** — key people / tools / terms / ideas mentioned: `<term>` · `<tool>` · `<name>` · …

---

## Key Facts

- <3-7 bullets with specific numbers, tools, facts>

---

## Architecture / Workflow

<diagram or system description>

---

## Key Insights

<numbered actionable insights>

---

## Quotes

> "<important quote>"

---

## Significance for Workflow

<how it fits the overall stack>

---

## Related Documents

- [[<related-summary>]] — <why related>
```

**Rules:**
- **TL;DR 3-layer scan е ЗАДЪЛЖИТЕЛЕН** (Nick Milo pattern, see [[Obsidian-Web-Clipper-Capture]]): Summary (1 параграф) → Headlines (3-6 bullets) → Things (people/tools/terms). Прави summary-то scan-имо на 3 скорости. Детайлните секции отдолу остават.
- YAML frontmatter е ЗАДЪЛЖИТЕЛЕН — без него Obsidian Properties е празен
- `tags`: 4–7 конкретни тага, lowercase-hyphenated (напр. `freqtrade`, `mcp-cost`, `token-savings`)
- `cluster`: взима се от `graph/knowledge_graph.json` → `clusters` keys
- Specific numbers > general statements
- Each bullet = 1 fact, not 1 topic
- Do not invent facts — only from the RAW file
- Related Documents = links to EXISTING summaries only

### 2d. Check for New Concept

Create `wiki/concepts/<Name>.md` if:
- Topic appears in 2+ existing summaries (cross-cutting)
- Or represents a new tool category / pattern

---

## Step 3: Batch Updates

### 3a. Update wiki/index.md

Add row to correct category table:
```markdown
| [[<SummaryName>]] | <Author> | <1-line description> |
```

Update header counts: `Sources: N | Summaries: N | Concepts: N`

Add log entry: `| <date> | +[[SummaryName]] — <key fact> |`

### 3b. Update wiki/hot.md

Prepend new section:
```markdown
## New <N> file(s) processed (<date>, auto-ingest)

- [[<Summary>]] — <key insight>

### New insights:
1. <most important finding>
```

### 3c. Update graph/knowledge_graph.json

```json
// meta:
"sources_count": +N,
"summaries_count": +N,

// In correct cluster → files[]:
{"file": "<SummaryName>", "key": "<one sentence>"}
```

Add to `critical_rules[]` if new non-obvious rule found.

### 3d. Add to NotebookLM

```
mcp__notebooklm__notebook_add_text(
  notebook_id="<from KB_CONFIG.md>",
  title="<descriptive title>",
  text="<synthesized content — key facts, insights, quotes>"
)
```

If 403 error → `mcp__notebooklm__refresh_auth()` → if still fails → run `notebooklm-mcp-auth` in terminal.

---

## Step 4: Update Registry

Update `raw/processed.json`:

```json
// Add to "processed":
{
  "raw_file": "<filename>",
  "summary": "wiki/summaries/<Name>.md",
  "date": "<today>",
  "notebooklm": true
}
// Remove from "unprocessed" if present
// Update meta.last_updated and meta.total_processed
```

---

## Step 5: Final Report

```
Processed: <N> new file(s)
  - <file1> → wiki/summaries/<Summary1>.md
  - <file2> → wiki/summaries/<Summary2>.md

NotebookLM: +N sources (total: N)
Wiki: Summaries N, Sources N, Concepts N

New category needed? yes/no
New concept needed? yes/no
```

---

## Special Cases

**CLAUDE.md-style instruction file** → Summary focuses on: what the file does, how to use it, key phases/commands

**Web research (not video)** → Type: "Deep Research"

**Duplicate of existing summary** → Check for new info → if yes: update existing summary → if no: add to processed.json with `"note": "duplicate of <SummaryName>"`

**Metadata file (exports, index)** → `"summary": null` in processed.json, no summary created

---

## Naming Conventions

**Summary names (PascalCase):** Descriptive, not author-based
- `lewis-jackson-build-bot.md` → `PassiveIncome-MLBot.md`
- Exception: author is part of brand (e.g., `ZubairTrabzada-16Skills.md`)

**Cluster mapping:** Read from `graph/knowledge_graph.json` → `clusters` keys
