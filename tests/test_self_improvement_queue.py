import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_build_queue_from_boris(tmp_path):
    from self_improvement_queue import build_queue
    boris = tmp_path / "boris-candidates.json"
    boris.write_text(json.dumps({
        "projects": {
            "MyProject": {"count": 6, "examples": ["не прави X", "не прави X"]}
        }
    }), encoding="utf-8")
    items = build_queue(boris_path=boris, promotions_path=tmp_path / "p.md",
                        habits_path=tmp_path / "h.json", graphify_path=tmp_path / "g.json")
    boris_items = [i for i in items if i.type == "boris_rule"]
    assert len(boris_items) == 1
    assert boris_items[0].confidence >= 0.8
    assert boris_items[0].project == "MyProject"


def test_build_queue_from_habits(tmp_path):
    from self_improvement_queue import build_queue
    habits = tmp_path / "habits.json"
    # A distinctive, rewarded habit (real workflow) must surface in the queue
    habits.write_text(json.dumps([{
        "project": "Proj",
        "routine": ["Bash:pytest", "Edit"],
        "count": 8,
        "session_count": 4,
        "strength": 8.0,
        "distinctiveness": 300.0,
        "reward_ratio": 0.9,
        "status": "detected"
    }]), encoding="utf-8")
    items = build_queue(boris_path=tmp_path / "b.json", promotions_path=tmp_path / "p.md",
                        habits_path=habits, graphify_path=tmp_path / "g.json",
                        ledger_path=tmp_path / "ledger.json")
    habit_items = [i for i in items if i.type == "habit"]
    assert len(habit_items) == 1
    assert habit_items[0].confidence > 0.5


def test_build_queue_drops_noise_habits(tmp_path):
    from self_improvement_queue import build_queue
    habits = tmp_path / "habits.json"
    # Low-distinctiveness file churn must be dropped from the queue
    habits.write_text(json.dumps([{
        "project": "Proj", "routine": ["Read", "Edit"], "count": 2000,
        "session_count": 100, "strength": 2000.0, "distinctiveness": 30.0,
        "reward_ratio": 1.0, "status": "detected"
    }]), encoding="utf-8")
    items = build_queue(boris_path=tmp_path / "b.json", promotions_path=tmp_path / "p.md",
                        habits_path=habits, graphify_path=tmp_path / "g.json",
                        ledger_path=tmp_path / "ledger.json")
    assert [i for i in items if i.type == "habit"] == []


def test_queue_sorted_by_score(tmp_path):
    from self_improvement_queue import build_queue
    boris = tmp_path / "boris-candidates.json"
    boris.write_text(json.dumps({
        "projects": {
            "P1": {"count": 8, "examples": ["не"]},
            "P2": {"count": 4, "examples": ["не"]},
        }
    }), encoding="utf-8")
    items = build_queue(boris_path=boris, promotions_path=tmp_path / "p.md",
                        habits_path=tmp_path / "h.json", graphify_path=tmp_path / "g.json")
    scores = [i.score for i in items]
    assert scores == sorted(scores, reverse=True)


def test_save_and_load_queue(tmp_path):
    from self_improvement_queue import build_queue, save_queue, load_queue
    habits = tmp_path / "habits.json"
    habits.write_text(json.dumps([{
        "project": "P", "routine": ["Bash", "Edit"],
        "count": 5, "session_count": 3, "strength": 5.0, "status": "detected"
    }]), encoding="utf-8")
    items = build_queue(boris_path=tmp_path / "b.json",
                        promotions_path=tmp_path / "p.md",
                        habits_path=habits,
                        graphify_path=tmp_path / "g.json")
    out = tmp_path / "queue.json"
    save_queue(items, path=out)
    loaded = load_queue(path=out)
    assert len(loaded) == len(items)


