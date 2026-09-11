"""Unit tests for agents/alert_agent.py (task 13.5).

Exercises `compose_incident_alerts` end-to-end against moto-mocked
`thunai-state`/`thunai-audit` tables, with a stub `compose_variant_fn` in
place of a real model call (this suite never invokes Bedrock).

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.11;
Design §3.1 (Alert).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from agents import alert_agent
from memory import state_store
from schemas.decisions import SafetyReview
from schemas.entities import Shelter
from tools import intake_tools

STATE_TABLE_NAME = "thunai-state-alert-agent-test"
AUDIT_TABLE_NAME = "thunai-audit-alert-agent-test"


def _create_table(client, table_name: str) -> None:
    client.create_table(
        TableName=table_name,
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


@pytest.fixture(autouse=True)
def _dynamo_tables(monkeypatch):
    monkeypatch.setenv("THUNAI_STATE_TABLE", STATE_TABLE_NAME)
    monkeypatch.setenv("THUNAI_AUDIT_TABLE", AUDIT_TABLE_NAME)
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    # Only Tamil + English, matching the design default, regardless of any
    # override left set by another test module.
    monkeypatch.delenv("THUNAI_CONFIGURED_LANGUAGES", raising=False)
    monkeypatch.setattr(intake_tools, "CONFIGURED_LANGUAGES", frozenset({"ta", "en"}))
    monkeypatch.setattr(alert_agent, "CONFIGURED_LANGUAGES", frozenset({"ta", "en"}))
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        _create_table(client, STATE_TABLE_NAME)
        _create_table(client, AUDIT_TABLE_NAME)
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


def _shelter(**overrides) -> Shelter:
    defaults = dict(
        shelter_id="shelter-1",
        name="Ward-7 Community Hall",
        coords=(13.08, 80.27),
        total_capacity=120,
        available_capacity=40,
    )
    defaults.update(overrides)
    return Shelter(**defaults)


def _fake_compose_ok(agent, *, channel, language, char_limit, attempt, **kwargs):
    """A trivially short recommended-action that always fits any channel
    limit used in these tests, on the first attempt."""
    return f"Move ({language[:2]}).", 0.9


def _fake_compose_never_fits(agent, *, channel, language, char_limit, attempt, **kwargs):
    """Recomposition never produces content short enough to fit."""
    return "X" * (char_limit + 500), 0.9


def _fake_compose_fails(agent, *, channel, language, char_limit, attempt, **kwargs):
    return "", 0.0


def _base_kwargs(**overrides) -> dict:
    defaults = dict(
        incident_id="inc-1",
        severity_band="WARNING",
        affected_areas=["Riverside Colony"],
        shelter_ids=["shelter-1"],
        validity_start="2026-09-01T06:00:00+05:30",
        validity_end="2026-09-01T18:00:00+05:30",
        audience_count=10,
        run_id="run-1",
        agent=MagicMock(),
    )
    defaults.update(overrides)
    return defaults


def _passing_review(reviewed_content_id: str, body: str) -> SafetyReview:
    return SafetyReview(
        reviewed_content_id=reviewed_content_id,
        pass_indicator=True,
        policy_version="test-v1",
    )


def _failing_review(reviewed_content_id: str, body: str) -> SafetyReview:
    return SafetyReview(
        reviewed_content_id=reviewed_content_id,
        pass_indicator=False,
        violated_policy_ids=["resident_contact_leak"],
        suggested_revisions=["remove the phone number"],
        policy_version="test-v1",
    )


# ---------------------------------------------------------------------------
# Fan-out across channel x language combinations (Req 7.1, 7.3)
# ---------------------------------------------------------------------------


class TestFanOut:
    def test_one_draft_per_channel_language_combination(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(compose_variant_fn=_fake_compose_ok, audience_count=5)
            )

        assert result["ok"] is True
        combos = {(d["channel"], d["language"]) for d in result["drafts"]}
        assert combos == {("email", "ta"), ("email", "en"), ("sms", "ta"), ("sms", "en")}
        assert result["status"] == "delivered"

    def test_each_variant_labelled_with_its_own_language(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(compose_variant_fn=_fake_compose_ok, audience_count=5)
            )

        for draft in result["drafts"]:
            assert f"({draft['language'][:2]})" in draft["body"]


# ---------------------------------------------------------------------------
# Character-limit recomposition (Req 7.4, 7.9)
# ---------------------------------------------------------------------------


class TestCharacterLimitRecomposition:
    def test_variant_exceeding_limit_after_recomposition_is_withheld_others_continue(self):
        state_store.put_shelter(_shelter())

        def _compose(agent, *, channel, language, char_limit, attempt, **kwargs):
            if channel == "sms":
                return _fake_compose_never_fits(agent, channel=channel, language=language, char_limit=char_limit, attempt=attempt)
            return _fake_compose_ok(agent, channel=channel, language=language, char_limit=char_limit, attempt=attempt)

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(**_base_kwargs(compose_variant_fn=_compose, audience_count=5))

        withheld_channels = {w["channel"] for w in result["withheld"]}
        assert withheld_channels == {"sms"}
        assert all(w["reason"] == "exceeds_channel_limit_after_recomposition_attempts" for w in result["withheld"])
        # The email variants still deliver.
        assert {d["channel"] for d in result["drafts"]} == {"email"}
        assert result["status"] == "delivered"

    def test_composition_failure_is_withheld_with_distinct_reason(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(compose_variant_fn=_fake_compose_fails, audience_count=5)
            )

        assert result["status"] == "no_deliverable_drafts"
        assert all(w["reason"] == "composition_failed" for w in result["withheld"])
        assert len(result["withheld"]) == 4  # 2 channels x 2 languages


# ---------------------------------------------------------------------------
# EVACUATE / mass-audience escalation withholds delivery (Req 7.6, 7.7)
# ---------------------------------------------------------------------------


class TestEscalationWithholdsDelivery:
    def test_evacuate_band_escalates_and_withholds_delivery(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(
                    severity_band="EVACUATE",
                    compose_variant_fn=_fake_compose_ok,
                    audience_count=5,
                )
            )

        assert result["status"] == "escalated"
        assert result["escalation"]["ok"] is True
        assert result["delivery_results"] == []

    def test_mass_audience_escalates_and_withholds_delivery(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(
                    severity_band="WARNING",
                    compose_variant_fn=_fake_compose_ok,
                    audience_count=310,  # > default threshold of 100
                )
            )

        assert result["status"] == "escalated"
        assert result["escalation"]["ok"] is True
        assert result["delivery_results"] == []

    def test_routine_below_threshold_delivers_without_escalation(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(
                    severity_band="WATCH",
                    compose_variant_fn=_fake_compose_ok,
                    audience_count=5,
                )
            )

        assert result["status"] == "delivered"
        assert result["escalation"] is None


# ---------------------------------------------------------------------------
# Failed safety review withholds and escalates that variant (Req 9.2, 7.5)
# ---------------------------------------------------------------------------


class TestSafetyReviewGate:
    def test_failed_review_withholds_variant_and_escalates(self):
        state_store.put_shelter(_shelter())

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_failing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(compose_variant_fn=_fake_compose_ok, audience_count=5)
            )

        assert result["status"] == "no_deliverable_drafts"
        assert all(w["reason"] == "failed_safety_review" for w in result["withheld"])
        assert result["drafts"] == []


# ---------------------------------------------------------------------------
# No-capacity guidance text (Req 7.2)
# ---------------------------------------------------------------------------


class TestNoCapacityGuidance:
    def test_no_shelter_capacity_populates_guidance_text(self):
        state_store.put_shelter(_shelter(available_capacity=0))

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(compose_variant_fn=_fake_compose_ok, audience_count=5)
            )

        assert result["status"] == "delivered"
        for draft in result["drafts"]:
            assert "Shelter:" in draft["body"]
        # pick_nearest_shelter_text falls back to the configured guidance.
        from tools.alert_tools import DEFAULT_NO_CAPACITY_GUIDANCE

        assert any(DEFAULT_NO_CAPACITY_GUIDANCE in d["body"] for d in result["drafts"])

    def test_named_shelter_used_when_capacity_available(self):
        state_store.put_shelter(_shelter(available_capacity=40, name="Ward-7 Community Hall"))

        with patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review):
            result = alert_agent.compose_incident_alerts(
                **_base_kwargs(compose_variant_fn=_fake_compose_ok, audience_count=5)
            )

        assert any("Ward-7 Community Hall" in d["body"] for d in result["drafts"])


# ---------------------------------------------------------------------------
# Idempotent delivery on retry (Req 7.10)
# ---------------------------------------------------------------------------


class TestIdempotentDelivery:
    def test_retried_delivery_for_same_incident_channel_language_band_does_not_resend(self):
        state_store.put_shelter(_shelter())
        provider = MagicMock()
        provider.send_sms.side_effect = lambda phone, body, idempotency_key: MagicMock(message_id=f"sns-{phone}")
        provider.send_email.side_effect = lambda addr, subject, body, idempotency_key: MagicMock(
            message_id=f"ses-{addr}"
        )

        recipients_by_channel = {"sms": ["+10000000001"], "email": ["resident@example.org"]}

        with (
            patch.object(alert_agent, "_submit_for_safety_review", side_effect=_passing_review),
            patch("tools.alert_tools.notification_provider", return_value=provider),
        ):
            first = alert_agent.compose_incident_alerts(
                **_base_kwargs(
                    compose_variant_fn=_fake_compose_ok,
                    audience_count=5,
                    recipients_by_channel=recipients_by_channel,
                )
            )
            second = alert_agent.compose_incident_alerts(
                **_base_kwargs(
                    compose_variant_fn=_fake_compose_ok,
                    audience_count=5,
                    recipients_by_channel=recipients_by_channel,
                )
            )

        assert first["status"] == "delivered"
        assert second["status"] == "delivered"
        # Each (channel, language, severity_band) combination is a distinct
        # idempotency key (Req 7.10) -- 2 configured languages means 2 sms
        # sends and 2 email sends on the FIRST call; the retried SECOND call
        # must perform no additional real sends at all.
        assert provider.send_sms.call_count == 2
        assert provider.send_email.call_count == 2
        second_already_delivered = {
            (r["channel"], r["language"]): r["already_delivered"] for r in second["delivery_results"]
        }
        assert all(second_already_delivered.values())
