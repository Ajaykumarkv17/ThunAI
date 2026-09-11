"""``Escalation_Service``: create / resolve / timeout / resume of an
``EscalationRecord`` (Req 11.1, 11.2, 11.4-11.12; design.md §3.5, Design
Question 2).

This module is the caller design.md's own sequence diagram names ``ESvc``:
it sits directly on top of ``tools/escalation_tools.py::create_escalation``
(persistence-only, already implemented — see that module's own docstring
for why the deterministic gate calls that function, not this one, to
*create* a record) and adds everything that module explicitly deferred:
the out-of-band notification (Req 11.2), the human-response resolution path
(Req 11.4, 11.7, 11.9, 11.10), the timeout-default path (Req 11.6), and the
resume invocation back into the AgentCore Runtime (Req 11.5, 11.12).

Verification note (mandatory per tasks.md 16.3, "verify first")
-------------------------------------------------------------------
Consulted the AWS documentation MCP server against the installed
``boto3==1.43.90`` (matching every other already-verified module's pin) for
the current ``bedrock-agentcore`` ``invoke_agent_runtime`` API surface:

- Confirmed parameter names: ``agentRuntimeArn`` (the target runtime),
  ``runtimeSessionId`` (a caller-chosen session id string), ``payload``
  (the request body — a JSON-serialisable blob; this module passes
  ``json.dumps(...)`` exactly as ``.kiro/steering/hackathon_guidelines.md``'s
  own worked example and design.md §3.11's `CfnRuntime`/CLI examples do),
  and ``qualifier`` (the runtime version alias, ``"DEFAULT"`` unless a
  specific version is pinned). No deviation from design.md §3.2's assumed
  ``invoke_agent_runtime(agentRuntimeArn=..., runtimeSessionId=...,
  payload=..., qualifier=...)`` shape.
- Confirmed ``runtimeSessionId`` length constraint: the AWS CLI/SDK example
  above and this repository's own steering doc both independently record
  "must be 33+ characters" as a real, unhelpful error if violated
  (``.kiro/steering/hackathon_guidelines.md``: ``runtimeSessionId="a" * 40,
  # must be 33+ characters``; the AgentCore devguide's Instances example
  literally comments ``# 33+ chars; reuse to keep the session``).
  :func:`_new_runtime_session_id` below always produces a 39-character id
  (a fixed ``"resume-"`` prefix plus a 32-hex-digit ``uuid4().hex``), safely
  clearing that floor.
- Confirmed the resume-invocation payload shape design.md §3.2 assumes:
  a **new** ``runtimeSessionId`` per resume invocation (never the sweep's
  original session id) — design.md's own sequence diagram labels the
  resume arrow ``invoke_agent_runtime(mode=resume, interruptResponse, new
  runtimeSessionId)``, and design.md's own open item 8 explicitly calls out
  "this design deliberately uses a **new** ``runtimeSessionId`` per
  invocation for the resume call (per the verified research's Req 19.4
  distinctness requirement)". This module therefore never reuses or derives
  the resume session id from the pausing run's session id — it generates a
  fresh one on every resume attempt (including a retried resume attempt
  after a failure), matching that explicit design decision.
- ``interruptResponse`` shape (``{"interruptId": ..., "response": ...}``)
  confirmed directly against the installed Strands SDK by Spike 16.1
  (``docs/spikes/16-hitl-findings.md``) — this module does not re-verify
  that shape independently; it reuses the spike's already-confirmed finding
  and the exact literal shape ``.kiro/steering/hackathon_guidelines.md``'s
  own worked interrupt/resume example uses.

No live smoke test of an actual interrupt→resume round trip was performed
by this task, per the explicit scoping note in this task's own
instructions: no `surface/entrypoint.py` (task 17.1) exists yet to receive
a `mode=resume` invocation, and no live `THUNAI_RUNTIME_ARN` is configured
in this development environment, so there is nothing yet to invoke a real
round trip against. `_default_resume_invoker` is written to the confirmed
API shape above; every test in `tests/unit/test_escalation_service.py`
exercises the resolve/timeout/create logic with an injected fake
`resume_invoker` (this module's own, documented test seam — see "Backend
selection" below), never a live or mocked ``boto3`` call, since
``bedrock-agentcore``'s ``invoke_agent_runtime`` is not something ``moto``
supports mocking (unlike DynamoDB) and no fixture-replay mode is documented
for it. The actual cross-process resume round trip is exercised by the
later integration test named in design.md's Traceability Matrix (Req 11.5)
once `surface/entrypoint.py` exists.

Backend selection (Req 19.5's synthetic-backend scoping; this module has
no such flag)
-----------------------------------------------------------------------------
Per design principle 5 (design.md Overview) and `integrations/__init__.py`'s
own docstring, ``SYNTHETIC_SENSORS`` is the **only** environment flag that
selects a synthetic backend anywhere in this codebase, and it affects
``Sensor_Provider`` only. The AgentCore resume invocation is, like
``Notification_Provider``/``Knowledge_Provider``, a **live-only** interface
with no synthetic backend — this module never checks a "use fakes" flag to
decide whether to call a real ``boto3`` client. Instead, exactly like
``integrations/notification_provider.py::SnsSesNotificationProvider``
(constructor-injected ``sns_client``/``ses_client``) and
``agents/incident_graph.py::execute_incident_graph`` (injectable
``node_runner``/``persist_progress``/``append_audit``), every function here
that would otherwise make a live call accepts an optional injectable
override (``resume_invoker``, ``notification_provider_factory``) so a test
can supply an in-process fake without needing a live
``THUNAI_RUNTIME_ARN``/AWS credentials — the established test-seam pattern
for a live-only integration in this codebase, not a new mechanism.

Persistence-vs-notification-vs-resume layering
-----------------------------------------------
- **Persistence** (create/resolve/timeout's *record* mutation) always goes
  through ``memory.state_store`` — this module never calls ``boto3``
  directly for DynamoDB reads/writes, mirroring every other higher-level
  module in this codebase.
- **Idempotent resolution** (Req 11.7, the design's named "Property 2"
  correctness property, applied here to escalation resolution specifically)
  is implemented with ``memory.state_store.idempotent_write`` keyed by
  ``f"resolve_escalation:{escalation_id}"`` (resp.
  ``f"timeout_escalation:{escalation_id}"``) — but only for the *mutating*
  half of the call (flip status, resume). Validation (record exists,
  currently OPEN, submitted option is one of the record's own persisted
  options) happens *before* that idempotent-write call, exactly mirroring
  ``tools/escalation_tools.py::create_escalation``'s own "validate first,
  consume the idempotency key only on success" pattern — this is
  deliberate: an *invalid* option submission (Req 11.10) must never be
  cached as "the" permanent outcome for that escalation, since a
  *different*, later, valid submission for the same still-OPEN escalation
  must still be able to succeed. Only once a resolution has actually
  succeeded does the escalation's own persisted ``status`` field become the
  durable idempotency marker Req 11.7 requires (checked again, fresh, at the
  top of every subsequent call) — the ``idempotent_write`` call additionally
  protects the narrow window where two *concurrent* valid submissions for
  the same still-OPEN record could otherwise both read ``status == "OPEN"``
  before either writes back (a bare read-then-write ``update_escalation_record``
  has no compare-and-swap of its own — see that function's own docstring,
  "not a conditional/atomic primitive"); ``idempotent_write``'s own
  DynamoDB-conditional-put race resolution (see ``memory/state_store.py``'s
  docstring, "Lost the race to record the outcome first...") is reused here
  as the atomicity primitive rather than adding a second, bespoke
  conditional-update helper to ``memory/state_store.py`` for this one case.
- **Resume** is attempted only *after* the resolution has already been
  durably persisted (Req 11.12: "IF resuming ... fails after a resolution is
  recorded, THEN ... leave the recorded resolution unchanged"). A resume
  failure is caught, recorded in ``Audit_Ledger`` with the interrupt
  identifier and a description of the actions not completed, and reported
  back to the caller as a non-fatal ``resume`` sub-result — it never rolls
  back or re-raises past the already-successful resolution write.

``render_template`` note (task 16.4)
-------------------------------------
``TEMPLATES``/:func:`render_template` below are the hand-authored,
per-``escalation_type`` templates design.md §3.5 specifies. Every
interpolated value still comes only from a persisted field — either
``record.decision_summary``/``record.options`` directly, or
``EscalationRecord.template_fields()`` (itself guaranteed, by that method's
own docstring, to expose only persisted values) — never from a model call
(Req 11.3).

``TEMPLATES`` covers every ``escalation_type`` string this codebase's agent
modules actually pass to ``create_escalation`` (surveyed across
``agents/dispatch_agent.py``, ``agents/alert_agent.py``,
``agents/intake_agent.py``, ``agents/monitor_agent.py``, and
``agents/knowledge_agent.py``), plus ``"dispatch_assignment"`` — design.md
§3.5's own worked example key, kept for backward compatibility with the
fixtures ``tests/unit/test_escalation_service.py``,
``tests/unit/test_escalation_tools.py``, and
``tests/unit/test_state_store.py`` already use as their generic example
``escalation_type``. Any ``escalation_type`` string not (yet) given its own
entry falls back to ``TEMPLATES["_generic"]`` — a still-fully-grounded
template built the same way — so a new, not-yet-templated escalation
category never breaks notification delivery (mirroring this module's
existing fail-safe posture elsewhere, e.g. Req 11.2's retry logic).

Two entries (``"dispatch_assignment"``, ``"alert_release"``) reproduce
design.md §3.5's own worked wording verbatim, including its two
type-specific field sets (``responder_name``/``location``/
``occupant_count``/``category``/``mobility_note`` and
``severity_band``/``affected_areas``/``audience_count`` respectively) and
its fixed, literal options text (``"[1] Approve dispatch  [2] Choose
different responder  [3] Hold"`` etc.) — those two options lines are
authored static wording, not a persisted field, so they carry no Req
11.3 obligation of their own; they are still worded to match this
codebase's own actual ``_ESCALATION_OPTIONS`` for those categories where
one exists. Every other entry instead renders its own record's actually
persisted ``options`` as a dynamic numbered list (:func:`_options_line`)
so the options a coordinator sees always match the record's own real
options, never a hardcoded guess.

``_SafeFieldsDict`` makes ``str.format_map`` tolerant of a template
referencing a type-specific field (e.g. ``{responder_name}``) that a given
record's ``context`` happens not to carry (e.g. the alert-safety-review
call site, which raises ``escalation_type="alert_release"`` with a
different ``context`` shape than the audience-threshold call site) —
missing fields render as an empty string rather than raising, since a
notification must never fail to send over a cosmetic gap (Req 11.2). This
never fabricates a value: an absent field is rendered as nothing at all,
not a guessed or model-generated substitute.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from harness.audit import append_audit_entry
from integrations.notification_provider import (
    NotificationProvider,
    NotificationSendError,
    notification_provider as _default_notification_provider,
)
from memory import state_store
from schemas.entities import EscalationRecord
from tools import escalation_tools

__all__ = [
    "ResumeInvocationError",
    "render_template",
    "create_escalation",
    "resolve_escalation",
    "apply_timeout_default",
    "sweep_timed_out_escalations",
]

# ---------------------------------------------------------------------------
# Configuration: where the out-of-band notification goes, and which
# AgentCore Runtime a resume invocation targets. Neither var exists yet in
# .env.example prior to this task; both are added there (below, in the
# "Notifications" section) since Escalation_Service is the first module
# that needs a concrete destination for the SMS/email Notification_Provider
# already sends against (previously only its *sender* identity,
# THUNAI_SES_SENDER, was configured).
# ---------------------------------------------------------------------------

_RUNTIME_ARN_ENV_VAR = "THUNAI_RUNTIME_ARN"
_COORDINATOR_PHONE_ENV_VAR = "THUNAI_COORDINATOR_PHONE"
_COORDINATOR_EMAIL_ENV_VAR = "THUNAI_COORDINATOR_EMAIL"

#: Req 11.11: 3 delivery attempts within 60 seconds of the first attempt.
_NOTIFY_MAX_ATTEMPTS = 3
_NOTIFY_RETRY_WINDOW_SECONDS = 60.0


class ResumeInvocationError(RuntimeError):
    """Raised when the AgentCore Runtime resume invocation fails (Req 11.12).

    Follows this codebase's established provider-pattern convention (see
    ``integrations/notification_provider.py``'s module docstring, "Failure-
    signalling convention") of raising a distinct exception type rather
    than returning a boolean. Callers in this module always catch it,
    record the failure, and continue — it is never allowed to propagate out
    of :func:`resolve_escalation`/:func:`apply_timeout_default`, since a
    resume failure must not unwind the already-persisted resolution
    (Req 11.12).
    """

    def __init__(self, *, escalation_id: str, run_id: str, cause: BaseException) -> None:
        self.escalation_id = escalation_id
        self.run_id = run_id
        self.cause = cause
        super().__init__(
            f"Escalation_Service failed to resume run_id={run_id!r} for "
            f"escalation_id={escalation_id!r}: {cause!r}"
        )


# ---------------------------------------------------------------------------
# TEMPLATES / render_template (Req 11.3, design.md §3.5) — hand-authored,
# per-escalation_type notification wording. See module docstring's
# "render_template note (task 16.4)" for the full rationale.
# ---------------------------------------------------------------------------


class _SafeFieldsDict(dict):
    """A ``dict`` that renders a missing ``str.format_map`` key as ``""``
    instead of raising ``KeyError`` (see module docstring's "render_template
    note"). Never fabricates a value — an absent field is simply blank.
    """

    def __missing__(self, key: str) -> str:  # noqa: D105 - see class docstring
        return ""


def _options_line(record: EscalationRecord) -> str:
    """Render ``record.options`` (a persisted field) as a numbered options
    line, e.g. ``"[1] Approve dispatch  [2] Hold"`` — used by every template
    entry that does not hardcode design.md §3.5's own fixed options text.
    """
    return "  ".join(f"[{index}] {option.label}" for index, option in enumerate(record.options, start=1))


#: Every interpolated field below comes from the persisted ``EscalationRecord``
#: (via ``record.template_fields()``, ``record.decision_summary``, or
#: ``record.options``) — never from a model call (Req 11.3).
TEMPLATES: dict[str, str] = {
    # design.md §3.5's own worked example, reproduced verbatim (kept for
    # backward compatibility with the existing test fixtures' generic
    # example escalation_type).
    "dispatch_assignment": (
        "ThunAI wants to send {responder_name} to {location} for {occupant_count} people "
        "({category}). {mobility_note}"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: [1] Approve dispatch  [2] Choose different responder  [3] Hold\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human} to avoid delay."
    ),
    "alert_release": (
        "ThunAI drafted a {severity_band} alert for {affected_areas} reaching {audience_count} residents.\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: [1] Send now  [2] Show me the draft  [3] Hold\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human} (nothing is sent by default)."
    ),
    # --- Dispatch_Agent categories (agents/dispatch_agent.py) -----------
    "dispatch_non_ambulatory": (
        "ThunAI wants your sign-off before dispatching for {stakes}.\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human} to avoid delay."
    ),
    "no_capacity": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    "shelter_capacity": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    # --- Intake_Agent categories (agents/intake_agent.py) ---------------
    "request_triage": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    "location_clarification": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    "manual_triage": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    # --- Monitor_Agent categories (agents/monitor_agent.py) -------------
    "hazard_monitoring": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    "data_outage": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    # --- Knowledge_Agent categories (agents/knowledge_agent.py) ---------
    "knowledge_no_answer": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    "knowledge_unavailable": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    # --- Shared across agent modules (Req 10.11) -------------------------
    "validation_failure": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
    # --- Fallback for any escalation_type not (yet) given its own entry
    # above -- still fully grounded in the same persisted fields, so a new
    # escalation category never breaks notification delivery.
    "_generic": (
        "{decision_summary}\n"
        "I'm asking you first because {reason}.\n"
        "Stakes: {stakes}\n"
        "Options: {options_line}\n"
        "If I hear nothing by {deadline_local}, I will {default_action_human}."
    ),
}


def render_template(record: EscalationRecord) -> dict[str, str]:
    """Render one escalation's notification content (Req 11.3, design.md §3.5).

    Every interpolated value comes from ``record.decision_summary``,
    ``record.options`` (both persisted fields on ``EscalationRecord``), or
    ``record.template_fields()`` (itself guaranteed, by that method's own
    docstring, to expose only persisted values) — no field here is ever
    generated by a model invocation.

    Args:
        record: The persisted ``EscalationRecord`` to render.

    Returns:
        A dict with keys ``"sms"`` (a single plain-text message suitable for
        an SMS body), ``"email_subject"``, and ``"email_body"`` (the same
        content, for the email channel).
    """
    tmpl = TEMPLATES.get(record.escalation_type, TEMPLATES["_generic"])
    fields = _SafeFieldsDict(
        **record.template_fields(),
        decision_summary=record.decision_summary,
        options_line=_options_line(record),
    )
    body = tmpl.format_map(fields)
    return {
        "sms": body,
        "email_subject": f"ThunAI decision needed: {record.decision_summary}",
        "email_body": body,
    }


# ---------------------------------------------------------------------------
# Audit helper: every state-changing call in this module records exactly
# one Audit_Ledger entry, via the same harness.audit.append_audit_entry
# every other module (agents/incident_graph.py, tools/*) already uses.
# ---------------------------------------------------------------------------


def _audit(
    *,
    run_id: str,
    tool_name: str,
    outcome: str,
    inputs: dict[str, Any],
    incident_id: str | None = None,
    approving_human_id: str | None = None,
) -> None:
    append_audit_entry(
        run_id=run_id,
        tool_name=tool_name,
        outcome=outcome,
        inputs=inputs,
        incident_id=incident_id,
        approving_human_id=approving_human_id,
    )


# ---------------------------------------------------------------------------
# Notification delivery with retry (Req 11.2, 11.11).
# ---------------------------------------------------------------------------


def _send_all_channels(record: EscalationRecord, *, provider: NotificationProvider) -> dict[str, dict[str, Any]]:
    """Attempt every configured out-of-band channel once; never raises.

    Returns a per-channel outcome dict, e.g.
    ``{"sms": {"ok": True, "message_id": "..."}, "email": {"ok": False,
    "error": "..."}}``. When neither ``THUNAI_COORDINATOR_PHONE`` nor
    ``THUNAI_COORDINATOR_EMAIL`` is configured, returns a single
    ``{"none_configured": {"ok": False, "error": "..."}}`` entry — treated
    by :func:`_deliver_with_retry` as a delivery failure like any other.
    """
    fields = render_template(record)
    outcomes: dict[str, dict[str, Any]] = {}

    phone = os.environ.get(_COORDINATOR_PHONE_ENV_VAR, "")
    if phone:
        try:
            result = provider.send_sms(
                phone, fields["sms"], idempotency_key=f"escalation:{record.escalation_id}:sms"
            )
            outcomes["sms"] = {"ok": True, "message_id": result.message_id}
        except NotificationSendError as exc:
            outcomes["sms"] = {"ok": False, "error": str(exc)}

    email = os.environ.get(_COORDINATOR_EMAIL_ENV_VAR, "")
    if email:
        try:
            result = provider.send_email(
                email,
                fields["email_subject"],
                fields["email_body"],
                idempotency_key=f"escalation:{record.escalation_id}:email",
            )
            outcomes["email"] = {"ok": True, "message_id": result.message_id}
        except NotificationSendError as exc:
            outcomes["email"] = {"ok": False, "error": str(exc)}

    if not outcomes:
        outcomes["none_configured"] = {
            "ok": False,
            "error": (
                f"neither {_COORDINATOR_PHONE_ENV_VAR} nor {_COORDINATOR_EMAIL_ENV_VAR} "
                "is configured; nothing to deliver to"
            ),
        }
    return outcomes


def _deliver_with_retry(
    record: EscalationRecord,
    *,
    provider: NotificationProvider,
    max_attempts: int = _NOTIFY_MAX_ATTEMPTS,
    retry_window_seconds: float = _NOTIFY_RETRY_WINDOW_SECONDS,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Attempt delivery up to ``max_attempts`` times within ``retry_window_seconds`` (Req 11.11).

    Succeeds as soon as any single channel attempt succeeds on any attempt
    (Req 11.2 requires *a* notification be delivered, not every channel).
    ``sleep``/``clock`` are injectable so a unit test exercises all 3
    attempts without a real 60-second wait.

    Returns:
        ``(delivered, last_outcomes)`` — ``delivered`` is ``True`` iff at
        least one channel succeeded on some attempt; ``last_outcomes`` is
        the per-channel outcome dict from the final attempt made.
    """
    sleep_fn = sleep or time.sleep
    clock_fn = clock or time.monotonic

    start = clock_fn()
    outcomes: dict[str, dict[str, Any]] = {}
    for attempt in range(1, max_attempts + 1):
        outcomes = _send_all_channels(record, provider=provider)
        if any(outcome.get("ok") for outcome in outcomes.values()):
            return True, outcomes
        if attempt < max_attempts and (clock_fn() - start) < retry_window_seconds:
            sleep_fn(0)
    return False, outcomes


# ---------------------------------------------------------------------------
# Resume invocation (Req 11.5, 11.12) — invoke_agent_runtime(mode=resume, ...).
# ---------------------------------------------------------------------------


def _new_runtime_session_id() -> str:
    """A fresh, 39-character ``runtimeSessionId`` (Req 19.4's per-invocation
    distinctness; the confirmed 33+-character floor — see module docstring's
    verification note)."""
    return f"resume-{uuid4().hex}"


def _default_resume_invoker(record: EscalationRecord, response_value: str) -> dict[str, Any]:
    """Invoke the AgentCore Runtime to resume ``record``'s paused run (Req 11.5).

    Builds the ``mode=resume`` payload in one of the two shapes design.md
    §3.2 distinguishes (see this module's docstring, "Persistence-vs-
    notification-vs-resume layering"):

    - ``record.interrupt_id`` set (a ``HumanInTheLoop``-paused node):
      ``{"mode": "resume", "run_id": ..., "escalation_id": ...,
      "interruptResponse": {"interruptId": ..., "response": response_value}}``
      — the SDK-native interrupt/resume replay shape (Spike 16.1's confirmed
      finding).
    - ``record.interrupt_id`` is ``None`` (a deterministic-gate pause, e.g.
      ``Rule_Engine`` calling ``create_escalation`` directly with no SDK
      interrupt object): ``{"mode": "resume", "run_id": ..., "escalation_id":
      ..., "resolved_option_id": response_value}`` — the entrypoint (task
      17.1, not yet implemented) is expected to look up the persisted run
      progress by ``run_id`` and continue the graph driver with this
      recorded decision instead of replaying an SDK interrupt that never
      existed for this pause.

    Always generates a **new** ``runtimeSessionId`` (Req 19.4) — never the
    pausing run's original session id.

    Raises:
        ResumeInvocationError: If ``THUNAI_RUNTIME_ARN`` is unset, or if the
            underlying ``invoke_agent_runtime`` call fails for any reason.
    """
    arn = os.environ.get(_RUNTIME_ARN_ENV_VAR, "")
    if not arn:
        raise ResumeInvocationError(
            escalation_id=record.escalation_id,
            run_id=record.run_id,
            cause=RuntimeError(f"{_RUNTIME_ARN_ENV_VAR} is not configured"),
        )

    payload: dict[str, Any] = {
        "mode": "resume",
        "run_id": record.run_id,
        "escalation_id": record.escalation_id,
    }
    if record.interrupt_id:
        payload["interruptResponse"] = {"interruptId": record.interrupt_id, "response": response_value}
    else:
        payload["resolved_option_id"] = response_value

    session_id = _new_runtime_session_id()

    try:
        import json

        import boto3

        client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload),
            qualifier="DEFAULT",
        )
    except Exception as exc:  # noqa: BLE001 - re-raised as ResumeInvocationError below
        raise ResumeInvocationError(
            escalation_id=record.escalation_id, run_id=record.run_id, cause=exc
        ) from exc

    return {"runtime_session_id": session_id, "status_code": response.get("statusCode")}


