"""Tests for auto_pinecone_save conversational-noise guard.

The guard prevents session wrap-up reports (chat output) from being saved as
"knowledge" — they pass the has_substance check (code/paths) but pollute recall.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_rejects_conversational_openers():
    from auto_pinecone_save import is_conversational_noise as noise
    samples = [
        "Готово. Ето пълният критичен одит.",
        "Перфектно. Готово. Ето как работи от сега:",
        "✅ Sprint 1 завършен + одитиран",
        "# 🔬 Критичен одит на системата",
        "Имам пълната картина. Сега — критичен филтър.",
        "**20/20 тестове. Всички log файлове съществуват.**",
        "Всичко работи — recall е активен.",
        "Финална истина, без спин: не успях.",
        "114/114. Всичко готово. Резюме на v4:",
        "Done. Here is the full report.",
    ]
    for s in samples:
        assert noise(s) is True, f"should reject as noise: {s!r}"


def test_keeps_genuine_learnings():
    from auto_pinecone_save import is_conversational_noise as noise
    samples = [
        "PATTERN: Model routing matrix — Haiku for config, Sonnet for CRUD.",
        "antipattern: sys.exit() in shared module kills importers.",
        "gotcha: Zod optional env var + plain === comparison = auth bypass.",
        "pattern: Hebbian vector-memory TTL extends by recall_count weekly.",
        "n8n production: Error Trigger workflow required from day 1.",
    ]
    for s in samples:
        assert noise(s) is False, f"should keep as learning: {s!r}"


def test_empty_is_noise():
    from auto_pinecone_save import is_conversational_noise as noise
    assert noise("") is True
    assert noise("   \n  \n") is True
