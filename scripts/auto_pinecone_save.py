#!/usr/bin/env python3
"""Auto-save session learnings to Pinecone when a Claude Code session ends.

Wired as a Stop hook in ~/.claude/settings.json.
Reads JSON from stdin (Claude Code hook format), detects the project
namespace, extracts learnings from the transcript, and calls pinecone.py.

SAFE: exits 0 in ALL cases. Never writes to stdout (would break hooks).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Secret detection — refuse to save content containing credentials.
# This is conservative: detect → SKIP save → log warning. No partial redaction.
# ---------------------------------------------------------------------------
SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),  # JWT
    re.compile(r"pcsk_[A-Za-z0-9_-]{20,}"),                                     # Pinecone
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),                                   # Anthropic
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),                                  # OpenAI project
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                                         # OpenAI generic
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                                         # GitHub PAT classic
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),                                # GitHub PAT fine-grained
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),                                    # GitLab PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                                            # AWS access key
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),                                      # Google API key
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),                                # Slack
    re.compile(r"Bearer\s+[A-Za-z0-9_.-]{40,}", re.IGNORECASE),                 # Bearer tokens
    re.compile(r"-----BEGIN\s+(?:RSA\s+|OPENSSH\s+|EC\s+)?PRIVATE\s+KEY-----"), # Private keys
    re.compile(r"\b(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*[''\"][^''\"\s]{8,}[''\"]", re.IGNORECASE),
]


def contains_secret(text: str) -> tuple[bool, str]:
    """Return (True, pattern_name) if text contains any known secret pattern."""
    if not text:
        return False, ""
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            # Don't include the matched secret in the return value
            return True, pattern.pattern[:40]
    return False, ""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).parent
PINECONE_CLI = SCRIPTS_DIR / "pinecone.py"
LOG_DIR = Path.home() / ".claude" / "logs"
LOG_FILE = LOG_DIR / "auto_pinecone_save.log"
WIKI_MAP_PATH = Path("{{WIKI_PATH}}/_shared/wiki-map.json")
ANTIGRAVITI_BASE = Path("{{CODE_PATH}}")
OBSIDIAN_BASE = Path("{{WIKI_PATH}}")

# Keywords that indicate a "learning" in message content
LEARNING_KEYWORDS = [
    "learned", "lesson", "gotcha", "pattern", "fixed by", "issue was",
    "не пипай", "трябва", "винаги", "никога", "TIL", "научих",
    "solution", "workaround", "discovered", "причина", "решение",
]

TTL_BY_TYPE = {
    "antipattern": 365,
    "decision": 365,
    "pattern": 365,
    "promoted": 9999,  # NEVER_EXPIRE sentinel
    "gotcha": 180,
    "learning": 90,
    # NEW (from Infinite Brain analysis 2026-05-08):
    "question": 180,    # open issues — medium-lived
    "hypothesis": 180,  # theories being tested
    "playbook": 365,    # repeatable procedures (SOPs)
    "fact": 365,        # verified static knowledge
}

SHORT_USER_MSG_THRESHOLD = 80  # chars; raised from 50 — short user replies often have brief followup

PATTERN_SIGNALS = (
    "perfect", "идеално", "точно така", "exactly", "чудесно",
    "great", "supurb", "thanks", "благодаря", "✅", "това работи",
)
ANTIPATTERN_SIGNALS = (
    "не прави", "грешно", "неправилно", "wrong", "don't do",
    "don't use", "avoid", "избягвай", "не използвай", "❌",
    "antipattern", "anti-pattern",
)
DECISION_MARKERS = (
    "решихме", "избрахме", "decided to", "we'll use", "ще ползваме",
    "ще използваме", "избирам", "the approach is", "best to use",
    "recommend", "препоръчвам", "go with", "let's use",
    "## decision", "## избор", "## решение", "## architecture",
    "trade-off", "trade off", "избор:", "решение:",
)
ADVERSARIAL_DEBUG_SIGNALS = (
    "all teammates agreed", "consensus", "competing hypotheses",
    "adversarial debug", "и тримата", "verified by 3 agents",
    "team confirmed", "consensus reached",
)
GOTCHA_MARKERS = (
    "fixed by", "issue was", "gotcha", "проблемът беше", "решението беше",
    "причината беше", "be careful", "watch out", "важно", "забележка",
    "tricky", "subtle", "easy to miss", "common mistake", "edge case",
    "important to note", "запомни", "имай предвид", "внимание",
    "note that", "обърни внимание",
)
QUESTION_MARKERS = (
    "open question", "открит въпрос", "trying to figure out", "не съм сигурен",
    "to investigate", "за изследване", "?", "недоразумение",
    "unclear how", "?:",  # explicit question prefix
)
HYPOTHESIS_MARKERS = (
    "hypothesis:", "хипотеза:", "we think that", "may be that",
    "theory:", "теория:", "приемам че", "believe that",
    "untested assumption", "непроверено предположение",
)
PLAYBOOK_MARKERS = (
    "playbook:", "workflow:", "procedure:", "процедура:", "step-by-step",
    "стъпка по стъпка", "## steps", "## workflow", "how to:", "как да:",
    "SOP:", "СОП:",
)
FACT_MARKERS = (
    "fact:", "факт:", "verified:", "потвърдено:", "знае се че",
    "known to be", "documented:", "официално",
)


def make_entry_id(namespace: str, content: str) -> str:
    """Idempotent ID: same content → same ID → upsert is no-op."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{namespace}-{content_hash}"