def _attempt_resume(
    record: EscalationRecord,
    response_value: str,
    *,
    resume_invoker: Callable[[EscalationRecord, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attempt to resume ``record``'s paused run; never raises (Req 11.12).

    On failure, records the resume failure in ``Audit_Ledger`` (naming the
    interrupt identifier and that the run's remaining actions were not
    completed) and returns a non-``ok`` result — the caller is responsible
    for notifying the coordinator (Req 11.12's "notify the coordinator that
    the resolved decision was not carried out"), since only the caller
    knows whether this was a resolve or a timeout-default resume.
    """
    invoker = resume_invoker or _default_resume_invoker
    try:
        outcome = invoker(record, response_value)
        return {"ok": True, **outcome}
    except ResumeInvocationError as exc:
        _audit(
            run_id=record.run_id,
            tool_name="escalation_service.resume",
            outcome="resume_failed",
            inputs={
                "escalation_id": record.escalation_id,
                "interrupt_id": record.interrupt_id,
                "actions_not_completed": "the paused run's remaining actions were not carried out",
                "error": str(exc),
            },
            incident_id=record.incident_id,
        )
        return {"ok": False, "reason": "resume_failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# create_escalation (Req 11.1, 11.2, 11.11) — persist, then notify.
# ---------------------------------------------------------------------------


def create_escalation(
    *,
    idempotency_key: str,
    run_id: str,
    escalation_type: str,
    decision_summary: str,
    reason: str,
    stakes: str,
    options: list[dict[str, str]],
    default_action: str,
    response_deadline_minutes: int,
    incident_id: str | None = None,
    interrupt_id: str | None = None,
    context: dict[str, Any] | None = None,
    notification_provider_factory: Callable[[], NotificationProvider] | None = None,
) -> dict[str, Any]:
    """Persist a new ``EscalationRecord`` and deliver its notification (Req 11.1, 11.2).

    Delegates persistence entirely to
    ``tools.escalation_tools.create_escalation`` (Req 11.1's own field-shape
    validation and idempotent-write-by-``idempotency_key`` already live
    there — this function does not re-implement either) and, once a record
    is durably OPEN, attempts delivery to every configured out-of-band
    channel with up to 3 attempts within 60 seconds (Req 11.11).

    Delivery is skipped when the record's own persisted ``notified`` field
    is already ``True`` — this is what makes a retried ``create_escalation``
    call for the same ``idempotency_key`` (e.g. after a process restart)
    never re-notify the coordinator (Req 11.2's "no re-notify on resume";
    see ``integrations/notification_provider.py``'s own docstring, which
    already documents this record field as the durable, cross-process
    source of truth it expects to exist).

    Args:
        idempotency_key, run_id, escalation_type, decision_summary, reason,
            stakes, options, default_action, response_deadline_minutes,
            incident_id, interrupt_id, context: Forwarded verbatim to
            ``tools.escalation_tools.create_escalation`` — see that
            function's own docstring for the full contract of each.
        notification_provider_factory: Optional injectable factory returning
            a ``NotificationProvider``; defaults to
            ``integrations.notification_provider.notification_provider``.
            Test seam only (see module docstring's "Backend selection").

    Returns:
        On a persistence validation failure: the same
        ``{"ok": False, "reason": ...}`` shape
        ``tools.escalation_tools.create_escalation`` returns, unmodified.

        On success: that same success dict
        (``{"ok": True, "escalation_id": ..., "status": "OPEN",
        "response_deadline": ..., "already_existed": bool}``) plus a
        ``"notified": bool`` key indicating whether the out-of-band
        notification was (already, or freshly) delivered, and a
        ``"notification_channels"`` dict with the per-channel delivery
        outcome from the most recent delivery attempt (omitted when
        notification was already delivered by a prior call and this call
        skipped re-sending).
    """
    persisted = escalation_tools.create_escalation(
        idempotency_key=idempotency_key,
        run_id=run_id,
        escalation_type=escalation_type,
        decision_summary=decision_summary,
        reason=reason,
        stakes=stakes,
        options=options,
        default_action=default_action,
        response_deadline_minutes=response_deadline_minutes,
        incident_id=incident_id,
        interrupt_id=interrupt_id,
        context=context,
    )
    if not persisted.get("ok"):
        return persisted

    escalation_id = persisted["escalation_id"]
    record = state_store.get_escalation_record(escalation_id)
    if record is None:  # pragma: no cover - defensive; create_escalation just wrote it
        return {**persisted, "notified": False}

    if record.notified:
        return {**persisted, "notified": True}

    provider = (notification_provider_factory or _default_notification_provider)()
    delivered, outcomes = _deliver_with_retry(record, provider=provider)

    if delivered:
        state_store.update_escalation_record(escalation_id, notified=True, notification_delivery_failed=False)
        _audit(
            run_id=run_id,
            tool_name="escalation_service.notify",
            outcome="delivered",
            inputs={"escalation_id": escalation_id, "channels": outcomes},
            incident_id=incident_id,
        )
    else:
        state_store.update_escalation_record(escalation_id, notification_delivery_failed=True)
        _audit(
            run_id=run_id,
            tool_name="escalation_service.notify",
            outcome="delivery_failed",
            inputs={"escalation_id": escalation_id, "channels": outcomes},
            incident_id=incident_id,
        )

    return {**persisted, "notified": delivered, "notification_channels": outcomes}


# ---------------------------------------------------------------------------
# resolve_escalation (Req 11.4, 11.5, 11.7, 11.8, 11.9, 11.10, 11.12).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedOutcome:
    """The shape returned as the durable ``idempotent_write`` outcome for a
    successful resolution, so a replayed duplicate call returns exactly the
    same dict every time (Req 11.7)."""

    escalation_id: str
    status: str
    resolved_option_id: str | None
    responding_human_id: str | None
    resolution_timestamp: str | None
    resume: dict[str, Any]


def resolve_escalation(
    escalation_id: str,
    option_id: str,
    *,
    responding_human_id: str | None = None,
    resume_invoker: Callable[[EscalationRecord, str], dict[str, Any]] | None = None,
    notification_provider_factory: Callable[[], NotificationProvider] | None = None,
) -> dict[str, Any]:
    """Resolve an OPEN ``EscalationRecord`` with a human-submitted option (Req 11.4).

    Args:
        escalation_id: The escalation to resolve.
        option_id: The submitted option identifier. Must match one of the
            record's own persisted ``EscalationOption.option_id`` values
            (Req 11.10).
        responding_human_id: The identifier of the human who submitted the
            response, recorded in ``Audit_Ledger`` (Req 11.8). ``None`` when
            not supplied by the caller (e.g. an anonymous webhook reply).
        resume_invoker: Optional injectable ``(record, response_value) ->
            dict`` used instead of :func:`_default_resume_invoker`. Test
            seam only.
        notification_provider_factory: Optional injectable
            ``NotificationProvider`` factory, used only on a resume failure
            (to notify the coordinator per Req 11.12). Test seam only.

    Returns:
        ``{"ok": False, "reason": "escalation_not_found"}`` when no such
        escalation exists.

        ``{"ok": False, "reason": "invalid_option",
        "accepted_option_ids": [...]}`` when ``option_id`` does not match
        any of the record's persisted options (Req 11.10) — the
        Escalation_Record's status, deadline, and default action are left
        unchanged, no run is resumed, and the rejected submission is
        recorded in ``Audit_Ledger``.

        ``{"ok": True, "already_resolved": True, "status": ..., ...}`` when
        the record was already ``RESOLVED``/``RESOLVED_BY_DEFAULT`` before
        this call (Req 11.7) — the recorded resolution is returned
        unchanged, no run is resumed, and the duplicate submission is
        recorded in ``Audit_Ledger``. This exact same shape is also what a
        second call for an escalation this function itself just resolved
        returns (the idempotent-write replay path).

        ``{"ok": True, "escalation_id": ..., "status": "RESOLVED",
        "resolved_option_id": ..., "responding_human_id": ...,
        "resolution_timestamp": ..., "resume": {...}}`` on a fresh
        resolution. ``resume`` is ``{"ok": True, ...}`` when the paused run
        was successfully resumed, or ``{"ok": False, "reason":
        "resume_failed", "error": ...}`` when it was not (Req 11.12) — in
        either case the resolution itself (everything outside ``resume``)
        is already durably recorded and is never rolled back.
    """
    record = state_store.get_escalation_record(escalation_id)
    if record is None:
        return {"ok": False, "reason": "escalation_not_found"}

    if record.status != "OPEN":
        _audit(
            run_id=record.run_id,
            tool_name="escalation_service.resolve",
            outcome="duplicate_resolution_submission",
            inputs={"escalation_id": escalation_id, "submitted_option_id": option_id},
            incident_id=record.incident_id,
            approving_human_id=responding_human_id,
        )
        return {
            "ok": True,
            "already_resolved": True,
            "status": record.status,
            "resolved_option_id": record.resolved_option_id,
            "responding_human_id": record.responding_human_id,
            "resolution_timestamp": record.resolution_timestamp,
        }

    accepted_option_ids = sorted(option.option_id for option in record.options)
    if option_id not in accepted_option_ids:
        _audit(
            run_id=record.run_id,
            tool_name="escalation_service.resolve",
            outcome="rejected_invalid_option",
            inputs={
                "escalation_id": escalation_id,
                "submitted_option_id": option_id,
                "accepted_option_ids": accepted_option_ids,
            },
            incident_id=record.incident_id,
            approving_human_id=responding_human_id,
        )
        return {"ok": False, "reason": "invalid_option", "accepted_option_ids": accepted_option_ids}

    def _do_resolve() -> dict[str, Any]:
        # Re-fetch inside the idempotent-write critical section: another
        # concurrent resolve (or a timeout sweep) may have already flipped
        # status away from OPEN between the checks above and this write.
        current = state_store.get_escalation_record(escalation_id)
        if current is None or current.status != "OPEN":  # pragma: no cover - race guard
            target = current or record
            return {
                "ok": True,
                "already_resolved": True,
                "status": target.status,
                "resolved_option_id": target.resolved_option_id,
                "responding_human_id": target.responding_human_id,
                "resolution_timestamp": target.resolution_timestamp,
            }

        resolution_timestamp = datetime.now(timezone.utc).isoformat()
        updated = state_store.update_escalation_record(
            escalation_id,
            status="RESOLVED",
            resolved_option_id=option_id,
            responding_human_id=responding_human_id,
            resolution_timestamp=resolution_timestamp,
        )
        _audit(
            run_id=updated.run_id,
            tool_name="escalation_service.resolve",
            outcome="resolved",
            inputs={"escalation_id": escalation_id, "resolved_option_id": option_id},
            incident_id=updated.incident_id,
            approving_human_id=responding_human_id,
        )

        resume_outcome = _attempt_resume(updated, option_id, resume_invoker=resume_invoker)
        if not resume_outcome.get("ok"):
            _notify_resume_failure(updated, notification_provider_factory=notification_provider_factory)

        return {
            "ok": True,
            "escalation_id": escalation_id,
            "status": "RESOLVED",
            "resolved_option_id": option_id,
            "responding_human_id": responding_human_id,
            "resolution_timestamp": resolution_timestamp,
            "resume": resume_outcome,
        }

    return state_store.idempotent_write(f"resolve_escalation:{escalation_id}", _do_resolve)


# ---------------------------------------------------------------------------
# apply_timeout_default (Req 11.6) and its sweep-friendly wrapper.
# ---------------------------------------------------------------------------


def apply_timeout_default(
    escalation_id: str,
    *,
    now: datetime | None = None,
    resume_invoker: Callable[[EscalationRecord, str], dict[str, Any]] | None = None,
    notification_provider_factory: Callable[[], NotificationProvider] | None = None,
) -> dict[str, Any]:
    """Apply the Escalation_Policy default action to a past-deadline OPEN record (Req 11.6).

    Args:
        escalation_id: The escalation to check/apply.
        now: The current time, for the deadline comparison. Defaults to
            ``datetime.now(timezone.utc)``; injectable so a test can force a
            deadline breach deterministically without sleeping.
        resume_invoker, notification_provider_factory: Same test seams as
            :func:`resolve_escalation`.

    Returns:
        ``{"ok": False, "reason": "escalation_not_found"}`` when no such
        escalation exists.

        ``{"ok": True, "already_resolved": True, "status": ...}`` when the
        record was not (or is no longer) ``OPEN`` — a no-op, matching
        Req 11.7's idempotent-resolution property for the timeout path too.

        ``{"ok": True, "reason": "deadline_not_yet_passed",
        "response_deadline": ...}`` when the record is still ``OPEN`` but
        its ``response_deadline`` has not yet passed — no state change.

        ``{"ok": True, "escalation_id": ..., "status":
        "RESOLVED_BY_DEFAULT", "default_action": ..., "resume": {...}}`` on
        a fresh timeout application: sets status to ``RESOLVED_BY_DEFAULT``,
        records the timeout and applied default action in ``Audit_Ledger``
        (Req 11.6), notifies the coordinator of the applied default action,
        and attempts resume with ``record.default_action`` as the replayed
        response value.
    """
    clock_now = now or datetime.now(timezone.utc)

    record = state_store.get_escalation_record(escalation_id)
    if record is None:
        return {"ok": False, "reason": "escalation_not_found"}

    if record.status != "OPEN":
        return {"ok": True, "already_resolved": True, "status": record.status}

    deadline = datetime.fromisoformat(record.response_deadline)
    if clock_now < deadline:
        return {"ok": True, "reason": "deadline_not_yet_passed", "response_deadline": record.response_deadline}

    def _do_timeout() -> dict[str, Any]:
        current = state_store.get_escalation_record(escalation_id)
        if current is None or current.status != "OPEN":  # pragma: no cover - race guard
            target = current or record
            return {"ok": True, "already_resolved": True, "status": target.status}

        resolution_timestamp = datetime.now(timezone.utc).isoformat()
        updated = state_store.update_escalation_record(
            escalation_id,
            status="RESOLVED_BY_DEFAULT",
            resolved_by_default=True,
            resolution_timestamp=resolution_timestamp,
        )
        _audit(
            run_id=updated.run_id,
            tool_name="escalation_service.timeout",
            outcome="resolved_by_default",
            inputs={"escalation_id": escalation_id, "default_action": updated.default_action},
            incident_id=updated.incident_id,
        )

        resume_outcome = _attempt_resume(updated, updated.default_action, resume_invoker=resume_invoker)
        _notify_default_applied(updated, notification_provider_factory=notification_provider_factory)
        if not resume_outcome.get("ok"):
            _notify_resume_failure(updated, notification_provider_factory=notification_provider_factory)

        return {
            "ok": True,
            "escalation_id": escalation_id,
            "status": "RESOLVED_BY_DEFAULT",
            "default_action": updated.default_action,
            "resolution_timestamp": resolution_timestamp,
            "resume": resume_outcome,
        }

    return state_store.idempotent_write(f"timeout_escalation:{escalation_id}", _do_timeout)


def sweep_timed_out_escalations(
    *,
    now: datetime | None = None,
    resume_invoker: Callable[[EscalationRecord, str], dict[str, Any]] | None = None,
    notification_provider_factory: Callable[[], NotificationProvider] | None = None,
) -> list[dict[str, Any]]:
    """Apply the default action to every currently-OPEN, past-deadline escalation (Req 11.6).

    Intended as the function ``triggers/timeout_sweeper_lambda.py``
    (task 20.3, not yet implemented) calls once per invocation, per
    design.md §5.1's 1-minute EventBridge sweep. Queries every OPEN
    escalation via ``memory.state_store.query_open_escalations`` and calls
    :func:`apply_timeout_default` for each one whose deadline has passed;
    an escalation whose deadline has not yet passed is left untouched (no
    call is made for it, so no audit entry is written for a no-op check).

    Returns:
        A list of :func:`apply_timeout_default`'s result dicts, one per
        past-deadline escalation found, in the order
        ``query_open_escalations`` returns them (soonest deadline first).
        Empty when no OPEN escalation is currently past its deadline.
    """
    clock_now = now or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    for record in state_store.query_open_escalations():
        deadline = datetime.fromisoformat(record.response_deadline)
        if clock_now < deadline:
            continue
        results.append(
            apply_timeout_default(
                record.escalation_id,
                now=clock_now,
                resume_invoker=resume_invoker,
                notification_provider_factory=notification_provider_factory,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Coordinator notifications for the timeout-applied and resume-failure
# cases (Req 11.6's "notify the coordinator of the applied default action";
# Req 11.12's "notify the coordinator that the resolved decision was not
# carried out"). Both are best-effort: a failure to send either of these
# follow-up notifications is not itself retried with Req 11.11's 3-attempt
# protocol (that protocol is reserved for the original escalation
# notification) and is not allowed to raise past this module's public
# functions — the underlying state change (timeout applied / resolution
# recorded) has already succeeded and must not be undone by a notification
# hiccup.
# ---------------------------------------------------------------------------


def _notify_default_applied(
    record: EscalationRecord,
    *,
    notification_provider_factory: Callable[[], NotificationProvider] | None = None,
) -> None:
    provider = (notification_provider_factory or _default_notification_provider)()
    message = (
        f"No response received for: {record.decision_summary}\n"
        f"Default action applied: {record.template_fields()['default_action_human']}."
    )
    _best_effort_notify(record, provider, message, kind="default_applied")


def _notify_resume_failure(
    record: EscalationRecord,
    *,
    notification_provider_factory: Callable[[], NotificationProvider] | None = None,
) -> None:
    provider = (notification_provider_factory or _default_notification_provider)()
    message = (
        f"Your decision was recorded but could not be carried out yet: {record.decision_summary}\n"
        "ThunAI will retry; no further action is needed from you right now."
    )
    _best_effort_notify(record, provider, message, kind="resume_failure")


def _best_effort_notify(record: EscalationRecord, provider: NotificationProvider, message: str, *, kind: str) -> None:
    phone = os.environ.get(_COORDINATOR_PHONE_ENV_VAR, "")
    email = os.environ.get(_COORDINATOR_EMAIL_ENV_VAR, "")
    idempotency_key = f"escalation:{record.escalation_id}:{kind}"
    if phone:
        try:
            provider.send_sms(phone, message, idempotency_key=f"{idempotency_key}:sms")
        except NotificationSendError:
            pass
    if email:
        try:
            provider.send_email(email, f"ThunAI: {kind}", message, idempotency_key=f"{idempotency_key}:email")
        except NotificationSendError:
            pass
