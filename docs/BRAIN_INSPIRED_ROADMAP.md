---
type: spec
tags: [domain::meta, project::all, status::done, brain-inspired, habits, consolidation, autonomy]
created: 2026-05-25
last_updated: 2026-05-25
cross_refs: [[[SYSTEM_GUIDE]], [[SELF_REGULATION]], [[AGENTIC_OS]]]
status_note: "IMPLEMENTED v1+v2+v3+v4+v5 2026-05-25. v1: 4 pillars (recall_tracker, habit_miner, self_improvement_queue, hebbian_consolidation). v2: tool-signature+IDF+reward (noise 2961->14), habit_ledger, boris_draft, semantic_merge. v3: habit_to_skill, suggestion_feedback, effectiveness_tracker, anticipate, salience, skill-match fix. v4 GAP-CLOSE: salience->TTL wired, dispatcher health.json, implicit feedback auto-suppress, 9 project CLAUDE.md deployed. v5: cross-project habit transfer (_mine_cross_project), MCP recall (query_and_track), session-start auto-recall. GitHub synced. 119/119 tests."
---

# Brain-Inspired Autonomy Roadmap — build next session

> Goal: make the self-learning OS **autonomous** and **brain-faithful**, with special
> emphasis on **habit building** (procedural memory). This spec is self-contained — a
> fresh session can read it and execute. Think of it as: give the system a brain's
> two memory systems (declarative + procedural) plus one attention filter.

---

## Critical reframe (the second-pass insight)

The brain has **two distinct memory systems** + **one salience filter**. The current
OS only really implements the declarative one (facts/learnings → Pinecone). The big
gaps are **procedural memory (habits)** and a **unified attention layer**.

| Brain system | Region | Current OS | Gap to build |
|--------------|--------|-----------|--------------|
| **Declarative** (facts/events) | hippocampus→cortex | learnings→Pinecone, dreaming | Hebbian reinforcement + consolidation |
| **Procedural** (habits/skills) | **basal ganglia** | skills (static) | **HABIT ENGINE — the focus** |
| **Attention/salience** | prefrontal/thalamus | scattered nudges | one unified ranked queue |

---

## Pillar 1 — HABIT ENGINE (procedural memory) ⭐ primary focus

Habits form in the basal ganglia via the **cue → routine → reward** loop; repetition
makes a sequence automatic ("chunking"). Mirror that:

- **Cue**: context that triggers a routine — primarily the project (cwd), optionally
  the session's opening action or a preceding event.
- **Routine**: a recurring ORDERED sequence of actions (not single keywords). Mine the
  JSONL transcripts (stage3_dreaming already parses them) for frequent action n-grams
  (length 2-4), e.g. `Bash(git status) → Bash(npm test) → Edit`.
- **Reward/reinforcement**: did the sequence recur + lead to a completed task (no
  immediate correction after)? Success strengthens it.
- **Chunking**: a routine seen ≥ N times → propose it as ONE skill (the chunked habit).
- **Habit strength**: `count × recency` (Hebbian). Decays if unused.
- **Graduation ladder** (the autonomy engine):
  `detected (≥N) → suggest SKILL → (skill used ≥M) → suggest AUTOMATION`
  — exactly how the brain automates a practiced behavior; gated by confidence.

**Build:**
- `habit_miner.py` — parse JSONL per project → frequent ordered action sequences →
  `~/.claude/logs/habits.json` (cue, routine, count, last_seen, strength, status).
- Extend `stage3_dreaming` (it already walks JSONL) to feed the miner — avoid a second
  full JSONL pass (efficiency).
- Surface top habit → skill candidates in the unified queue (Pillar 3).
- Optional: a `habit-to-skill` helper that scaffolds a skill from a routine via skill-creator.

**Critical guardrails:** tool sequences are noisy — require ≥4 occurrences across ≥2
sessions before suggesting; ignore trivial sequences (single read/ls); never
auto-create skills (suggest only) until the ladder is proven.

## Pillar 2 — Declarative consolidation (Hebbian memory)

Make memory use-based, like synaptic plasticity ("fire together, wire together").

- **Recall tracking**: `recall_tracker` — log every Pinecone query hit (id → hit count,
  last_recalled). Wire into `pinecone.py query` + `/recall`.
- **Consolidation in dreaming** (the "sleep" pass):
  - **Strengthen**: memories recalled often → extend TTL / mark NEVER_EXPIRE.
  - **Decay**: memories never recalled past their window → let TTL expire (already via
    pinecone_cleanup_expired) but now usage-weighted, not purely time.
  - **Merge**: near-duplicate vectors (cosine > 0.95) → consolidate into one (dedup +
    keep the strongest). Use existing `cleanup_pinecone.py` as the base.
