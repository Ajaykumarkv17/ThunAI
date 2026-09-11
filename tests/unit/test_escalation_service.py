"""Unit tests for surface/escalation_service.py (task 16.3).

Validates: Requirements 11.1, 11.2, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10,
11.11, 11.12; Design §3.5, Design Question 2.

No live boto3 call is made anywhere in this file: DynamoDB access goes
through a moto-mocked `thunai-state` table (mirroring
tests/unit/test_escalation_tools.py's own fixture exactly), the out-of-band
notification goes through an in-process fake `NotificationProvider`, and the
AgentCore resume invocation goes through an in-process fake
`resume_invoker` — the injectable test seams this module's own docstring
documents.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from integrations.notification_provider import NotificationSendError, SendResult
from memory import state_store
from surface import escalation_service as esvc

TABLE_NAME = "thunai-state-escalation-service-test"
AUDIT_TABLE_NAME = "thunai-audit-escalation-service-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    os.environ["THUNAI_STATE_TABLE"] = TABLE_NAME
    os.environ["THUNAI_AUDIT_TABLE"] = AUDIT_TABLE_NAME
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ["THUNAI_COORDINATOR_PHONE"] = "+14155550100"
    os.environ["THUNAI_COORDINATOR_EMAIL"] = "coordinator@example.org"
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
        client.create_table(
            TableName=AUDIT_TABLE_NAME,
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
                    "IndexName": "gsi1",
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


class FakeNotificationProvider:
    """An in-process fake satisfying `NotificationProvider`'s Protocol."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.sms_calls: list[tuple[str, str, str]] = []
        self.email_calls: list[tuple[str, str, str, str]] = []

    def send_sms(self, phone: str, body: str, idempotency_key: str) -> SendResult:
        self.sms_calls.append((phone, body, idempotency_key))
        if self.fail:
            raise NotificationSendError(
                channel="sms", recipient=phone, idempotency_key=idempotency_key, cause=RuntimeError("boom")
            )
        return SendResult(message_id="fake-sms-1", idempotency_key=idempotency_key)

    def send_email(self, address: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        self.email_calls.append((address, subject, body, idempotency_key))
        if self.fail:
            raise NotificationSendError(
                channel="email", recipient=address, idempotency_key=idempotency_key, cause=RuntimeError("boom")
            )
        return SendResult(message_id="fake-email-1", idempotency_key=idempotency_key)


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


def _fake_resume_invoker(calls: list):
    def _invoke(record, response_value):
        calls.append((record.escalation_id, response_value))
        return {"runtime_session_id": "resume-fake-session", "status_code": 200}

    return _invoke


def _failing_resume_invoker(calls: list):
    def _invoke(record, response_value):
        calls.append((record.escalation_id, response_value))
        raise esvc.ResumeInvocationError(
            escalation_id=record.escalation_id, run_id=record.run_id, cause=RuntimeError("runtime unreachable")
        )

    return _invoke


# ---------------------------------------------------------------------------
# render_template (Req 11.3's structural grounding requirement)
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_generic_template_every_interpolated_value_is_a_persisted_field(self):
        """"no_capacity" (and every other non-design.md-worked-example
        escalation_type) renders through the generic, options-line-based
        template shape -- every interpolated value traces back to a
        persisted field."""
        create_result = esvc.create_escalation(
            **_valid_kwargs(escalation_type="no_capacity"),
            notification_provider_factory=lambda: FakeNotificationProvider(),
        )
        record = state_store.get_escalation_record(create_result["escalation_id"])

        rendered = esvc.render_template(record)

        assert record.decision_summary in rendered["sms"]
        assert record.reason in rendered["sms"]
        assert record.stakes in rendered["sms"]
        for option in record.options:
            assert option.label in rendered["sms"]
        assert record.decision_summary in rendered["email_body"]
        assert record.decision_summary in rendered["email_subject"]

    def test_dispatch_assignment_template_matches_design_wording_and_grounding(self):
        """design.md §3.5's own worked "dispatch_assignment" template:
        numbered options, "I'm asking you first because...", "Stakes:",
        and the "If I hear nothing by ... I will ..." deadline sentence --
        every interpolated value is a persisted field (record.context,
        reason, stakes, response_deadline, default_action)."""
        create_result = esvc.create_escalation(
            **_valid_kwargs(
                escalation_type="dispatch_assignment",
                context={
                    "responder_name": "Karthik R.",
                    "location": "14 Kanal Street",
                    "occupant_count": 5,
                    "category": "RESCUE",
                    "mobility_note": "One occupant needs mobility assistance. ",
                },
            ),
            notification_provider_factory=lambda: FakeNotificationProvider(),
        )
        record = state_store.get_escalation_record(create_result["escalation_id"])

        rendered = esvc.render_template(record)

        assert "Karthik R." in rendered["sms"]
        assert "14 Kanal Street" in rendered["sms"]
        assert "5 people" in rendered["sms"]
        assert "(RESCUE)" in rendered["sms"]
        assert "One occupant needs mobility assistance." in rendered["sms"]
        assert f"I'm asking you first because {record.reason}" in rendered["sms"]
        assert f"Stakes: {record.stakes}" in rendered["sms"]
        assert "Options: [1] Approve dispatch  [2] Choose different responder  [3] Hold" in rendered["sms"]
        assert "If I hear nothing by" in rendered["sms"]
        assert "I will hold the assignment and keep searching for capacity" in rendered["sms"]

    def test_alert_release_template_matches_design_wording_and_grounding(self):
        """design.md §3.5's own worked "alert_release" template."""
        create_result = esvc.create_escalation(
            **_valid_kwargs(
                escalation_type="alert_release",
                default_action="hold",
                context={
                    "severity_band": "EVACUATE",
                    "affected_areas": "Ward-7 South, Riverside Colony",
                    "audience_count": 310,
                },
            ),
            notification_provider_factory=lambda: FakeNotificationProvider(),
        )
        record = state_store.get_escalation_record(create_result["escalation_id"])

        rendered = esvc.render_template(record)

        assert "EVACUATE alert for Ward-7 South, Riverside Colony reaching 310 residents" in rendered["sms"]
        assert f"I'm asking you first because {record.reason}" in rendered["sms"]
        assert f"Stakes: {record.stakes}" in rendered["sms"]
        assert "Options: [1] Send now  [2] Show me the draft  [3] Hold" in rendered["sms"]
        assert "If I hear nothing by" in rendered["sms"]
        assert "I will hold (nothing is sent by default)" in rendered["sms"]

    def test_unknown_escalation_type_falls_back_to_generic_template(self):
        """An escalation_type with no dedicated TEMPLATES entry still
        renders a fully grounded message via the "_generic" fallback --
        notification delivery never breaks on a new/unlisted category."""
        create_result = esvc.create_escalation(
            **_valid_kwargs(escalation_type="some_future_category"),
            notification_provider_factory=lambda: FakeNotificationProvider(),
        )
        record = state_store.get_escalation_record(create_result["escalation_id"])

        rendered = esvc.render_template(record)

        assert record.decision_summary in rendered["sms"]
        assert record.reason in rendered["sms"]
        assert record.stakes in rendered["sms"]

    def test_missing_type_specific_field_renders_blank_not_an_error(self):
        """"dispatch_assignment" with no context (no responder_name/location/
        etc.) must not raise -- a missing type-specific field renders as an
        empty string, never a fabricated value (Req 11.3)."""
        create_result = esvc.create_escalation(
            **_valid_kwargs(escalation_type="dispatch_assignment"),
            notification_provider_factory=lambda: FakeNotificationProvider(),
        )
        record = state_store.get_escalation_record(create_result["escalation_id"])

        rendered = esvc.render_template(record)  # must not raise

        assert f"I'm asking you first because {record.reason}" in rendered["sms"]


# ---------------------------------------------------------------------------
# create_escalation: persists OPEN + notifies (Req 11.1, 11.2)
# ---------------------------------------------------------------------------


class TestCreateEscalation:
    def test_persists_open_and_delivers_notification(self):
        provider = FakeNotificationProvider()
        result = esvc.create_escalation(**_valid_kwargs(), notification_provider_factory=lambda: provider)

        assert result["ok"] is True
        assert result["status"] == "OPEN"
        assert result["notified"] is True
        assert len(provider.sms_calls) == 1
        assert len(provider.email_calls) == 1

        record = state_store.get_escalation_record(result["escalation_id"])
        assert record.notified is True
        assert record.notification_delivery_failed is False

    def test_delivery_failure_does_not_lose_the_persisted_record(self):
        provider = FakeNotificationProvider(fail=True)
        result = esvc.create_escalation(**_valid_kwargs(), notification_provider_factory=lambda: provider)

        assert result["ok"] is True
        assert result["status"] == "OPEN"
        assert result["notified"] is False

        record = state_store.get_escalation_record(result["escalation_id"])
        assert record is not None
        assert record.status == "OPEN"
        assert record.notification_delivery_failed is True
        # 3 attempts within the retry window, each attempting both channels.
        assert len(provider.sms_calls) == esvc._NOTIFY_MAX_ATTEMPTS

    def test_retried_create_call_does_not_re_notify(self):
        provider = FakeNotificationProvider()
        kwargs = _valid_kwargs()

        first = esvc.create_escalation(**kwargs, notification_provider_factory=lambda: provider)
        second = esvc.create_escalation(**kwargs, notification_provider_factory=lambda: provider)

        assert first["escalation_id"] == second["escalation_id"]
        assert second["notified"] is True
        # Only the first call actually sent anything.
        assert len(provider.sms_calls) == 1

    def test_persistence_validation_failure_propagates_unmodified(self):
        result = esvc.create_escalation(**_valid_kwargs(idempotency_key=""))
        assert result == {"ok": False, "reason": "missing_idempotency_key"}


# ---------------------------------------------------------------------------
# resolve_escalation (Req 11.4, 11.5, 11.7, 11.8, 11.9, 11.10, 11.12)
# ---------------------------------------------------------------------------


class TestResolveEscalation:
    def _create(self, **overrides) -> str:
        result = esvc.create_escalation(
            **_valid_kwargs(**overrides), notification_provider_factory=lambda: FakeNotificationProvider()
        )
        assert result["ok"] is True
        return result["escalation_id"]

    def test_valid_option_resolves_and_resumes(self):
        escalation_id = self._create()
        calls: list = []

        result = esvc.resolve_escalation(
            escalation_id, "approve", responding_human_id="coord-1", resume_invoker=_fake_resume_invoker(calls)
        )

        assert result["ok"] is True
        assert result["status"] == "RESOLVED"
        assert result["resolved_option_id"] == "approve"
        assert result["responding_human_id"] == "coord-1"
        assert result["resume"]["ok"] is True
        assert calls == [(escalation_id, "approve")]

        record = state_store.get_escalation_record(escalation_id)
        assert record.status == "RESOLVED"
        assert record.resolved_option_id == "approve"
        assert record.responding_human_id == "coord-1"
        assert record.resolution_timestamp is not None

    def test_invalid_option_is_rejected_and_leaves_record_unchanged(self):
        escalation_id = self._create()

        result = esvc.resolve_escalation(escalation_id, "not-a-real-option")

        assert result == {"ok": False, "reason": "invalid_option", "accepted_option_ids": ["approve", "hold"]}
        record = state_store.get_escalation_record(escalation_id)
        assert record.status == "OPEN"
        assert record.resolved_option_id is None

    def test_unknown_escalation_id_is_rejected(self):
        result = esvc.resolve_escalation("does-not-exist", "approve")
        assert result == {"ok": False, "reason": "escalation_not_found"}

    def test_resolution_is_idempotent_second_call_same_outcome(self):
        escalation_id = self._create()
        calls: list = []

        first = esvc.resolve_escalation(escalation_id, "approve", resume_invoker=_fake_resume_invoker(calls))
        second = esvc.resolve_escalation(escalation_id, "hold", resume_invoker=_fake_resume_invoker(calls))

        # The second call (even with a *different* option) leaves the first
        # call's recorded outcome unchanged (Req 11.7).
        assert second["resolved_option_id"] == first["resolved_option_id"] == "approve"
        assert second["resolution_timestamp"] == first["resolution_timestamp"]
        # Resume was only actually invoked once.
        assert calls == [(escalation_id, "approve")]

    def test_duplicate_submission_after_resolution_reports_already_resolved(self):
        escalation_id = self._create()
        esvc.resolve_escalation(escalation_id, "approve", resume_invoker=_fake_resume_invoker([]))

        # A fresh (non-idempotent-write-cached) resolve_escalation() call for
        # an already-resolved record still reports the recorded resolution
        # rather than erroring or re-resuming.
        result = esvc.resolve_escalation(escalation_id, "hold")

        assert result["ok"] is True
        assert result["already_resolved"] is True
        assert result["status"] == "RESOLVED"
        assert result["resolved_option_id"] == "approve"

    def test_resume_failure_is_recorded_and_resolution_kept(self):
        escalation_id = self._create()
        calls: list = []
        provider = FakeNotificationProvider()

        result = esvc.resolve_escalation(
            escalation_id,
            "approve",
            resume_invoker=_failing_resume_invoker(calls),
            notification_provider_factory=lambda: provider,
        )

        assert result["ok"] is True
        assert result["status"] == "RESOLVED"
        assert result["resume"]["ok"] is False
        assert result["resume"]["reason"] == "resume_failed"

        # The resolution itself is durably kept despite the resume failure.
        record = state_store.get_escalation_record(escalation_id)
        assert record.status == "RESOLVED"
        assert record.resolved_option_id == "approve"

        # The coordinator was notified that the decision was not carried out
        # (best-effort follow-up notification, Req 11.12).
        assert len(provider.sms_calls) >= 1


# ---------------------------------------------------------------------------
# apply_timeout_default / sweep_timed_out_escalations (Req 11.6)
# ---------------------------------------------------------------------------


class TestApplyTimeoutDefault:
    def _create(self, **overrides) -> str:
        result = esvc.create_escalation(
            **_valid_kwargs(**overrides), notification_provider_factory=lambda: FakeNotificationProvider()
        )
        assert result["ok"] is True
        return result["escalation_id"]

    def test_past_deadline_applies_default_action_and_resumes(self):
        escalation_id = self._create(response_deadline_minutes=1)
        calls: list = []
        future = datetime.now(timezone.utc) + timedelta(minutes=5)

        result = esvc.apply_timeout_default(escalation_id, now=future, resume_invoker=_fake_resume_invoker(calls))

        assert result["ok"] is True
        assert result["status"] == "RESOLVED_BY_DEFAULT"
        assert result["default_action"] == "hold_assignment"
        assert result["resume"]["ok"] is True
        assert calls == [(escalation_id, "hold_assignment")]

        record = state_store.get_escalation_record(escalation_id)
        assert record.status == "RESOLVED_BY_DEFAULT"
        assert record.resolved_by_default is True

    def test_deadline_not_yet_passed_is_a_no_op(self):
        escalation_id = self._create(response_deadline_minutes=60)

        result = esvc.apply_timeout_default(escalation_id, now=datetime.now(timezone.utc))

        assert result == {
            "ok": True,
            "reason": "deadline_not_yet_passed",
            "response_deadline": state_store.get_escalation_record(escalation_id).response_deadline,
        }
        record = state_store.get_escalation_record(escalation_id)
        assert record.status == "OPEN"

    def test_already_resolved_record_is_a_no_op(self):
        escalation_id = self._create(response_deadline_minutes=1)
        esvc.resolve_escalation(escalation_id, "approve", resume_invoker=_fake_resume_invoker([]))
        future = datetime.now(timezone.utc) + timedelta(minutes=5)

        result = esvc.apply_timeout_default(escalation_id, now=future)

        assert result == {"ok": True, "already_resolved": True, "status": "RESOLVED"}

    def test_unknown_escalation_id_is_rejected(self):
        result = esvc.apply_timeout_default("does-not-exist")
        assert result == {"ok": False, "reason": "escalation_not_found"}

    def test_sweep_applies_default_to_every_past_deadline_open_escalation(self):
        past_due = self._create(idempotency_key="escalate:run-1:a", response_deadline_minutes=1)
        not_due = self._create(idempotency_key="escalate:run-2:b", run_id="run-2", response_deadline_minutes=60)
        calls: list = []
        future = datetime.now(timezone.utc) + timedelta(minutes=5)

        results = esvc.sweep_timed_out_escalations(now=future, resume_invoker=_fake_resume_invoker(calls))

        assert len(results) == 1
        assert results[0]["escalation_id"] == past_due
        assert state_store.get_escalation_record(past_due).status == "RESOLVED_BY_DEFAULT"
        assert state_store.get_escalation_record(not_due).status == "OPEN"
