"""incident_tracker.py — Повтаряща се потребителска жалба = инцидент с приоритет.

Чете boris-candidates.json (7-дневен плъзгащ прозорец), клъстерира примерите
по текстова близост и маркира клъстери с ≥3 уникални примера като OPEN инциденти.

State се пази в ~/.claude/logs/incidents-state.json (персистентно между runs).
Изход:  ~/.claude/logs/incidents.json  (четен от daily_brief).

Usage:
    python incident_tracker.py          # update + write + кратко резюме
    python incident_tracker.py --list   # печата отворените инциденти
    python incident_tracker.py --resolve <id>   # ръчно затваря инцидент
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import string
import sys
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Пътища по подразбиране (DI-injectable)
# ---------------------------------------------------------------------------

_LOGS = Path.home() / ".claude" / "logs"
DEFAULT_CANDIDATES = _LOGS / "boris-candidates.json"
DEFAULT_STATE      = _LOGS / "incidents-state.json"
DEFAULT_OUT        = _LOGS / "incidents.json"

# ---------------------------------------------------------------------------
# Stopwords — кратки / граматически думи на БГ + EN
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    "не", "се", "да", "ли", "за", "на", "от", "със", "като", "това",
    "той", "тя", "то", "че", "си", "ми", "ме", "го", "я", "е", "са",
    "съм", "при", "по", "до", "във", "вв", "и", "или", "но", "а",
    "ако", "как", "кой", "има", "нма", "ще", "беше", "нещо", "всичко",
    "the", "and", "is", "in", "of", "to", "a", "an", "it", "be",
})

# Окончания за груб stemming — по-дългите първи (за greedy match)
_SUFFIXES: tuple[str, ...] = (
    "ята", "ите", "ове", "ото", "ата", "ето",
    "то", "та", "те", "ът", "ят",
)

# Стоп-префикс за системен шум (не потребителски жалби)
_SYSTEM_NOISE_PREFIX = "stop hook feedback"


# ---------------------------------------------------------------------------
# Текстова обработка
# ---------------------------------------------------------------------------

def normalize(text: str) -> list[str]:
    """Нормализира текст до списък от токени за сравнение.

    Стъпки: lowercase → маха пунктуация → split → маха кратки (<3 знака) и
    stopwords → груб stemming на окончания при дума >5 знака.

    Args:
        text: Суров текст (БГ или EN, може с правописни грешки).

    Returns:
        Списък от нормализирани токени. Може да е празен.
    """
    text = text.lower()
    # Маха пунктуация (запазва буквите и цифрите)
    text = text.translate(str.maketrans(string.punctuation, " " * len(string.punctuation)))
    tokens = text.split()

    result: list[str] = []
    for tok in tokens:
        if len(tok) < 3:
            continue
        if tok in _STOPWORDS:
            continue
        # Груб stemming
        if len(tok) > 5:
            for suf in _SUFFIXES:
                if tok.endswith(suf) and len(tok) - len(suf) >= 3:
                    tok = tok[: -len(suf)]
                    break
        result.append(tok)
    return result


def similarity(a: list[str], b: list[str]) -> float:
    """Изчислява текстова близост между два списъка токени.

    Взима max(Jaccard, SequenceMatcher ratio) за по-добро покритие.

    Args:
        a: Нормализирани токени на първия текст.
        b: Нормализирани токени на втория текст.

    Returns:
        Float в [0, 1].
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    set_a = set(a)
    set_b = set(b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    jaccard = intersection / union if union else 0.0

    seq = difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()

    return max(jaccard, seq)


# ---------------------------------------------------------------------------
# Помощни функции за I/O
# ---------------------------------------------------------------------------

def _load_json_tolerant(path: Path, default: Any) -> Any:
    """Зарежда JSON; при липсващ/счупен файл връща *default* без изключение."""
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def _atomic_write(path: Path, data: Any) -> None:
    """Записва JSON атомарно (temp → replace) с UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        # Почистваме temp файла при грешка
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Клъстериране с персистентно състояние
# ---------------------------------------------------------------------------

def update_state(
    candidates: dict[str, Any],
    state: dict[str, Any],
    threshold: float = 0.30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Добавя примери от candidates към state-а (клъстерира по проект).

    Алгоритъм:
    - За всеки пример от candidates (по проект) търси съвпадащ клъстер в
      СЪЩИЯ проект с similarity ≥ threshold.
    - Точни текстови дубликати (от предишен run) НЕ се добавят втори път.
    - Ако няма съвпадение → нов клъстер.
    - Примери, започващи с "Stop hook feedback" (системен шум) — игнорирани.

    Args:
        candidates: Речник от boris-candidates.json (ключ "projects" + "generated").
        state: Текущото персистентно състояние {"clusters": [...]}.
        threshold: Минимална сходност за същия клъстер (default 0.45).
        now: Дата за seen (default utcnow).

    Returns:
        Обновен state речник.
    """
    if now is None:
        now = _now_utc()

    # Извличаме датата от candidates ако е налична
    generated_str: str = candidates.get("generated", now.isoformat())
    try:
        seen_dt = datetime.fromisoformat(generated_str)
        if seen_dt.tzinfo is None:
            seen_dt = seen_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        seen_dt = now

    seen_iso = seen_dt.isoformat()

    clusters: list[dict[str, Any]] = state.get("clusters", [])

    projects_data: dict[str, Any] = candidates.get("projects", {})

    for project, proj_info in projects_data.items():
        if not isinstance(proj_info, dict):
            continue
        examples: list[Any] = proj_info.get("examples") or []

        for raw_example in examples:
            if not isinstance(raw_example, str) or not raw_example.strip():
                continue

            example = raw_example.strip()

            # Филтриране на системен шум
            if example.lower().startswith(_SYSTEM_NOISE_PREFIX):
                continue

            norm_ex = normalize(example)

            # Намери най-добрия клъстер в същия проект
            best_cluster: dict[str, Any] | None = None
            best_score: float = 0.0

            for cluster in clusters:
                if cluster.get("project") != project:
                    continue
                rep_norm = normalize(cluster.get("representative", ""))
                score = similarity(norm_ex, rep_norm)
                if score >= threshold and score > best_score:
                    best_score = score
                    best_cluster = cluster

            if best_cluster is not None:
                # Дедупликация — не добавяй точен дубликат
                existing_texts = {e["text"] for e in best_cluster.get("examples", [])}
                if example not in existing_texts:
                    best_cluster.setdefault("examples", []).append(
                        {"text": example, "seen": seen_iso}
                    )
                    best_cluster["last_seen"] = seen_iso
                    # Обнови representative ако новият пример е по-дълъг
                    if len(example) > len(best_cluster.get("representative", "")):
                        best_cluster["representative"] = example
            else:
                # Нов клъстер
                cluster_id = str(uuid.uuid4())[:8]
                clusters.append({
                    "id": cluster_id,
                    "project": project,
                    "representative": example,
                    "examples": [{"text": example, "seen": seen_iso}],
                    "first_seen": seen_iso,
                    "last_seen": seen_iso,
                    "status": "open",
                })

    state["clusters"] = clusters
    return state


def derive_incidents(
    state: dict[str, Any],
    min_count: int = 3,
    resolve_after_days: int = 14,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Извлича инциденти от state-а.

    Клъстер с ≥ min_count уникални примера = OPEN инцидент.
    Клъстер без нов пример ≥ resolve_after_days → status resolved.

    Args:
        state: Персистентен state с "clusters".
        min_count: Минимален брой примери за инцидент (default 3).
        resolve_after_days: Дни без нов пример преди auto-resolve (default 14).
        now: Текущо UTC време (default utcnow).

    Returns:
        Списък с инцидент-речници (id, project, title, count, first_seen,
        last_seen, examples, status).
    """
    if now is None:
        now = _now_utc()

    incidents: list[dict[str, Any]] = []
    resolve_cutoff = now - timedelta(days=resolve_after_days)

    for cluster in state.get("clusters", []):
        examples: list[dict[str, Any]] = cluster.get("examples", [])
        count = len(examples)
        if count < min_count:
            continue

        # Ако е ръчно затворен — пропускаме
        if cluster.get("status") == "resolved":
            # Все пак го включваме в resolved_recent ако е скорошен
            incidents.append(_make_incident(cluster, "resolved"))
            continue

        # Auto-resolve ако няма нов пример ≥ resolve_after_days
        last_seen_str = cluster.get("last_seen", "")
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_str)
            if last_seen_dt.tzinfo is None:
                last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last_seen_dt = now

        if last_seen_dt < resolve_cutoff:
            status = "resolved"
            cluster["status"] = "resolved"
        else:
            status = "open"

        incidents.append(_make_incident(cluster, status))

    return incidents


def _make_incident(cluster: dict[str, Any], status: str) -> dict[str, Any]:
    """Конструира инцидент-речник от клъстер."""
    examples = cluster.get("examples", [])
    count = len(examples)

    # title = най-дългият representative, отрязан до 90 знака
    representative = cluster.get("representative", "")
    title = representative[:90] if representative else "(без заглавие)"

    # До 3 примера за изход
    sample_examples = [e["text"] for e in examples[:3]]

    return {
        "id": cluster.get("id", ""),
        "project": cluster.get("project", ""),
        "title": title,
        "count": count,
        "first_seen": cluster.get("first_seen", ""),
        "last_seen": cluster.get("last_seen", ""),
        "examples": sample_examples,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Четене / запис на state и изход
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    """Зарежда incidents-state.json; при проблем връща празен state."""
    data = _load_json_tolerant(path, {})
    if not isinstance(data, dict):
        return {"clusters": []}
    if "clusters" not in data or not isinstance(data["clusters"], list):
        data["clusters"] = []
    return data


def load_candidates(path: Path) -> dict[str, Any]:
    """Зарежда boris-candidates.json; при проблем връща празен candidates."""
    data = _load_json_tolerant(path, {})
    if not isinstance(data, dict):
        return {"projects": {}}
    if "projects" not in data:
        data["projects"] = {}
    return data


def build_output(
    incidents: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Строи финалния incidents.json речник.

    Args:
        incidents: Списък от derive_incidents().
        now: Текущо UTC за "generated".

    Returns:
        Речник с ключове "generated", "open", "resolved_recent".
    """
    if now is None:
        now = _now_utc()

    open_inc = [i for i in incidents if i["status"] == "open"]
    # "recent" must mean recent — without the 30d window every resolved
    # incident ever would accumulate and incidents.json grows unbounded.
    cutoff = (now - timedelta(days=30)).isoformat()
    resolved = [i for i in incidents
                if i["status"] == "resolved" and str(i.get("last_seen", "")) >= cutoff]

    return {
        "generated": now.isoformat(),
        "open": open_inc,
        "resolved_recent": resolved,
    }


def prune_state(state: dict[str, Any], max_silent_days: int = 60,
                now: datetime | None = None) -> dict[str, Any]:
    """Drop clusters with no new example for *max_silent_days*.

    Without pruning, incidents-state.json grows forever (every correction adds
    examples; nothing is ever removed). A cluster silent for 60 days is either
    fixed or abandoned — if the complaint returns, a fresh cluster forms and
    needs min_count repeats again, which is the desired fail-safe behaviour.
    """
    if now is None:
        now = _now_utc()
    cutoff = (now - timedelta(days=max_silent_days)).isoformat()
    kept = [c for c in state.get("clusters", [])
            if str(c.get("last_seen", "")) >= cutoff]
    return {**state, "clusters": kept}


# ---------------------------------------------------------------------------
# CLI команди
# ---------------------------------------------------------------------------

def cmd_update(
    candidates_path: Path = DEFAULT_CANDIDATES,
    state_path: Path = DEFAULT_STATE,
    out_path: Path = DEFAULT_OUT,
    now: datetime | None = None,
) -> int:
    """Обновява state, изчислява инциденти, записва incidents.json.

    Returns:
        Брой отворени инциденти.
    """
    if now is None:
        now = _now_utc()

    candidates = load_candidates(candidates_path)
    state = load_state(state_path)

    state = update_state(candidates, state, now=now)
    state = prune_state(state, now=now)
    _atomic_write(state_path, state)

    incidents = derive_incidents(state, now=now)
    output = build_output(incidents, now=now)
    _atomic_write(out_path, output)

    open_count = len(output["open"])
    resolved_count = len(output["resolved_recent"])

    print(f"Incidents: {open_count} open, {resolved_count} resolved_recent")
    for inc in output["open"]:
        print(f"  [OPEN] {inc['id']} | {inc['project']} | count={inc['count']} | {inc['title'][:70]}")

    return open_count


def cmd_list(
    out_path: Path = DEFAULT_OUT,
) -> None:
    """Печата отворените инциденти от incidents.json."""
    data = _load_json_tolerant(out_path, {})
    if not isinstance(data, dict):
        print("incidents.json липсва или е счупен.")
        return

    open_inc = data.get("open", [])
    if not open_inc:
        print("Няма отворени инциденти.")
        return

    print(f"Отворени инциденти ({len(open_inc)}):")
    for inc in open_inc:
        print(
            f"  [{inc['id']}] {inc['project']} | "
            f"брой={inc['count']} | last={inc.get('last_seen','?')[:10]}"
        )
        print(f"    {inc['title']}")


def cmd_resolve(
    incident_id: str,
    state_path: Path = DEFAULT_STATE,
    out_path: Path = DEFAULT_OUT,
    now: datetime | None = None,
) -> bool:
    """Ръчно затваря инцидент по ID.

    Args:
        incident_id: Краткото ID на клъстера.
        state_path: Пътека до incidents-state.json.
        out_path: Пътека до incidents.json.
        now: Текущо UTC.

    Returns:
        True ако е намерен и затворен, False иначе.
    """
    if now is None:
        now = _now_utc()

    state = load_state(state_path)
    found = False
    for cluster in state.get("clusters", []):
        if cluster.get("id") == incident_id:
            cluster["status"] = "resolved"
            found = True
            break

    if not found:
        print(f"Инцидент '{incident_id}' не е намерен.")
        return False

    _atomic_write(state_path, state)

    # Презапис на incidents.json
    incidents = derive_incidents(state, now=now)
    output = build_output(incidents, now=now)
    _atomic_write(out_path, output)

    print(f"Инцидент '{incident_id}' е затворен.")
    return True


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Incident Tracker — повтаряща се жалба = инцидент."
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Печата отворените инциденти."
    )
    parser.add_argument(
        "--resolve", metavar="ID",
        help="Ръчно затваря инцидент по ID."
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.resolve:
        cmd_resolve(args.resolve)
    else:
        cmd_update()

    sys.exit(0)


if __name__ == "__main__":
    main()
