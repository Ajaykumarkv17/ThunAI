"""Unit tests for agents/intake_agent.py (task 13.3).

Validates: Requirements 5.1-5.10; Design §3.1 (Intake), §4.1.

Mirrors tests/unit/test_dispatch_agent.py's / tests/unit/test_incident_tools.py's
established moto-mocked `thunai-state` fixture, and
agents/dispatch_agent.py::run_dispatch_decision's own "produce_decision
callable" test pattern -- no live Bedrock invocation is required anywhere
in this file.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from agents import intake_agent
from memory import state_store
from schemas.decisions import EmergencyRequest
from schemas.structured import ValidationFailureFallback

TABLE_NAME = "thunai-state-intake-agent-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Fresh moto-mocked `thunai-state` table for every test, mirroring
    tests/unit/test_incident_tools.py's / test_intake_tools.py's fixture."""
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


def _request(**overrides) -> EmergencyRequest:
    defaults = dict(
        request_category="RESCUE",
        occupant_count=4,
        location_reference="Old Ferry Road",
        mobility_assistance="unknown",
        medical_need="unknown",
        source_language="en",
        urgency_band="URGENT",
        confidence=0.9,
        action="execute",
        category="routine_intake_request",
        is_irreversible_action=False,
        rationale="Resident is stranded and needs a boat.",
        dispatch_eligible=True,
    )
    defaults.update(overrides)
    return EmergencyRequest(**defaults)


def _request_with_invalid_category(**overrides) -> EmergencyRequest:
    """Build an EmergencyRequest bypassing the closed request_category
    Literal's validation, for the defensive "no category assigned" check
    (Req 5.8) -- EmergencyRequest.request_category cannot itself hold an
    empty string once validated, so this uses model_construct to simulate
    the (schema-disallowed but defensively checked) case."""
    defaults = dict(
        request_category="",
        occupant_count=4,
        location_reference="Old Ferry Road",
        location_candidates=[],
        mobility_assistance="unknown",
        medical_need="unknown",
        source_language="en",
        urgency_band="URGENT",
        confidence=0.9,
        action="execute",
        category="routine_intake_request",
        is_irreversible_action=False,
        rationale="Unclear message.",
        dispatch_eligible=True,
    )
    defaults.update(overrides)
    return EmergencyRequest.model_construct(**defaults)


# ---------------------------------------------------------------------------
# postprocess_emergency_request (Req 5.3, 5.6)
# ---------------------------------------------------------------------------


class TestPostprocessEmergencyRequest:
    def test_mobility_assistance_true_forces_immediate(self):
        request = _request(mobility_assistance=True, urgency_band="ROUTINE")

        result = intake_agent.postprocess_emergency_request(
            request,
            location_candidate_count=1,
            location_candidates=[],
            is_outside_configured_languages=False,
        )

        assert result.urgency_band == "IMMEDIATE"

    def test_medical_need_true_forces_immediate(self):
        request = _request(medical_need=True, urgency_band="ROUTINE")

        result = intake_agent.postprocess_emergency_request(
            request,
            location_candidate_count=1,
            location_candidates=[],
            is_outside_configured_languages=False,
        )

        assert result.urgency_band == "IMMEDIATE"

    def test_unambiguous_location_leaves_dispatch_eligible_unchanged(self):
        request = _request(dispatch_eligible=True)

        result = intake_agent.postprocess_emergency_request(
            request,
            location_candidate_count=1,
            location_candidates=[],
            is_outside_configured_languages=False,
        )

        assert result.dispatch_eligible is True
        assert result.location_candidates == []

    def test_ambiguous_location_withholds_dispatch_eligibility_and_records_candidates(self):
        request = _request(dispatch_eligible=True)

        result = intake_agent.postprocess_emergency_request(
            request,
            location_candidate_count=2,
            location_candidates=["Old Ferry Road", "Ward-7 South"],
            is_outside_configured_languages=False,
        )

        assert result.dispatch_eligible is False
        assert result.location_candidates == ["Old Ferry Road", "Ward-7 South"]

    def test_zero_candidates_also_withholds_dispatch_eligibility(self):
        request = _request(dispatch_eligible=True)

        result = intake_agent.postprocess_emergency_request(
            request,
            location_candidate_count=0,
            location_candidates=[],
            is_outside_configured_languages=False,
        )

        assert result.dispatch_eligible is False

    def test_never_invents_a_fact_the_model_did_not_state(self):
        # occupant_count/mobility_assistance/medical_need stay exactly as
        # the model set them when no forcing rule applies.
        request = _request(occupant_count="unknown", mobility_assistance="unknown", medical_need="unknown")

        result = intake_agent.postprocess_emergency_request(
            request,
            location_candidate_count=1,
            location_candidates=[],
            is_outside_configured_languages=False,
        )

        assert result.occupant_count == "unknown"
        assert result.mobility_assistance == "unknown"
        assert result.medical_need == "unknown"

    def test_does_not_mutate_the_original_request(self):
        request = _request(urgency_band="ROUTINE", mobility_assistance=True)
        intake_agent.postprocess_emergency_request(
            request, location_candidate_count=1, location_candidates=[], is_outside_configured_languages=False
        )
        assert request.urgency_band == "ROUTINE"  # original untouched


