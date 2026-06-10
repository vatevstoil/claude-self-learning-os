---
name: four-layer-init
description: Bootstrap a new project with the full 4-layer memory architecture (Pinecone + Obsidian Wiki + GitNexus + CLAUDE.md). Use when user says "setup 4-layer for <project>", "add memory architecture", "onboard <project> to memory ecosystem", "init memory for new project", or similar. Creates reproducible infrastructure that cuts session startup tokens 40-180x across projects.
---

# Four-Layer Memory — Project Bootstrap

> **Reference architecture:** `{{RESEARCH_PATH}}\Claude Code Resurch\wiki\concepts\Graphify-vs-GitNexus.md`
> **Session protocol:** `~/.claude/references/session-start.md`
>
> **Goal:** Apply the 4-layer memory stack to ANY project so every session starts with ~5k tokens instead of 400k+.

## When to Invoke

Trigger on phrases like:
- "Setup 4-layer for StroyOffice"
- "Onboard <project> to memory architecture"
- "Init GitNexus + Pinecone for <project>"
- "Apply the memory ecosystem"

## Prerequisites Check (run BEFORE onboarding)

```bash
# 1. GitNexus installed globally?
gitnexus --version   # should return 1.6.1+

# 2. Pinecone credentials in ~/.claude/.env?
test -f ~/.claude/.env && grep -q PINECONE_API_KEY ~/.claude/.env && echo OK || echo MISSING

# 3. Pinecone wrapper script present?
test -f ~/.claude/scripts/pinecone.py && echo OK || echo MISSING
```

If any MISSING → see "Initial global setup" at the bottom.

## The 7-Step Project Onboarding

### Step 1 — Project path + name (ask user)

```
Project path:   {{CODE_PATH}}\<ProjectName>
Wiki path:      {{WIKI_PATH}}\<ProjectName>\wiki
Pinecone ns:    <ProjectName>           (case-exact, matches folder name)
```

### Step 2 — GitNexus analyze + verify

```bash
cd "{{CODE_PATH}}\<ProjectName>"
gitnexus analyze    # ~1-2 min depending on size
gitnexus status     # must show ✅ up-to-date
gitnexus list       # verify project appears in global registry
```

Expected output: N nodes + M edges. For most projects: 5k–50k nodes.

### Step 3 — Git post-commit hook (auto re-index)

```bash
cat > .git/hooks/post-commit << 'EOF'
#!/bin/sh
# Auto re-index GitNexus graph after each commit (async, non-blocking).
command -v gitnexus >/dev/null 2>&1 || exit 0
(
  cd "$(git rev-parse --show-toplevel)" && \
  gitnexus analyze > "$TMPDIR/gitnexus-reindex.log" 2>&1 &
) >/dev/null 2>&1
exit 0
EOF
chmod +x .git/hooks/post-commit
```

⚠ Hooks do NOT copy between git worktrees — re-install per worktree if needed.

### Step 4 — Obsidian Wiki structure (Karpathy method)

If `{{WIKI_PATH}}\<ProjectName>\wiki\` doesn't exist:

```bash
python ~/.claude/skills/dev-wiki/scripts/init_dev_wiki.py --project "<ProjectName>"
```

Otherwise verify structure:
```
{{WIKI_PATH}}\<ProjectName>\wiki\
├── index.md          ← Navigation + TOC (<100 lines)
├── SPRINT.md         ← Active sprint (<60 lines)
├── concepts/         ← Cross-cutting patterns
├── sources/          ← Referenced research/docs
├── archive/          ← Completed sprints
└── graph/
    └── knowledge_graph.json   ← Graphify output (L2a)
```

**Premium visual:** dev wikis live INSIDE the big `{{WIKI_PATH}}` vault, which
already carries the premium style (gold/dark snippet + project-colored graph) —
so a new project subfolder inherits it automatically; do NOT create a nested
`.obsidian/`. ONLY if this project is a **standalone vault** (its own
`.obsidian/`, e.g. Fakturka.bg / DCTL), style it once:
```bash
python ~/.claude/scripts/obsidian_premium_viz.py "{{WIKI_PATH}}\<ProjectName>"
```

### Step 5 — Update project `CLAUDE.md` (<100 lines)

Add this block near the top:

```markdown
## 4-Layer Memory (see ~/.claude/references/session-start.md)
- **L4 Pinecone**: `python3 ~/.claude/scripts/pinecone.py query "<ProjectName>" "<q>"`
- **L3 Wiki**: `{{WIKI_PATH}}\<ProjectName>\wiki\`
- **L2b GitNexus**: MCP auto + 7 skills (impact(), pr-review, refactoring, ...)
- **L2a Graphify**: `wiki/graph/knowledge_graph.json` (run `/graphify` if stale)
- **L1**: this CLAUDE.md + `/clear` at 50% context
```

### Step 6 — Graphify JSON (optional, only if project >50 files)

```
/graphify
```

Outputs `{{WIKI_PATH}}\<ProjectName>\wiki\graph\knowledge_graph.json`.

### Step 7 — Save initial project summary to Pinecone

