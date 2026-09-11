"""Unit tests for memory/state_store.py (task 6.1).

Runs against moto's mocked DynamoDB (`moto.mock_aws`), since no live
`thunai-state` table exists yet (CDK infra tasks 21.x are not yet
implemented).

Validates: Requirements 15.1, 15.2, 15.3, 15.4, 15.5, 15.8, 15.9, 15.10,
15.11, 6.4, 6.7, 6.10; Design §4.4.
"""

from __future__ import annotations

import os
import time

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from memory import state_store
from schemas.entities import EscalationOption, EscalationRecord, Incident, Responder, Shelter

TABLE_NAME = "thunai-state-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Provide a fresh moto-mocked `thunai-state` table for every test."""
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
        name="Community Hall",
        coords=(13.08, 80.27),
        total_capacity=40,
        available_capacity=40,
    )
    defaults.update(overrides)
    return Shelter(**defaults)


def _incident(**overrides) -> Incident:
    defaults = dict(
        incident_id="inc-1",
        river_reach_id="reach-1",
        severity_band="WATCH",
        status="OPEN",
        affected_areas=["Ward 4"],
        created_at="2026-09-03T10:00:00+05:30",
        updated_at="2026-09-03T10:00:00+05:30",
        last_readings={},
        triggered_rule_ids=["river_level_watch"],
        rule_set_version="rule-set-v1",
    )
    defaults.update(overrides)
    return Incident(**defaults)


def _escalation(**overrides) -> EscalationRecord:
    defaults = dict(
        escalation_id="esc-1",
        incident_id="inc-1",
        escalation_type="dispatch_assignment",
        status="OPEN",
        decision_summary="Dispatch to 14 Kanal Street.",
        reason="mobility-assistance requests always need sign-off",
        stakes="occupant cannot self-evacuate",
        options=[
            EscalationOption(option_id="1", label="Approve"),
            EscalationOption(option_id="2", label="Hold"),
        ],
        default_action="hold_assignment",
        response_deadline="2026-09-03T14:52:00+05:30",
        run_id="run-1",
    )
    defaults.update(overrides)
    return EscalationRecord(**defaults)


# ---------------------------------------------------------------------------
# idempotent_write
# ---------------------------------------------------------------------------


class TestIdempotentWrite:
    def test_rejects_missing_key(self):
        with pytest.raises(state_store.MissingIdempotencyKeyError):
            state_store.idempotent_write("", lambda: "result")

    def test_replays_within_window_without_reexecuting(self):
        calls = []

        def perform():
            calls.append(1)
            return {"value": len(calls)}

        first = state_store.idempotent_write("key-1", perform)
        second = state_store.idempotent_write("key-1", perform)
        third = state_store.idempotent_write("key-1", perform)

        assert first == {"value": 1}
        assert second == first
        assert third == first
        assert len(calls) == 1  # perform() only ran once

    def test_different_keys_execute_independently(self):
        calls = []

        def perform():
            calls.append(1)
            return len(calls)

        r1 = state_store.idempotent_write("key-a", perform)
        r2 = state_store.idempotent_write("key-b", perform)

        assert r1 == 1
        assert r2 == 2
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# assign_responder_atomic
# ---------------------------------------------------------------------------


class TestAssignResponderAtomic:
    def test_succeeds_when_available(self):
        state_store.put_responder(_responder())

        result = state_store.assign_responder_atomic("resp-1", "req-1", idempotency_key="assign:req-1")

        assert result["availability_status"] == "ASSIGNED"
        assert result["active_assignment_id"] == "req-1"
        assert result["active_assignment_count"] == 1

    def test_fails_when_already_assigned(self):
        state_store.put_responder(_responder(availability_status="ASSIGNED", active_assignment_id="req-0"))

        with pytest.raises(state_store.ResponderNoLongerAvailableError):
            state_store.assign_responder_atomic("resp-1", "req-1", idempotency_key="assign:req-1")

        # Left unchanged.
        current = state_store.get_responder("resp-1")
        assert current.availability_status == "ASSIGNED"
        assert current.active_assignment_id == "req-0"

    def test_single_conditional_update_not_read_then_write(self):
        """A racing second assignment attempt (simulating a concurrent
        caller) must fail atomically rather than double-assigning."""
        state_store.put_responder(_responder())

        first = state_store.assign_responder_atomic("resp-1", "req-1", idempotency_key="assign:req-1")
        assert first["availability_status"] == "ASSIGNED"

        with pytest.raises(state_store.ResponderNoLongerAvailableError):
            state_store.assign_responder_atomic("resp-1", "req-2", idempotency_key="assign:req-2")

        current = state_store.get_responder("resp-1")
        assert current.active_assignment_id == "req-1"  # the second attempt never took hold

    def test_no_longer_appears_in_available_gsi_after_assignment(self):
        state_store.put_responder(_responder())
        assert len(state_store.query_available_responders()) == 1

        state_store.assign_responder_atomic("resp-1", "req-1", idempotency_key="assign:req-1")

        assert state_store.query_available_responders() == []


