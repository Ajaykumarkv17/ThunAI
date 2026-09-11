"""Unit tests for tools/alert_tools.py (task 12.5).

Validates: Requirements 7.1, 7.10; Design §3.1 (Alert), §4.4.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from schemas.entities import Shelter
from tools import alert_tools

TABLE_NAME = "thunai-state-alert-tools-test"


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


# ---------------------------------------------------------------------------
# get_channel_limits
# ---------------------------------------------------------------------------


class TestGetChannelLimits:
    def test_returns_default_limits(self, monkeypatch):
        for var in (
            alert_tools.SMS_CHANNEL_CHAR_LIMIT_ENV_VAR,
            alert_tools.EMAIL_CHANNEL_CHAR_LIMIT_ENV_VAR,
            alert_tools.ALERT_RECOMPOSITION_ATTEMPT_COUNT_ENV_VAR,
            "THUNAI_MASS_NOTIFICATION_AUDIENCE_THRESHOLD",
        ):
            monkeypatch.delenv(var, raising=False)

        result = alert_tools.get_channel_limits()

        assert result["channel_character_limits"]["sms"] == alert_tools.DEFAULT_SMS_CHANNEL_CHAR_LIMIT
        assert result["channel_character_limits"]["email"] == alert_tools.DEFAULT_EMAIL_CHANNEL_CHAR_LIMIT
        assert result["recomposition_attempt_count"] == alert_tools.DEFAULT_ALERT_RECOMPOSITION_ATTEMPT_COUNT
        assert result["mass_notification_audience_threshold"] == 100

    def test_respects_env_overrides(self, monkeypatch):
        monkeypatch.setenv(alert_tools.SMS_CHANNEL_CHAR_LIMIT_ENV_VAR, "70")
        monkeypatch.setenv(alert_tools.EMAIL_CHANNEL_CHAR_LIMIT_ENV_VAR, "500")
        monkeypatch.setenv(alert_tools.ALERT_RECOMPOSITION_ATTEMPT_COUNT_ENV_VAR, "3")
        monkeypatch.setenv("THUNAI_MASS_NOTIFICATION_AUDIENCE_THRESHOLD", "50")

        result = alert_tools.get_channel_limits()

        assert result["channel_character_limits"] == {"sms": 70, "email": 500}
        assert result["recomposition_attempt_count"] == 3
        assert result["mass_notification_audience_threshold"] == 50


# ---------------------------------------------------------------------------
# get_shelter_capacity
# ---------------------------------------------------------------------------


class TestGetShelterCapacity:
    def test_reports_capacity_for_named_shelters(self):
        state_store.put_shelter(_shelter(shelter_id="shelter-1", available_capacity=40))
        state_store.put_shelter(_shelter(shelter_id="shelter-2", available_capacity=0))

        result = alert_tools.get_shelter_capacity(["shelter-1", "shelter-2"])

        assert result["ok"] is True
        by_id = {s["shelter_id"]: s for s in result["shelters"]}
        assert by_id["shelter-1"]["available_capacity"] == 40
        assert by_id["shelter-2"]["available_capacity"] == 0
        assert result["any_capacity_available"] is True

    def test_no_capacity_guidance_when_all_full(self):
        state_store.put_shelter(_shelter(shelter_id="shelter-1", available_capacity=0))

        result = alert_tools.get_shelter_capacity(["shelter-1"])

        assert result["any_capacity_available"] is False
        assert result["no_capacity_guidance"] == alert_tools.DEFAULT_NO_CAPACITY_GUIDANCE

    def test_unknown_shelter_id_is_omitted_not_errored(self):
        result = alert_tools.get_shelter_capacity(["does-not-exist"])

        assert result["ok"] is True
        assert result["shelters"] == []
        assert result["any_capacity_available"] is False

    def test_empty_shelter_ids_returns_empty_result(self):
        result = alert_tools.get_shelter_capacity(None)

        assert result["ok"] is True
        assert result["shelters"] == []

    def test_respects_configured_no_capacity_guidance_override(self, monkeypatch):
        monkeypatch.setenv(alert_tools.NO_CAPACITY_GUIDANCE_ENV_VAR, "Go to the nearest police station.")
        state_store.put_shelter(_shelter(available_capacity=0))

        result = alert_tools.get_shelter_capacity(["shelter-1"])

        assert result["no_capacity_guidance"] == "Go to the nearest police station."


# ---------------------------------------------------------------------------
# deliver_alert
# ---------------------------------------------------------------------------


def _mock_provider():
    provider = MagicMock()
    provider.send_sms.side_effect = lambda phone, body, idempotency_key: MagicMock(message_id=f"sns-{phone}")
    provider.send_email.side_effect = lambda addr, subject, body, idempotency_key: MagicMock(
        message_id=f"ses-{addr}"
    )
    return provider


class TestDeliverAlert:
    def test_sends_sms_to_every_recipient(self):
        provider = _mock_provider()
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            result = alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="sms",
                language="en",
                severity_band="WARNING",
                recipients=["+10000000001", "+10000000002"],
                body="River level rising. Move to higher ground.",
            )

        assert result["ok"] is True
        assert result["audience_count"] == 2
        assert result["delivered_count"] == 2
        assert provider.send_sms.call_count == 2
        assert result["already_delivered"] is False

    def test_sends_email_with_subject(self):
        provider = _mock_provider()
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            result = alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="email",
                language="en",
                severity_band="WARNING",
                recipients=["resident@example.org"],
                body="Please move to higher ground.",
                subject="Flood warning",
            )

        assert result["ok"] is True
        provider.send_email.assert_called_once()
        assert provider.send_email.call_args.args[0] == "resident@example.org"

    def test_email_without_subject_is_rejected(self):
        provider = _mock_provider()
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            result = alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="email",
                language="en",
                severity_band="WARNING",
                recipients=["resident@example.org"],
                body="body",
            )

        assert result == {"ok": False, "reason": "empty_subject_for_email"}
        provider.send_email.assert_not_called()

    def test_missing_field_rejected_without_calling_provider(self):
        provider = _mock_provider()
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            result = alert_tools.deliver_alert(
                incident_id="",
                channel="sms",
                language="en",
                severity_band="WARNING",
                recipients=["+10000000001"],
                body="body",
            )

        assert result == {"ok": False, "reason": "missing_idempotency_key"}
        provider.send_sms.assert_not_called()

    def test_retried_call_for_same_key_does_not_resend(self):
        provider = _mock_provider()
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            first = alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="sms",
                language="en",
                severity_band="WARNING",
                recipients=["+10000000001"],
                body="body",
            )
            second = alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="sms",
                language="en",
                severity_band="WARNING",
                recipients=["+10000000001"],
                body="a different body entirely",
            )

        assert provider.send_sms.call_count == 1
        assert first["already_delivered"] is False
        assert second["already_delivered"] is True
        assert second["sent"] == first["sent"]

    def test_different_severity_band_is_a_distinct_delivery(self):
        provider = _mock_provider()
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="sms",
                language="en",
                severity_band="WATCH",
                recipients=["+10000000001"],
                body="body",
            )
            alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="sms",
                language="en",
                severity_band="WARNING",
                recipients=["+10000000001"],
                body="body",
            )

        assert provider.send_sms.call_count == 2

    def test_per_recipient_send_failure_is_recorded_not_raised(self):
        provider = _mock_provider()
        provider.send_sms.side_effect = RuntimeError("sandbox: unverified number")
        with patch("tools.alert_tools.notification_provider", return_value=provider):
            result = alert_tools.deliver_alert(
                incident_id="inc-1",
                channel="sms",
                language="en",
                severity_band="EVACUATE",
                recipients=["+10000000001"],
                body="body",
            )

        assert result["ok"] is True
        assert result["delivered_count"] == 0
        assert result["failed"][0]["recipient"] == "+10000000001"
