"""Tests for incident_tracker.py — повтаряща се жалба = инцидент."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

# Фиксирано "сега" за детерминирани тестове
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
PAST_RECENT = NOW - timedelta(days=3)
PAST_OLD = NOW - timedelta(days=20)   # > resolve_after_days=14


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidates(project: str, examples: list[str], generated: str | None = None) -> dict:
    """Строи минимален candidates-речник за тестване."""
    if generated is None:
        generated = NOW.isoformat()
    return {
        "generated": generated,
        "window_days": 7,
        "projects": {
            project: {"count": len(examples), "examples": examples},
        },
    }


def _empty_state() -> dict:
    return {"clusters": []}


# ---------------------------------------------------------------------------
# 1. normalize() — токенизация и stemming
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_lowercase_and_punctuation_removed(self):
        from incident_tracker import normalize
        result = normalize("Клавишната! Комбинация?")
        assert all(tok == tok.lower() for tok in result)
        assert all("!" not in tok and "?" not in tok for tok in result)

    def test_short_tokens_removed(self):
        from incident_tracker import normalize
        result = normalize("не се да ли а и")
        assert result == []

    def test_stopwords_removed(self):
        from incident_tracker import normalize
        result = normalize("клавишната комбинация не работи")
        assert "не" not in result
        # значимите думи присъстват
        assert any("клавишна" in tok or "комбинаци" in tok for tok in result)

    def test_stemming_removes_suffix(self):
        from incident_tracker import normalize
        # "прозорците" → "прозорц" (суфикс "ите")
        result = normalize("прозорците изскачат")
        assert any(tok.startswith("прозорц") for tok in result)

    def test_empty_string(self):
        from incident_tracker import normalize
        assert normalize("") == []

    def test_keeps_meaningful_tokens(self):
        from incident_tracker import normalize
        result = normalize("терминалът изскача постоянно")
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# 2. similarity() — текстова близост
# ---------------------------------------------------------------------------

class TestSimilarity:
    def test_identical_texts_score_one(self):
        from incident_tracker import normalize, similarity
        a = normalize("клавишната комбинация не работи")
        assert similarity(a, a) == pytest.approx(1.0)

    def test_similar_bg_messages_above_threshold(self):
        from incident_tracker import normalize, similarity
        a = normalize("клавишната комбинация за четене отново не работи")
        b = normalize("програмата неработи клавишната комбинация отново не стартира четенето")
        score = similarity(a, b)
        assert score >= 0.30, f"Score {score} — твърде нисък за сходни съобщения"

    def test_unrelated_texts_low_score(self):
        from incident_tracker import normalize, similarity
        a = normalize("клавишната комбинация не работи")
        b = normalize("фактура не се изпраща по имейл")
        score = similarity(a, b)
        assert score < 0.45, f"Score {score} — фалшиво позитивен"

    def test_empty_both(self):
        from incident_tracker import similarity
        assert similarity([], []) == pytest.approx(1.0)

    def test_empty_one_side(self):
        from incident_tracker import normalize, similarity
        a = normalize("нещо важно")
        assert similarity(a, []) == pytest.approx(0.0)
        assert similarity([], a) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. update_state() — клъстериране на сходни БГ съобщения
# ---------------------------------------------------------------------------

class TestUpdateState:
    def test_reed_keyboard_shortcut_clusters(self):
        """Трите Reed жалби за клавишна комбинация трябва да попаднат в 1 клъстер."""
        from incident_tracker import update_state

        candidates = _candidates("J--Antigraviti-Reed", [
            "клавишната комбинация за четене отново не работи",
            "програмата неработи клавишната комбинация отново не стартира четенето",
            "отново има проблем пап клавишната комбинация не стартира четенето",
        ])
        state = update_state(candidates, _empty_state(), now=NOW)

        reed_clusters = [c for c in state["clusters"] if c["project"] == "J--Antigraviti-Reed"]
        assert len(reed_clusters) == 1, f"Очакван 1 клъстер, получен {len(reed_clusters)}"
        assert len(reed_clusters[0]["examples"]) == 3

    def test_trading_terminal_clusters(self):
        """Петте Trading жалби (реални пълни текстове) → поне един клъстер с ≥3 примера."""
        from incident_tracker import update_state, derive_incidents

        # Реалните пълни съобщения от boris-candidates.json (не съкратени!)
        candidates = _candidates("J--Obsidian-Resurch-Claude-Trading", [
            "постоянно се отваря терминала на компютъра периодично през известно"
            " време може ли този процес да го скриеш така че да не ми затваря"
            " другите приложения и да не м",
            "отново се отваря терминала и не се затваря са",
            "това не е нормален начин на работа преди не беше така има проблем",
            "продължават да изскачат прозорци направи всичко каквото трябва за"
            " да не се появяват тези изскачащи прозорци повече",
            "неееее не е спокоен цял ден немога да работя заради тези смотани"
            "  прозорци който се отварят отварям да ги спреш",
        ])
        state = update_state(candidates, _empty_state(), threshold=0.30, now=NOW)
        incidents = derive_incidents(state, min_count=3, now=NOW)

        # Трябва да има поне 1 отворен инцидент за Trading
        trading_open = [
            i for i in incidents
            if i["status"] == "open"
            and i["project"] == "J--Obsidian-Resurch-Claude-Trading"
        ]
        assert trading_open, "Няма open инцидент за Trading терминал/прозорци"
        assert trading_open[0]["count"] >= 3

    def test_stop_hook_noise_filtered(self):
        """Примери Started с 'Stop hook feedback' се игнорират."""
        from incident_tracker import update_state

        candidates = _candidates("J--Antigraviti-Reed", [
            "Stop hook feedback:\n⚠ Wiki/Reed: log.md не е обновено",
            "клавишната комбинация не работи",
        ])
        state = update_state(candidates, _empty_state(), now=NOW)

        reed_clusters = [c for c in state["clusters"] if c["project"] == "J--Antigraviti-Reed"]
        all_examples = [e["text"] for c in reed_clusters for e in c["examples"]]
        assert all(not t.lower().startswith("stop hook") for t in all_examples)
        assert len(all_examples) == 1  # само реалната жалба

    def test_dedup_same_example_across_runs(self):
        """Същият пример от предишен run не брои втори път."""
        from incident_tracker import update_state

        msg = "клавишната комбинация не работи"
        cand1 = _candidates("J--Antigraviti-Reed", [msg], generated="2026-06-03T10:00:00")
        cand2 = _candidates("J--Antigraviti-Reed", [msg], generated="2026-06-05T10:00:00")

        state = _empty_state()
        state = update_state(cand1, state, now=NOW)
        state = update_state(cand2, state, now=NOW)

        reed_clusters = [c for c in state["clusters"] if c["project"] == "J--Antigraviti-Reed"]
        assert len(reed_clusters[0]["examples"]) == 1  # не 2

    def test_different_projects_separate_clusters(self):
        """Едно и също съобщение в два различни проекта → два отделни клъстера."""
        from incident_tracker import update_state

        cand = {
            "generated": NOW.isoformat(),
            "window_days": 7,
            "projects": {
                "Proj-A": {"count": 1, "examples": ["клавишната не работи"]},
                "Proj-B": {"count": 1, "examples": ["клавишната не работи"]},
            },
        }
        state = update_state(cand, _empty_state(), now=NOW)
        projects_in_state = {c["project"] for c in state["clusters"]}
        assert "Proj-A" in projects_in_state
        assert "Proj-B" in projects_in_state

    def test_new_similar_message_extends_cluster(self):
        """Нов (различен) сходен текст разширява съществуващ клъстер."""
        from incident_tracker import update_state

        cand1 = _candidates("Proj", ["клавишната комбинация не работи"], "2026-06-03T10:00:00")
        state = update_state(cand1, _empty_state(), now=NOW)
        initial_count = len(state["clusters"][0]["examples"])

        cand2 = _candidates("Proj", ["клавишната комбинация отново не стартира"], "2026-06-05T10:00:00")
        state = update_state(cand2, state, now=NOW)
        new_count = len(state["clusters"][0]["examples"])

        assert new_count > initial_count


# ---------------------------------------------------------------------------
# 4. derive_incidents() — прагове и auto-resolve
# ---------------------------------------------------------------------------

class TestDeriveIncidents:
    def test_cluster_with_3_examples_is_open(self):
        from incident_tracker import derive_incidents

        state = {
            "clusters": [{
                "id": "abc123",
                "project": "Proj",
                "representative": "клавишната комбинация не работи отново",
                "examples": [
                    {"text": "клавишната комбинация не работи", "seen": PAST_RECENT.isoformat()},
                    {"text": "клавишната не стартира", "seen": PAST_RECENT.isoformat()},
                    {"text": "отново не работи клавишната", "seen": PAST_RECENT.isoformat()},
                ],
                "first_seen": PAST_RECENT.isoformat(),
                "last_seen": PAST_RECENT.isoformat(),
                "status": "open",
            }]
        }
        incidents = derive_incidents(state, min_count=3, now=NOW)
        open_inc = [i for i in incidents if i["status"] == "open"]
        assert len(open_inc) == 1
        assert open_inc[0]["count"] == 3

    def test_cluster_with_2_examples_below_threshold(self):
        from incident_tracker import derive_incidents

        state = {
            "clusters": [{
                "id": "xyz",
                "project": "Proj",
                "representative": "само две",
                "examples": [
                    {"text": "едно", "seen": PAST_RECENT.isoformat()},
                    {"text": "две", "seen": PAST_RECENT.isoformat()},
                ],
                "first_seen": PAST_RECENT.isoformat(),
                "last_seen": PAST_RECENT.isoformat(),
                "status": "open",
            }]
        }
        incidents = derive_incidents(state, min_count=3, now=NOW)
        assert incidents == []

    def test_old_cluster_auto_resolved(self):
        """Клъстер без нов пример ≥14 дни → auto-resolved."""
        from incident_tracker import derive_incidents

        state = {
            "clusters": [{
                "id": "old1",
                "project": "Proj",
                "representative": "стар проблем",
                "examples": [
                    {"text": "стар проблем 1", "seen": PAST_OLD.isoformat()},
                    {"text": "стар проблем 2", "seen": PAST_OLD.isoformat()},
                    {"text": "стар проблем 3", "seen": PAST_OLD.isoformat()},
                ],
                "first_seen": PAST_OLD.isoformat(),
                "last_seen": PAST_OLD.isoformat(),
                "status": "open",
            }]
        }
        incidents = derive_incidents(state, min_count=3, resolve_after_days=14, now=NOW)
        resolved = [i for i in incidents if i["status"] == "resolved"]
        open_inc = [i for i in incidents if i["status"] == "open"]
        assert len(resolved) == 1
        assert len(open_inc) == 0

    def test_title_truncated_to_90(self):
        from incident_tracker import derive_incidents

        long_text = "к" * 150
        state = {
            "clusters": [{
                "id": "t1",
                "project": "Proj",
                "representative": long_text,
                "examples": [
                    {"text": f"пример {i}", "seen": PAST_RECENT.isoformat()}
                    for i in range(3)
                ],
                "first_seen": PAST_RECENT.isoformat(),
                "last_seen": PAST_RECENT.isoformat(),
                "status": "open",
            }]
        }
        incidents = derive_incidents(state, min_count=3, now=NOW)
        assert len(incidents[0]["title"]) <= 90

    def test_examples_capped_at_3(self):
        from incident_tracker import derive_incidents

        state = {
            "clusters": [{
                "id": "cap",
                "project": "Proj",
                "representative": "много примери",
                "examples": [
                    {"text": f"пример {i}", "seen": PAST_RECENT.isoformat()}
                    for i in range(6)
                ],
                "first_seen": PAST_RECENT.isoformat(),
                "last_seen": PAST_RECENT.isoformat(),
                "status": "open",
            }]
        }
        incidents = derive_incidents(state, min_count=3, now=NOW)
        assert len(incidents[0]["examples"]) <= 3


# ---------------------------------------------------------------------------
# 5. Толерантни loaders — липсващ / счупен JSON
# ---------------------------------------------------------------------------

class TestTolerantLoaders:
    def test_load_state_missing_file(self, tmp_path):
        from incident_tracker import load_state
        state = load_state(tmp_path / "nonexistent.json")
        assert state == {"clusters": []}

    def test_load_state_corrupt_json(self, tmp_path):
        from incident_tracker import load_state
        p = tmp_path / "bad.json"
        p.write_text("{not valid{{", encoding="utf-8")
        state = load_state(p)
        assert state == {"clusters": []}

    def test_load_candidates_missing_file(self, tmp_path):
        from incident_tracker import load_candidates
        candidates = load_candidates(tmp_path / "nonexistent.json")
        assert candidates == {"projects": {}}

    def test_load_candidates_corrupt_json(self, tmp_path):
        from incident_tracker import load_candidates
        p = tmp_path / "bad.json"
        p.write_text("[[[[broken", encoding="utf-8")
        candidates = load_candidates(p)
        assert candidates == {"projects": {}}


# ---------------------------------------------------------------------------
# 6. cmd_update() — E2E интеграционен тест с tmp_path
# ---------------------------------------------------------------------------

class TestCmdUpdate:
    def test_full_update_creates_incidents_json(self, tmp_path):
        from incident_tracker import cmd_update

        # Reed с 3 сходни жалби за клавишна комбинация
        candidates = {
            "generated": NOW.isoformat(),
            "window_days": 7,
            "projects": {
                "J--Antigraviti-Reed": {
                    "count": 3,
                    "examples": [
                        "клавишната комбинация за четене отново не работи",
                        "програмата неработи клавишната комбинация отново не стартира четенето",
                        "отново има проблем клавишната комбинация не стартира четенето",
                    ],
                }
            },
        }
        cand_path = tmp_path / "boris-candidates.json"
        cand_path.write_text(json.dumps(candidates), encoding="utf-8")

        state_path = tmp_path / "incidents-state.json"
        out_path = tmp_path / "incidents.json"

        open_count = cmd_update(
            candidates_path=cand_path,
            state_path=state_path,
            out_path=out_path,
            now=NOW,
        )

        assert open_count >= 1
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert "open" in data
        assert len(data["open"]) >= 1
        assert data["open"][0]["project"] == "J--Antigraviti-Reed"

    def test_update_idempotent_on_same_run(self, tmp_path):
        """Два пъти същите данни → count НЕ се удвоява (dedup работи)."""
        from incident_tracker import cmd_update

        candidates = {
            "generated": NOW.isoformat(),
            "window_days": 7,
            "projects": {
                "Proj": {
                    "count": 3,
                    "examples": ["жалба едно", "жалба две", "жалба три"],
                }
            },
        }
        cand_path = tmp_path / "boris-candidates.json"
        cand_path.write_text(json.dumps(candidates), encoding="utf-8")
        state_path = tmp_path / "state.json"
        out_path = tmp_path / "out.json"

        cmd_update(candidates_path=cand_path, state_path=state_path, out_path=out_path, now=NOW)
        cmd_update(candidates_path=cand_path, state_path=state_path, out_path=out_path, now=NOW)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        clusters = [c for c in state["clusters"] if c["project"] == "Proj"]
        total = sum(len(c["examples"]) for c in clusters)
        assert total == 3  # не 6


# ---------------------------------------------------------------------------
# 7. cmd_resolve() — ръчно затваряне
# ---------------------------------------------------------------------------

class TestCmdResolve:
    def test_resolve_marks_cluster_resolved(self, tmp_path):
        from incident_tracker import cmd_resolve, load_state

        state = {
            "clusters": [{
                "id": "testid",
                "project": "Proj",
                "representative": "клавишна проблем",
                "examples": [
                    {"text": "жалба 1", "seen": PAST_RECENT.isoformat()},
                    {"text": "жалба 2", "seen": PAST_RECENT.isoformat()},
                    {"text": "жалба 3", "seen": PAST_RECENT.isoformat()},
                ],
                "first_seen": PAST_RECENT.isoformat(),
                "last_seen": PAST_RECENT.isoformat(),
                "status": "open",
            }]
        }
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        out_path = tmp_path / "out.json"

        result = cmd_resolve("testid", state_path=state_path, out_path=out_path, now=NOW)
        assert result is True

        updated = load_state(state_path)
        cluster = updated["clusters"][0]
        assert cluster["status"] == "resolved"

    def test_resolve_nonexistent_id_returns_false(self, tmp_path):
        from incident_tracker import cmd_resolve

        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"clusters": []}), encoding="utf-8")
        out_path = tmp_path / "out.json"

        result = cmd_resolve("ghost-id", state_path=state_path, out_path=out_path, now=NOW)
        assert result is False


# ---------------------------------------------------------------------------
# 8. Facturka разнородност — НЕ трябва да генерира false-positive инцидент
# ---------------------------------------------------------------------------

class TestFacturkaNoFalsePositives:
    def test_facturka_diverse_messages_no_big_cluster(self):
        """Реалните разнородни Facturka жалби НЕ трябва да образуват инцидент ≥3."""
        from incident_tracker import update_state, derive_incidents

        # Реалните пълни съобщения от boris-candidates.json — разнородни теми
        candidates = _candidates("j--Antigraviti-Facturka-bg", [
            "последният фаил кото снимах за разход не се отчете не го виждам"
            " в списъка за последният месеж",
            "искам да финализираме задачите искам да завършиш всичко което"
            " трябва и да качим всичко на сървара което не е качено",
            "провери началното табло мисля че не показва всички данни"
            " който трябва да показва",
            "в момента сайта не зарежда",
            "направи това което препоръчваш дейтвай самостоятелно и реши"
            " всички проблеми с който се сблъскаш докато не постигнеш перфектни резултати",
        ])
        state = update_state(candidates, _empty_state(), threshold=0.30, now=NOW)
        incidents = derive_incidents(state, min_count=3, now=NOW)
        open_inc = [i for i in incidents if i["status"] == "open"
                    and i["project"] == "j--Antigraviti-Facturka-bg"]
        assert open_inc == [], (
            f"Facturka генерира фалшив инцидент: {[i['title'] for i in open_inc]}"
        )