- **Hebbian TTL**: `effective_ttl = base_ttl × (1 + recall_count)` capped at NEVER_EXPIRE.

## Pillar 3 — Unified salience/attention (one inbox, not 5 alarms)

The brain has ONE attention system, not separate alarms. Today the OS scatters:
promotions queue, Boris nudges, graph-enrichment, habit candidates, health alerts.
Consolidate into one ranked **self-improvement queue**:

- `self_improvement_queue.py` — aggregate all candidate sources (promotions, Boris
  rules, habit→skill, graph enrichment) → score by value × confidence → write
  `~/.claude/logs/improvement-queue.json`.
- `session_start_brief` surfaces the **top 1-2** for the current project only (stays
  token-light). Everything else lives in the dashboard.
- **Auto-apply** the highest-confidence items (≥0.9) like reflexes; queue the rest for
  human approval (prefrontal judgment). This is the autonomy dial.

---

## Implementation order (phased — TDD, each phase shippable)

1. **Recall tracking** (smallest, enables Hebbian) — `recall_tracker` + wire into pinecone.py.
2. **Habit miner** — `habit_miner.py` + dreaming integration + habits.json. (Primary focus.)
3. **Unified queue** — `self_improvement_queue.py` + session_start_brief refactor (replace scattered nudges).
4. **Consolidation** — Hebbian TTL + merge pass in dreaming.
5. Wire all into the weekly/dreaming dispatcher; update SYSTEM_GUIDE + SELF_REGULATION docs.

## Critical risks to respect (from this session's lessons)
- **Token budget**: reuse stage3_dreaming's single JSONL pass; don't double-walk.
- **Noise**: habit/correction detection is noisy (Bulgarian "не"; varied tool seqs) →
  high thresholds, suggest-don't-auto until proven.
- **ASCII namespaces** (Cyrillic → ns_util.sanitize_ns), **e5 chunk ≤1400 chars**,
  **idempotent IDs**, **never overwrite hand-made graphs** — all still apply.
- **One attention surface** — do NOT add a 6th separate nudge; fold into the unified queue.

## Success criteria
The system, unattended: detects a repeated routine → proposes a skill → (once used)
proposes an automation; strengthens memories you actually recall and lets unused ones
fade; and presents ONE ranked list of "what to codify next" instead of scattered
alarms. That is a brain: declarative + procedural memory, consolidated during sleep,
filtered by attention — running itself.

---

## v4 Gap-Close (2026-05-25)

Gaps identified post-v3 and resolved:

### 1. Salience → TTL wiring (`hebbian_consolidation.py`)
- `load_salience()` reads `salience.json` → `{session_id: score}`
- `apply_hebbian_ttls(salience=...)` checks `meta["session_id"]` against salience map
- Bonus: +2 virtual recalls for score ≥ 0.8 (SECURITY+MONEY), +1 for ≥ 0.5
- Result: high-stakes events now persist proportionally longer than routine work

### 2. Dispatcher health reporting (`automation_dispatcher.py`)
- `write_health(run_type, results)` writes `~/.claude/logs/health.json`
- All three task groups (daily/weekly/dreaming) now track per-task success/fail
- `health.json` shape: `{last_run, run_type, tasks_total, tasks_failed, failures[], status}`
- Status "DEGRADED" logged at WARNING level for immediate visibility

### 3. Implicit feedback auto-suppress (`suggestion_feedback.py` + `session_start_brief.py`)
- `record_surfaced(item_id, threshold=5)` — call each time an item is shown
- After 5 surfaces without explicit action → auto-dismiss (exponential back-off, `implicit=True`)
- `session_start_brief.py` calls `record_surfaced` for each shown queue item
- Prevents nagging: items the user consistently ignores fade away automatically

### 4. Project coverage expansion
- 9 additional active projects received `CLAUDE.md` (Boris loop enabled)
- Coverage: 15 → 24 of 42 projects (remaining 18 are inactive)

### Remaining gaps (next iteration)
- Cross-project habit transfer — same routine in 5 projects = 5 separate records, not 1 universal
- MCP recall instrumentation — Hebbian TTL misses ~90% of real recalls (MCP path)
- GitHub `claude-self-learning-os` release stale — doesn't reflect v2/v3/v4 improvements
- ROI calibration — `hourly_value` still $50 default, never tuned
