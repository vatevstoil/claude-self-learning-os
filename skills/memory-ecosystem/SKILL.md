---
name: memory-ecosystem
description: 4-layer memory protocol for Claude Code sessions - decides which memory tool to use and when
---

# Memory Ecosystem — Session Protocol

> This skill defines HOW Claude should use the 4 memory layers during every session.
> Run this protocol at session start and before major code changes.

## 4 Memory Layers (Always Available)

| Layer | Tool | Question It Answers | When |
|-------|------|-------------------|------|
| **L1** | Graphify (knowledge_graph.json) | "What is this project? Business logic?" | Session start |
| **L2** | GitNexus MCP (impact/context/query) | "What breaks if I change X?" | Before code changes |
| **L3** | Long-term memory (semantic search) | "What did we discuss/decide before?" | When recalling history |
| **L4** | NotebookLM MCP (notebook_query) | "What do external sources say?" | Research tasks |

## Session Start Protocol

```
1. READ knowledge_graph.json (Graphify)
   → Understand clusters, critical_rules, architecture
   → ~2,600 tokens, instant orientation

2. READ SPRINT.md + learnings.md (Dev Wiki)
   → Current tasks, recent lessons

3. IF user mentions past decision → long-term memory search
   → "search memory for [topic]"
```

## Before Code Change Protocol

```
IF project has .gitnexus/ folder:
  1. impact({target: "[file/symbol]", direction: "both", minConfidence: 0.7})
  2. Review blast radius
  3. THEN make the change

IF project has NO .gitnexus/:
  → Use Graphify cluster connections + grep for references
  → RECOMMEND: "This project would benefit from GitNexus. Run: npx gitnexus analyze"
```

## Research Protocol

```
IF user asks about external topic / new technology:
  1. CHECK NotebookLM notebooks → notebook_query(notebook_id, query)
  2. IF not in notebooks → research_start(query, source="web")
  3. After research → optionally vectorize into long-term memory

IF user asks about past conversation / decision:
  1. Long-term memory → semantic search in relevant namespace
  2. IF not found → check wiki learnings.md / log.md
```

## GitNexus Key Tools (When Available)

| Tool | Use When |
|------|----------|
| `impact(target, direction, minConfidence)` | Before ANY refactor/rename/delete |
| `context(symbol)` | Understanding an unknown function/class |
| `query(search_term)` | Finding where something is used |
| `detect_changes()` | After git diff — see risk level |
| `rename(old, new)` | Multi-file coordinated rename |

## Decision Tree — Which Tool?

```
Is it a CODE question about dependencies?
  → GitNexus (impact, context, query)

Is it a PROJECT CONTEXT question (what/why/how)?
  → Graphify (read knowledge_graph.json)

Is it about a PAST DECISION or conversation?
  → Long-term memory (semantic search via pinecone.py)

Is it about EXTERNAL KNOWLEDGE (new tech, research)?
  → NotebookLM (notebook_query or research_start)

Is it about LIBRARY DOCS (API, syntax)?
  → Context7 MCP (not this skill — use ctx7)
```

## Token Budget

```
Graphify read:     ~2,600 tokens (1x per session)
GitNexus impact:   ~500 tokens (per query)
Memory search:     ~300 tokens (per query)
NotebookLM query:  0 tokens (free — Google pays)
────────────────────────────────────────────
vs grep 300 files: ~180,000 tokens (53x MORE)
```

## Project Status Check

Before using GitNexus tools, verify the project has been indexed:
```
IF .gitnexus/ folder exists in project root → GitNexus is ready
IF NOT → suggest: "npx gitnexus analyze" (takes ~30s for most projects)
```

## End of Session

```
1. Update wiki (SPRINT.md, log.md) — per Dev Wiki Protocol
2. IF significant decisions made → save to long-term memory:
   "/pinecone-memory save this conversation"
3. IF knowledge_graph.json is outdated → suggest re-graphify
```
