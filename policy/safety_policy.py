r"""Safety_QA_Agent policy: identifiers, version, and PII/secret detection (Req 9.3, 9.8).

`Safety_QA_Agent` (design.md §3.1, implemented in task 13.6) reviews every
proposed outbound communication and every proposed `Irreversible_Action`
against this policy before release, and its typed output
(`schemas.decisions.SafetyReview`) carries `violated_policy_ids` populated
from `detect_pii_or_secrets()` below and `policy_version` populated from
`SAFETY_POLICY_VERSION` (Req 9.1, 9.5).

Req 9.3 names three PII categories Safety_QA_Agent must check every proposed
outbound communication for: resident names, resident contact numbers, and
resident dwelling addresses. Req 9.8 requires the active safety policy —
"every policy identifier and the policy version" — to be publishable to
Coordinator_Console, which is why `SAFETY_POLICY_IDS` and
`SAFETY_POLICY_VERSION` are declared as simple, introspectable module-level
constants rather than buried inside a function body.

This module also extends the closed policy-id set with `resident_id_leak`
(national-identifier-shaped numbers, e.g. a 12-digit Aadhaar-like sequence)
and `secret_leak` (credential/API-key-shaped strings), mirroring the
`harness/redaction.py` field-name list referenced in design.md §3.6
(`resident_name`, `resident_contact`, `resident_address`, `*_secret`,
`*_credential`, `*_api_key`) — the same categories that must never reach an
audit entry, a log record, or a trace attribute unredacted.

Detection scope, stated honestly: `resident_name_leak` and
`resident_address_leak` are declared policy identifiers (Req 9.3 requires
Safety_QA_Agent to check for them and to mark violations with the violated
policy identifier), but neither a personal name nor a street address has a
reliable, low-false-positive regex shape in free text. This module therefore
detects only the PII/secret categories that *do* have a stable text pattern —
phone numbers, email addresses, national-identifier-shaped digit sequences,
and credential/API-key-shaped strings — via `detect_pii_or_secrets()`.
Name/address detection (e.g. cross-referencing the resident's own stored
`resident_name`/`resident_address` field values against the drafted content)
is left to `Safety_QA_Agent`'s own implementation in task 13.6, which has
the resident record available and this module does not.

Verification note (mandatory per tasks.md 3.3): this module is pure Python
using only the standard-library `re` module — no external SDK surface to
verify. Patterns are pre-compiled at import time (`re.compile`) rather than
compiled on every call, and the phone/national-ID patterns use zero-width
digit-boundary lookaround (``(?<!\d)`` / ``(?!\d)``) so that a 10-digit
phone number embedded inside a longer digit run (e.g. a 12-digit national
ID) is not mismatched as the other pattern.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Closed set of safety-policy identifiers (Req 9.1, 9.3, 9.5, 9.6, 9.8).
# Every identifier that Safety_QA_Agent may place into
# `SafetyReview.violated_policy_ids` must be a member of this set.
# ---------------------------------------------------------------------------
SAFETY_POLICY_IDS: Final[frozenset[str]] = frozenset(
    {
        "resident_name_leak",
        "resident_contact_leak",
        "resident_address_leak",
        "resident_id_leak",
        "secret_leak",
    }
)

# ---------------------------------------------------------------------------
# Active safety policy version (Req 9.1, 9.5, 9.8).
# Referenced by `schemas.decisions.SafetyReview.policy_version`. Bump this
# string whenever `SAFETY_POLICY_IDS` or any pattern below changes, mirroring
# how `policy.rule_engine_rules.RULE_SET_VERSION` is bumped on threshold
# changes.
# ---------------------------------------------------------------------------
SAFETY_POLICY_VERSION: Final[str] = "safety-policy-v1"

# ---------------------------------------------------------------------------
# Compiled PII/secret detection patterns, keyed by pattern name.
# ---------------------------------------------------------------------------
PII_SECRET_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    # Indian mobile number: optional +91/91/0 prefix, then a 10-digit number
    # starting with 6-9 (the valid Indian mobile leading-digit range),
    # bounded so it does not match inside a longer digit run.
    "indian_mobile_number": re.compile(r"(?<!\d)(?:\+?91[-\s]?|0)?[6-9]\d{9}(?!\d)"),
    # Email address.
    "email_address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # National-identifier-shaped number, e.g. a 12-digit Aadhaar-like
    # sequence, optionally grouped in 4s.
    "national_id_number": re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)"),
    # AWS access key id shape.
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Generic credential/API-key assignment, e.g. `api_key: "..."` or
    # `secret=...`, matching the harness redaction field-name suffixes
    # `*_secret`, `*_credential`, `*_api_key`.
    "generic_secret_assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|credential|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{8,}"
    ),
}

# ---------------------------------------------------------------------------
# Mapping from detection pattern name to the safety-policy identifier it
# violates when matched (Req 9.3).
# ---------------------------------------------------------------------------
PATTERN_TO_POLICY_ID: Final[dict[str, str]] = {
    "indian_mobile_number": "resident_contact_leak",
    "email_address": "resident_contact_leak",
    "national_id_number": "resident_id_leak",
    "aws_access_key_id": "secret_leak",
    "generic_secret_assignment": "secret_leak",
}

assert set(PATTERN_TO_POLICY_ID) == set(PII_SECRET_PATTERNS), (
    "every declared pattern must map to exactly one safety-policy identifier"
)
assert set(PATTERN_TO_POLICY_ID.values()) <= SAFETY_POLICY_IDS, (
    "every mapped policy identifier must be a member of SAFETY_POLICY_IDS"
)


def detect_pii_or_secrets(text: str) -> list[str]:
    """Scan free text for PII/secret shapes and return the violated policy ids.

    Args:
        text: The proposed outbound communication content to scan (e.g. one
            `AlertDraft` variant's rendered body).

    Returns:
        A sorted list of unique `SAFETY_POLICY_IDS` members for every
        pattern in `PII_SECRET_PATTERNS` that matched `text` at least once.
        Empty when no pattern matched.
    """
    violated_policy_ids: set[str] = set()
    for pattern_name, pattern in PII_SECRET_PATTERNS.items():
        if pattern.search(text):
            violated_policy_ids.add(PATTERN_TO_POLICY_ID[pattern_name])
    return sorted(violated_policy_ids)
