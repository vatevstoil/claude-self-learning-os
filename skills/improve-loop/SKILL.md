---
name: improve-loop
description: Autonomous self-improvement loop — adversarial audit → grouped fixes → fresh-eyes verify → repeat until dry, with PROOF of completion at every step. Use when the user says "improve the system", "self-improvement loop", "намери и поправи слабостите", "пусни loop на самоподобрения", "одит и оправи всичко", "направи го перфектно", or wants a full find→fix→verify cycle where "done" must be demonstrated, never claimed.
---

# Improve Loop — find → fix → prove, until dry

Codified from two full production runs (2026-07-02: 31 confirmed findings;
2026-07-03: 12 confirmed findings *in same-day code*). Both reached dry state
with the full test suite green. The loop exists because the author of code is
its worst reviewer — and because "probably works" is not a deliverable.

## Invariants (non-negotiable — these are what make weaker models safe)

1. **Evidence before "done".** Every claim of completion carries the command
   that proves it and its output. No runnable check → the work is not finished,
   it is a hypothesis. Pre-register the pass condition BEFORE running the check.
2. **Adversarial verify.** Every audit finding gets an independent refuter
   prompted to DISPROVE it ("if not certain it's real → refuted"). Only
   confirmed findings get fixed. This kills plausible-but-wrong work.
3. **Fresh-eyes verifier between rounds.** Per-group fixers are scoped to their
   own diff and CANNOT see cross-cutting regressions. One verifier reads the
   ENTIRE round diff before the next round starts. (This catch saved a
   precision-poisoning regression on 07-02.)
4. **Honest outcome vocabulary.** `insufficient_data` is a valid, reportable
   verdict — never game a metric to look decided. If tests fail, the report
   says so with the output. A silenced signal is NOT a fixed problem.
5. **Root cause, not symptom.** Before fixing, grep every caller; one guard in
   the shared function beats N guards at call sites. A signal that
   pattern-matches a known failure may have a different cause — verify the
   evidence supports THIS fix before changing state.
6. **WIRE > extend > add.** In a mature system the upgrade is almost never a
   new generator — it is a new source in an existing queue, a consumer for a
   write-only ledger, or a retired dead limb. Check "who would TELL me if this
   dies?" — if the answer is "no one", wire that consumer first.
7. **Scale ceremony to stakes.** A typo fix is one action + one check. A
   migration gets the full loop + independent verification.

## The loop

```
0 GROUND   read the system map / graph / memory FIRST; real state (git, tests
           baseline count) before touching anything. Record the baseline.
1 AUDIT    fan out N read-only finders (one per dimension/stream/cluster) +
           an adversarial refuter per finding. Keep only CONFIRMED.
2 FIX      group confirmed findings by file-ownership (no two fixers share a
           file). Each fixer: surgical diff + test + real-producer run.
3 VERIFY   fresh-eyes agent reads the WHOLE round diff for cross-cutting
           regressions; full test suite; every touched producer actually run
           and its consumer rendered.
4 REPEAT   next round on what fresh-eyes + the suite found. DRY = 2
           consecutive rounds with zero new confirmed findings.
5 CLOSE    full suite green (report exact counts vs baseline) → update the
           knowledge graph + memory with the earned rules → honest final
           report: what was found, what was fixed, what remains OPEN (incl.
           user-action blockers), each with evidence.
```

## Audit workflow template (Workflow tool)

Pipeline per stream — findings verify while other streams still audit:

```js
const results = await pipeline(STREAMS,
  s => agent(`Read-only adversarial audit of ${s.files}. Focus: ${s.focus}.
    Read the real code — do not assume. Only defects with a concrete failure
    scenario (input/state → wrong output). Return findings (may be empty).`,
    { phase: 'Audit', schema: FINDINGS }),
  (rev, s) => parallel((rev?.findings || []).map(f => () =>
    agent(`Try to REFUTE by reading the real code + existing tests: ${f.summary}
      / ${f.failure_scenario}. Not CERTAIN it's real → refuted=true.`,
      { phase: 'Verify', schema: VERDICT, effort: 'high' })
      .then(v => ({ ...f, refuted: v?.refuted !== false })))))
const confirmed = results.filter(Boolean).flat().filter(Boolean)
                         .filter(f => !f.refuted)
```

## Per-fix Definition of Done (the fixer's contract)

- [ ] compile/lint clean on every touched file
- [ ] the REAL producer ran and produced the expected output (state the
      pre-registered pass condition, show the actual output)
- [ ] the consumer renders with the real data
- [ ] the specific test file is green, then the FULL suite is green
- [ ] deliberate shortcuts marked in-code (`ponytail:` ceiling + upgrade path)
- [ ] report deviations from spec explicitly — never silently

## Anti-deception rules (for every subagent this skill dispatches)

Include in every fixer/auditor prompt: *"Your final message is a report to an
orchestrator, not a human to impress. State exactly what you verified with
which command and what you did NOT verify. An honest 'blocked/unverified' is
worth more than a confident guess — false 'done' poisons the whole loop."*

## Cadence

On demand (after any substantial build), or monthly as system hygiene. Between
runs, the standing gates (trust tiers, judge, integrity guard, health monitor)
regulate the system; this loop is the deep pass that finds what they cannot.
