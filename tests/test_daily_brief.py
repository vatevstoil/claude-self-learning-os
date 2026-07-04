"""Tests for daily_brief.py pure functions (open-note capture, priorities, render)."""
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
import json

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
db = importlib.import_module("daily_brief")


# ── open-note extraction ─────────────────────────────────────────────────────

def test_extract_real_notes():
    text = "blah {напомни ми за X} more {check the deploy} end"
    assert db.extract_open_notes(text) == ["напомни ми за X", "check the deploy"]


def test_extract_skips_empty_placeholders():
    assert db.extract_open_notes("{ }") == []
    assert db.extract_open_notes("{}") == []
    assert db.extract_open_notes("{…}") == []
    assert db.extract_open_notes("{...}") == []
    assert db.extract_open_notes("{ · }") == []


def test_extract_mixed():
    assert db.extract_open_notes("{ } {real one} {.}") == ["real one"]


def test_extract_ignores_braces_inside_html_comments():
    # The brief template puts example braces in a comment — must NOT be captured.
    text = "<!-- напр. {напомни ми за X}, игнорирай -->\n{реална бележка}\n{ }"
    assert db.extract_open_notes(text) == ["реална бележка"]


def test_build_brief_open_notes_header_has_no_capturable_braces():
    # Regression: the open-notes header/comment must not contain {...} that the
    # extractor would later mistake for a real note (caused phantom carried notes).
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [])
    seg = md.split("## 📝 Open notes", 1)[1]
    assert db.extract_open_notes(seg) == []  # a fresh brief has zero real notes


# ── priorities ───────────────────────────────────────────────────────────────

def test_priorities_includes_queue_and_stale():
    out = db.top_priorities(queue=[], stale=[{"project": "StroyOffice"}], queue_depth=2)
    assert any("replay-queue" in p for p in out)
    assert any("StroyOffice" in p for p in out)


def test_priorities_sorted_by_score_and_capped():
    queue = [
        {"id": "a", "type": "boris_rule", "project": "X", "score": "0.3"},
        {"id": "b", "type": "boris_rule", "project": "Y", "score": "0.9"},
    ]
    out = db.top_priorities(queue=queue, stale=[], queue_depth=0, k=3)
    assert len(out) <= 3
    # higher score (Y) appears before lower (X)
    joined = " | ".join(out)
    assert joined.index("Y") < joined.index("X")


def test_priorities_empty_state_is_empty():
    assert db.top_priorities(queue=[], stale=[], queue_depth=0) == []


# ── render ───────────────────────────────────────────────────────────────────

def test_build_brief_has_core_sections():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], ["⚡ do thing"],
                        {}, 0, [])
    assert "# Daily Brief — 2026-06-05" in md
    assert "## 🎯 Днес" in md
    assert "## 📊 Система" in md
    assert "## 📝 Open notes" in md
    assert "{ }" in md  # the writable placeholder
    assert "A** (92/100)" in md or "A (92/100)" in md


def test_build_brief_renders_carried_notes():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0,
                        ["напомни за релийза"])
    assert "Хванати бележки" in md
    assert "напомни за релийза" in md


# ── memory backend (ollama doctor) line ──────────────────────────────────────

def test_build_brief_memory_backend_ok_fresh():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    doctor = {"ts": "2026-06-05T08:00:00+00:00", "ok": True, "stage": "embed"}
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        doctor=doctor)
    assert "🧠 Memory backend: ✓" in md


def test_build_brief_memory_backend_down():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    doctor = {"ts": "2026-06-05T08:00:00+00:00", "ok": False,
              "stage": "spawn", "reason": "access_denied"}
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        doctor=doctor)
    assert "🧠 Memory backend: ❌ DOWN" in md
    assert "stage=spawn" in md and "reason=access_denied" in md


def test_build_brief_memory_backend_stale_or_missing():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    # ok=True but the diag is 3 days old → doctor stopped reporting.
    doctor = {"ts": "2026-06-02T08:00:00+00:00", "ok": True}
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        doctor=doctor)
    assert "🧠 Memory backend: ❓ doctor не е репортвал" in md
    # missing diag entirely
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [])
    assert "🧠 Memory backend: ❓ няма doctor диагностика" in md


# ── gemini briefs line ───────────────────────────────────────────────────────

def test_build_brief_gemini_line_appears_when_count_nonzero():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        gemini_briefs=3)
    assert "📤 3 Gemini brief(s) готови за изпращане" in md
    assert "~/.claude/gemini-tasks/" in md


def test_build_brief_gemini_line_absent_when_count_zero():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        gemini_briefs=0)
    assert "Gemini brief" not in md


