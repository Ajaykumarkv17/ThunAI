"""Unit tests for tools/escalation_tools.py (task 12.7).

Validates: Requirements 11.1; Design §3.4, §3.5.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from tools import escalation_tools

TABLE_NAME = "thunai-state-escalation-tools-test"


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


def _valid_kwargs(**overrides) -> dict:
    defaults = dict(
        idempotency_key="escalate:run-1:dispatch_assignment",
        run_id="run-1",
        escalation_type="dispatch_assignment",
        decision_summary="Dispatch Karthik R. to 14 Kanal Street for 5 people.",
        reason="mobility-assistance requests always need sign-off before dispatch.",
        stakes="this occupant cannot self-evacuate; the boat is the only equipped responder.",
        options=[
            {"option_id": "approve", "label": "Approve dispatch"},
            {"option_id": "hold", "label": "Hold"},
        ],
        default_action="hold_assignment",
        response_deadline_minutes=20,
        incident_id="incident-reach-7",
    )
    defaults.update(overrides)
    return defaults


class TestCreateEscalationSuccess:
    def test_persists_open_escalation_record(self):
        result = escalation_tools.create_escalation(**_valid_kwargs())

        assert result["ok"] is True
        assert result["status"] == "OPEN"
        assert result["already_existed"] is False
        assert "escalation_id" in result and result["escalation_id"]

        record = state_store.get_escalation_record(result["escalation_id"])
        assert record is not None
        assert record.status == "OPEN"
        assert record.run_id == "run-1"
        assert record.incident_id == "incident-reach-7"
        assert record.default_action == "hold_assignment"
        assert len(record.options) == 2

    def test_response_deadline_is_future_absolute_timestamp(self):
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        result = escalation_tools.create_escalation(**_valid_kwargs(response_deadline_minutes=20))
        deadline = datetime.fromisoformat(result["response_deadline"])

        assert deadline > before
        assert deadline.tzinfo is not None

    def test_context_is_persisted_for_template_rendering(self):
        result = escalation_tools.create_escalation(
            **_valid_kwargs(context={"responder_name": "Karthik R.", "location": "14 Kanal Street"})
        )
        record = state_store.get_escalation_record(result["escalation_id"])
        assert record.context["responder_name"] == "Karthik R."
        fields = record.template_fields()
        assert fields["responder_name"] == "Karthik R."
        assert fields["reason"] == record.reason


class TestCreateEscalationIdempotency:
    def test_retried_call_with_same_key_replays_first_outcome(self):
        kwargs = _valid_kwargs()
        first = escalation_tools.create_escalation(**kwargs)
        second = escalation_tools.create_escalation(**kwargs)

        assert first["escalation_id"] == second["escalation_id"]
        assert second["already_existed"] is True

        # Exactly one EscalationRecord exists, not two.
        records = state_store.query_open_escalations()
        assert len(records) == 1

    def test_missing_idempotency_key_is_rejected(self):
        result = escalation_tools.create_escalation(**_valid_kwargs(idempotency_key=""))
        assert result == {"ok": False, "reason": "missing_idempotency_key"}
        assert state_store.query_open_escalations() == []


class TestCreateEscalationValidation:
    def test_decision_summary_over_200_chars_is_rejected(self):
        result = escalation_tools.create_escalation(**_valid_kwargs(decision_summary="x" * 201))
        assert result == {"ok": False, "reason": "decision_summary_too_long", "limit": 200}
        assert state_store.query_open_escalations() == []

    @pytest.mark.parametrize(
        "options",
        [
            [{"option_id": "only_one", "label": "Approve"}],
            [{"option_id": f"opt-{i}", "label": f"Option {i}"} for i in range(6)],
        ],
    )
    def test_options_count_outside_2_to_5_is_rejected(self, options):
        result = escalation_tools.create_escalation(**_valid_kwargs(options=options))
        assert result["ok"] is False
        assert result["reason"] == "invalid_options_count"
        assert state_store.query_open_escalations() == []

    def test_duplicate_option_ids_are_rejected(self):
        result = escalation_tools.create_escalation(
            **_valid_kwargs(
                options=[
                    {"option_id": "approve", "label": "Approve"},
                    {"option_id": "approve", "label": "Approve again"},
                ]
            )
        )
        assert result == {"ok": False, "reason": "duplicate_option_ids"}
        assert state_store.query_open_escalations() == []
