"""``Intake_Agent``'s `@tool` functions: location resolution, language
detection, and inbound-message dedupe (Req 5.6, 5.7, 5.9; design.md §3.1
(Intake)).

Verification note (mandatory per tasks.md 12.3, "verify first"):
    Consulted the current Strands Agents docs (via the Ref documentation
    search tool; no live Strands docs MCP server is configured in this
    environment) for the `@tool` decorator + docstring contract —
    `docs/user-guide/concepts/tools/custom-tools.md` ("Basic Example"):
    "The decorator extracts information from your function's docstring to
    create the tool specification. The first paragraph becomes the tool's
    description, and the 'Args' section provides parameter descriptions.
    These are combined with the function's type hints to create a complete
    tool specification." This matches every already-implemented `@tool` in
    this repo (`tools/sensor_tools.py`, `tools/dispatch_tools.py`) exactly —
    no deviation. Every tool below follows the same first-paragraph +
    "when to use" + `Args:` + `Returns:` shape, and returns a plain,
    JSON-serialisable dict carrying an explicit `ok: bool` field rather than
    raising for an expected, non-exceptional outcome (e.g. "no location
    candidate matched"), matching this repo's established convention
    (`tools/dispatch_tools.py`'s module docstring, "typed-plain-dict"
    convention).

    None of the three tools implemented here touch image content at all —
    Req 5.1's "as text or as an uploaded hazard image" input path and any
    image-reading helper belong to `agents/intake_agent.py` (task 13.3, not
    yet implemented), which is the component that actually receives the
    inbound message payload and decides whether to hand an image
    `ContentBlock` to the model. `resolve_location`/`detect_language`/
    `dedupe_check` all operate on plain text/string arguments only (a
    resident's stated or transcribed location wording, message text, and a
    message identifier respectively), so no image-handling convention needed
    verification for this module.

Design note — no full geocoding service specified (task instruction: "exact
geocoding logic can use a simple fixture/lookup approach against seeded ward
location data if no full geocoding service is specified in design"):
    design.md names no geocoding integration anywhere in §3.1/§3.7's
    integration-seam list (`integrations/sensor_provider.py`,
    `notification_provider.py`, `knowledge_provider.py` are the only three
    live-backed seams), and `tools/intake_tools.py`'s own design.md comment
    (`# resolve_location, detect_language, dedupe_check`, line ~248) gives no
    further call-shape detail. `resolve_location` therefore matches free
    text against a small **gazetteer** built from the two named-location
    collections that already exist in the seeded ward data
    (`seed/seed_dataset.json`): the five `resident_points[].area_id` ward-area
    labels ("Ward-7 North", "Ward-7 South", "Riverside Colony", "Kanal
    Street", "Old Ferry Road") and the three `shelters[].name` values
    ("Ward-7 Community Hall", "Riverside Colony School", "Kanal Street
    Marriage Hall") — the only named, geographically-anchored locations this
    codebase currently seeds. Matching is deliberately simple and fully
    deterministic (case-insensitive token overlap, no fuzzy/edit-distance
    matching, no external geocoding call), which is exactly what Req 5.6
    needs: a *bounded, auditable* candidate list Intake_Agent can escalate
    for a human to pick from, not a best-effort single geocoded point. This
    also means `resolve_location` alone cannot resolve resident-specific
    street/landmark wording (e.g. "12 Lake View Road", "near the banyan tree
    behind the old bus depot") to a single ward area — by design, since two
    residents naming different, more specific landmarks within the same ward
    area should not silently collapse to one area-level match; `Intake_Agent`
    itself (task 13.3) is still responsible for composing the final
    `EmergencyRequest.location_reference` from the resident's own wording,
    using this tool's candidate list only as an *ambiguity signal* (Req 5.6)
    when ward-area/shelter-name terms are present in that wording.

Design note — `Community_Language_Configuration` not yet declared as code:
    Req 5.1/5.8 and the glossary's `Community_Language_Configuration`
    ("Tamil and English... at minimum") are referenced throughout
    requirements.md/design.md but no prior task (1-11, or 12.1/12.2) has
    declared it as a Python constant anywhere in this codebase yet (grep
    confirmed: no `CONFIGURED_LANGUAGES`/`Community_Language_Configuration`
    symbol exists outside markdown). `detect_language` therefore declares
    `CONFIGURED_LANGUAGES` and `DEFAULT_LANGUAGE` here, following the exact
    env-override-with-in-code-default pattern already established by
    `tools/dispatch_tools.py`'s "Configuration judgment call" and
    `policy/escalation_policy.py`'s `_read_float_env`/`_read_int_env`
    helpers, so a later task (e.g. 13.3's `Intake_Agent`, or a future
    `policy/`-level module) can import these two names from here rather than
    re-declaring them, and so the configured set stays overridable via
    `.env` without a code change, matching every other tunable in this
    codebase.

Detection method — script-based heuristic, not a language-ID model:
    `detect_language` classifies by counting Tamil-script code points
    (Unicode block U+0B80-U+0BFF) versus Latin-alphabet code points in the
    input text and picking whichever script dominates; this is a
    deterministic, zero-model-call signal `Intake_Agent`'s own model can use
    alongside its own free-form judgement when composing
    `EmergencyRequest.source_language` (design.md's Rule_Engine/harness
    precedent: deterministic Python code for anything that must be
    auditable and repeatable). It has one known, explicitly-documented
    limitation: Tanglish (Tamil words transliterated into the Latin
    alphabet, e.g. `seed/sample_messages/messages.json`'s `MSG-003`,
    `source_language_hint: "ta-en"`) contains no Tamil-script code points at
    all and is therefore detected as `"en"` by this script-based heuristic.
    That fixture's own README already anticipates this ("Intake_Agent must
    still perform its own `source_language` detection... rather than
    trusting this field, so the eval suite can check the model's own
    detection") — `detect_language` is one deterministic input to that
    detection, not a replacement for it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Final

from strands import tool

from memory import state_store

__all__ = [
    "CONFIGURED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "LOCATION_GAZETTEER_PATH_ENV_VAR",
    "DEFAULT_LOCATION_GAZETTEER_PATH",
    "MAX_LOCATION_CANDIDATES",
    "resolve_location",
    "detect_language",
    "dedupe_check",
    "reset_location_gazetteer_cache",
]

# ---------------------------------------------------------------------------
# Community_Language_Configuration (see module docstring's design note)
# ---------------------------------------------------------------------------

CONFIGURED_LANGUAGES_ENV_VAR: Final[str] = "THUNAI_CONFIGURED_LANGUAGES"
DEFAULT_LANGUAGE_ENV_VAR: Final[str] = "THUNAI_DEFAULT_LANGUAGE"

_DEFAULT_CONFIGURED_LANGUAGES: Final[tuple[str, ...]] = ("ta", "en")
"""Req 5.8/8.5/12.4/13.4/14.4: Tamil and English, the minimum
Community_Language_Configuration set named throughout requirements.md."""

DEFAULT_LANGUAGE: Final[str] = os.environ.get(DEFAULT_LANGUAGE_ENV_VAR, "") or "en"
"""The configured default language a message is treated as when no
language could be detected, or composed in when the detected language is
outside `CONFIGURED_LANGUAGES` (Req 8.5's "configured default language")."""


def _read_configured_languages() -> frozenset[str]:
    raw = os.environ.get(CONFIGURED_LANGUAGES_ENV_VAR)
    if not raw:
        return frozenset(_DEFAULT_CONFIGURED_LANGUAGES)
    return frozenset(code.strip().lower() for code in raw.split(",") if code.strip())


CONFIGURED_LANGUAGES: Final[frozenset[str]] = _read_configured_languages()
"""Community_Language_Configuration: the configured set of language codes
ThunAI_Platform composes resident-facing content in. Defaults to `{"ta",
"en"}`; overridable via `THUNAI_CONFIGURED_LANGUAGES` (comma-separated
codes), matching this codebase's established env-override convention."""


# ---------------------------------------------------------------------------
# Location gazetteer, built from the seeded ward-area and shelter names
# (see module docstring's design note on geocoding)
# ---------------------------------------------------------------------------

LOCATION_GAZETTEER_PATH_ENV_VAR: Final[str] = "THUNAI_LOCATION_GAZETTEER_PATH"

DEFAULT_LOCATION_GAZETTEER_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "seed" / "seed_dataset.json"
)

