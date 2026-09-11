"""Unit tests for agents/dispatch_agent.py (task 13.4).

Validates: Requirements 6.1, 6.2, 6.3, 6.5, 6.6, 6.8, 6.10, 6.11; Design §3.1 (Dispatch).

Every test drives `run_dispatch_decision` directly with a stubbed
`produce_decision` callable, per that function's own docstring — no live
Bedrock invocation is made anywhere in this file.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from schemas.decisions import DispatchDecision
from schemas.entities import Responder, Shelter
from schemas.structured import ValidationFailureFallback

from agents import dispatch_agent

TABLE_NAME = "thunai-state-dispatch-agent-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Fresh moto-mocked `thunai-state` table for every test, mirroring
    tests/unit/test_dispatch_tools.py's fixture."""
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


def _responder(**overrides) -> Responder:
    defaults = dict(
        responder_id="resp-1",
        name="Karthik R.",
        home_coords=(13.08, 80.27),
        equipment=["boat"],
        availability_status="AVAILABLE",
    )
    defaults.update(overrides)
    return Responder(**defaults)


def _shelter(**overrides) -> Shelter:
    defaults = dict(
        shelter_id="shelter-1",
        name="Ward-7 Community Hall",
        coords=(13.08, 80.27),
        total_capacity=50,
        available_capacity=50,
    )
    defaults.update(overrides)
    return Shelter(**defaults)


def _decision(**overrides) -> DispatchDecision:
    defaults = dict(
        selected_responder_id="resp-1",
        alternative_responder_ids=[],
        selection_rationale="Nearest available responder with a boat.",
        confidence=0.9,
        action="execute",
        category="routine_dispatch_ambulatory",
        is_irreversible_action=False,
    )
    defaults.update(overrides)
    return DispatchDecision(**defaults)


# ---------------------------------------------------------------------------
# rank_candidates
# ---------------------------------------------------------------------------


class TestRankCandidates:
    def test_prefers_equipped_candidate_over_closer_unequipped_one(self):
        candidates = [
            {"responder_id": "near-no-boat", "distance_km": 1.0, "active_assignment_count": 0, "equipment": []},
            {"responder_id": "far-boat", "distance_km": 3.0, "active_assignment_count": 0, "equipment": ["boat"]},
        ]
        ranked = dispatch_agent.rank_candidates(candidates, required_equipment=["boat"])
        assert [c["responder_id"] for c in ranked] == ["far-boat", "near-no-boat"]

    def test_prefers_lower_load_before_distance_when_equipment_ties(self):
        candidates = [
            {"responder_id": "busy-close", "distance_km": 1.0, "active_assignment_count": 2, "equipment": ["boat"]},
            {"responder_id": "free-far", "distance_km": 4.0, "active_assignment_count": 0, "equipment": ["boat"]},
        ]
        ranked = dispatch_agent.rank_candidates(candidates, required_equipment=["boat"])
        assert [c["responder_id"] for c in ranked] == ["free-far", "busy-close"]

    def test_no_required_equipment_ranks_by_load_then_distance(self):
        candidates = [
            {"responder_id": "a", "distance_km": 2.0, "active_assignment_count": 0, "equipment": []},
            {"responder_id": "b", "distance_km": 1.0, "active_assignment_count": 0, "equipment": []},
        ]
        ranked = dispatch_agent.rank_candidates(candidates)
        assert [c["responder_id"] for c in ranked] == ["b", "a"]

    def test_never_mutates_input_list(self):
        candidates = [{"responder_id": "a", "distance_km": 2.0, "active_assignment_count": 0, "equipment": []}]
        original = list(candidates)
        dispatch_agent.rank_candidates(candidates)
        assert candidates == original


# ---------------------------------------------------------------------------
# evaluate_shelter_capacity
# ---------------------------------------------------------------------------


