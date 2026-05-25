---
name: fix-flow
description: "Safe, project-aware fix workflow for the 4-layer ecosystem. Enforces impact-check BEFORE editing, surgical change, verify AFTER, and logs the gotcha — so fixes don't break other things and the lesson is captured. Use when the user says 'поправи', 'оправи', 'fix this', 'счупи се', 'fix the bug/error', or asks to fix anything in a project under J:\\Antigraviti. NOT for finding an unknown root cause (use systematic-debugging first), NOT for bulk regex edits across many files (use bulk-fixer)."
---

# fix-flow — Safe project fix workflow

Codifies the global CLAUDE.md discipline (`tools first, grep last` · impact before
change · verify · record correction) into a repeatable 5-step fix loop. This is the
**execution** guardrail — it assumes you already know WHAT to fix. If the root cause
is unknown, run `systematic-debugging` first, then come back here to apply the fix.

## When to use vs not

| Situation | Use |
|-----------|-----|
| Known fix to apply in a project | **fix-flow** (this) |
| Unknown root cause / mysterious bug | `systematic-debugging` first |
| Same edit across 5+ files (regex) | `bulk-fixer` |
| Enum/status drift across layers | `enum-sync-checker` |
| Pre-deploy gate | `pre-deploy-check` |

## The 5 steps (do them in order, don't skip)

### 1. UNDERSTAND — pin the target
- Restate the bug in one line + the exact symptom (error text, wrong behavior).
- Define "done": the concrete, verifiable success condition.
- Read the project graph FIRST (cheap context): `{{WIKI_PATH}}\<Project>\graph\knowledge_graph.json`
  — check `critical_rules` for a known gotcha before touching code.

### 2. IMPACT — what will break (BEFORE editing)
- GitNexus MCP: `impact({target: "<symbol/function>", minConf: 0.8})`.
  1 `impact()` ≈ 500 tokens, replaces ~10 Grep+Read (~50k tokens). Always prefer.
- If GitNexus has no data for the repo, fall back to a single targeted Grep for callers.
- Output: list the call sites / dependents the fix could affect. If the blast radius
  is large, surface it to the user before proceeding.

### 3. FIX — surgical edit
- Touch ONLY what the fix requires (Karpathy: surgical edits). No drive-by refactors.
- Respect the project's `critical_rules` (e.g. static routes before `/{id}`,
  tenant-isolation field, soft-delete filter, money column type).
- Keep the change minimal and reversible.

### 4. VERIFY — evidence before claiming done
Run the project's real check (never claim "fixed" without running it):
- Python/FastAPI: `python -m py_compile <file>` then the relevant `pytest` / endpoint curl
- TS/React: `npx tsc --noEmit` and/or `npm run build`
- Confirm the original symptom is gone (re-trigger it). Paste the passing output.
- This is the `verification-before-completion` rule: evidence, not assertion.

### 5. LOG — capture the lesson (only if novel)
- If the bug was non-obvious, append a dated one-liner to
  `{{WIKI_PATH}}\<Project>\wiki\sources\learnings.md` (or `.ai/learnings.md`).
- If it hit a 2nd project → promote to `_shared/patterns.md`.
- If worth cross-session recall: `python3 ~/.claude/scripts/pinecone.py save <NS> <id> "<gotcha>"`.
- If it's a recurring class of mistake → ask: "Да обновя ли CLAUDE.md с това правило?" (Boris pattern).

## Output to the user

Report in this shape (concise, Bulgarian per global rules):
```
Проблем: <one line>
Impact: <N call sites checked — safe / affected: ...>
Fix: <what changed, which file:line>
Verify: <command run → passing output>
Logged: <learnings.md / pinecone / none (trivial)>
```

## Anti-patterns (do NOT)
- ❌ Edit before running `impact()` — you may silently break a caller.
- ❌ Claim "fixed" without running the verify command.
- ❌ Bundle unrelated refactors into the fix.
- ❌ Re-grep the whole codebase when the graph + impact() answer it.