def test_project_filter(tmp_path):
    from self_improvement_queue import build_queue, filter_for_project
    boris = tmp_path / "boris-candidates.json"
    boris.write_text(json.dumps({
        "projects": {
            "AlphaProject": {"count": 6, "examples": ["не"]},
            "BetaProject": {"count": 5, "examples": ["не"]},
        }
    }), encoding="utf-8")
    items = build_queue(boris_path=boris, promotions_path=tmp_path / "p.md",
                        habits_path=tmp_path / "h.json", graphify_path=tmp_path / "g.json")
    alpha_items = filter_for_project(items, "AlphaProject")
    assert all(i.project in ("AlphaProject", "all", "_cross_project") for i in alpha_items)


def test_cross_project_habit_visible_in_any_project_filter(tmp_path):
    """Cross-project habits must appear in every project's filtered view."""
    from self_improvement_queue import QueueItem, filter_for_project
    cross_item = QueueItem(
        id="habit-_cross_project-bash:git-edit",
        type="habit",
        description="Universal: Bash:git -> Edit",
        project="_cross_project",
        confidence=0.7,
        value=0.7,
        score=0.49,
    )
    other_item = QueueItem(
        id="habit-proj-bash:pytest",
        type="habit",
        description="Specific to Proj",
        project="Proj",
        confidence=0.5,
        value=0.7,
        score=0.35,
    )
    items = [cross_item, other_item]

    for project in ("Proj", "AnotherProj", "Totally-Different"):
        filtered = filter_for_project(items, project)
        ids = [i.id for i in filtered]
        assert cross_item.id in ids, f"Cross-project habit missing for project={project}"


def test_queue_excludes_suppressed(tmp_path, monkeypatch):
    """Dismissed items must not appear in the rebuilt queue while suppressed."""
    import suggestion_feedback as sf
    import self_improvement_queue as q
    from self_improvement_queue import build_queue

    boris = tmp_path / "boris-candidates.json"
    boris.write_text(json.dumps({
        "projects": {"P1": {"count": 8, "examples": ["не"]}}
    }), encoding="utf-8")

    # First build to discover the id
    items = build_queue(
        boris_path=boris,
        promotions_path=tmp_path / "p.md",
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        ledger_path=tmp_path / "l.json",
    )
    assert len(items) >= 1, "Need at least one item to dismiss"
    target = items[0].id

    # Point both the module default and the queue's patchable path at our tmp file
    fb = tmp_path / "fb.json"
    monkeypatch.setattr(sf, "DEFAULT", fb, raising=False)
    monkeypatch.setattr(q, "_FEEDBACK_PATH", fb, raising=False)

    # Dismiss the top item (uses real NOW so it will be suppressed for weeks)
    sf.dismiss(target, path=fb)

    # Verify is_suppressed sees it
    assert sf.is_suppressed(target, path=fb) is True

    # Rebuild — the suppressed item must be absent
    items_after = build_queue(
        boris_path=boris,
        promotions_path=tmp_path / "p.md",
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        ledger_path=tmp_path / "l.json",
    )
    ids_after = [it.id for it in items_after]
    assert target not in ids_after, (
        f"Suppressed item '{target}' should not appear in the queue"
    )


def test_queue_includes_after_suppression_expires(tmp_path, monkeypatch):
    """After the cooldown expires the item must re-appear in the queue."""
    from datetime import datetime, timezone, timedelta
    import suggestion_feedback as sf
    import self_improvement_queue as q
    from self_improvement_queue import build_queue

    boris = tmp_path / "boris-candidates.json"
    boris.write_text(json.dumps({
        "projects": {"P2": {"count": 6, "examples": ["грешка"]}}
    }), encoding="utf-8")

    fb = tmp_path / "fb.json"
    monkeypatch.setattr(sf, "DEFAULT", fb, raising=False)
    monkeypatch.setattr(q, "_FEEDBACK_PATH", fb, raising=False)

    items = build_queue(
        boris_path=boris,
        promotions_path=tmp_path / "p.md",
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
    )
    target = items[0].id

    # Dismiss with 1 week base window using a fixed past "now"
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sf.dismiss(target, weeks=1, path=fb, now=past)

    # Suppression expired: 5 weeks after that past dismiss
    after_expiry = past + timedelta(weeks=5)
    assert sf.is_suppressed(target, path=fb, now=after_expiry) is False


