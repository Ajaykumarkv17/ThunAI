"""Unit tests for tools/intake_tools.py (task 12.3).

Validates: Requirements 5.6, 5.7, 5.9; Design §3.1 (Intake).
"""

from __future__ import annotations

import json
import os

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from tools import intake_tools

TABLE_NAME = "thunai-state-intake-tools-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Fresh moto-mocked `thunai-state` table for every test, mirroring
    tests/unit/test_state_store.py's / test_dispatch_tools.py's fixture."""
    os.environ["THUNAI_STATE_TABLE"] = TABLE_NAME
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


@pytest.fixture(autouse=True)
def _gazetteer_reset():
    intake_tools.reset_location_gazetteer_cache()
    yield
    intake_tools.reset_location_gazetteer_cache()


def _call(tool_fn, **kwargs):
    return tool_fn(**kwargs)


# ---------------------------------------------------------------------------
# resolve_location
# ---------------------------------------------------------------------------


class TestResolveLocation:
    def test_unambiguous_ward_area_match_returns_single_candidate(self):
        result = _call(intake_tools.resolve_location, location_text="near Old Ferry Road")

        assert result["ok"] is True
        assert result["candidate_count"] == 1
        assert result["candidates"][0]["location_reference"] == "Old Ferry Road"
        assert result["candidates"][0]["kind"] == "ward_area"
        assert isinstance(result["candidates"][0]["coords"], list)

    def test_shelter_name_match_returns_shelter_kind(self):
        result = _call(intake_tools.resolve_location, location_text="Riverside Colony School")

        refs = [c["location_reference"] for c in result["candidates"]]
        assert "Riverside Colony School" in refs
        shelter_candidate = next(
            c for c in result["candidates"] if c["location_reference"] == "Riverside Colony School"
        )
        assert shelter_candidate["kind"] == "shelter"
        assert shelter_candidate["coords"] == [11.26047, 79.8509]

    def test_ambiguous_wording_returns_multiple_ranked_candidates(self):
        # "Riverside Colony" matches both the ward-area entry and the
        # shelter named "Riverside Colony School" on shared keywords.
        result = _call(intake_tools.resolve_location, location_text="Riverside Colony")

        assert result["candidate_count"] >= 2
        scores = [c["score"] for c in result["candidates"]]
        assert scores == sorted(scores, reverse=True)

    def test_no_match_returns_empty_candidates(self):
        result = _call(intake_tools.resolve_location, location_text="somewhere unrelated entirely")

        assert result["ok"] is True
        assert result["candidate_count"] == 0
        assert result["candidates"] == []

    def test_candidates_capped_at_max_location_candidates(self):
        result = _call(intake_tools.resolve_location, location_text="street")

        assert result["candidate_count"] <= intake_tools.MAX_LOCATION_CANDIDATES

    def test_tool_is_decorated_and_carries_docstring(self):
        assert hasattr(intake_tools.resolve_location, "tool_spec")
        assert "Args:" in intake_tools.resolve_location.__doc__
        assert "Returns:" in intake_tools.resolve_location.__doc__


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------


class TestDetectLanguage:
    def test_detects_tamil_script_text(self):
        result = _call(
            intake_tools.detect_language,
            text="எங்க வீட்டுக்குள்ள தண்ணி வந்துடுச்சு",
        )

        assert result["ok"] is True
        assert result["detected_language"] == "ta"
        assert result["is_outside_configured_languages"] is False

    def test_detects_english_text(self):
        result = _call(
            intake_tools.detect_language,
            text="My father is having chest pains, please send medical help now.",
        )

        assert result["detected_language"] == "en"
        assert result["is_outside_configured_languages"] is False

    def test_detects_unknown_for_text_with_no_letters(self):
        result = _call(intake_tools.detect_language, text="12345 !!! ###")

        assert result["detected_language"] == "unknown"
        assert result["is_outside_configured_languages"] is True

    def test_language_outside_configured_set_is_flagged(self, monkeypatch):
        monkeypatch.setenv(intake_tools.CONFIGURED_LANGUAGES_ENV_VAR, "en")
        # Reload the module-level constant via a fresh read helper call —
        # CONFIGURED_LANGUAGES is read once at import time, so exercise the
        # underlying detection logic against an explicitly-narrowed set by
        # monkeypatching the module constant directly instead.
        monkeypatch.setattr(intake_tools, "CONFIGURED_LANGUAGES", frozenset({"en"}))

        result = _call(
            intake_tools.detect_language,
            text="எங்க வீட்டுக்குள்ள தண்ணி",
        )

        assert result["detected_language"] == "ta"
        assert result["is_outside_configured_languages"] is True

    def test_configured_languages_defaults_include_tamil_and_english(self):
        assert intake_tools.CONFIGURED_LANGUAGES == frozenset({"ta", "en"})

    def test_tool_is_decorated_and_carries_docstring(self):
        assert hasattr(intake_tools.detect_language, "tool_spec")
        assert "Args:" in intake_tools.detect_language.__doc__
        assert "Returns:" in intake_tools.detect_language.__doc__


# ---------------------------------------------------------------------------
# dedupe_check
# ---------------------------------------------------------------------------


class TestDedupeCheck:
    def test_first_call_is_not_a_duplicate(self):
        result = _call(intake_tools.dedupe_check, message_id="MSG-001")

        assert result == {
            "ok": True,
            "message_id": "MSG-001",
            "is_duplicate": False,
            "prior_request_id": None,
        }

    def test_second_call_after_processing_reports_duplicate_with_prior_request_id(self):
        idempotency_key = intake_tools._intake_idempotency_key("MSG-001")
        state_store.idempotent_write(
            idempotency_key, lambda: {"request_id": "req-abc", "state": "DISPATCH_ELIGIBLE"}
        )

        result = _call(intake_tools.dedupe_check, message_id="MSG-001")

        assert result["ok"] is True
        assert result["is_duplicate"] is True
        assert result["prior_request_id"] == "req-abc"

    def test_duplicate_without_recorded_request_id_reports_none(self):
        idempotency_key = intake_tools._intake_idempotency_key("MSG-005")
        state_store.idempotent_write(
            idempotency_key, lambda: {"routed_to": "knowledge_agent"}
        )

        result = _call(intake_tools.dedupe_check, message_id="MSG-005")

        assert result["is_duplicate"] is True
        assert result["prior_request_id"] is None

    def test_empty_message_id_returns_typed_failure(self):
        result = _call(intake_tools.dedupe_check, message_id="")

        assert result == {"ok": False, "reason": "missing_message_id"}

    def test_different_message_ids_are_independent(self):
        idempotency_key = intake_tools._intake_idempotency_key("MSG-001")
        state_store.idempotent_write(idempotency_key, lambda: {"request_id": "req-abc"})

        result = _call(intake_tools.dedupe_check, message_id="MSG-002")

        assert result["is_duplicate"] is False

    def test_tool_is_decorated_and_carries_docstring(self):
        assert hasattr(intake_tools.dedupe_check, "tool_spec")
        assert "Args:" in intake_tools.dedupe_check.__doc__
        assert "Returns:" in intake_tools.dedupe_check.__doc__


# ---------------------------------------------------------------------------
# Gazetteer construction sanity (against the real seed dataset)
# ---------------------------------------------------------------------------


class TestGazetteerConstruction:
    def test_default_gazetteer_path_exists_and_parses(self):
        assert intake_tools.DEFAULT_LOCATION_GAZETTEER_PATH.exists()
        data = json.loads(intake_tools.DEFAULT_LOCATION_GAZETTEER_PATH.read_text(encoding="utf-8"))
        assert "resident_points" in data
        assert "shelters" in data

    def test_gazetteer_contains_all_five_ward_areas_and_three_shelters(self):
        gazetteer = intake_tools._gazetteer()
        ward_areas = {e["location_reference"] for e in gazetteer if e["kind"] == "ward_area"}
        shelters = {e["location_reference"] for e in gazetteer if e["kind"] == "shelter"}

        assert ward_areas == {
            "Ward-7 North",
            "Ward-7 South",
            "Riverside Colony",
            "Kanal Street",
            "Old Ferry Road",
        }
        assert shelters == {
            "Ward-7 Community Hall",
            "Riverside Colony School",
            "Kanal Street Marriage Hall",
        }
