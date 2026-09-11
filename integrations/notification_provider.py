"""``Notification_Provider`` — LIVE ONLY, no synthetic backend (design.md §3.7).

Implements the out-of-band SMS/email delivery interface used by
``Escalation_Service`` (Req 11.2) to notify the coordinator when a run pauses
for a human decision. Unlike ``integrations/sensor_provider.py``, this module
declares **no** fake/stub implementation — per the task title ("LIVE ONLY —
no synthetic backend") and design.md §3.7's explicit statement that the
integration seam is "asymmetric on purpose": only ``Sensor_Provider`` has a
synthetic backend, because a real flood cannot be summoned for a demo, while
notifications are real in every environment.

Verification note (mandatory per tasks.md 10.2, "verify first")
-----------------------------------------------------------------
Consulted the AWS documentation MCP server against the installed
``boto3==1.43.90`` before writing this module:

- **SNS SMS send API.** ``boto3.client("sns").publish(PhoneNumber=<E.164>,
  Message=<body>)`` is still the current, documented API for sending a
  single SMS directly to a phone number with no subscription required
  (confirmed via "Sending SMS messages using Amazon SNS" /
  "Publish an Amazon SNS SMS text message using an AWS SDK",
  docs.aws.amazon.com/sns/latest/dg/sms_sending-overview.html). The response
  contains ``MessageId``, used below as ``SendResult.message_id``.
- **SES send API — v1 vs v2.** AWS's own current code-library examples show
  *both* ``boto3.client("ses").send_email(Source=..., Destination=...,
  Message={...})`` (SES v1) and ``boto3.client("sesv2").send_email(
  FromEmailAddress=..., Destination=..., Content={"Simple": {...}})`` (SES
  v2 / "SESv2"). AWS's SESv2 API reference and current example set treat
  SESv2 as the actively-maintained surface for new integrations (the SESv2
  "Coupon Newsletter" workflow example is the featured Python walkthrough at
  docs.aws.amazon.com/code-library/latest/ug/python_3_sesv2_code_examples.html,
  while the v1 ``send_email`` code-library page is the older, unchanged
  reference example). This module therefore uses ``boto3.client("sesv2")``
  and its ``send_email(FromEmailAddress=..., Destination={"ToAddresses":
  [...]}, Content={"Simple": {"Subject": {"Data": ...}, "Body": {"Text":
  {"Data": ...}}}})`` shape — a simple transactional single-recipient send,
  matching this module's needs exactly. ``design.md``'s own §3.7 sketch
  writes ``ses.send_email(...)`` without specifying v1 vs v2; using SESv2
  here is a docs-driven refinement of that sketch, not a contradiction of it
  (the response still carries ``MessageId``, used the same way).
- **Sandbox behaviour (Req 19.10a).** Confirmed from the current docs:
  - *SES*: a new account's SES identity starts in the **sandbox** in every
    region independently. In the sandbox, mail can be sent only to a
    **verified recipient address/domain** (or the SES mailbox simulator),
    capped at 200 messages/24h and 1 message/sec, and the **sender**
    ("From"/"Source") identity must always be verified regardless of sandbox
    status ("Request production access (Moving out of the Amazon SES
    sandbox)", docs.aws.amazon.com/ses/latest/dg/request-production-access.html).
  - *SNS SMS*: a new account's SNS SMS sending starts in its own, distinct
    **SMS sandbox**. In the SMS sandbox, SMS can be sent only to **verified
    destination phone numbers** (verified via
    ``VerifySMSSandboxPhoneNumber`` / the console, an OTP-based flow), with
    every other SNS feature otherwise available
    (docs.aws.amazon.com/sns/latest/api/API_VerifySMSSandboxPhoneNumber.html;
    "Introducing the SMS sandbox for Amazon SNS", AWS Compute Blog). So yes
    — an equivalent sandbox restriction does apply to SNS SMS, structured the
    same way as SES's (verified-recipient-only), but it is a **separate**
    sandbox mechanism from SES's, with its own verification API/console flow
    and no shared identity with SES's verified-identity list.

  Both restrictions match ``.env.example``'s existing comment on
  ``THUNAI_SES_SENDER`` ("SES/SNS start in sandbox mode (verified recipients
  only)") — no deviation from that comment's stated behaviour was found;
  this note only fills in the SNS-specific mechanism name (its own "SMS
  sandbox", not the SES sandbox) that the existing comment did not spell
  out. The one-time verification steps for the coordinator's own phone
  number and email address belong in the README per Req 19.10a; this module
  does not perform that verification itself — it simply issues the
  ``publish``/``send_email`` calls, which AWS will reject with a
  ``ClientError`` naming the unverified identity until that one-time
  verification step described in the AWS docs above is completed by the
  operator, at which point the same code path succeeds unchanged.

Failure-signalling convention
------------------------------
Following the established convention in this codebase (``memory/
audit_ledger.py`` raises distinct exception types rather than returning a
boolean/typed-failure value; ``memory/state_store.py`` does the same for
``ResponderNoLongerAvailableError``/``ShelterCapacityExceededError``/
``StateWriteFailureError``), :class:`SnsSesNotificationProvider` **raises**
:class:`NotificationSendError` on a send failure rather than returning a
typed failure object. A successful send returns a :class:`SendResult`
carrying the provider-assigned message id; there is no "returns False on
failure" code path anywhere in this module, matching the rest of the
codebase's provider-pattern modules.

Idempotency-per-key (Req 11.2 "no re-notify on resume")
----------------------------------------------------------
``Escalation_Service``'s pause/resume cycle (task 16.3, not yet implemented)
may call the notify path again — e.g. after a process restart between the
initial escalation notification and the eventual resume — and a
resident/coordinator must never receive a duplicate SMS/email for the same
escalation. Both ``send_sms``/``send_email`` accept a caller-supplied
``idempotency_key`` (the caller's choice of format, e.g.
``f"escalation:{escalation_id}:sms"``) and never issue a second real
SNS/SES call for a key that already produced a successful send in this
provider instance; the cached :class:`SendResult` is returned again, with
``already_sent=True`` set on the replay so a caller can distinguish a fresh
send from a replay if it wants to (e.g. for logging), without that
distinction affecting the returned ``message_id``.

Why this module does **not** reuse ``memory.state_store.idempotent_write``:
read that module's docstring and signature first (per the task instructions)
before making this choice. ``idempotent_write(key, perform)`` is a good
building block for state *writes that must be durable across processes and
survive 72 hours of retry* — it persists the recorded outcome as an item in
the live ``thunai-state`` DynamoDB table so a *different* process, possibly
long after the first call, can still replay the exact recorded outcome. That
is a heavier, differently-scoped mechanism than what this module needs, for
two concrete reasons:

1. **Layering.** ``integrations/`` is the low-level seam that
   ``tools/alert_tools.py`` / ``surface/escalation_service.py`` sit on top
   of; ``memory/state_store.py`` is itself a higher-level component that
   several of those same callers *also* depend on directly. Making
   ``integrations/notification_provider.py`` import ``memory/state_store.py``
   would mean this leaf integration module reaches back up past the tools
   layer into the state layer, and would make sending a notification depend
   on ``thunai-state`` (and the ``THUNAI_STATE_TABLE`` env var, and a live
   DynamoDB table) being configured and reachable — a coupling this module
   has no other reason to need, and one that would make its unit tests
   require a DynamoDB double (moto/mocked table) just to test an SMS send.
2. **Timing/ownership semantics differ.** ``idempotent_write`` durably
   records the outcome so a *second process* can replay it days later
   (Req 15.2's 72-hour window) — appropriate for state entities that
   `Escalation_Service`/`state_store` already own the durability story for.
   The specific "no re-notify on resume" case this task is scoped to (Req
   11.2) is about a *single provider instance not re-sending within one
   run's process lifetime*; `Escalation_Service`'s own persisted
   `EscalationRecord` (already tracking whether a notification was
   delivered, per Req 11.1/11.2's persisted fields) is the durable,
   cross-process source of truth for "was this escalation already
   notified", and it will pass the same ``idempotency_key`` again on a
   resume specifically so *this* provider's own within-process cache — a
   trivial safety net, not the primary mechanism — also refuses to
   double-send if a resume happens to reuse the very same live process.

   A minimal in-memory, lock-guarded ``dict`` on the provider instance is
   therefore implemented directly in this module rather than reusing
   ``idempotent_write``, documented here as required by the task
   instructions.

Requirements: 11.2, 19.10a. Design: §3.7.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Protocol, runtime_checkable

import boto3
from pydantic import BaseModel

__all__ = [
    "SendResult",
    "NotificationSendError",
    "NotificationProvider",
    "SnsSesNotificationProvider",
    "notification_provider",
]


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------


class SendResult(BaseModel):
    """The outcome of one successful send (or one idempotent replay).

    Attributes:
        message_id: The provider-assigned message id (SNS's or SES's
            ``MessageId``) from the underlying send call that actually
            reached AWS. On a replay (same ``idempotency_key`` seen before),
            this is the id from the *original* send — no second call is made.
        idempotency_key: The idempotency key the send was performed/replayed
            under, echoed back for the caller's convenience.
        already_sent: ``True`` when this result was served from the
            per-instance idempotency cache rather than from a fresh SNS/SES
            call (i.e. this exact ``idempotency_key`` was already sent
            successfully once before by this provider instance).
    """

    message_id: str
    idempotency_key: str
    already_sent: bool = False


class NotificationSendError(RuntimeError):
    """Raised when an SNS or SES send fails.

    Follows the codebase's established provider-pattern convention (see
    module docstring's "Failure-signalling convention") of raising a
    distinct exception type rather than returning a boolean or a typed
    failure value. Wraps the underlying ``botocore`` exception as
    ``__cause__``.
    """

    def __init__(self, *, channel: str, recipient: str, idempotency_key: str, cause: BaseException) -> None:
        self.channel = channel
        self.recipient = recipient
        self.idempotency_key = idempotency_key
        self.cause = cause
        super().__init__(
            f"Notification_Provider failed to send {channel} to {recipient!r} "
            f"(idempotency_key={idempotency_key!r}): {cause!r}"
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class NotificationProvider(Protocol):
    """The ``Notification_Provider`` interface (design.md §3.7).

    Every method returns a :class:`SendResult` on success (idempotently
    replayed for a repeated ``idempotency_key``) and raises
    :class:`NotificationSendError` on failure — there is no other failure
    signal.
    """

    def send_sms(self, phone: str, body: str, idempotency_key: str) -> SendResult:
        """Send one SMS to ``phone`` (E.164 format), idempotent per key."""
        ...

    def send_email(self, address: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        """Send one email to ``address``, idempotent per key."""
        ...


# ---------------------------------------------------------------------------
# The live (and only) implementation
# ---------------------------------------------------------------------------


class SnsSesNotificationProvider:
    """Real Amazon SNS (SMS) + Amazon SES v2 (email). LIVE ONLY.

    Idempotent per ``idempotency_key`` so a resumed run never re-notifies a
    resident/coordinator for the same escalation (see module docstring for
    why this is a within-process cache rather than a reuse of
    ``memory.state_store.idempotent_write``).

    Args:
        region: AWS region for both the SNS and SES clients. Defaults to
            ``AWS_REGION`` (falling back to ``"us-west-2"``, matching every
            other module's default in this codebase).
        sender_email: The verified SES "From" identity. Defaults to
            ``THUNAI_SES_SENDER`` (``.env.example``). Required for
            :meth:`send_email`; :meth:`send_sms` does not need it.
        sns_client: Optional pre-built boto3 SNS client, for tests
            (constructor injection, per the task's testing guidance) — when
            omitted, a real ``boto3.client("sns", region_name=...)`` is
            constructed.
        ses_client: Optional pre-built boto3 SESv2 client, for tests —
            when omitted, a real ``boto3.client("sesv2", region_name=...)``
            is constructed.
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        sender_email: str | None = None,
        sns_client: Any = None,
        ses_client: Any = None,
    ) -> None:
        self._region = region or os.environ.get("AWS_REGION", "us-west-2")
        self._sender_email = sender_email or os.environ.get("THUNAI_SES_SENDER")
        self._sns = sns_client if sns_client is not None else boto3.client("sns", region_name=self._region)
        self._ses = ses_client if ses_client is not None else boto3.client("sesv2", region_name=self._region)
        # Per-instance idempotency cache (see module docstring's rationale
        # for not reusing memory.state_store.idempotent_write here). Guarded
        # by a lock; held for the duration of the underlying send call too,
        # which serialises all sends on this provider instance — an
        # acceptable tradeoff for a low-volume emergency-notification path
        # (documented explicitly rather than left implicit).
        self._sent: dict[str, SendResult] = {}
        self._lock = threading.Lock()

    # -- idempotency dispatcher ------------------------------------------------

    def _send_once(self, idempotency_key: str, do_send: Callable[[], str]) -> SendResult:
        """Return the cached :class:`SendResult` for ``idempotency_key`` if
        one already exists; otherwise call ``do_send()`` (which must return
        the provider-assigned message id or raise), cache, and return the
        fresh result.
        """
        with self._lock:
            cached = self._sent.get(idempotency_key)
            if cached is not None:
                return cached.model_copy(update={"already_sent": True})
            message_id = do_send()
            result = SendResult(message_id=message_id, idempotency_key=idempotency_key, already_sent=False)
            self._sent[idempotency_key] = result
            return result

    # -- SMS ---------------------------------------------------------------

    def send_sms(self, phone: str, body: str, idempotency_key: str) -> SendResult:
        """Send one SMS via ``sns.publish(PhoneNumber=..., Message=...)``.

        Args:
            phone: Destination phone number in E.164 format (e.g.
                ``"+14155550100"``). While the account is in the SNS SMS
                sandbox, this must be a verified destination number (see
                module docstring's verification note).
            body: The SMS message text.
            idempotency_key: Caller-supplied key; a repeat within this
                provider instance's lifetime replays the cached result
                without a second ``publish`` call.

        Returns:
            The :class:`SendResult` (fresh or replayed).

        Raises:
            NotificationSendError: If the underlying ``publish`` call fails.
        """

        def _do_send() -> str:
            try:
                response = self._sns.publish(PhoneNumber=phone, Message=body)
            except Exception as exc:  # noqa: BLE001 - re-raised as NotificationSendError below
                raise NotificationSendError(
                    channel="sms", recipient=phone, idempotency_key=idempotency_key, cause=exc
                ) from exc
            return response["MessageId"]

        return self._send_once(idempotency_key, _do_send)

    # -- Email ---------------------------------------------------------------

    def send_email(self, address: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        """Send one email via SESv2 ``send_email(FromEmailAddress=...,
        Destination={"ToAddresses": [...]}, Content={"Simple": {...}})``.

        Args:
            address: Destination email address. While the account is in the
                SES sandbox, this must be a verified recipient address/domain
                (see module docstring's verification note).
            subject: The email subject line.
            body: The plain-text email body.
            idempotency_key: Caller-supplied key; a repeat within this
                provider instance's lifetime replays the cached result
                without a second ``send_email`` call.

        Returns:
            The :class:`SendResult` (fresh or replayed).

        Raises:
            NotificationSendError: If the underlying ``send_email`` call
                fails, or if no verified sender identity
                (``THUNAI_SES_SENDER``) is configured.
        """
        if not self._sender_email:
            raise NotificationSendError(
                channel="email",
                recipient=address,
                idempotency_key=idempotency_key,
                cause=RuntimeError(
                    "THUNAI_SES_SENDER is not configured; SnsSesNotificationProvider "
                    "requires a verified SES sender identity to send email."
                ),
            )

        def _do_send() -> str:
            try:
                response = self._ses.send_email(
                    FromEmailAddress=self._sender_email,
                    Destination={"ToAddresses": [address]},
                    Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}},
                )
            except Exception as exc:  # noqa: BLE001 - re-raised as NotificationSendError below
                raise NotificationSendError(
                    channel="email", recipient=address, idempotency_key=idempotency_key, cause=exc
                ) from exc
            return response["MessageId"]

        return self._send_once(idempotency_key, _do_send)


# ---------------------------------------------------------------------------
# Resolver (mirrors integrations/sensor_provider.py's provider-function
# pattern; there is no flag here — this interface is always live).
# ---------------------------------------------------------------------------


def notification_provider() -> NotificationProvider:
    """Return the (only, live) ``Notification_Provider`` implementation.

    Unlike ``integrations/sensor_provider.py::sensor_provider()``, there is
    no environment flag to check here — ``Notification_Provider`` has no
    synthetic backend in any environment (design.md §3.7).
    """
    return SnsSesNotificationProvider()