def test_build_brief_gemini_default_is_zero():
    # gemini_briefs kwarg defaults to 0 → line absent
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [])
    assert "Gemini brief" not in md


def test_build_brief_gemini_line_in_sistema_section():
    # The line must appear inside the "## 📊 Система" section (before anticipations).
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        gemini_briefs=2)
    sistema_idx = md.index("## 📊 Система")
    gemini_idx = md.index("📤 2 Gemini brief")
    # Gemini line must come after the section header
    assert gemini_idx > sistema_idx


# ── growth pulse + CHECK-beat consumption ─────────────────────────────────────

def _setup_growth(tmp_path, monkeypatch, eval_lines, n_actions=5):
    """Wire a fake growth project + eval trail into daily_brief's paths."""
    logs = tmp_path / "logs"; logs.mkdir()
    skills = tmp_path / "skills" / "growth-loop"; skills.mkdir(parents=True)
    wiki = tmp_path / "wiki"; wiki.mkdir()
    out = wiki / "G.md"
    rows = "".join(f"| {i} | a | l | S | H | http://x |\n" for i in range(1, n_actions + 1))
    out.write_text(
        "---\nweek_of: 2026-06-20\nverdict: needs-attention\n---\n"
        "| # | A | L | E | I | S |\n|---|---|---|---|---|---|\n" + rows,
        encoding="utf-8",
    )
    (skills / "projects.json").write_text(
        json.dumps({"projects": {"p": {"display_name": "P", "output": str(out)}}}),
        encoding="utf-8",
    )
    (logs / "growth-eval.jsonl").write_text("\n".join(eval_lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(db, "LOGS", logs)
    monkeypatch.setattr(db, "SKILLS", skills.parent)


def test_growth_pulse_parses_table_actions_and_frontmatter_verdict(tmp_path, monkeypatch):
    # Regression: actions were parsed from a numbered list, but the file is a table.
    _setup_growth(tmp_path, monkeypatch, ['{"project":"p","verdict":"PASS","score":1.0}'])
    p = db._growth_pulses()[0]
    assert p["actions"] == 5                # table rows counted, not 0
    assert p["verdict"] == "needs-attention"  # read from frontmatter
    assert p["check_verdict"] == "PASS"
    assert p["check_regressed"] is False


def test_growth_pulse_single_pass_not_regressed(tmp_path, monkeypatch):
    _setup_growth(tmp_path, monkeypatch, ['{"project":"p","verdict":"PASS"}'])
    assert db._growth_pulses()[0]["check_regressed"] is False


def test_growth_pulse_detects_sustained_regression(tmp_path, monkeypatch):
    _setup_growth(tmp_path, monkeypatch, [
        '{"project":"p","verdict":"PASS"}',
        '{"project":"p","verdict":"WEAK"}',
        '{"project":"p","verdict":"FAIL","ok":false}',
    ])
    p = db._growth_pulses()[0]
    assert p["check_verdict"] == "FAIL"
    assert p["check_regressed"] is True
    assert p["check_trend"][-2:] == ["WEAK", "FAIL"]


def test_growth_pulse_one_good_run_breaks_streak(tmp_path, monkeypatch):
    _setup_growth(tmp_path, monkeypatch, [
        '{"project":"p","verdict":"WEAK"}',
        '{"project":"p","verdict":"PASS"}',
    ])
    assert db._growth_pulses()[0]["check_regressed"] is False


# ── silenced-not-fixed incidents + hook runtime health lines ─────────────────

def test_build_brief_silenced_line_renders_with_zero_open():
    # 0 open + N silenced is EXACTLY the blind spot the line exists for.
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [],
                        incidents=[], silenced_count=4)
    assert "4 инцидент(а) затворени по замлъкване" in md
    assert "incident_tracker.py" in md


def test_build_brief_no_silenced_line_at_zero():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92}, [], [], {}, 0, [])
    assert "по замлъкване" not in md


def test_build_brief_hook_issues_line():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    health = {"grade": "B", "overall": 85,
              "issues": {"hooks": ["hook auto_pinecone_save: 40% fail (n=50)"]}}
    md = db.build_brief(now, health, [], [], {}, 0, [])
    assert "Hook здраве" in md
    assert "auto_pinecone_save" in md


def test_build_brief_no_hook_line_when_healthy():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = db.build_brief(now, {"grade": "A", "overall": 92,
                              "issues": {"hooks": []}}, [], [], {}, 0, [])
    assert "Hook здраве" not in md


def test_item_type_from_id_discipline():
    assert db._item_type_from_id(
        "discipline-opus-4-8-abs_path_hygiene_pct") == "discipline_gap"
