"""Unit tests for policy/safety_policy.py (task 3.3).

Covers: each declared pattern matches a realistic positive example and does
not false-positive on ordinary safe text, `SAFETY_POLICY_VERSION` is a
non-empty string, and `detect_pii_or_secrets()` returns the expected
policy-id list for text containing multiple violation types at once.
"""

from policy.safety_policy import (
    PATTERN_TO_POLICY_ID,
    PII_SECRET_PATTERNS,
    SAFETY_POLICY_IDS,
    SAFETY_POLICY_VERSION,
    detect_pii_or_secrets,
)

SAFE_TEXT = (
    "The river level near the ward has risen slightly overnight. Residents "
    "in low-lying areas should move valuables to higher ground and follow "
    "guidance from the coordinator. Shelter B has capacity for forty more "
    "people."
)

POSITIVE_EXAMPLES = {
    "indian_mobile_number": "Please call the responder at 9876543210 for pickup.",
    "email_address": "Reply to coordinator@thunai-ward7.example with updates.",
    "national_id_number": "Aadhaar on file: 1234 5678 9012 for verification.",
    "aws_access_key_id": "Found stray credential AKIAABCDEFGHIJKLMNOP in the log.",
    "generic_secret_assignment": 'config had api_key: "sk_live_abcdefgh12345678" committed.',
}


def test_safety_policy_ids_is_closed_and_nonempty() -> None:
    assert isinstance(SAFETY_POLICY_IDS, frozenset)
    assert SAFETY_POLICY_IDS
    for policy_id in SAFETY_POLICY_IDS:
        assert isinstance(policy_id, str) and policy_id


def test_safety_policy_version_is_nonempty_string() -> None:
    assert isinstance(SAFETY_POLICY_VERSION, str)
    assert SAFETY_POLICY_VERSION.strip()


def test_every_pattern_maps_to_a_declared_policy_id() -> None:
    assert set(PATTERN_TO_POLICY_ID) == set(PII_SECRET_PATTERNS)
    assert set(PATTERN_TO_POLICY_ID.values()) <= SAFETY_POLICY_IDS


def test_each_pattern_matches_its_positive_example() -> None:
    for pattern_name, example_text in POSITIVE_EXAMPLES.items():
        pattern = PII_SECRET_PATTERNS[pattern_name]
        assert pattern.search(example_text), (
            f"pattern {pattern_name!r} did not match its positive example"
        )


def test_no_pattern_false_positives_on_safe_text() -> None:
    for pattern_name, pattern in PII_SECRET_PATTERNS.items():
        assert not pattern.search(SAFE_TEXT), (
            f"pattern {pattern_name!r} false-positived on ordinary safe text"
        )


def test_detect_pii_or_secrets_returns_empty_for_safe_text() -> None:
    assert detect_pii_or_secrets(SAFE_TEXT) == []


def test_detect_pii_or_secrets_detects_each_category_individually() -> None:
    for pattern_name, example_text in POSITIVE_EXAMPLES.items():
        expected_policy_id = PATTERN_TO_POLICY_ID[pattern_name]
        assert detect_pii_or_secrets(example_text) == [expected_policy_id]


def test_detect_pii_or_secrets_detects_multiple_violation_types_at_once() -> None:
    combined_text = (
        "Resident can be reached at 9876543210 or coordinator@thunai-ward7.example. "
        "Their Aadhaar on file is 1234 5678 9012. "
        "Do not commit credential AKIAABCDEFGHIJKLMNOP anywhere."
    )
    result = detect_pii_or_secrets(combined_text)
    assert result == sorted(
        {"resident_contact_leak", "resident_id_leak", "secret_leak"}
    )
    # Result must be deduplicated and sorted, not merely non-empty.
    assert result == ["resident_contact_leak", "resident_id_leak", "secret_leak"]
