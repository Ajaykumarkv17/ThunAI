"""Unit tests for the `seed/knowledge_docs/` fixture set (task 11.4).

Validates the curated community flood-safety documents that `scripts/seed.sh`
will ingest into the live S3 Vectors-backed Bedrock Knowledge Base: topic
coverage, per-language file pairing, a non-empty `source_version` tag on
every document, and that no bare unlabelled phone-number-like string could be
mistaken for a real emergency contact number.

Requirements: 8.1a, 18.6. Design: §3.9, §3.11 (KnowledgeStack).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

KNOWLEDGE_DOCS_DIR = Path(__file__).resolve().parents[2] / "seed" / "knowledge_docs"

REQUIRED_TOPICS = {
    "water_safety",
    "shelter_guidance",
    "evacuation_kit",
    "evacuation_basics",
}

REQUIRED_LANGUAGES = {"en", "ta"}

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# A "phone-number-shaped" string: a run of digits (optionally grouped with
# spaces/hyphens) at least 6 digits long, e.g. "1800-425-1234" or "9445512345".
PHONE_LIKE_RE = re.compile(r"(?<!\[)(?<!\w)(?:\d[\d\-\s]{4,}\d)(?!\w)")


def _doc_files() -> list[Path]:
    """All ingestible knowledge documents, excluding this directory's own
    README (which documents the fixture set but is not itself a retrievable
    safety document)."""
    return sorted(
        path for path in KNOWLEDGE_DOCS_DIR.glob("*.md") if path.name != "README.md"
    )


def _parse_front_matter(text: str) -> dict[str, str]:
    match = FRONT_MATTER_RE.match(text)
    assert match, "document is missing a YAML-style front-matter block"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_knowledge_docs_directory_exists_and_is_non_empty():
    assert KNOWLEDGE_DOCS_DIR.is_dir()
    assert _doc_files(), "seed/knowledge_docs/ contains no markdown documents"


def test_at_least_four_required_topics_are_covered():
    topics_found = set()
    for path in _doc_files():
        fields = _parse_front_matter(path.read_text(encoding="utf-8"))
        topic = fields.get("topic")
        if topic:
            topics_found.add(topic)

    missing = REQUIRED_TOPICS - topics_found
    assert not missing, f"missing required topic(s): {sorted(missing)}"
    assert len(topics_found) >= 4


@pytest.mark.parametrize("topic", sorted(REQUIRED_TOPICS))
def test_each_required_topic_has_both_a_tamil_and_an_english_file(topic: str):
    languages_for_topic = set()
    for path in _doc_files():
        fields = _parse_front_matter(path.read_text(encoding="utf-8"))
        if fields.get("topic") == topic:
            language = fields.get("language")
            assert language, f"{path.name} declares no 'language' field"
            languages_for_topic.add(language)

    missing = REQUIRED_LANGUAGES - languages_for_topic
    assert not missing, f"topic '{topic}' is missing language(s): {sorted(missing)}"


def test_every_document_declares_a_non_empty_source_version():
    docs = _doc_files()
    assert docs

    for path in docs:
        fields = _parse_front_matter(path.read_text(encoding="utf-8"))
        source_version = fields.get("source_version")
        assert source_version, f"{path.name} has no 'source_version' field"
        assert source_version.strip() != "", f"{path.name} has an empty 'source_version' value"


def test_no_bare_unlabelled_phone_number_like_string():
    """Any phone-number-shaped digit run must be wrapped in an obvious
    placeholder marker like `[...]` so nobody mistakes it for a real
    emergency contact number (Req 18.6 data-minimisation / no invented
    specific claims)."""
    docs = _doc_files()
    assert docs

    for path in docs:
        full_text = path.read_text(encoding="utf-8")
        # Exclude the front-matter block: fields like `source_version:
        # 2026-09-v1` contain digit runs that are not phone numbers and are
        # not part of the resident-facing content this check is protecting.
        front_matter_match = FRONT_MATTER_RE.match(full_text)
        body_start = front_matter_match.end() if front_matter_match else 0
        text = full_text[body_start:]
        for match in PHONE_LIKE_RE.finditer(text):
            start, end = match.span()
            # Walk outward from the match to see if it sits inside a
            # bracketed placeholder, e.g. "[LOCAL EMERGENCY HELPLINE NUMBER]".
            preceding = text[:start]
            following = text[end:]
            last_open = preceding.rfind("[")
            last_close = preceding.rfind("]")
            inside_brackets = last_open != -1 and last_open > last_close
            next_close = following.find("]")
            next_open = following.find("[")
            closes_before_reopening = next_close != -1 and (
                next_open == -1 or next_close < next_open
            )
            assert inside_brackets and closes_before_reopening, (
                f"{path.name} contains an unlabelled phone-number-like string "
                f"'{match.group(0)}' not wrapped in a placeholder marker"
            )
