"""Unit tests for integrations/notification_provider.py (task 10.2).

Covers:
- send_sms / send_email call the correct boto3 client methods with the
  correct parameters (SNS `publish`, SESv2 `send_email`), using a
  constructor-injected MagicMock client rather than a real AWS call.
- Idempotency: calling send with the same idempotency key twice results in
  exactly one real underlying send call (the second call is a replay).
- Different idempotency keys each result in their own send call.
"""

from unittest.mock import MagicMock

import pytest

from integrations.notification_provider import (
    NotificationSendError,
    SnsSesNotificationProvider,
)


def _provider(sns_client=None, ses_client=None, sender_email="coordinator@example.org"):
    return SnsSesNotificationProvider(
        region="us-west-2",
        sender_email=sender_email,
        sns_client=sns_client if sns_client is not None else MagicMock(),
        ses_client=ses_client if ses_client is not None else MagicMock(),
    )


def test_send_sms_calls_sns_publish_with_correct_parameters():
    sns_client = MagicMock()
    sns_client.publish.return_value = {"MessageId": "sns-msg-1"}
    provider = _provider(sns_client=sns_client)

    result = provider.send_sms("+14155550100", "River level is rising.", "escalation:esc-1:sms")

    sns_client.publish.assert_called_once_with(
        PhoneNumber="+14155550100", Message="River level is rising."
    )
    assert result.message_id == "sns-msg-1"
    assert result.idempotency_key == "escalation:esc-1:sms"
    assert result.already_sent is False


def test_send_email_calls_sesv2_send_email_with_correct_parameters():
    ses_client = MagicMock()
    ses_client.send_email.return_value = {"MessageId": "ses-msg-1"}
    provider = _provider(ses_client=ses_client)

    result = provider.send_email(
        "resident@example.org", "Flood watch issued", "Please move to higher ground.", "escalation:esc-1:email"
    )

    ses_client.send_email.assert_called_once_with(
        FromEmailAddress="coordinator@example.org",
        Destination={"ToAddresses": ["resident@example.org"]},
        Content={
            "Simple": {
                "Subject": {"Data": "Flood watch issued"},
                "Body": {"Text": {"Data": "Please move to higher ground."}},
            }
        },
    )
    assert result.message_id == "ses-msg-1"
    assert result.already_sent is False


def test_send_email_without_sender_raises_notification_send_error():
    provider = _provider(sender_email=None)

    with pytest.raises(NotificationSendError):
        provider.send_email("resident@example.org", "subject", "body", "escalation:esc-2:email")


def test_send_sms_failure_raises_notification_send_error():
    sns_client = MagicMock()
    sns_client.publish.side_effect = RuntimeError("boom")
    provider = _provider(sns_client=sns_client)

    with pytest.raises(NotificationSendError):
        provider.send_sms("+14155550100", "body", "escalation:esc-3:sms")


def test_send_sms_same_idempotency_key_twice_sends_exactly_once():
    sns_client = MagicMock()
    sns_client.publish.return_value = {"MessageId": "sns-msg-2"}
    provider = _provider(sns_client=sns_client)

    first = provider.send_sms("+14155550100", "body", "escalation:esc-4:sms")
    second = provider.send_sms("+14155550100", "a different body entirely", "escalation:esc-4:sms")

    assert sns_client.publish.call_count == 1
    assert first.already_sent is False
    assert second.already_sent is True
    assert second.message_id == first.message_id == "sns-msg-2"


def test_send_email_same_idempotency_key_twice_sends_exactly_once():
    ses_client = MagicMock()
    ses_client.send_email.return_value = {"MessageId": "ses-msg-2"}
    provider = _provider(ses_client=ses_client)

    first = provider.send_email("resident@example.org", "subject", "body", "escalation:esc-5:email")
    second = provider.send_email("resident@example.org", "subject", "a different body", "escalation:esc-5:email")

    assert ses_client.send_email.call_count == 1
    assert first.already_sent is False
    assert second.already_sent is True
    assert second.message_id == first.message_id == "ses-msg-2"


def test_send_sms_different_idempotency_keys_each_send():
    sns_client = MagicMock()
    sns_client.publish.side_effect = [{"MessageId": "sns-a"}, {"MessageId": "sns-b"}]
    provider = _provider(sns_client=sns_client)

    first = provider.send_sms("+14155550100", "body", "escalation:esc-6:sms")
    second = provider.send_sms("+14155550100", "body", "escalation:esc-7:sms")

    assert sns_client.publish.call_count == 2
    assert first.message_id == "sns-a"
    assert second.message_id == "sns-b"
    assert first.already_sent is False
    assert second.already_sent is False


def test_send_email_different_idempotency_keys_each_send():
    ses_client = MagicMock()
    ses_client.send_email.side_effect = [{"MessageId": "ses-a"}, {"MessageId": "ses-b"}]
    provider = _provider(ses_client=ses_client)

    first = provider.send_email("resident@example.org", "s", "b", "escalation:esc-8:email")
    second = provider.send_email("resident@example.org", "s", "b", "escalation:esc-9:email")

    assert ses_client.send_email.call_count == 2
    assert first.message_id == "ses-a"
    assert second.message_id == "ses-b"