# ---------------------------------------------------------------------------
# decrement_shelter_capacity / increment_shelter_capacity
# ---------------------------------------------------------------------------


class TestShelterCapacity:
    def test_decrement_succeeds_within_bounds(self):
        state_store.put_shelter(_shelter(total_capacity=40, available_capacity=40))

        result = state_store.decrement_shelter_capacity("shelter-1", 5, idempotency_key="place:req-1")

        assert result["available_capacity"] == 35

    def test_decrement_rejects_going_below_zero(self):
        state_store.put_shelter(_shelter(total_capacity=40, available_capacity=3))

        with pytest.raises(state_store.ShelterCapacityExceededError):
            state_store.decrement_shelter_capacity("shelter-1", 5, idempotency_key="place:req-1")

        current = state_store.get_shelter("shelter-1")
        assert current.available_capacity == 3  # unchanged

    def test_increment_rejects_exceeding_total_capacity(self):
        state_store.put_shelter(_shelter(total_capacity=40, available_capacity=38))

        with pytest.raises(state_store.ShelterCapacityOverReleaseError):
            state_store.increment_shelter_capacity("shelter-1", 5, idempotency_key="release:req-1")

        current = state_store.get_shelter("shelter-1")
        assert current.available_capacity == 38  # unchanged

    def test_increment_succeeds_within_bounds(self):
        state_store.put_shelter(_shelter(total_capacity=40, available_capacity=30))

        result = state_store.increment_shelter_capacity("shelter-1", 5, idempotency_key="release:req-1")

        assert result["available_capacity"] == 35


# ---------------------------------------------------------------------------
# Entity CRUD, last-write-wins
# ---------------------------------------------------------------------------


class TestEntityCrud:
    def test_incident_round_trip(self):
        incident = _incident()
        state_store.put_incident(incident)

        fetched = state_store.get_incident("inc-1")

        assert fetched == incident

    def test_incident_last_write_wins(self):
        state_store.put_incident(_incident(severity_band="WATCH"))
        state_store.put_incident(_incident(severity_band="WARNING", updated_at="2026-09-03T11:00:00+05:30"))

        fetched = state_store.get_incident("inc-1")

        assert fetched.severity_band == "WARNING"

    def test_incident_missing_returns_none(self):
        assert state_store.get_incident("does-not-exist") is None

    def test_update_incident_merges_fields(self):
        state_store.put_incident(_incident())

        updated = state_store.update_incident("inc-1", status="CLOSED")

        assert updated.status == "CLOSED"
        assert updated.severity_band == "WATCH"  # untouched fields preserved

    def test_shelter_round_trip(self):
        shelter = _shelter()
        state_store.put_shelter(shelter)

        assert state_store.get_shelter("shelter-1") == shelter

    def test_responder_round_trip(self):
        responder = _responder()
        state_store.put_responder(responder)

        assert state_store.get_responder("resp-1") == responder

    def test_escalation_record_round_trip(self):
        record = _escalation()
        state_store.put_escalation_record(record)

        assert state_store.get_escalation_record("esc-1") == record

    def test_update_escalation_record_merges_fields(self):
        state_store.put_escalation_record(_escalation())

        updated = state_store.update_escalation_record("esc-1", status="RESOLVED", resolved_option_id="1")

        assert updated.status == "RESOLVED"
        assert updated.resolved_option_id == "1"


# ---------------------------------------------------------------------------
# GSI query helpers
# ---------------------------------------------------------------------------