# ---------------------------------------------------------------------------
# validate_category_assignment (Req 5.2)
# ---------------------------------------------------------------------------


class TestValidateCategoryAssignment:
    def test_information_with_no_physical_assistance_has_no_defect(self):
        request = _request(request_category="INFORMATION", mobility_assistance=False, medical_need=False)
        assert intake_agent.validate_category_assignment(request) == []

    def test_information_with_mobility_assistance_true_is_flagged(self):
        request = _request(request_category="INFORMATION", mobility_assistance=True)
        defects = intake_agent.validate_category_assignment(request)
        assert len(defects) == 1

    def test_information_with_medical_need_true_is_flagged(self):
        request = _request(request_category="INFORMATION", medical_need=True)
        defects = intake_agent.validate_category_assignment(request)
        assert len(defects) == 1

    def test_rescue_category_is_never_flagged(self):
        request = _request(request_category="RESCUE", mobility_assistance=True, medical_need=True)
        assert intake_agent.validate_category_assignment(request) == []


# ---------------------------------------------------------------------------
# resident_facing_fields (Req 5.10)
# ---------------------------------------------------------------------------


class TestResidentFacingFields:
    def test_excludes_pii_and_free_text(self):
        persisted = {
            "request_id": "MSG-001",
            "request_category": "RESCUE",
            "urgency_band": "IMMEDIATE",
            "state": "DISPATCH_ELIGIBLE",
            "created_at": "2026-09-01T10:00:00+05:30",
            "updated_at": "2026-09-01T10:00:00+05:30",
            "resident_free_text": "my address is 12 Lake View Road, call me at 98765xxxxx",
            "sender_reference": "resident-synthetic-014",
            "location_reference": "12 Lake View Road",
            "occupant_count": 4,
            "mobility_assistance": "unknown",
            "medical_need": "unknown",
        }

        result = intake_agent.resident_facing_fields(persisted)

        assert result == {
            "request_id": "MSG-001",
            "request_category": "RESCUE",
            "urgency_band": "IMMEDIATE",
            "state": "DISPATCH_ELIGIBLE",
            "created_at": "2026-09-01T10:00:00+05:30",
            "updated_at": "2026-09-01T10:00:00+05:30",
        }
        assert "resident_free_text" not in result
        assert "sender_reference" not in result
        assert "location_reference" not in result
        assert "occupant_count" not in result


# ---------------------------------------------------------------------------
# process_inbound_message (Req 5.1, 5.4-5.9): the deterministic driver.
# ---------------------------------------------------------------------------