MAX_LOCATION_CANDIDATES: Final[int] = 5
"""Req 5.6: "up to a maximum of 5 ranked candidates."""

_STOPWORDS: Final[frozenset[str]] = frozenset(
    {"the", "a", "an", "of", "near", "at", "in", "on", "street", "road", "st", "rd", "and"}
)

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase word/number tokens from `text`, with common generic
    location words filtered out so overlap scoring reflects genuinely
    distinguishing terms (e.g. "riverside", "kanal") rather than words that
    would trivially match every gazetteer entry (e.g. "street", "road")."""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    return frozenset(token for token in tokens if token not in _STOPWORDS)


def _location_gazetteer_path() -> Path:
    raw = os.environ.get(LOCATION_GAZETTEER_PATH_ENV_VAR)
    return Path(raw) if raw else DEFAULT_LOCATION_GAZETTEER_PATH


def _build_gazetteer(dataset_path: Path) -> list[dict[str, Any]]:
    """Build the location gazetteer from a `seed_dataset.json`-shaped file:
    one entry per distinct `resident_points[].area_id` (ward areas) and one
    entry per `shelters[].name` (named shelters), each carrying its
    coordinates and a pre-tokenised keyword set for overlap scoring."""
    data = json.loads(dataset_path.read_text(encoding="utf-8"))

    entries: list[dict[str, Any]] = []
    seen_area_ids: set[str] = set()
    for point in data.get("resident_points", []):
        area_id = point.get("area_id")
        if not area_id or area_id in seen_area_ids:
            continue
        seen_area_ids.add(area_id)
        entries.append(
            {
                "location_reference": area_id,
                "kind": "ward_area",
                "coords": point.get("coords"),
                "keywords": _tokenize(area_id),
            }
        )
    for shelter in data.get("shelters", []):
        name = shelter.get("name")
        if not name:
            continue
        entries.append(
            {
                "location_reference": name,
                "kind": "shelter",
                "coords": shelter.get("coords"),
                "keywords": _tokenize(name),
            }
        )
    return entries


_gazetteer_cache: list[dict[str, Any]] | None = None
_gazetteer_cache_path: Path | None = None


def _gazetteer() -> list[dict[str, Any]]:
    """Return the cached gazetteer, rebuilding it if the configured
    gazetteer path has changed since the last call (e.g. between tests that
    set `THUNAI_LOCATION_GAZETTEER_PATH` to different fixture files)."""
    global _gazetteer_cache, _gazetteer_cache_path
    path = _location_gazetteer_path()
    if _gazetteer_cache is None or _gazetteer_cache_path != path:
        _gazetteer_cache = _build_gazetteer(path)
        _gazetteer_cache_path = path
    return _gazetteer_cache


def reset_location_gazetteer_cache() -> None:
    """Drop the cached gazetteer (test-only helper; see `_gazetteer()`)."""
    global _gazetteer_cache, _gazetteer_cache_path
    _gazetteer_cache = None
    _gazetteer_cache_path = None


@tool
def resolve_location(location_text: str) -> dict[str, Any]:
    """Resolve a resident's free-text location wording to ranked candidate
    ward locations.

    Matches `location_text` against the seeded ward-area and shelter-name
    gazetteer (`seed/seed_dataset.json`'s `resident_points[].area_id` and
    `shelters[].name`) by case-insensitive keyword overlap, and returns
    every gazetteer entry that shares at least one distinguishing keyword
    with `location_text`, ranked by descending overlap score. This is a
    deterministic lookup, not a geocoding service — it recognises the
    configured ward areas and shelters by name, not arbitrary street
    addresses or landmarks a resident might mention.

    Use this once per inbound message to decide whether the resident's
    stated location is unambiguous. Per Req 5.6: when this tool returns a
    `candidate_count` other than exactly 1, record every returned candidate
    (up to the configured maximum) in `EmergencyRequest.location_candidates`
    and escalate a location-clarification decision rather than guessing.
    When `candidate_count == 1`, that single candidate's
    `location_reference` may be used directly with no escalation.

    Args:
        location_text: The resident's own wording for their location, taken
            verbatim from the inbound message (e.g. "near Riverside Colony,
            behind the school").

    Returns:
        {
            "ok": True,
            "candidate_count": int,   # number of candidates returned below
            "candidates": [
                {
                    "location_reference": str,        # e.g. "Riverside Colony"
                    "kind": "ward_area" | "shelter",
                    "coords": [float, float] | None,  # [latitude, longitude]
                    "score": int,                      # matched keyword count, >= 1
                }, ...
            ],  # ranked by descending score, ties broken alphabetically,
                # truncated to at most 5 candidates (Req 5.6)
        }
        `candidates` is `[]` (with `candidate_count` 0) when no gazetteer
        entry shares a keyword with `location_text` — this also counts as
        "other than exactly one" per Req 5.6 and should be escalated the
        same way multiple candidates would be, since the location could not
        be resolved at all.
    """
    query_tokens = _tokenize(location_text)

    scored: list[tuple[int, dict[str, Any]]] = []
    if query_tokens:
        for entry in _gazetteer():
            overlap = len(query_tokens & entry["keywords"])
            if overlap > 0:
                scored.append((overlap, entry))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["location_reference"]))
    scored = scored[:MAX_LOCATION_CANDIDATES]

    candidates = [
        {
            "location_reference": entry["location_reference"],
            "kind": entry["kind"],
            "coords": list(entry["coords"]) if entry["coords"] is not None else None,
            "score": score,
        }
        for score, entry in scored
    ]

    return {
        "ok": True,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# detect_language (Req 5.1, 5.8; Community_Language_Configuration)
# ---------------------------------------------------------------------------

_TAMIL_RANGE: Final[tuple[int, int]] = (0x0B80, 0x0BFF)


def _script_char_counts(text: str) -> tuple[int, int]:
    """Return `(tamil_char_count, latin_char_count)` for `text`."""
    tamil_count = 0
    latin_count = 0
    for char in text:
        code_point = ord(char)
        if _TAMIL_RANGE[0] <= code_point <= _TAMIL_RANGE[1]:
            tamil_count += 1
        elif char.isascii() and char.isalpha():
            latin_count += 1
    return tamil_count, latin_count


@tool
def detect_language(text: str) -> dict[str, Any]:
    """Detect the dominant script of an inbound resident message.

    Counts Tamil-script code points (Unicode block U+0B80-U+0BFF) versus
    Latin-alphabet letters in `text` and reports whichever dominates as the
    detected language (`"ta"` or `"en"`), or `"unknown"` when `text`
    contains no letters of either script. This is a deterministic,
    zero-model-call signal Intake_Agent's own model should combine with its
    own judgement (this tool alone cannot distinguish Tanglish — Tamil words
    written in the Latin alphabet — from genuine English; see module
    docstring).

    Use this once per inbound message. Per Req 5.8: when the returned
    `is_outside_configured_languages` is `true` (including the `"unknown"`
    case), preserve the message, create no dispatch-eligible request, and
    escalate a manual-triage decision rather than guessing a language.

    Args:
        text: The inbound resident message's raw free text.

    Returns:
        {
            "ok": True,
            "detected_language": "ta" | "en" | "unknown",
            "is_outside_configured_languages": bool,  # true for "unknown"
                # and for any detected language not in Community_Language_
                # Configuration (Req 5.8)
            "tamil_char_count": int,
            "latin_char_count": int,
            "configured_languages": list[str],  # e.g. ["en", "ta"], sorted
        }
    """
    tamil_count, latin_count = _script_char_counts(text)

    if tamil_count == 0 and latin_count == 0:
        detected_language = "unknown"
    elif tamil_count > latin_count:
        detected_language = "ta"
    elif latin_count > 0:
        detected_language = "en"
    else:
        detected_language = "ta"

    return {
        "ok": True,
        "detected_language": detected_language,
        "is_outside_configured_languages": detected_language not in CONFIGURED_LANGUAGES,
        "tamil_char_count": tamil_count,
        "latin_char_count": latin_count,
        "configured_languages": sorted(CONFIGURED_LANGUAGES),
    }


# ---------------------------------------------------------------------------
# dedupe_check (Req 5.9 — idempotence)
# ---------------------------------------------------------------------------

INTAKE_IDEMPOTENCY_KEY_PREFIX: Final[str] = "intake"
"""Matches the `f"intake:{message_id}"` Idempotency_Key convention every
Intake_Agent write derived from the inbound message identifier is expected
to use (design.md §4.4's `REQUEST#{request_id}` key schema note,
`request_id` = inbound message id; mirrors `tools/dispatch_tools.py`'s own
`f"assign:{request_id}"` convention for the same reason)."""


def _intake_idempotency_key(message_id: str) -> str:
    return f"{INTAKE_IDEMPOTENCY_KEY_PREFIX}:{message_id}" if message_id else ""


@tool
def dedupe_check(message_id: str) -> dict[str, Any]:
    """Check whether an inbound resident message has already been processed.

    Reads `memory/state_store.py`'s idempotency-record store (the same
    72-hour-retained record `state_store.idempotent_write` consults, task
    6.1) for the Idempotency_Key derived from `message_id`
    (`f"intake:{message_id}"`), without performing any write. Use this at
    the start of processing an inbound message so Intake_Agent can, per Req
    5.9, leave State_Store exactly as the first processing left it and raise
    no additional escalation and no additional Knowledge_Agent routing when
    the same message identifier arrives a second time — rather than relying
    solely on the eventual write-time idempotency check, which cannot by
    itself prevent a second escalation or a second Knowledge_Agent routing
    decision from being raised before that write is attempted.

    Args:
        message_id: The inbound message identifier to check, exactly as
            received (e.g. the SES/API Gateway-assigned message id).

    Returns:
        On success: {"ok": True, "message_id": str, "is_duplicate": bool,
        "prior_request_id": str | None}. `prior_request_id` is the request
        identifier recorded by the first processing's outcome when
        `is_duplicate` is `true` and that outcome recorded one (`None`
        otherwise — e.g. the first processing routed to Knowledge_Agent or
        preserved the message for manual triage rather than creating a
        request).

        On failure: {"ok": False, "reason": "missing_message_id"} when
        `message_id` is empty (no idempotency key could be derived).
    """
    idempotency_key = _intake_idempotency_key(message_id)
    if not idempotency_key:
        return {"ok": False, "reason": "missing_message_id"}

    # `_get_idempotency_record` is memory.state_store's own private helper
    # (module docstring's deviation note explains why this module has no
    # public peek-only accessor: `idempotent_write` is the only exported
    # entry point, and it is a perform-or-replay primitive, not a read-only
    # check). Reading it directly here is a read-only lookup against the
    # exact mechanism task 6.1 already built for this purpose, and avoids
    # widening memory/state_store.py's public API for a single caller —
    # this task's scope is `tools/intake_tools.py` only.
    existing = state_store._get_idempotency_record(idempotency_key)  # noqa: SLF001

    if existing is None:
        return {
            "ok": True,
            "message_id": message_id,
            "is_duplicate": False,
            "prior_request_id": None,
        }

    recorded_outcome = existing.get("recorded_outcome")
    prior_request_id = (
        recorded_outcome.get("request_id")
        if isinstance(recorded_outcome, dict)
        else None
    )
    return {
        "ok": True,
        "message_id": message_id,
        "is_duplicate": True,
        "prior_request_id": prior_request_id,
    }
