"""Validation test for the seed/sample_messages/ fixture set (task 11.3).

Asserts the fixture set's structural and label-validity properties so a
future implementer of Intake_Agent (task 13.3) and the eval suite (task
28.1/28.5) can trust these fixtures without re-checking them by hand.
"""

import json
from pathlib import Path
from typing import get_args

import pytest

from schemas.decisions import EmergencyRequest

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "seed" / "sample_messages" / "messages.json"


def _load_messages() -> list[dict]:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _field_literal_values(field_name: str) -> tuple:
    field_info = EmergencyRequest.model_fields[field_name]
    return get_args(field_info.annotation)


@pytest.fixture(scope="module")
def messages() -> list[dict]:
    return _load_messages()


def test_message_count_between_6_and_8(messages):
    assert 6 <= len(messages) <= 8


def test_every_message_has_non_empty_text(messages):
    for msg in messages:
        assert isinstance(msg.get("text"), str)
        assert msg["text"].strip() != ""


def test_message_ids_are_unique(messages):
    ids = [msg["message_id"] for msg in messages]
    assert len(ids) == len(set(ids))
    assert all(isinstance(i, str) and i.strip() for i in ids)


def test_covers_required_categories(messages):
    categories = {msg["expected"]["request_category"] for msg in messages}
    required = {"RESCUE", "MEDICAL", "SHELTER", "SUPPLIES", "INFORMATION"}
    missing = required - categories
    assert not missing, f"Missing required category coverage: {missing}"


def test_exactly_one_mobility_assistance_fixture(messages):
    matches = [m for m in messages if m["expected"].get("mobility_assistance") is True]
    assert len(matches) == 1, f"Expected exactly 1 mobility-assistance fixture, found {len(matches)}"


def test_exactly_one_ambiguous_location_fixture(messages):
    matches = [m for m in messages if "expected_location_candidates_count_min" in m]
    assert len(matches) == 1, f"Expected exactly 1 ambiguous-location fixture, found {len(matches)}"
    assert matches[0]["expected_location_candidates_count_min"] >= 2


def test_exactly_one_off_topic_manual_triage_fixture(messages):
    matches = [m for m in messages if m.get("expected_manual_triage") is True]
    assert len(matches) == 1, f"Expected exactly 1 off-topic/manual-triage fixture, found {len(matches)}"
    # The off-topic fixture should be labelled OTHER, per requirements.md 5.2/5.8.
    assert matches[0]["expected"]["request_category"] == "OTHER"


def test_both_tamil_and_english_represented(messages):
    hints = {msg["source_language_hint"] for msg in messages}
    has_tamil = any(h in ("ta", "ta-en") for h in hints)
    has_english = any(h in ("en", "ta-en") for h in hints)
    assert has_tamil, "No Tamil-language message found in fixture set"
    assert has_english, "No English-language message found in fixture set"


@pytest.mark.parametrize(
    "field_name",
    ["request_category", "mobility_assistance", "medical_need", "urgency_band"],
)
def test_expected_labels_use_valid_literal_values(messages, field_name):
    valid_values = _field_literal_values(field_name)
    for msg in messages:
        value = msg["expected"][field_name]
        assert value in valid_values, (
            f"{msg['message_id']}.expected.{field_name} = {value!r} is not one of "
            f"the valid EmergencyRequest values {valid_values!r}"
        )


def test_expected_dispatch_eligible_is_bool(messages):
    for msg in messages:
        assert isinstance(msg["expected"]["dispatch_eligible"], bool)


def test_expected_occupant_count_matches_field_type(messages):
    for msg in messages:
        value = msg["expected"]["occupant_count"]
        if value == "unknown":
            continue
        assert isinstance(value, int)
        assert 0 <= value <= 99