def detect_type(content: str, last_user: str) -> str:
    """Classify the entry by content/user signals. First match wins."""
    user_lower = (last_user or "").lower()
    content_lower = (content or "").lower()

    # 1. Adversarial-debug consensus → decision (HIGH confidence, ttl=730)
    if any(s in content_lower for s in ADVERSARIAL_DEBUG_SIGNALS):
        return "decision"

    # 2. Anti-patterns from user feedback
    if any(s in user_lower for s in ANTIPATTERN_SIGNALS):
        return "antipattern"

    # 3. Confirmed pattern (user satisfaction in short message)
    if len(user_lower) <= SHORT_USER_MSG_THRESHOLD and any(s in user_lower for s in PATTERN_SIGNALS):
        return "pattern"

    # 4. Decision markers
    if any(s in content_lower for s in DECISION_MARKERS):
        return "decision"

    # 5. Hypothesis (more specific than gotcha/learning)
    if any(s in content_lower for s in HYPOTHESIS_MARKERS):
        return "hypothesis"

    # 6. Playbook (procedural workflow)
    if any(s in content_lower for s in PLAYBOOK_MARKERS):
        return "playbook"

    # 7. Verified fact
    if any(s in content_lower for s in FACT_MARKERS):
        return "fact"

    # 8. Gotcha (debugging discoveries)
    if any(s in content_lower for s in GOTCHA_MARKERS):
        return "gotcha"

    # 9. Open question (catch broad question signals)
    if any(s in content_lower for s in QUESTION_MARKERS):
        return "question"

    return "learning"


def build_metadata(content: str, last_user: str, session_id: str, project: str, source_model: str = "") -> dict:
    """Build rich metadata dict for Pinecone upsert."""
    entry_type = detect_type(content, last_user)
    ttl = TTL_BY_TYPE.get(entry_type, 90)
    if entry_type == "decision" and any(s in content.lower() for s in ADVERSARIAL_DEBUG_SIGNALS):
        ttl = 730  # adversarial-verified consensus → 2 years
    return {
        "text": content[:500],
        "project": project,
        "type": entry_type,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "session_id": session_id[:8] if session_id else "",
        "tokens": len(content) // 4,
        "ttl_days": ttl,
        "source_model": source_model[:30] if source_model else "",  # e.g., "claude-opus-4-7"
    }


# Minimum/maximum char length for a candidate assistant message
MIN_CONTENT_LEN = 100
MAX_CONTENT_LEN = 2000
MAX_SAVE_CONTENT = 1500


# ---------------------------------------------------------------------------
# Logging — append-only to file, never stdout
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("auto_pinecone_save")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5_242_880,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# Project → namespace detection
# ---------------------------------------------------------------------------
def _load_wiki_map() -> dict[str, str]:
    """Load project→wiki name mapping from wiki-map.json."""
    try:
        return json.loads(WIKI_MAP_PATH.read_text(encoding="utf-8")).get("mapping", {})
    except Exception as exc:
        log.warning("Could not load wiki-map.json: %s", exc)
        return {}


