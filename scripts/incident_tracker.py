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
import re
import string
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from core_io import load_json_tolerant as _load_json_tolerant, atomic_write_json as _atomic_write, now_utc as _now_utc

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Пътища по подразбиране (DI-injectable)
# ---------------------------------------------------------------------------

_LOGS = Path.home() / ".claude" / "logs"
DEFAULT_CANDIDATES = _LOGS / "boris-candidates.json"
DEFAULT_STATE      = _LOGS / "incidents-state.json"
DEFAULT_OUT        = _LOGS / "incidents.json"
DEFAULT_PROPOSALS  = _LOGS / "fix-proposals.json"

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

# Delegation / meta-instruction phrases: the user directing the assistant to act,
# NOT a malfunction report. These polluted incidents (e.g. the Facturka
# "направи това което препоръчваш действай самостоятелно реши всички проблеми"
# cluster). Filtered ONLY when no malfunction signal is present — see
# _is_delegation_noise (conservative, to avoid dropping real complaints).
_DELEGATION_MARKERS: tuple[str, ...] = (
    "направи това което препоръчваш",
    "действай самостоятелно",
    "дейтвай самостоятелно",
    "реши всички проблеми",
    "реши всичките проблеми",
    "поправи всички проблеми",
    "поправи вички проблеми",
    "направи всичко което не си направил",
    "направи комит",
    "направи commit",
    "продължи по приоритети",
    "извлечи поука",
    "do what you recommend",
    "act autonomously",
    "make a commit",
)

# Concrete malfunction signals. If ANY is present the message is a genuine
# complaint and is NEVER treated as delegation noise (false-negative guard).
# Deliberately excludes generic words like "проблем"/"отново" that also appear
# inside delegations.
_MALFUNCTION_SIGNALS: tuple[str, ...] = (
    "не работи", "неработи", "не се вижда", "не се виждат", "не стартира",
    "не се отваря", "не зарежда", "не се затваря", "не показва", "не става",
    "счуп", "грешка", "бъг", "крашва", "забива", "изскача", "изскачат",
    "error", "broken", "bug", "crash",
)


def _is_delegation_noise(text: str) -> bool:
    """True if *text* is a delegation/meta-instruction, not a malfunction report.

    Conservative by design: a message counts as delegation noise ONLY when it
    matches a known delegation phrase AND contains no concrete malfunction
    signal. This keeps real complaints phrased as commands (e.g. "направи всичко
    за да спрат изскачащите прозорци") out of the filter.

    Args:
        text: Raw user message.

    Returns:
        True if the message should be skipped as delegation noise.
    """
    low = text.lower()
    if any(sig in low for sig in _MALFUNCTION_SIGNALS):
        return False
    return any(marker in low for marker in _DELEGATION_MARKERS)


# Financial / brokerage personal data: real-money transfers and portfolio
# positions must NEVER accumulate in incidents-state.json (plaintext log that
# feeds subagent prompts). Dropped UNCONDITIONALLY — this is a privacy guard,
# not a malfunction classifier. Tickers require a position/action qualifier so a
# bare mention inside an unrelated complaint is not over-matched.
_FINANCIAL_NOISE_RE = re.compile(
    r"реални пари|истински пари|прехвърл\w*\s+\w*\s*пари|"
    r"\brevolut\b|банков\w*\s+сметк|\bIBAN\b|"
    r"\b(IONQ|NVDA|NVIDIA|INTC|AAPL|TSLA|AMD|MSFT|GOOGL|META|AMZN)\b"
    r".{0,40}?(\d+\s*%|прода|купув|позици)",
    re.IGNORECASE,
)


def _is_financial_noise(text: str) -> bool:
    """True if *text* carries real-money / brokerage personal data.

    Unlike delegation noise this drops unconditionally: money transfers and
    portfolio positions are sensitive personal data that must not be persisted
    to the plaintext incident log, regardless of any malfunction signal present.

    Args:
        text: Raw user message.

    Returns:
        True if the message should be skipped as financial personal data.
    """
    return bool(_FINANCIAL_NOISE_RE.search(text))


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
# Клъстериране с персистентно състояние
# ---------------------------------------------------------------------------

