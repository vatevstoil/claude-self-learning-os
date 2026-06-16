---
name: graphify
description: "Builds a compact JSON knowledge graph for a software project, enabling ~70x token savings compared to scanning source files. Creates knowledge_graph.json (meta + architecture + critical_rules + clusters) and per-cluster Obsidian .md files for RAG-style access. Triggers on: 'graphify', 'knowledge graph', 'map the project', 'build graph', 'create graph', 'граф на проекта', 'знания граф', or when starting work on a new project without an existing graph."
---

# Graphify

Build a compact JSON knowledge graph for a software project. Result: `knowledge_graph.json` +
per-cluster `.md` files readable in ~70x fewer tokens than scanning source files directly.

## When to Use

- New project — no graph exists yet
- Project has changed significantly — graph is stale
- User says "graphify", "build graph", "граф на проекта"

## Step 1 — Initialize Output Directory

```bash
python skills/graphify/scripts/init_graph.py "<ProjectName>" "<{{WIKI_PATH}}/Project/graph>"
```

This creates placeholder files. Proceed to fill them in.

## Step 1b — Audit Script Completeness (for script-collection projects)

If the project is a collection of scripts (e.g. `scripts/*.py`), the graph must
not silently omit files. Count them and record the number for the meta block:

```bash
ls scripts/ | grep '\.py$' | wc -l          # total .py files on disk
```

Every file on disk should land in *some* cluster (Step 3). Any file not placed
in a cluster is invisible to every agent that trusts the graph as the map.
Record the disk count — it becomes `meta.script_count` in Step 4.

## Step 2 — Discover Project Structure

Read the entry point first (80% of architecture info lives here):

```bash
# Python/FastAPI
head -120 backend/main.py          # app.include_router() calls -> all domains
grep "include_router" backend/main.py  # full list of routers

# Node.js/Express
head -80 src/app.js                # app.use() calls -> all route domains
grep "app.use\|require.*router" src/app.js

# Then list top-level directories only
ls backend/
ls src/
```

Do NOT read all files — entry point + 2-3 `ls` commands are enough for Step 2.

## Step 3 — Identify Clusters

From the entry point, group related routers/modules into 6-14 domain clusters.

**Cluster naming (use these standard names when they fit):**
`auth` | `invoicing` | `expenses` | `accounting` | `tax_compliance` | `hr_payroll` |
`clients` | `platform` | `ai` | `frontend` | `core` | `api` | `ml` | `tasks` |
`queue_workers` | `crawler` | `graph` | `identity`

Each cluster = one `.md` file + one entry in `clusters{}` in the JSON.

## Step 4 — Fill knowledge_graph.json

Use `references/graph-template.json` as the structure guide.

**Add these self-auditing fields to `meta`** (so staleness is detectable —
`integrity_guard.py` compares them against disk and FLAGS drift):
- `script_count` — the integer from Step 1b (total `.py` files on disk).
- `test_count` — from `python -m pytest tests/ --collect-only -q 2>&1 | grep "tests collected"` (use a hidden-window subprocess; set `null` if pytest is unavailable).
- `graph_updated` — today's date.

**critical_rules is the most valuable section** — add 5-15 rules that are:
- Non-obvious (NOT derivable just by reading the code)
- Actionable (tell what TO DO or NOT TO DO)
- Specific (include field names, method names, error symptoms)

Example rules worth capturing:
```
"Expense IDs = UUID strings — NOT integers"
"Login = form-encoded: POST /auth/login expects form data, NOT JSON"
"Static routes BEFORE /{id} — /suggest-category before /{expense_id}"
"predict_proba: do NOT pass cat_features (causes runtime error)"
"exitClientContext() — correct method. NOT stopActingAsClient"
```

## Step 4b — Typed Edges (optional but high-value)

Add an `edges` array to the JSON: typed relationships **between clusters** so an AI
can decide "go deeper or skip" from the edge type alone — without reading file
contents (the Infinite Brain insight: typed edges cut traversal tokens ~15x).

```json
"edges": [
  {"from": "frontend", "to": "auth", "type": "depends_on", "note": "UI gates on session"},
  {"from": "queue_workers", "to": "api", "type": "followed_by", "note": "API enqueues, worker consumes"}
]
```

Edge types: `depends_on` · `part_of` · `preceded_by`/`followed_by` (pipeline order) ·
`derived_from` · `supports` · `contradicts` · `related_to` · `authored`.