def detect_namespace(cwd: str) -> str | None:
    """Return Pinecone namespace for the given working directory, or None."""
    cwd_path = Path(cwd).resolve()
    wiki_map = _load_wiki_map()

    # Case 1: {{CODE_PATH}}\<Project>\...
    try:
        rel = cwd_path.relative_to(ANTIGRAVITI_BASE)
        project_name = rel.parts[0] if rel.parts else None
        if project_name:
            ns = wiki_map.get(project_name)
            if ns:
                log.debug("Detected namespace '%s' via Antigraviti for project '%s'", ns, project_name)
                return ns
            # Not in map — use the raw project name as fallback namespace
            log.debug("Project '%s' not in wiki-map, using raw name as NS", project_name)
            return project_name
    except ValueError:
        pass  # cwd not under ANTIGRAVITI_BASE

    # Case 2: {{WIKI_PATH}}\<wiki>\...
    try:
        rel = cwd_path.relative_to(OBSIDIAN_BASE)
        wiki_name = rel.parts[0] if rel.parts else None
        if wiki_name and wiki_name not in ("_shared",):
            log.debug("Detected namespace '%s' via Obsidian path", wiki_name)
            return wiki_name
    except ValueError:
        pass

    # Case 3: {{RESEARCH_PATH}}\<wiki>\... (research wikis)
    # Non-ASCII folder names → ASCII namespace (Pinecone ID compatibility)
    RESEARCH_NS_MAP = {
        "Петър Дънов": "PetarDanov",
        "Асистент_Стоил": "AsistentStoil",
        "Дело Зоя": "DeloZoya",
    }
    try:
        rel = cwd_path.relative_to(Path("{{RESEARCH_PATH}}"))
        wiki_name = rel.parts[0] if rel.parts else None
        if wiki_name and wiki_name not in ("_meta", "_shared"):
            # Apply ASCII mapping if Cyrillic name
            mapped = RESEARCH_NS_MAP.get(wiki_name, wiki_name)
            if mapped != wiki_name:
                log.debug("Mapped Cyrillic '%s' → '%s' for namespace", wiki_name, mapped)
                return mapped
            log.debug("Detected namespace '%s' via Obsidian Resurch path", wiki_name)
            return wiki_name
    except ValueError:
        pass

    # Case 4: ~/.claude/* — system meta-work + TTS consolidation
    # Captures system improvement work; routes TTS subdir to Reed (per MEMORY.md)
    # Skips auto-state subdirs (cache, logs, projects, plugins) to avoid noise.
    claude_base = Path.home() / ".claude"
    try:
        rel = cwd_path.relative_to(claude_base)
        first = rel.parts[0] if rel.parts else "(root)"
        # TTS app runtime data → Reed project (consolidation per MEMORY.md)
        if first == "tts":
            log.debug("Detected namespace 'Reed' via ~/.claude/tts (TTS consolidation)")
            return "Reed"
        # Skip auto-managed state directories
        AUTO_STATE_DIRS = {
            "cache", "logs", "projects", "plugins", "agents",
            "ide", "file-history", "paste-cache", "debug",
            "backups", "double-shot-latte",
        }
        if first in AUTO_STATE_DIRS:
            log.debug("Skipping auto-state dir '%s' under ~/.claude", first)
            return None
        # Capture system meta-work: scripts, plans, references, rules, commands, hooks, root
        log.debug("Detected namespace '_claude_meta' via ~/.claude (subdir=%s)", first)
        return "_claude_meta"
    except ValueError:
        pass

    log.info("No project namespace detected for cwd='%s'", cwd)
    return None


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------
def _contains_learning(text: str) -> bool:
    """Return True if text contains at least one learning keyword."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in LEARNING_KEYWORDS)


def _has_code_or_path(text: str) -> bool:
    """Return True if text looks like it contains code snippets or file paths."""
    return any(marker in text for marker in ("```", "/", "\\", ".py", ".ts", ".js", ".sh"))


def extract_learnings(transcript_path: str) -> tuple[str, str, str] | None:
    """
    Parse JSONL transcript and return (summary, last_user, source_model) tuple, or None if no learnings.

    Returns:
        - tuple[str, str, str]: (joined_summary_with_optional_user_feedback, last_user_message, source_model)
        - None: when no learnings detected (no assistant messages, no learning keywords, etc.)
    """
    path = Path(transcript_path)
    if not path.exists():
        log.warning("Transcript not found: %s", transcript_path)
        return None

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        log.warning("Cannot read transcript '%s': %s", transcript_path, exc)
        return None

    assistant_messages: list[str] = []
    user_messages: list[str] = []
    last_assistant_model = ""

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        # Determine role: new format uses event.type, old format uses event.role
        role = event.get("type") or event.get("role", "")

        # Get content list — try both new and old formats
        content_list = None
        msg = event.get("message")
        if isinstance(msg, dict):
            # New Claude Code format: event.message.content
            mc = msg.get("content")
            if isinstance(mc, list):
                content_list = mc
            elif isinstance(mc, str):
                # Some user messages have string content directly
                content_list = [{"type": "text", "text": mc}]
        if content_list is None:
            # Old format fallback: event.content
            ec = event.get("content")
            if isinstance(ec, list):
                content_list = ec
            elif isinstance(ec, str):
                content_list = [{"type": "text", "text": ec}]

        if not content_list:
            continue

        # Extract ONLY text blocks (skip tool_use, tool_result, thinking, etc.)
        parts: list[str] = []
        for block in content_list:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "text")
            # Save only actual text content; skip tool calls/results
            if block_type == "text":
                txt = block.get("text", "")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
            # Skip tool_use, tool_result, thinking blocks — they're noise

        content = "\n\n".join(parts).strip()
        if not content:
            continue

        if role in ("assistant", "tool_result"):
            assistant_messages.append(content)
            if role == "assistant" and isinstance(msg, dict):
                model_name = msg.get("model", "")
                if model_name:
                    last_assistant_model = model_name
        elif role == "user":
            user_messages.append(content)

    if not assistant_messages:
        log.info("No assistant messages found in transcript")
        return None

    last_assistant = assistant_messages[-1]
    last_user = user_messages[-1] if user_messages else ""

    # Check if last assistant message is worth saving
    is_learning = _contains_learning(last_assistant) or _contains_learning(last_user)
    has_substance = (
        MIN_CONTENT_LEN <= len(last_assistant) <= MAX_CONTENT_LEN
        and _has_code_or_path(last_assistant)
    )

    if not is_learning and not has_substance:
        log.info(
            "No learnings detected (assistant_len=%d, is_learning=%s, has_substance=%s)",
            len(last_assistant),
            is_learning,
            has_substance,
        )
        return None

    # Build compact summary
    summary_parts: list[str] = []

    # Trim assistant message
    trimmed_assistant = last_assistant[:MAX_SAVE_CONTENT]
    if len(last_assistant) > MAX_SAVE_CONTENT:
        trimmed_assistant += "…"
    summary_parts.append(trimmed_assistant)

    # Append user feedback if short and useful
    if last_user and len(last_user) <= 300 and last_user != last_assistant:
        summary_parts.append(f"\n[User feedback]: {last_user[:300]}")

    return "\n".join(summary_parts), last_user, last_assistant_model


# ---------------------------------------------------------------------------
# Pinecone save via subprocess (reuses existing CLI)
# ---------------------------------------------------------------------------
def save_to_pinecone(namespace: str, entry_id: str, content: str,
                    last_user: str = "", session_id: str = "",
                    source_model: str = "") -> bool:
    """Call pinecone.py save via subprocess with rich metadata."""
    metadata = build_metadata(content, last_user, session_id, namespace, source_model)
    meta_arg = (
        f"type={metadata['type']},"
        f"date={metadata['date']},"
        f"project={metadata['project']},"
        f"ttl_days={metadata['ttl_days']}"
    )
    if metadata.get('source_model'):
        # Sanitize: avoid commas/equals in value
        sm = metadata['source_model'].replace(',', '_').replace('=', '_')
        meta_arg += f",source_model={sm}"
    cmd = [sys.executable, str(PINECONE_CLI), "save", namespace, entry_id, content, "--meta", meta_arg]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            log.info("Saved to Pinecone: ns='%s' id='%s' type='%s'",
                     namespace, entry_id, metadata['type'])
            return True
        else:
            log.warning("pinecone.py exited %d: %s",
                        result.returncode, (result.stderr or result.stdout)[:300])
            return False
    except subprocess.TimeoutExpired:
        log.warning("pinecone.py timed out after 60s")
        return False
    except Exception as exc:
        log.warning("Failed to call pinecone.py: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-save session learnings to Pinecone (Claude Code Stop hook)."
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Test mode: read JSON from stdin but skip actual Pinecone save if transcript missing.",
    )
    args = parser.parse_args()

    try:
        raw_input = sys.stdin.read()
    except Exception as exc:
        log.error("Cannot read stdin: %s", exc)
        sys.exit(0)

    # Parse hook payload
    try:
        payload: dict = json.loads(raw_input) if raw_input.strip() else {}
    except json.JSONDecodeError as exc:
        log.warning("Invalid JSON from stdin: %s | raw=%s", exc, raw_input[:200])
        payload = {}

    session_id: str = payload.get("session_id") or "unknown"
    transcript_path: str = payload.get("transcript_path") or ""
    cwd: str = payload.get("cwd") or os.getcwd()

    log.info(
        "Stop hook fired: session_id='%s' cwd='%s' transcript='%s'",
        session_id,
        cwd,
        transcript_path,
    )

    # Detect namespace
    namespace = detect_namespace(cwd)
    if not namespace:
        log.info("Skipping: no namespace for cwd='%s'", cwd)
        sys.exit(0)

    # Extract learnings from transcript
    if args.manual and not Path(transcript_path).exists():
        log.info("[manual mode] transcript_path='%s' not found — skipping Pinecone save", transcript_path)
        sys.exit(0)

    result = extract_learnings(transcript_path)
    if not result:
        log.info("No learnings to save for session '%s'", session_id)
        sys.exit(0)
    content, last_user, source_model = result

    entry_id = make_entry_id(namespace, content)

    log.info(
        "Saving learning: ns='%s' id='%s' content_len=%d",
        namespace,
        entry_id,
        len(content),
    )

    save_to_pinecone(namespace, entry_id, content, last_user=last_user, session_id=session_id, source_model=source_model)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Last-resort catch — NEVER crash the hook
        try:
            log.critical("Unhandled exception in auto_pinecone_save: %s", exc, exc_info=True)
        except Exception:
            pass
        sys.exit(0)