```bash
python3 ~/.claude/scripts/pinecone.py save "<ProjectName>" \
  "onboarding-$(date +%Y-%m-%d)" \
  "<ProjectName> onboarded to 4-layer memory architecture on $(date +%Y-%m-%d). Stack: <tech-stack>. Main purpose: <one-sentence>. Wiki at {{WIKI_PATH}}\<ProjectName>\wiki\. GitNexus indexed with N nodes. Pinecone namespace: <ProjectName>." \
  --meta "project=<ProjectName>,type=onboarding,date=$(date +%Y-%m-%d)"
```

## Verification Checklist

After Step 7, run these and expect all green:

```bash
# 1. GitNexus fresh
cd "{{CODE_PATH}}\<ProjectName>" && gitnexus status | grep -q "up-to-date" && echo "✓ L2b"

# 2. Post-commit hook active
test -x .git/hooks/post-commit && echo "✓ hook"

# 3. Wiki structure
test -f "{{WIKI_PATH}}\<ProjectName>\wiki\index.md" && echo "✓ L3"

# 4. Graphify JSON (if Step 6 done)
test -f "{{WIKI_PATH}}\<ProjectName>\wiki\graph\knowledge_graph.json" && echo "✓ L2a"

# 5. Pinecone writable
python3 ~/.claude/scripts/pinecone.py query "<ProjectName>" "onboarding" --topk 1 | grep -q "onboarding" && echo "✓ L4"

# 6. CLAUDE.md updated
grep -q "4-Layer Memory" "{{CODE_PATH}}\<ProjectName>\CLAUDE.md" && echo "✓ L1"
```

## Output Template (report to user)

```
✅ <ProjectName> onboarded to 4-layer architecture

| Layer | Status |
|-------|--------|
| L1 CLAUDE.md | updated with 4-layer block |
| L2a Graphify | <fresh/deferred> |
| L2b GitNexus | <N> nodes, <M> edges, ✅ fresh |
| L3 Wiki | {{WIKI_PATH}}\<ProjectName>\wiki\ |
| L4 Pinecone | namespace "<ProjectName>", onboarding saved |

Next sessions will start at ~5k tokens instead of 400k+.
To continue: "Recall last <ProjectName> session"
→ will trigger pinecone.py query + wiki/SPRINT.md read.
```

---

## Initial Global Setup (one-time, if prerequisites missing)

### Install GitNexus globally

```powershell
# On Windows with npm 11.x bug (Cannot destructure property 'package'):
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\npm-cache\_npx"
npm cache clean --force
npm install -g gitnexus
gitnexus setup    # auto-configs Claude Code MCP + 7 skills + PreToolUse/PostToolUse hooks
```

### Pinecone credentials

1. Get API key from https://pinecone.io (free tier)
2. Create index named `memory` with:
   - dim: **1024**
   - metric: **cosine**
   - model: **multilingual-e5-large**
3. Save to `~/.claude/.env`:

```
PINECONE_API_KEY=pcsk_xxxxx
PINECONE_INDEX_HOST=memory-<hash>.svc.<region>.pinecone.io
PINECONE_INDEX_NAME=memory
PINECONE_EMBED_MODEL=multilingual-e5-large
PINECONE_DIMENSION=1024
```

4. `chmod 600 ~/.claude/.env`
5. Verify:

```bash
python3 ~/.claude/scripts/pinecone.py query "test" "hello" --topk 1
```

### Pinecone CLI wrapper

Already at `~/.claude/scripts/pinecone.py` — if missing, regenerate via:

```
Tell Claude: "Re-create pinecone.py CLI wrapper at ~/.claude/scripts/"
```

Wrapper auto-loads `~/.claude/.env`, supports `save | query | list | delete` per namespace.

### `~/.claude/.gitignore`

Ensure contains `.env` and `*.key` so credentials never leak.

---

## Token Savings — Expected Impact

| Workflow | Before | After | Savings |
|----------|--------|-------|---------|
| Session startup | 30–50k tokens | ~5k | **10x** |
| Refactor impact analysis | 20k+ grep tokens | ~500 (`gitnexus impact`) | **40x** |
| Cross-session recall | re-read history | ~300 (`pinecone query`) | **∞** |
| Codebase overview new session | 50k+ reads | ~2.6k (Graphify JSON) | **20x** |
| **Typical active session total** | 500k–1M | ~25k | **~40x** |

---

## Supported Projects (one-time onboarding required)

Each project gets its own Pinecone namespace, wiki folder, and gitnexus index:

```
{{CODE_PATH}}\Facturka.bg          → namespace: "Fakturka.bg"      ✅ onboarded 2026-04-17
{{CODE_PATH}}\StroyOffice-Pro      → namespace: "StroyOffice"      ⚪ pending
{{CODE_PATH}}\Higgsfield           → namespace: "Higgsfield"       ⚪ pending
{{CODE_PATH}}\Cinemind             → namespace: "Cinemind"         ⚪ pending
{{CODE_PATH}}\CasinoScore-AI       → namespace: "CasinoScore"      ⚪ pending
...
```

Run this skill once per project; then just use the normal session-start protocol.