class TestEvaluateShelterCapacity:
    def test_sufficient_capacity(self):
        state_store.put_shelter(_shelter(available_capacity=10))
        result = dispatch_agent.evaluate_shelter_capacity("shelter-1", 5)
        assert result == {"ok": True, "sufficient": True, "available_capacity": 10, "reason": None}

    def test_insufficient_capacity(self):
        state_store.put_shelter(_shelter(available_capacity=2))
        result = dispatch_agent.evaluate_shelter_capacity("shelter-1", 5)
        assert result["sufficient"] is False
        assert result["reason"] == "insufficient_capacity"
        assert result["available_capacity"] == 2

    def test_shelter_not_found(self):
        result = dispatch_agent.evaluate_shelter_capacity("no-such-shelter", 1)
        assert result == {"ok": True, "sufficient": False, "available_capacity": 0, "reason": "shelter_not_found"}


# ---------------------------------------------------------------------------
# run_dispatch_decision: successful assignment (Req 6.1, 6.2, 6.3, 6.8)
# ---------------------------------------------------------------------------


class TestSuccessfulAssignment:
    def test_assigns_selected_responder(self):
        state_store.put_responder(_responder())

        result = dispatch_agent.run_dispatch_decision(
            request_id="req-1",
            run_id="run-1",
            produce_decision=lambda: _decision(),
        )

        assert result["ok"] is True
        assert result["outcome"] == "assigned"
        assert result["assigned_responder_id"] == "resp-1"

        updated = state_store.get_responder("resp-1")
        assert updated.availability_status == "ASSIGNED"
        assert updated.active_assignment_count == 1

    def test_never_ask_category_at_sufficient_confidence_executes_without_escalation(self):
        state_store.put_responder(_responder())
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-1",
            run_id="run-1",
            produce_decision=lambda: _decision(category="routine_dispatch_ambulatory", confidence=0.95),
        )
        assert result["outcome"] == "assigned"


# ---------------------------------------------------------------------------
# run_dispatch_decision: no-capacity escalation (Req 6.6)
# ---------------------------------------------------------------------------


class TestNoCapacityEscalation:
    def test_escalates_when_no_candidate_named(self):
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-2",
            run_id="run-1",
            produce_decision=lambda: _decision(selected_responder_id=None, alternative_responder_ids=[]),
        )
        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "no_capacity"
        assert result["escalation"]["ok"] is True
        assert result["assigned_responder_id"] is None

    def test_escalates_when_low_confidence_forces_ask_human(self):
        state_store.put_responder(_responder())
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-2b",
            run_id="run-1",
            produce_decision=lambda: _decision(confidence=0.1),
        )
        assert result["outcome"] == "escalated"
        # Confidence-floor escalation still keeps the request's own category.
        assert result["escalation_type"] == "routine_dispatch_ambulatory"
        # No assignment was attempted.
        assert state_store.get_responder("resp-1").availability_status == "AVAILABLE"


# ---------------------------------------------------------------------------
# run_dispatch_decision: mobility/medical always-ask (Req 6.5)
# ---------------------------------------------------------------------------


class TestMobilityMedicalAlwaysAsk:
    def test_mobility_assistance_escalates_and_withholds_assignment(self):
        state_store.put_responder(_responder())
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-3",
            run_id="run-1",
            produce_decision=lambda: _decision(confidence=0.99, action="execute"),
            mobility_assistance=True,
        )
        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "dispatch_non_ambulatory"
        assert result["assigned_responder_id"] is None
        # The responder must remain untouched pending approval.
        assert state_store.get_responder("resp-1").availability_status == "AVAILABLE"

    def test_medical_need_escalates_even_at_high_confidence(self):
        state_store.put_responder(_responder())
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-4",
            run_id="run-1",
            produce_decision=lambda: _decision(confidence=1.0, action="execute", is_irreversible_action=False),
            medical_need=True,
        )
        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "dispatch_non_ambulatory"


# ---------------------------------------------------------------------------
# run_dispatch_decision: shelter-capacity escalation (Req 6.11)
# ---------------------------------------------------------------------------