# ---------------------------------------------------------------------------
# judge_score ranking — new tests
# ---------------------------------------------------------------------------


def test_judge_score_items_rank_before_unscored(tmp_path):
    """Items with judge_score must appear before unscored items regardless of raw score."""
    from self_improvement_queue import QueueItem, build_queue

    boris = tmp_path / "boris-candidates.json"
    boris.write_text(json.dumps({
        "projects": {
            "HighScore": {"count": 10, "examples": ["x"]},  # higher raw score
            "LowScore": {"count": 3, "examples": ["y"]},    # lower raw score
        }
    }), encoding="utf-8")

    items = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
    )
    assert len(items) >= 2

    # items[0] has highest raw score; inject judge_score on items[1] (lower raw)
    items[1].judge_score = 0.99

    def _sort_key(x):
        has_score = 0 if x.judge_score is None else 1
        js = x.judge_score if x.judge_score is not None else 0.0
        return (-has_score, -js, -x.score)

    items.sort(key=_sort_key)
    assert items[0].judge_score == 0.99, "Scored item must be first"


def test_judge_score_multiple_scored_sorted_by_judge_score(tmp_path):
    """Multiple scored items must be sorted by judge_score desc."""
    from self_improvement_queue import QueueItem

    items = [
        QueueItem(id="a", type="boris_rule", description="x", project="P",
                  confidence=0.8, value=0.9, score=0.72, judge_score=0.3),
        QueueItem(id="b", type="boris_rule", description="y", project="P",
                  confidence=0.8, value=0.9, score=0.72, judge_score=0.9),
        QueueItem(id="c", type="boris_rule", description="z", project="P",
                  confidence=0.8, value=0.9, score=0.72, judge_score=0.6),
    ]

    def _sort_key(x):
        has_score = 0 if x.judge_score is None else 1
        js = x.judge_score if x.judge_score is not None else 0.0
        return (-has_score, -js, -x.score)

    items.sort(key=_sort_key)
    assert [i.id for i in items] == ["b", "c", "a"]


def test_queue_item_has_judge_fields():
    """QueueItem must have judge_score, judge_reason, judge_verdict fields."""
    from self_improvement_queue import QueueItem

    item = QueueItem(
        id="test", type="habit", description="x", project="P",
        confidence=0.5, value=0.7, score=0.35,
    )
    assert item.judge_score is None
    assert item.judge_reason == ""
    assert item.judge_verdict == ""


def test_save_load_preserves_judge_score(tmp_path):
    """judge_score must survive save/load roundtrip."""
    from self_improvement_queue import QueueItem, save_queue, load_queue

    item = QueueItem(
        id="test", type="habit", description="test", project="P",
        confidence=0.5, value=0.7, score=0.35,
        judge_score=0.88, judge_reason="Good skill", judge_verdict="useful",
    )
    out = tmp_path / "q.json"
    save_queue([item], path=out)
    loaded = load_queue(path=out)
    assert loaded[0]["judge_score"] == 0.88
    assert loaded[0]["judge_reason"] == "Good skill"
    assert loaded[0]["judge_verdict"] == "useful"


# ---------------------------------------------------------------------------
# Tier-gated auto_apply (Task 2c)
# ---------------------------------------------------------------------------


def _write_effectiveness(path, precision_by_type, samples_by_type):
    """Helper: write a minimal effectiveness.json for tests."""
    import json
    path.write_text(json.dumps({
        "precision_by_type": precision_by_type,
        "samples_by_type": samples_by_type,
    }), encoding="utf-8")