Add 3-10 edges for the non-obvious cross-cluster relationships. Skip for trivial projects.

## Step 5 — Fill Cluster .md Files

Use `references/cluster-template.md` as structure guide.

For each cluster, read only:
1. The router/module file (endpoints + key functions)
2. The model definition (fields, constraints)

Fill in: files list, endpoints, data model, critical rules specific to this cluster.

## Step 5b — Clean Orphaned Cluster Files

Run ONLY after the new `.md` files exist (Step 5) — never before, or a crash
mid-write could delete files with no replacement. Delete `.md` files that no
longer correspond to a current cluster (a renamed/removed cluster leaves a stale
file that misleads agents into seeing clusters the JSON does not have):

```powershell
$graphDir = '{{WIKI_PATH}}\ProjectName\graph'
$keys = (Get-Content "$graphDir\knowledge_graph.json" -Raw | ConvertFrom-Json).clusters.PSObject.Properties.Name
$keep = $keys + @('index','shared-patterns','cross_project')
Get-ChildItem $graphDir -Filter '*.md' | Where-Object { $_.BaseName -notin $keep } | Remove-Item -Confirm:$false
```

## Step 6 — Update CLAUDE.md

Add (or update) this section in the project's CLAUDE.md:

```markdown
## Knowledge Graph (RAG)
**Before searching files — read the graph first (~70x fewer tokens):**
`{{WIKI_PATH}}\ProjectName\graph\knowledge_graph.json`
Obsidian visualization: `{{WIKI_PATH}}\ProjectName\graph\`
```

Find the right insertion point — before "On-Demand Reference" or at the end of Architecture section.

## Step 7 — shared-patterns.md

Fill `graph/shared-patterns.md` with which cross-project patterns apply:
- `{{RESEARCH_PATH}}\Claude Code Resurch\wiki\concepts\` — available patterns
- Check: FastAPI-Router-Pattern, Multi-tenant-Isolation, SaaS-Plan-Gating, Soft-Delete-Audit-Pattern

## Output Checklist

- [ ] `knowledge_graph.json` — meta (incl. script_count + test_count), architecture, critical_rules (5-15), clusters
- [ ] `index.md` — navigation table with all clusters
- [ ] One `.md` per cluster — files, endpoints, data model, rules
- [ ] `shared-patterns.md` — applicable cross-project patterns
- [ ] Project `CLAUDE.md` — Knowledge Graph RAG section added
- [ ] Step 7b post-write verification run — printed counts match intent

## Step 7b — Post-Write Verification (MANDATORY before reporting done)

After writing `knowledge_graph.json`, READ IT BACK FROM DISK and compare the
actual counts against what you intended. This guards against the failure mode
where an agent reports "updated to N clusters / M rules" while the file on disk
holds different values (the graph generated in memory but never persisted, or a
later regen clobbered a manual edit).

```python
import json
d = json.load(open(r"{{WIKI_PATH}}\ProjectName\graph\knowledge_graph.json", encoding="utf-8"))
print("clusters:", len(d["clusters"]),
      "| critical_rules:", len(d["critical_rules"]),
      "| script_count:", d["meta"].get("script_count"),
      "| test_count:", d["meta"].get("test_count"))
```

Only declare done when the printed numbers match what you intended to write. If
they differ, the write failed or was overwritten — re-write and re-verify. Never
report counts from memory; report only what this read-back prints.

## Token Budget

| Action | Tokens |
|--------|--------|
| Read entry point (head -120) | ~800 |
| grep include_router | ~200 |
| ls top-level dirs | ~100 |
| Read 1-2 key router files | ~1,500 |
| **Total discovery** | **~2,600** |
| vs. scanning all files | ~180,000+ |

**Rule:** If entry point + ls gives enough to write the graph — stop reading. Do NOT read every file.

## Existing Graphs (Projects with Graphs)

| Project | Graph Path |
|---------|-----------|
| Fakturka.bg | `{{WIKI_PATH}}\Fakturka.bg\graph\` |
| CasinoScore AI | `{{WIKI_PATH}}\CasinoScore\graph\` |
| Autoagency | `{{WIKI_PATH}}\Autoagency\graph\` |
| StroyOffice Pro | `{{WIKI_PATH}}\StroyOffice\graph\` |
| Cinemind | `{{WIKI_PATH}}\Cinemind\graph\` |
| DaVinci Plugin (DCTL) | `{{WIKI_PATH}}\DCTL\graph\` |