class TestShelterCapacityEscalation:
    def test_escalates_before_producing_a_dispatch_decision(self):
        state_store.put_shelter(_shelter(available_capacity=1))
        calls: list[int] = []

        def _produce():
            calls.append(1)
            return _decision()

        result = dispatch_agent.run_dispatch_decision(
            request_id="req-5",
            run_id="run-1",
            produce_decision=_produce,
            occupant_count=5,
            shelter_id="shelter-1",
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "shelter_capacity"
        assert calls == []  # produce_decision was never called

    def test_sufficient_shelter_capacity_proceeds_to_dispatch(self):
        state_store.put_shelter(_shelter(available_capacity=10))
        state_store.put_responder(_responder())
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-6",
            run_id="run-1",
            produce_decision=lambda: _decision(),
            occupant_count=5,
            shelter_id="shelter-1",
        )
        assert result["outcome"] == "assigned"


# ---------------------------------------------------------------------------
# run_dispatch_decision: re-plan on responder-no-longer-available (Req 6.10)
# ---------------------------------------------------------------------------


class TestResponderNoLongerAvailableReplan:
    def test_falls_back_to_alternative_when_selected_responder_unavailable(self):
        state_store.put_responder(
            _responder(responder_id="resp-1", availability_status="ASSIGNED", active_assignment_id="other-req")
        )
        state_store.put_responder(_responder(responder_id="resp-2"))

        result = dispatch_agent.run_dispatch_decision(
            request_id="req-7",
            run_id="run-1",
            produce_decision=lambda: _decision(
                selected_responder_id="resp-1", alternative_responder_ids=["resp-2"]
            ),
        )

        assert result["outcome"] == "assigned"
        assert result["assigned_responder_id"] == "resp-2"
        assert len(result["attempted"]) == 2
        assert result["attempted"][0]["result"]["reason"] == "responder_no_longer_available"

    def test_escalates_no_capacity_when_every_alternative_is_unavailable(self):
        state_store.put_responder(
            _responder(responder_id="resp-1", availability_status="ASSIGNED", active_assignment_id="other-req")
        )
        state_store.put_responder(
            _responder(responder_id="resp-2", availability_status="ASSIGNED", active_assignment_id="other-req-2")
        )

        result = dispatch_agent.run_dispatch_decision(
            request_id="req-8",
            run_id="run-1",
            produce_decision=lambda: _decision(
                selected_responder_id="resp-1", alternative_responder_ids=["resp-2"]
            ),
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "no_capacity"
        assert len(result["attempted"]) == 2


# ---------------------------------------------------------------------------
# run_dispatch_decision: idempotent replay for the same request id (Req 6.3, 6.9)
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    def test_second_call_for_same_request_id_does_not_double_assign(self):
        state_store.put_responder(_responder())

        first = dispatch_agent.run_dispatch_decision(
            request_id="req-9",
            run_id="run-1",
            produce_decision=lambda: _decision(),
        )
        assert first["outcome"] == "assigned"
        assert state_store.get_responder("resp-1").active_assignment_count == 1

        second = dispatch_agent.run_dispatch_decision(
            request_id="req-9",
            run_id="run-1",
            produce_decision=lambda: _decision(),
        )

        assert second["outcome"] == "assigned"
        assert second["assigned_responder_id"] == "resp-1"
        # No second write: the responder's assignment count is unchanged.
        assert state_store.get_responder("resp-1").active_assignment_count == 1


# ---------------------------------------------------------------------------
# run_dispatch_decision: structured-output validation failure (Req 10.11)
# ---------------------------------------------------------------------------


class TestValidationFailureFallback:
    def test_validation_failure_escalates(self):
        fallback = ValidationFailureFallback(raw_output="not json", error_detail="boom")
        result = dispatch_agent.run_dispatch_decision(
            request_id="req-10",
            run_id="run-1",
            produce_decision=lambda: fallback,
        )
        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "validation_failure"


# ---------------------------------------------------------------------------
# build_dispatch_agent (Req 4.12, 4.13): construction only, no model call.
# ---------------------------------------------------------------------------


class TestBuildDispatchAgent:
    def test_agent_id_and_no_session_manager(self, monkeypatch):
        monkeypatch.setenv("THUNAI_LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
        monkeypatch.setenv("THUNAI_HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")
        agent = dispatch_agent.build_dispatch_agent()
        assert agent.agent_id == dispatch_agent.AGENT_ID
        assert agent.name == dispatch_agent.AGENT_ID
        assert agent._session_manager is None