class TestProcessInboundMessage:
    def test_dispatch_eligible_creation_when_confident_and_not_always_ask(self):
        request = _request(
            request_category="SUPPLIES",
            location_reference="Old Ferry Road",
            confidence=0.9,
            action="execute",
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-101",
            run_id="run-1",
            text="We need drinking water at Old Ferry Road.",
            produce_request=lambda: request,
        )

        assert result == {
            "ok": True,
            "outcome": "created",
            "request_id": "MSG-101",
            "dispatch_eligible": True,
        }
        persisted = state_store.get_request("MSG-101")
        assert persisted["state"] == "DISPATCH_ELIGIBLE"
        assert persisted["resident_free_text"] == "We need drinking water at Old Ferry Road."

    def test_low_confidence_escalates_request_triage_and_withholds_dispatch(self):
        request = _request(
            request_category="SUPPLIES",
            location_reference="Old Ferry Road",
            confidence=0.2,
            action="execute",
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-102",
            run_id="run-1",
            text="Need some supplies maybe.",
            produce_request=lambda: request,
        )

        assert result["ok"] is True
        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "request_triage"
        assert result["escalation"]["ok"] is True
        persisted = state_store.get_request("MSG-102")
        assert persisted["state"] == "NON_DISPATCH_ELIGIBLE"

    def test_action_ask_human_escalates_request_triage_even_with_high_confidence(self):
        request = _request(
            request_category="SUPPLIES",
            location_reference="Old Ferry Road",
            confidence=0.95,
            action="ask_human",
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-103",
            run_id="run-1",
            text="Not sure what's needed here.",
            produce_request=lambda: request,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "request_triage"

    def test_ambiguous_location_escalates_location_clarification(self):
        # "Riverside Colony" matches both the ward-area entry and the
        # shelter named "Riverside Colony School" in the seeded gazetteer.
        request = _request(
            request_category="RESCUE",
            location_reference="Riverside Colony",
            confidence=0.95,
            action="execute",
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-104",
            run_id="run-1",
            text="Water rising near Riverside Colony, please help.",
            produce_request=lambda: request,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "location_clarification"
        persisted = state_store.get_request("MSG-104")
        assert persisted["state"] == "NON_DISPATCH_ELIGIBLE"
        assert len(persisted["location_candidates"]) >= 2

    def test_information_category_routes_to_knowledge_agent_no_request_created(self):
        request = _request(
            request_category="INFORMATION",
            mobility_assistance=False,
            medical_need=False,
            confidence=0.9,
            action="execute",
            dispatch_eligible=False,
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-105",
            run_id="run-1",
            text="Will water come to our area today?",
            produce_request=lambda: request,
        )

        assert result == {"ok": True, "outcome": "routed_to_knowledge_agent", "request_id": None}
        assert state_store.get_request("MSG-105") is None

    def test_no_category_escalates_manual_triage_and_preserves_message(self):
        request = _request_with_invalid_category(confidence=0.9, action="execute")

        result = intake_agent.process_inbound_message(
            message_id="MSG-106",
            run_id="run-1",
            text="Is this the SIM card shop?",
            produce_request=lambda: request,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "manual_triage"
        assert result["request_id"] is None
        assert state_store.get_request("MSG-106") is None

    def test_unsupported_language_escalates_manual_triage(self):
        request = _request(request_category="OTHER", confidence=0.9, action="execute")

        result = intake_agent.process_inbound_message(
            message_id="MSG-107",
            run_id="run-1",
            text="12345 !!! ###",  # detect_language reports "unknown" -> outside configured set
            produce_request=lambda: request,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "manual_triage"

    def test_information_with_mobility_assistance_true_escalates_manual_triage_not_knowledge(self):
        # Self-contradictory model output: INFORMATION paired with an
        # indicator that already asserts physical assistance is needed.
        request = _request(
            request_category="INFORMATION",
            mobility_assistance=True,
            confidence=0.9,
            action="execute",
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-108",
            run_id="run-1",
            text="My mother uses a wheelchair, water everywhere, need info.",
            produce_request=lambda: request,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "manual_triage"

    def test_validation_failure_escalates_and_creates_no_request(self):
        fallback = ValidationFailureFallback(raw_output="garbage", error_detail="could not parse")

        result = intake_agent.process_inbound_message(
            message_id="MSG-109",
            run_id="run-1",
            text="???",
            produce_request=lambda: fallback,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "validation_failure"
        assert result["request_id"] is None
        assert state_store.get_request("MSG-109") is None

    def test_mobility_true_forces_immediate_and_request_triage_escalation(self):
        # mobility_assistance is an always-ask signal (Req 6.5-adjacent for
        # Intake: mobility/medical forces IMMEDIATE; the request is still
        # dispatch-decided by policy.decide() on the request's own
        # category/confidence/action, which here is confident+execute, so
        # this asserts the IMMEDIATE forcing specifically).
        request = _request(
            request_category="RESCUE",
            mobility_assistance=True,
            urgency_band="ROUTINE",
            location_reference="Old Ferry Road",
            confidence=0.95,
            action="execute",
        )

        result = intake_agent.process_inbound_message(
            message_id="MSG-110",
            run_id="run-1",
            text="My mother uses a wheelchair, water is rising.",
            produce_request=lambda: request,
        )

        assert result["outcome"] == "created"
        persisted = state_store.get_request("MSG-110")
        assert persisted["urgency_band"] == "IMMEDIATE"
        assert persisted["mobility_assistance"] is True

    def test_idempotent_replay_on_second_call_for_same_message_id(self):
        call_count = {"n": 0}
        request = _request(
            request_category="SUPPLIES",
            location_reference="Old Ferry Road",
            confidence=0.9,
            action="execute",
        )

        def _produce():
            call_count["n"] += 1
            return request

        first = intake_agent.process_inbound_message(
            message_id="MSG-111", run_id="run-1", text="need supplies", produce_request=_produce
        )
        second = intake_agent.process_inbound_message(
            message_id="MSG-111", run_id="run-2", text="need supplies", produce_request=_produce
        )

        assert first == second
        assert call_count["n"] == 1  # the model was never invoked a second time

    def test_idempotent_replay_of_escalated_outcome(self):
        request = _request(
            request_category="SUPPLIES",
            location_reference="Old Ferry Road",
            confidence=0.1,
            action="execute",
        )
        call_count = {"n": 0}

        def _produce():
            call_count["n"] += 1
            return request

        first = intake_agent.process_inbound_message(
            message_id="MSG-112", run_id="run-1", text="maybe supplies", produce_request=_produce
        )
        second = intake_agent.process_inbound_message(
            message_id="MSG-112", run_id="run-2", text="maybe supplies", produce_request=_produce
        )

        assert first == second
        assert call_count["n"] == 1

    def test_pii_never_placed_in_resident_facing_projection_of_persisted_request(self):
        request = _request(
            request_category="RESCUE",
            location_reference="Old Ferry Road",
            confidence=0.95,
            action="execute",
        )

        intake_agent.process_inbound_message(
            message_id="MSG-113",
            run_id="run-1",
            text="Water in our house at 12 Lake View Road, call 98765xxxxx.",
            produce_request=lambda: request,
            sender_reference="resident-synthetic-099",
        )

        persisted = state_store.get_request("MSG-113")
        resident_facing = intake_agent.resident_facing_fields(persisted)

        assert "resident_free_text" not in resident_facing
        assert "sender_reference" not in resident_facing
        assert "location_reference" not in resident_facing
        assert persisted["resident_free_text"] == "Water in our house at 12 Lake View Road, call 98765xxxxx."
        assert persisted["sender_reference"] == "resident-synthetic-099"


# ---------------------------------------------------------------------------
# build_intake_prompt (Req 1.4, 5.1: image content handling)
# ---------------------------------------------------------------------------


class TestBuildIntakePrompt:
    def test_text_only_returns_plain_string(self):
        prompt = intake_agent.build_intake_prompt("hello", image_bytes=None)
        assert prompt == "hello"

    def test_none_text_and_no_image_returns_empty_string(self):
        prompt = intake_agent.build_intake_prompt(None, image_bytes=None)
        assert prompt == ""

    def test_image_only_returns_content_block_list_with_image_block(self):
        prompt = intake_agent.build_intake_prompt(None, image_bytes=b"fake-bytes", image_format="png")

        assert isinstance(prompt, list)
        assert len(prompt) == 1
        assert prompt[0]["image"] == {"format": "png", "source": {"bytes": b"fake-bytes"}}

    def test_text_and_image_returns_text_block_then_image_block(self):
        prompt = intake_agent.build_intake_prompt("water everywhere", image_bytes=b"fake-bytes")

        assert len(prompt) == 2
        assert prompt[0]["text"] == "water everywhere"
        assert prompt[1]["image"]["format"] == "jpeg"
        assert prompt[1]["image"]["source"]["bytes"] == b"fake-bytes"


# ---------------------------------------------------------------------------
# build_intake_agent (Req 4.12, 4.13)
# ---------------------------------------------------------------------------


class TestBuildIntakeAgent:
    def test_agent_id_and_no_session_manager(self, monkeypatch):
        monkeypatch.setenv("THUNAI_LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
        monkeypatch.setenv("THUNAI_HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")

        agent = intake_agent.build_intake_agent()

        assert agent.agent_id == intake_agent.AGENT_ID
        assert agent.system_prompt == intake_agent.SYSTEM_PROMPT

    def test_system_prompt_differs_from_dispatch_agent_prompt(self, monkeypatch):
        monkeypatch.setenv("THUNAI_LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
        monkeypatch.setenv("THUNAI_HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")

        from agents import dispatch_agent

        assert intake_agent.SYSTEM_PROMPT != dispatch_agent.SYSTEM_PROMPT
        assert intake_agent.AGENT_ID != dispatch_agent.AGENT_ID