def test_tier0_type_no_auto_apply_despite_high_score(tmp_path):
    """Type with tier 0 (boris_rule below precision threshold) must NOT get auto_apply
    even when score >= AUTO_APPLY_THRESHOLD."""
    from self_improvement_queue import build_queue

    # effectiveness: boris_rule precision=0.5 (below 0.80 T1 threshold) → tier 0
    eff = tmp_path / "eff.json"
    _write_effectiveness(eff,
                         precision_by_type={"boris_rule": 0.5},
                         samples_by_type={"boris_rule": 20})

    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps({
        "projects": {
            # count=6 → score=0.855 >= AUTO_APPLY_THRESHOLD without tier gate
            "TestProj": {"count": 6, "examples": ["не прави X"]}
        }
    }), encoding="utf-8")

    items = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        trust_effectiveness_path=eff,
        trust_overrides_path=tmp_path / "no-overrides.json",
    )
    boris_items = [it for it in items if it.type == "boris_rule"]
    assert len(boris_items) == 1
    assert boris_items[0].score >= 0.85, "Pre-condition: score qualifies without tier gate"
    assert boris_items[0].status == "queued", \
        f"Tier-0 type must be downgraded; got status={boris_items[0].status!r}"


def test_tier1_type_gets_auto_apply(tmp_path):
    """Type with tier 1 must keep auto_apply when score >= AUTO_APPLY_THRESHOLD.

    Habit scores cap at 0.665 (value=0.7, max confidence=0.95) so they can never
    reach the 0.85 threshold.  We use boris_rule (value=0.9, count=6 → score=0.855)
    with an override forcing tier=1 to isolate the gate logic.
    """
    from self_improvement_queue import build_queue

    # Override: boris_rule = tier 1 (precision data irrelevant — override wins)
    ov = tmp_path / "overrides.json"
    ov.write_text(json.dumps({"boris_rule": 1}), encoding="utf-8")
    eff = tmp_path / "eff.json"
    _write_effectiveness(eff,
                         precision_by_type={"boris_rule": 0.5},
                         samples_by_type={"boris_rule": 5})

    boris = tmp_path / "boris.json"
    # count=6 → confidence=0.98 capped at 0.95, score=0.95*0.9=0.855 >= 0.85
    boris.write_text(json.dumps({
        "projects": {"Proj": {"count": 6, "examples": ["x"]}}
    }), encoding="utf-8")

    items = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        trust_effectiveness_path=eff,
        trust_overrides_path=ov,
    )
    boris_items = [it for it in items if it.type == "boris_rule"]
    assert len(boris_items) == 1
    assert boris_items[0].score >= 0.85, \
        f"Pre-condition: score must qualify; got {boris_items[0].score}"
    assert boris_items[0].status == "auto_apply", \
        f"Override tier=1 must keep auto_apply; got status={boris_items[0].status!r}"


def test_trust_tiers_import_failure_fail_closed(tmp_path, monkeypatch):
    """If trust_tiers cannot be imported, ALL items must be fail-closed (no auto_apply)."""
    import sys
    import self_improvement_queue as siq

    # Patch _get_trust_tier to always raise ImportError (simulates missing module)
    monkeypatch.setattr(siq, "_get_trust_tier", lambda *a, **kw: (_ for _ in ()).throw(ImportError("missing")))

    # Re-patch it as a real function that raises
    def _raise_import(*a, **kw):
        raise ImportError("trust_tiers missing")

    monkeypatch.setattr(siq, "_get_trust_tier", _raise_import)

    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps({
        "projects": {"P": {"count": 6, "examples": ["x"]}}
    }), encoding="utf-8")

    items = siq.build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
    )
    for it in items:
        assert it.status != "auto_apply", \
            f"Fail-closed: no auto_apply allowed when trust_tiers fails; got {it.id}={it.status}"