def update_state(
    candidates: dict[str, Any],
    state: dict[str, Any],
    threshold: float = 0.45,
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
            # Филтриране на delegation/meta-инструкции (не са malfunction жалби)
            if _is_delegation_noise(example):
                continue
            # Финансови / брокерски лични данни — никога не персистирай (privacy)
            if _is_financial_noise(example):
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
    Клъстер без нов пример ≥ resolve_after_days → status resolved (resolution="silence").
    Ръчно затворен клъстер → status resolved, resolution запазен от cluster.

    Args:
        state: Персистентен state с "clusters".
        min_count: Минимален брой примери за инцидент (default 3).
        resolve_after_days: Дни без нов пример преди auto-resolve (default 14).
        now: Текущо UTC време (default utcnow).

    Returns:
        Списък с инцидент-речници (id, project, title, count, first_seen,
        last_seen, examples, status, resolution).
        ``resolution`` може да е "fixed" | "silence" | None.
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

        # Ако е ръчно затворен — включваме с текущия resolution
        if cluster.get("status") == "resolved":
            resolution: str | None = cluster.get("resolution", None)
            incidents.append(_make_incident(cluster, "resolved", resolution=resolution))
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
            # Auto-resolve by silence — only set if not already explicitly set
            if cluster.get("resolution") is None:
                cluster["resolution"] = "silence"
            incident_resolution: str | None = cluster.get("resolution")
        else:
            status = "open"
            incident_resolution = None

        incidents.append(_make_incident(cluster, status, resolution=incident_resolution))

    return incidents


def reopen_if_recurs(
    state: dict[str, Any],
    candidates: dict[str, Any],
    now: datetime | None = None,
    threshold: float = 0.45,
) -> tuple[dict[str, Any], list[str]]:
    """Reopen resolved clusters that receive a new matching example.

    If a resolved cluster (status='resolved') gets a new similar example from
    *candidates*, it is reopened: status='open', resolution=None.

    This prevents regressions from being masked by a prior resolve.

    Args:
        state: Persistent state with "clusters".
        candidates: boris-candidates.json dict with "projects".
        now: Current UTC time (default utcnow).
        threshold: Similarity threshold to match new example to cluster.

    Returns:
        Tuple of (updated_state, list_of_reopened_cluster_ids).
    """
    if now is None:
        now = _now_utc()

    seen_iso = now.isoformat()
    reopened_ids: list[str] = []

    projects_data: dict[str, Any] = candidates.get("projects", {})

    for project, proj_info in projects_data.items():
        if not isinstance(proj_info, dict):
            continue
        examples: list[Any] = proj_info.get("examples") or []

        for raw_example in examples:
            if not isinstance(raw_example, str) or not raw_example.strip():
                continue
            example = raw_example.strip()

            # Skip system noise
            if example.lower().startswith(_SYSTEM_NOISE_PREFIX):
                continue
            # Skip delegation / meta-instructions (not malfunction complaints)
            if _is_delegation_noise(example):
                continue
            # Skip real-money / brokerage personal data (privacy — never persist)
            if _is_financial_noise(example):
                continue

            norm_ex = normalize(example)

            for cluster in state.get("clusters", []):
                if cluster.get("project") != project:
                    continue
                if cluster.get("status") != "resolved":
                    continue

                rep_norm = normalize(cluster.get("representative", ""))
                score = similarity(norm_ex, rep_norm)
                if score < threshold:
                    continue

                # Check it's not already in the cluster (no exact duplicate)
                existing_texts = {e["text"] for e in cluster.get("examples", [])}
                if example in existing_texts:
                    continue

                # Reopen the cluster
                cluster["status"] = "open"
                cluster["resolution"] = None
                cluster.setdefault("examples", []).append(
                    {"text": example, "seen": seen_iso}
                )
                cluster["last_seen"] = seen_iso
                if len(example) > len(cluster.get("representative", "")):
                    cluster["representative"] = example

                cid = cluster.get("id", "")
                if cid not in reopened_ids:
                    reopened_ids.append(cid)

    return state, reopened_ids


def reconcile_with_proposals(
    state: dict[str, Any],
    proposals: list[dict[str, Any]],
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Close incident clusters whose fix-proposal the user already resolved.

    A fix-proposal carries the SAME id as its incident cluster. When the user
    marks a proposal ``status='resolved'`` (e.g. confirmed already-fixed), the
    matching open cluster must be closed too — otherwise incidents.json keeps
    reporting a bug that is done (the incidents-status desync).

    Only ``status == 'resolved'`` closes a cluster. ``'accepted'`` (a fix is
    queued but not yet done), ``'suppressed'`` (won't auto-fix — e.g. a
    user-handled project) and ``'proposed'`` deliberately leave the incident
    OPEN: those are still live problems.

    Args:
        state: Persistent state with "clusters".
        proposals: List of proposal dicts from fix-proposals.json.
        now: Current UTC time (unused today; kept for signature symmetry/testability).

    Returns:
        Tuple of (updated_state, list_of_closed_cluster_ids).
    """
    resolved_ids = {
        str(p.get("id")) for p in proposals
        if isinstance(p, dict) and p.get("status") == "resolved" and p.get("id")
    }
    if not resolved_ids:
        return state, []

    closed: list[str] = []
    for cluster in state.get("clusters", []):
        cid = str(cluster.get("id", ""))
        if cid in resolved_ids and cluster.get("status") != "resolved":
            cluster["status"] = "resolved"
            if cluster.get("resolution") is None:
                cluster["resolution"] = "fixed"
            closed.append(cid)
    return state, closed


def _make_incident(
    cluster: dict[str, Any],
    status: str,
    resolution: str | None = None,
) -> dict[str, Any]:
    """Конструира инцидент-речник от клъстер.

    Args:
        cluster: Cluster dict from state.
        status: "open" | "resolved".
        resolution: "fixed" | "silence" | None.

    Returns:
        Incident dict with backwards-compatible fields plus new ``resolution``.
    """
    examples = cluster.get("examples", [])
    count = len(examples)

    # title = най-дългият representative, отрязан до 90 знака
    representative = cluster.get("representative", "")
    title = representative[:90] if representative else "(без заглавие)"

    # До 5 примера за изход — съвпада с proposer-а (examples[:5]), за да не
    # се изпускат най-новите повторения от fix-промпта.
    sample_examples = [e["text"] for e in examples[:5]]

    return {
        "id": cluster.get("id", ""),
        "project": cluster.get("project", ""),
        "title": title,
        "count": count,
        "first_seen": cluster.get("first_seen", ""),
        "last_seen": cluster.get("last_seen", ""),
        "examples": sample_examples,
        "status": status,
        "resolution": resolution,  # new field — None for open, "fixed"/"silence" for resolved
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


def load_proposals(path: Path) -> list[dict[str, Any]]:
    """Зарежда fix-proposals.json; връща списък от proposal-речници (толерантно).

    Приема и двата формата: {"proposals": [...]} или директно [...].
    При липсващ/счупен файл връща [].
    """
    data = _load_json_tolerant(path, {})
    if isinstance(data, dict):
        props = data.get("proposals", [])
    elif isinstance(data, list):
        props = data
    else:
        props = []
    return [p for p in props if isinstance(p, dict)]


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
    proposals_path: Path = DEFAULT_PROPOSALS,
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
    proposals = load_proposals(proposals_path)

    # Reconcile FIRST: close any cluster whose fix-proposal the user has already
    # marked 'resolved', so a fixed bug stops being reported as open (the
    # incidents-status desync). reopen_if_recurs still runs afterwards, so a
    # genuinely recurring complaint re-opens the cluster (regression guard).
    state, reconciled = reconcile_with_proposals(state, proposals, now=now)

    # Reopen: a resolved cluster that just received a fresh matching complaint
    # must be detected BEFORE update_state appends that example to it. If
    # update_state ran first it would add the example, and reopen_if_recurs'
    # duplicate guard would then skip it — masking the regression and leaving the
    # cluster stuck on "resolved". Order matters; this is the M1 audit fix.
    state, reopened = reopen_if_recurs(state, candidates, now=now)
    state = update_state(candidates, state, now=now)
    state = prune_state(state, now=now)
    _atomic_write(state_path, state)

    incidents = derive_incidents(state, now=now)
    output = build_output(incidents, now=now)
    _atomic_write(out_path, output)

    open_count = len(output["open"])
    resolved_count = len(output["resolved_recent"])

    if reconciled:
        print(f"Closed {len(reconciled)} incident(s) via resolved fix-proposal: {', '.join(reconciled)}")
    if reopened:
        print(f"Reopened {len(reopened)} incident(s) due to recurrence: {', '.join(reopened)}")
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
    resolution: str = "fixed",
) -> bool:
    """Ръчно затваря инцидент по ID.

    Args:
        incident_id: Краткото ID на клъстера.
        state_path: Пътека до incidents-state.json.
        out_path: Пътека до incidents.json.
        now: Текущо UTC.
        resolution: "fixed" (реален фикс, default) | "silence" | каквото е подходящо.

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
            cluster["resolution"] = resolution
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

    print(f"Инцидент '{incident_id}' е затворен (resolution={resolution}).")
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
    parser.add_argument(
        "--resolution",
        default="fixed",
        choices=["fixed", "silence"],
        help="Resolution type when using --resolve: 'fixed' (real fix, default) or 'silence'.",
    )
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.resolve:
        cmd_resolve(args.resolve, resolution=args.resolution)
    else:
        cmd_update()

    sys.exit(0)


if __name__ == "__main__":
    main()
