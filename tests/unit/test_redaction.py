"""Unit tests for harness/redaction.py::redact_fields() (task 8.1).

Covers exact-name field redaction at the top level and at various nesting
depths (including inside lists-of-dicts), suffix-pattern redaction
(`*_secret`, `*_credential`, `*_api_key`), pass-through of non-matching
ordinary fields, and safe handling of non-dict/non-string leaf values
(numbers, booleans, None, lists of plain strings).

Per redaction.py's own documented design decision, `redact_fields()` is
field-name-matching only (design.md §3.6's exact wording) and does not scan
string *values* for PII-shaped content — so no test here exercises value
content scanning under a non-matching key name.
"""

from harness.redaction import REDACTION_PLACEHOLDER, redact_fields


def test_exact_name_fields_redacted_at_top_level():
    data = {
        "resident_name": "Priya Kumar",
        "resident_contact": "+91-9876543210",
        "resident_address": "12 River Road",
        "request_category": "RESCUE",
    }

    result = redact_fields(data)

    assert result["resident_name"] == REDACTION_PLACEHOLDER
    assert result["resident_contact"] == REDACTION_PLACEHOLDER
    assert result["resident_address"] == REDACTION_PLACEHOLDER
    assert result["request_category"] == "RESCUE"


def test_exact_name_fields_redacted_inside_nested_dict():
    data = {
        "tool_input": {
            "resident_name": "Arun Raj",
            "occupant_count": 3,
        }
    }

    result = redact_fields(data)

    assert result["tool_input"]["resident_name"] == REDACTION_PLACEHOLDER
    assert result["tool_input"]["occupant_count"] == 3


def test_exact_name_fields_redacted_inside_list_of_dicts():
    data = {
        "requests": [
            {"resident_name": "A", "resident_address": "1 Main St"},
            {"resident_name": "B", "resident_address": "2 Main St"},
        ]
    }

    result = redact_fields(data)

    for entry in result["requests"]:
        assert entry["resident_name"] == REDACTION_PLACEHOLDER
        assert entry["resident_address"] == REDACTION_PLACEHOLDER


def test_suffix_pattern_fields_redacted_at_various_depths():
    data = {
        "db_password_secret": "supersecret",
        "aws_credential": "AKIAABC",
        "third_party_api_key": "abc123",
        "nested": {
            "auth_secret": "hidden",
            "list_field": [{"sns_credential": "hidden-too", "ok": 1}],
        },
    }

    result = redact_fields(data)

    assert result["db_password_secret"] == REDACTION_PLACEHOLDER
    assert result["aws_credential"] == REDACTION_PLACEHOLDER
    assert result["third_party_api_key"] == REDACTION_PLACEHOLDER
    assert result["nested"]["auth_secret"] == REDACTION_PLACEHOLDER
    assert result["nested"]["list_field"][0]["sns_credential"] == REDACTION_PLACEHOLDER
    assert result["nested"]["list_field"][0]["ok"] == 1


def test_non_matching_fields_pass_through_unchanged():
    data = {
        "incident_id": "inc_123",
        "severity_band": "WARNING",
        "confidence": 0.82,
        "notes": "responder dispatched",
    }

    result = redact_fields(data)

    assert result == data


def test_non_dict_non_string_leaf_values_handled_without_error():
    data = {
        "occupant_count": 3,
        "confidence": 0.75,
        "is_irreversible_action": True,
        "resolved": False,
        "prior_reading": None,
        "tags": ["urgent", "flood", "ward-7"],
        "readings": [1.2, 3.4, None, True],
    }

    result = redact_fields(data)

    assert result == data


def test_redacted_key_with_non_dict_value_is_still_replaced():
    # Even if a matched key's value is itself a nested structure, the
    # replacement is the flat placeholder string, not a recursive redaction
    # of that structure's contents.
    data = {"resident_address": {"line1": "12 River Road", "line2": "Ward 7"}}

    result = redact_fields(data)

    assert result["resident_address"] == REDACTION_PLACEHOLDER


def test_top_level_list_input_is_handled():
    data = [
        {"resident_name": "A"},
        {"resident_name": "B", "ok": 1},
    ]

    result = redact_fields(data)

    assert result[0]["resident_name"] == REDACTION_PLACEHOLDER
    assert result[1]["resident_name"] == REDACTION_PLACEHOLDER
    assert result[1]["ok"] == 1


def test_scalar_top_level_input_passes_through_unchanged():
    assert redact_fields("just a string") == "just a string"
    assert redact_fields(42) == 42
    assert redact_fields(None) is None
    assert redact_fields(True) is True


def test_original_data_not_mutated():
    data = {"resident_name": "Priya", "other": {"nested": "value"}}
    original_copy = {"resident_name": "Priya", "other": {"nested": "value"}}

    redact_fields(data)

    assert data == original_copy