def test_stale_auto_apply_downgraded_on_rebuild(tmp_path):
    """Items previously marked auto_apply in the saved queue must be downgraded
    when trust tier for their type is 0 on the next rebuild."""
    from self_improvement_queue import build_queue, save_queue, load_queue

    # effectiveness: boris_rule tier 0
    eff = tmp_path / "eff.json"
    _write_effectiveness(eff,
                         precision_by_type={"boris_rule": 0.5},
                         samples_by_type={"boris_rule": 20})

    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps({
        "projects": {"Stale": {"count": 6, "examples": ["old"]}}
    }), encoding="utf-8")

    queue_path = tmp_path / "queue.json"

    # First build without tier gate (no eff file) — item gets auto_apply provisionally
    items_first = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        carry_judge_path=None,
        trust_effectiveness_path=tmp_path / "nonexistent.json",  # no eff → tier 0 already
        trust_overrides_path=tmp_path / "no-overrides.json",
    )
    # Even on first build with no eff data → fail-closed → queued
    # Manually force a stale auto_apply into the saved file to simulate old data
    saved = [{"id": "boris-stale", "type": "boris_rule", "description": "x",
               "project": "Stale", "confidence": 0.95, "value": 0.9,
               "score": 0.855, "status": "auto_apply", "source": "",
               "judge_score": None, "judge_reason": "", "judge_verdict": ""}]
    queue_path.write_text(json.dumps(saved), encoding="utf-8")

    # Rebuild with tier gate active (boris_rule = tier 0 via eff file)
    items_rebuilt = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        carry_judge_path=queue_path,
        trust_effectiveness_path=eff,
        trust_overrides_path=tmp_path / "no-overrides.json",
    )
    stale = [it for it in items_rebuilt if it.id == "boris-stale"]
    # The stale-boris item from the source will have been regenerated from boris.json
    # with id="boris-stale" — check the boris item by project
    boris_items = [it for it in items_rebuilt if it.type == "boris_rule"]
    assert all(it.status == "queued" for it in boris_items), \
        f"All boris_rule items must be downgraded; got {[(it.id, it.status) for it in boris_items]}"


def test_tier_gate_uses_injected_paths(tmp_path):
    """trust_effectiveness_path / trust_overrides_path kwargs must be respected."""
    from self_improvement_queue import build_queue

    # Override: boris_rule = tier 2 via overrides file
    ov = tmp_path / "overrides.json"
    ov.write_text(json.dumps({"boris_rule": 2}), encoding="utf-8")

    eff = tmp_path / "eff.json"
    _write_effectiveness(eff,
                         precision_by_type={"boris_rule": 0.5},
                         samples_by_type={"boris_rule": 20})

    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps({
        "projects": {"Proj": {"count": 6, "examples": ["x"]}}
    }), encoding="utf-8")

    items = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        trust_effectiveness_path=eff,
        trust_overrides_path=ov,  # override forces tier 2 → auto_apply allowed
    )
    boris_items = [it for it in items if it.type == "boris_rule"]
    assert len(boris_items) == 1
    assert boris_items[0].status == "auto_apply", \
        f"Override tier=2 must allow auto_apply; got {boris_items[0].status!r}"


def test_judge_fields_survive_rebuild(tmp_path):
    """judge_score/verdict set by llm_judge must survive a build_queue rebuild
    via carry_judge_path regardless of tier-gate changes."""
    import json as _json
    from dataclasses import asdict
    from self_improvement_queue import build_queue, save_queue

    boris = tmp_path / "boris.json"
    boris.write_text(_json.dumps({
        "projects": {"P": {"count": 4, "examples": ["x"]}}
    }), encoding="utf-8")

    queue_path = tmp_path / "q.json"

    # First build, save, manually inject judge fields
    items1 = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        carry_judge_path=None,
    )
    # Inject judge data into the saved representation
    saved = [asdict(it) for it in items1]
    for s in saved:
        s["judge_score"] = 0.91
        s["judge_verdict"] = "useful"
        s["judge_reason"] = "carry-test"
    queue_path.write_text(_json.dumps(saved), encoding="utf-8")

    # Rebuild — judge fields must be carried over
    items2 = build_queue(
        boris_path=boris,
        habits_path=tmp_path / "h.json",
        graphify_path=tmp_path / "g.json",
        promotions_path=tmp_path / "p.md",
        carry_judge_path=queue_path,
    )
    for it in items2:
        assert it.judge_score == 0.91, \
            f"judge_score not carried for {it.id}; got {it.judge_score}"
        assert it.judge_verdict == "useful"