class TestGsiQueries:
    def test_query_incidents_by_severity_orders_desc(self):
        state_store.put_incident(_incident(incident_id="inc-low", severity_band="WATCH", updated_at="2026-09-03T10:00:00+05:30"))
        state_store.put_incident(_incident(incident_id="inc-high", severity_band="EVACUATE", updated_at="2026-09-03T09:00:00+05:30"))

        results = state_store.query_incidents_by_severity()

        assert [i.incident_id for i in results] == ["inc-high", "inc-low"]

    def test_query_available_responders_excludes_assigned(self):
        state_store.put_responder(_responder(responder_id="r1", availability_status="AVAILABLE"))
        state_store.put_responder(_responder(responder_id="r2", availability_status="ASSIGNED"))

        results = state_store.query_available_responders()

        assert [r.responder_id for r in results] == ["r1"]

    def test_query_open_escalations_orders_by_deadline(self):
        state_store.put_escalation_record(_escalation(escalation_id="esc-late", response_deadline="2026-09-03T18:00:00+05:30"))
        state_store.put_escalation_record(_escalation(escalation_id="esc-soon", response_deadline="2026-09-03T12:00:00+05:30"))
        state_store.put_escalation_record(_escalation(escalation_id="esc-resolved", status="RESOLVED", response_deadline="2026-09-03T09:00:00+05:30"))

        results = state_store.query_open_escalations()

        assert [e.escalation_id for e in results] == ["esc-soon", "esc-late"]


# ---------------------------------------------------------------------------
# Processed-trigger-event dedup
# ---------------------------------------------------------------------------


class TestProcessedTriggerEvents:
    def test_first_record_returns_true_and_marks_processed(self):
        assert state_store.has_trigger_event_been_processed("trigger-1") is False

        recorded = state_store.record_trigger_event("trigger-1")

        assert recorded is True
        assert state_store.has_trigger_event_been_processed("trigger-1") is True

    def test_duplicate_record_returns_false(self):
        state_store.record_trigger_event("trigger-1")

        recorded_again = state_store.record_trigger_event("trigger-1")

        assert recorded_again is False


# ---------------------------------------------------------------------------
# Run-progress persistence
# ---------------------------------------------------------------------------


class TestRunProgress:
    def test_persist_and_retrieve_run_progress(self):
        state_store.persist_run_progress("run-1", "monitor", "succeeded", {"band": "WATCH"})
        state_store.persist_run_progress("run-1", "dispatch", "paused", {"interrupt_id": "int-1"})

        progress = state_store.get_run_progress("run-1")

        node_ids = {p["node_id"] for p in progress}
        assert node_ids == {"monitor", "dispatch"}
        paused = next(p for p in progress if p["node_id"] == "dispatch")
        assert paused["status"] == "paused"
        assert paused["payload"] == {"interrupt_id": "int-1"}

    def test_empty_run_returns_empty_list(self):
        assert state_store.get_run_progress("no-such-run") == []


# ---------------------------------------------------------------------------
# 3-failure/30s write-failure escalation hook point
# ---------------------------------------------------------------------------


class TestWriteFailureEscalationHook:
    def test_raises_distinct_exception_after_three_failures(self):
        attempts = {"count": 0}

        def always_fails():
            attempts["count"] += 1
            raise ClientError(
                error_response={"Error": {"Code": "InternalServerError", "Message": "boom"}},
                operation_name="UpdateItem",
            )

        with pytest.raises(state_store.StateWriteFailureError) as exc_info:
            state_store._write_with_retry(always_fails, backoff_seconds=(0.0, 0.0))

        assert attempts["count"] == 3
        assert exc_info.value.attempts == 3

    def test_succeeds_after_transient_failure_then_recovery(self):
        attempts = {"count": 0}

        def fails_once_then_succeeds():
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise ClientError(
                    error_response={"Error": {"Code": "InternalServerError", "Message": "boom"}},
                    operation_name="UpdateItem",
                )
            return "ok"

        result = state_store._write_with_retry(fails_once_then_succeeds, backoff_seconds=(0.0, 0.0))

        assert result == "ok"
        assert attempts["count"] == 2

    def test_conditional_check_failure_is_never_retried(self):
        attempts = {"count": 0}

        def conditional_failure():
            attempts["count"] += 1
            raise ClientError(
                error_response={"Error": {"Code": "ConditionalCheckFailedException", "Message": "nope"}},
                operation_name="UpdateItem",
            )

        with pytest.raises(ClientError) as exc_info:
            state_store._write_with_retry(conditional_failure, backoff_seconds=(0.0, 0.0))

        assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert attempts["count"] == 1  # never retried
