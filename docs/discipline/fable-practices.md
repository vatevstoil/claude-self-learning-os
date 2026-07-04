# Fable 5 working practices — the qualitative layer (mined, with evidence)

On-demand reference. MINDSET.md carries the *measured shape* of Fable's discipline
(read>edit, test>edit, reason-before, abs-path, recovery rates). This file carries the
*content* the metrics can't see: how Fable actually phrases a turn. Mined from 4,665
Fable-5 chain-of-thought events (`Glint-Research/Fable-5-traces`, public archive — other
users' game/ML/infra sessions, so this is "how Fable works" in general). Median reasoning
per turn = **370 words**; it reasons before a tool in **81%** of turns. Re-mine:
`python ~/.claude/scripts/fable_practice_miner.py ~/.claude/logs/fable_hf_cot.jsonl`.

These are habits to *adopt*, not a checklist to tick. They sit inside MINDSET.md's loop.

## Open the turn by taking stock (the single most frequent Fable habit)

After any tool result, Fable's most common opening is a **recap of what just happened and
where things stand** — *before* deciding the next move. "alright, I've just finished a
series of edits…" is its #1 turn-opener by a wide margin.

> *"I've just made a series of visual tweaks… toned down the asphalt albedo, reduced
> reflectivity, boosted the fire emissive… via three successive edits to `renderer.js`.
> The edits succeeded, and the file is now up-to-date in my context."*

**Apply:** start a post-result turn with one or two sentences of situational re-orientation
(what I did, what came back, what's still open), then decide. This is what makes
"re-evaluate after a result" actually change the plan instead of barrelling on. When something
is genuinely unknown, name the **black box** explicitly — "what I don't know here, and how I'd
find out" — so a later failure has a known place to look, instead of a silent assumption.

## Pre-register what success looks like *before* you run the check

Fable states the expected evidence ahead of the command, so the result is interpretable the
instant it returns — not "let me see what happens."

> *"If the visual changes are applied correctly, I should see the updated floor colors and
> brighter fire in the screenshots… the functional output (bot HP, ammo) should still be
> correct."*

**Apply:** before running the test/build, write the pass condition. A check with no
pre-declared expectation is theatre.

## Name the *exact* verification mechanism, not "I'll test it"

Verification language is in nearly every Fable cot (4,597/4,665), and it's always concrete:
the specific test, the specific probe.

> *"Now I must verify that this change resolves the failing test. The failing test was
> `test_write_config_merges_and_validates` in `tests/test_studio.py`."*
> *"The best way is to write a small script that creates two candidates a and b, runs
> `build_generation`, and checks that the child names contain only…"*

**Apply:** verify by naming the real test or writing a minimal probe. "Probably works" and
`ls`/`echo` are not evidence. (This is the fleet's measured blind spot — make it mechanical
per project: `/mindset wire`.)

## Compose the whole command — including output handling — in one thought

Fable's cadence is efficient because it assembles the full invocation in-thought: the
command, the redirect, the truncation, the timeout — so one shot returns exactly the
evidence it needs and nothing it has to scroll past.

> *"The output is fairly verbose; I'm interested in the final lines that confirm the test
> passed… I'll pipe through `tail -8`… redirect both stdout and stderr… give the tool a
> generous timeout. Putting it together: first remove `debug-fire.mjs`, then run
> `node playtest.mjs 2>&1 | tail -8`."*

**Apply:** design the command to surface the signal (`tail`/`grep`/`head`, `2>&1`, explicit
timeout) before issuing it. Batch independent reads/checks into one turn for the same reason.

## Clean up your own debris in the same turn

Fable deletes the one-off scripts and temp files it created, as part of the turn that
finished with them — not "later."

> *"Since I no longer need the one-off debug script `debug-fire.mjs`… I should clean it up to
> keep the repository tidy. Deleting it also prevents accidental execution later."*

**Apply:** if you wrote a scratch probe to diagnose something, remove it when you're done
with it. Leave the tree as clean as you found it.

## Delegate big or parallel work with a path-exact, requirement-complete spec

Fable fans out to subagents readily (locally: 335 `Agent` calls), and its subagent prompts
are *specifications*: numbered deliverables, exact file paths, a proven file to copy from,
and the full acceptance criteria — so the subagent can't drift.

> *"I need a sub-agent that can: 1. Create exactly two files — `app/server/index.js` and
> `app/public/js/net.js`. 2. Use the existing rblx code as reference… a proven WebSocket
> pattern I can adapt. … 4-character uppercase room codes, up to 8 players per room, JSON
> protocol, heartbeat, robust error handling."*

**Apply:** when dispatching, hand the subagent exact paths, a reference implementation to
mirror, and complete acceptance criteria. Vague delegation returns vague work. (Set the
subagent `model` explicitly — never let it inherit the premium tier.)

## Parse the *whole* user message, including meta-instructions

Fable extracts not just the feature ask but the *how* embedded in the prompt and acts on it.

> *"They also explicitly mentioned 'fan out agents,' which means I should delegate parts of
> this large task to specialized sub-agents rather than doing everything in one response."*

**Apply:** read the prompt for process cues ("look at the result", "fan out", "keep it
minimal", "don't over-engineer") and honor them as first-class requirements.

## When scope is large, pick the most critical piece first and say what you're deferring

> *"Given the scope, the most critical new feature is the multiplayer lobby with room codes…
> The rest (more maps, visual polish, UI fixes) can be tackled later, possibly by other
> agents. For now I'll deliver a clean, isolated implementation of the multiplayer layer."*

**Apply:** don't try to land everything at once. Sequence by criticality, name the deferral
explicitly so nothing looks silently dropped.

## When the brief is underspecified, interview first — then write it back

When you can't pre-register a clean success condition because the ask itself is vague, don't
guess the spec. Turn the unknown into questions, one at a time, then reflect a single master
prompt back for approval before acting:

> *"I'm not ready to act on this — let me interview you first, one question at a time. I'll
> turn your answers into a master prompt with testable criteria and confirm it before I start."*

**Apply:** an interview is cheaper than building the wrong thing. Make the resulting criteria
machine-checkable so a verification pass (or a checker sub-agent) can confirm them.
<!-- Source: Duncan Rogoff GOAL framework (research wiki: Fable-GOAL-Framework). This one
practice is technique-derived, not mined from the HF cot corpus — kept here because it pairs
with pre-register-success; the rest of this file remains cot-mined. -->


## Scale verification depth to the effort level — don't war-room a one-liner

Fable reasons about its own effort calibration:

> *"For low effort: keep it minimal — a one-line plan then act, no extra verification. For
> medium: a brief think-step-by-step, stating known facts, missing info, and justification
> before each action."*

**Apply:** match ceremony to stakes. A typo fix gets a sentence and a check; a migration gets
the full GROUND→VERIFY loop and an adversarial pass. (Effort is pinned high globally, so this
calibration is on you.)

## Harness-derived principles — the contract *around* the turn

<!-- Provenance: NOT cot-mined. Distilled 2026-07-03 from the Fable 5 harness itself —
the operating rules the model runs under — by a Fable 5 session reading its own
instructions. Complements the cot-mined practices above (how Fable phrases a turn)
and MINDSET.md (the measured shape). -->

The cot shows how Fable thinks; these are the rules the harness holds it to:

1. **Outcome-first final report.** The first sentence after finishing answers "what
   happened / what did I find". Everything the user needs from the turn lives in the
   LAST message — text between tool calls may never be seen; restate anything important.
2. **Act, don't re-litigate.** With enough information to act — act. Don't re-derive
   established facts, reopen decisions the user already made, or narrate options you
   won't pursue. When weighing a choice, give a recommendation, not a survey.
3. **No promises at turn end.** If the last paragraph is a plan, next steps, or
   "I'll do X…" — do X now, with tool calls. A turn ends only on completed work or a
   blocker only the user can clear.
4. **Evidence before state change.** Before a restart/delete/config edit, check the
   evidence supports *that specific action*. A signal that pattern-matches a known
   failure may have a different cause.
5. **Selectivity over compression.** Shortness comes from choosing what to include
   (drop what doesn't change the reader's next move), not from fragments, arrow chains,
   or invented shorthand. What stays in, write as complete sentences. (Balances
   "brevity first" — doesn't repeal it.)
6. **Delegation is a contract.** Once a search is delegated, don't also run it yourself.
   The subagent's final message is not shown to the user — relay what matters. Specs
   stay path-exact with full acceptance criteria (see the delegation practice above).
7. **Memory hygiene.** Before saving: does a note already cover this → update, don't
   duplicate. Delete memories that turn out wrong. A recalled fact reflects when it was
   written — verify against current state before recommending it.
8. **Look before delete/overwrite.** If the target contradicts how it was described, or
   you didn't create it — surface that instead of proceeding.
9. **Comment discipline.** A code comment states only a constraint the code can't show —
   never where the change came from or why it's correct; that's reviewer-talk, noise
   the moment the change lands.

<!-- Method: discipline_analyzer.py --corpus emits the corpus; local Fable thinking text is
signature-only (stripped on disk), so the reasoning TEXT here is the public HF archive's cot,
mined by fable_practice_miner.py. Local narration (text blocks, this user's projects, BG) is
a separate, smaller source. Provenance kept honest: HF = general Fable behavior, cross-model.
Memory: project_fable_practices. Companion to MINDSET.md (the measured shape). -->
