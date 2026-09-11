"""The one AgentCore Runtime entrypoint: ``/invocations`` + ``/ping``, mode-dispatched.

Tasks 17.1 (app + mode dispatch + cold-start validation) and 17.2 (run record,
failure detection, dedupe, sweep skip, rejection records).

Design Question 1 (design.md): **a single AgentCore Runtime**, fronted by one
``bedrock_agentcore.runtime.BedrockAgentCoreApp`` with one ``@app.entrypoint``,
because Req 19.3 is singular — "expose exactly one invocation endpoint and
exactly one health endpoint". The payload's ``mode`` discriminator selects the
work:

===========  ==========================================================
``mode``     Dispatched to
===========  ==========================================================
``sweep``    ``Incident_Graph`` from the ``rule_engine`` entry point
``intake``   ``Incident_Graph`` from the ``intake`` entry point
``resume``   the paused node of an earlier run, replayed with the
             coordinator's recorded decision (Design Question 2)
``chat``     ``Coordinator_Orchestrator``, one ad-hoc question
===========  ==========================================================

``BedrockAgentCoreApp`` registers both required routes itself — ``POST
/invocations`` and ``GET /ping`` (confirmed by reading the installed
``bedrock_agentcore`` package: its ``__init__`` builds
``Route("/invocations", ..., methods=["POST"])`` and ``Route("/ping", ...,
methods=["GET"])``) — so this module adds no route of its own and cannot
accidentally expose a second invocation endpoint. ``/ping`` is answered by the
SDK from in-memory state with no I/O, which is what keeps it inside Req 19.11's
1000ms budget; nothing in this module makes it slower, and in particular the
cold-start validation below never runs on the ping path.

**Nothing ever blocks inside ``/invocations`` waiting on a human** (Design
Question 2). When a node pauses for an escalation, the graph driver persists run
progress, ``Escalation_Service`` has already persisted the ``EscalationRecord``
and fired the out-of-band notification, and this entrypoint returns a
``paused`` response immediately. Resume arrives later as a *separate*
``mode=resume`` invocation.

Cost Model (design.md): ``validate_config()`` and ``validate_policy()`` run
**once per cold start**, before the first model invocation, and reject the run
naming every absent or invalid value (Req 19.9, 10.10). They are cached for the
life of the process (one microVM = one process) so the check costs nothing per
invocation.

Run lifecycle records (task 17.2, Req 1.5-1.10, design.md's Error Handling
table). Every one of these is written to ``Audit_Ledger`` through
``harness.audit.append_audit_entry`` — the same append-only path every tool call
uses — so a coordinator sees run lifecycle events and tool calls in one ordered
trail rather than two:

======================  ==========================================  ========
Record                  Written when                                Req
======================  ==========================================  ========
``entrypoint.run``      a run starts: run id, trigger type, trigger  1.5
                        source id, UTC start timestamp, resolved
                        configuration identifiers
``entrypoint.run``      a run reaches a terminal status              1.5
 (outcome=terminal)
``entrypoint.failure``  the run errored, or exceeded the configured   1.6
                        max run duration; a failure notification is
                        delivered through ``Escalation_Service``
``entrypoint.skip``     a sweep tick arrived while a prior sweep is   1.8
                        still in progress; names the in-progress run
``entrypoint.duplicate``a trigger source id was already recorded;     1.9
                        references the original run's id
``entrypoint.rejection``the payload failed validation; names the      1.10
                        failed check
======================  ==========================================  ========

Alongside the append-only ledger entry, a run *index* item is written to
``State_Store`` (``memory.state_store.put_run_record``) — the queryable copy
Req 1.7/12.8's "20 most recent runs, newest first" list reads through
``surface/queries.py``. See ``memory/state_store.py``'s "Run records" section
for why that index exists and why it needs no new table or index.

Verification note (per this task's instructions, docs/MCP verification was
explicitly skipped; what was verified instead was the **installed package**):
``bedrock_agentcore.runtime.BedrockAgentCoreApp`` was read directly from the
installed distribution. Confirmed: the two routes above; that
``@app.entrypoint`` registers exactly one handler (``self.handlers["main"]``,
so a second decorated function would silently replace the first — there is
exactly one here); that a handler whose **second parameter is literally named
``context``** receives the SDK's ``RequestContext`` (``_takes_context`` checks
``params[1] == "context"``), which is why ``invoke(payload, context=None)``
uses that exact name; that the handler's return value is JSON-serialised
as-is; and that an exception escaping the handler becomes an HTTP 500. This
module therefore never lets an exception escape — every failure path returns a
structured error dict, so a caller (a trigger Lambda) gets the failure *cause*
rather than an opaque 500.

Deviations and inferences recorded explicitly:

1. **Run-failure escalation type.** Req 1.6 requires the failure notification
   to go "to the coordinator through ``Escalation_Service``".
   ``policy/escalation_policy.py``'s ``CATEGORY_TABLE`` has no ``run_failure``
   category (it covers decision categories, not infrastructure ones beyond
   ``config_fault``/``state_write_failure``/``memory_unavailable``), so this
   module raises the notification with ``escalation_type="run_failure"``, which
   renders through ``surface/escalation_service.py``'s ``_generic`` template,
   and with ``default_action="hold"`` — the withhold-by-default posture every
   entry in ``DEFAULT_ACTION`` uses. No threshold is invented here: the
   deadline comes from ``policy.escalation_policy.RESPONSE_DEADLINE_MINUTES``,
   and ``decide()`` is not consulted because a run failure is not an autonomy
   decision — there is no proposed action to gate.
2. **Max run duration is enforced here, in-process.** Req 1.6's 900s ceiling is
   applied with ``asyncio.wait_for`` around the dispatched work, so the failure
   is detected and both the failure record and the notification are written
   inside the same invocation — far inside the 300s notification budget. This
   complements, and does not replace, the graph's own Req 4.4 overall-run
   timeout (default 600s), which halts the graph gracefully; this ceiling is
   the backstop for a run that never returns at all.
3. **Resume replays the paused node only.** design.md §3.2 describes resume as
   "reconstructs the paused node (a fresh Strands ``Agent`` object — cheap, no
   model call, built from the same static config every time)" and replaying
   either the SDK ``interruptResponse`` or the recorded decision into it. That
   is exactly what ``_run_resume`` does. Advancing the *remaining* graph nodes
   after a successful resume needs a driver entry point that starts from an
   arbitrary node with the prior nodes' outputs restored, which
   ``agents/incident_graph.py``'s driver does not expose (its contract is
   entry-point-to-terminal); that continuation is left to the driver rather
   than re-implemented here, and the resume response reports the replayed
   node's outcome so a caller can see what happened.
4. **Sweep readings are gathered here, not inside the graph.**
   ``agents/rule_engine.py``'s declared input contract is
   ``invocation_state["readings"]``/``["now"]``/``["last_known_band"]`` — the
   node deliberately performs no I/O and reads no clock, for determinism
   (Property 1/4). Something outside it must therefore fetch the readings; the
   entrypoint is that something, reading through
   ``integrations.sensor_provider.get_sensor_provider()`` (the one place the
   ``SYNTHETIC_SENSORS`` flag is honoured) and the reach's stored incident for
   ``last_known_band`` (Req 2.9's "no decrease" input).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Final

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agents import config as agent_config
from agents.incident_graph import (
    NODE_INTAKE,
    NODE_RULE_ENGINE,
    build_incident_graph,
    execute_incident_graph,
)
from harness.audit import append_audit_entry
from harness import observability
from memory import state_store
from policy import escalation_policy
from policy.rule_engine_rules import RULE_SET_VERSION, THRESHOLDS
from surface import escalation_service

__all__ = [
    "app",
    "VALID_MODES",
    "MAX_RUN_DURATION_SECONDS",
    "MAX_PAYLOAD_BYTES",
    "SUPPORTED_IMAGE_FORMATS",
    "ConfigurationInvalidError",
    "validate_cold_start",
    "invoke",
]

app = BedrockAgentCoreApp()
"""The single AgentCore Runtime app (Req 19.3): one ``/invocations``, one
``/ping``, both registered by the SDK itself."""


# ---------------------------------------------------------------------------
# Contract constants.
# ---------------------------------------------------------------------------

VALID_MODES: Final[frozenset[str]] = frozenset({"sweep", "intake", "resume", "chat"})
"""The payload ``mode`` discriminator's closed set (Design Question 1)."""

MAX_RUN_DURATION_SECONDS: Final[int] = 900
"""Req 1.6: "a configured maximum run duration of 900 seconds or less"."""

FAILURE_NOTIFICATION_BUDGET_SECONDS: Final[int] = 300
"""Req 1.6: the coordinator must be notified within 300s of detection. The
notification is sent synchronously on the failure path, so the real elapsed
time is orders of magnitude inside this budget; the constant is kept as the
declared budget the failure record is checked against."""

MAX_PAYLOAD_BYTES: Final[int] = 10 * 1024 * 1024
"""Req 1.4/1.10: 10 megabytes."""

SUPPORTED_IMAGE_FORMATS: Final[frozenset[str]] = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp"}
)
"""Req 1.4/1.10: supported hazard-image formats."""

_RIVER_REACH_ENV_VAR: Final[str] = "THUNAI_RIVER_REACH_ID"
_DEFAULT_RIVER_REACH_ID: Final[str] = "reach-7"

#: Private key under which ``_run_graph`` threads the raw ``RunResult`` back to
#: ``invoke`` so the Observability_Layer (task 22) can read the run's Strands
#: ``result.metrics``. Stripped from the response dict before it is returned, so
#: the (non-JSON-serialisable) ``RunResult`` never reaches a caller.
_OBSERVABILITY_RESULT_KEY: Final[str] = "_observability_run_result"

#: Run statuses this module writes to the run index item. ``in_progress`` is
#: the only non-terminal one — Req 1.8's sweep-skip check is exactly "is there
#: a sweep run whose status is not terminal".
_STATUS_IN_PROGRESS: Final[str] = "in_progress"


class ConfigurationInvalidError(RuntimeError):
    """Raised at cold start when configuration or policy validation fails.

    Carries every problem found (Req 19.9's "an error identifying each absent
    value", Req 10.10's policy-defect list) rather than only the first.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "ThunAI configuration is invalid; "
            f"{len(self.problems)} problem(s): " + "; ".join(self.problems)
        )


# ---------------------------------------------------------------------------
# Cold-start validation (Req 19.9, 10.10; design.md Cost Model).
# ---------------------------------------------------------------------------

_cold_start_problems: list[str] | None = None


def validate_cold_start(*, force: bool = False) -> list[str]:
    """Run ``validate_config()`` and ``validate_policy()`` once per process.

    Both are run — never short-circuited on the first failure — so the returned
    list names every absent model id, every out-of-range threshold, and every
    policy defect in one pass (Req 19.9, 10.10).

    Args:
        force: Re-run the checks even if they already ran (test seam).

    Returns:
        The list of problems found. Empty when the configuration is valid.
    """
    global _cold_start_problems
    if _cold_start_problems is not None and not force:
        return _cold_start_problems

    problems: list[str] = []
    try:
        agent_config.validate_config()
    except ValueError as exc:
        problems.append(str(exc))
    # validate_policy returns defects rather than raising (Req 10.10).
    problems.extend(escalation_policy.validate_policy())
    if not RULE_SET_VERSION:
        # Req 2.10 / design.md's Error Handling table: an absent Rule_Set_Version
        # means no band may be computed at all.
        problems.append("policy.rule_engine_rules.RULE_SET_VERSION is absent")

    _cold_start_problems = problems
    return problems


def _resolved_config_ids() -> dict[str, Any]:
    """The "resolved configuration identifiers" Req 1.5 requires on the run record."""
    return {
        "policy_version": escalation_policy.POLICY_VERSION,
        "rule_set_version": RULE_SET_VERSION,
        "low_cost_model_id": agent_config.LOW_COST_MODEL_ID,
        "high_capability_model_id": agent_config.HIGH_CAPABILITY_MODEL_ID,
        "region": os.environ.get("AWS_REGION", ""),
        "synthetic_sensors": os.environ.get("SYNTHETIC_SENSORS", ""),
        "graph_limits": list(agent_config.get_graph_execution_limits()),
    }


# ---------------------------------------------------------------------------
# Payload validation (Req 1.10) — every rejection names the failed check.
# ---------------------------------------------------------------------------


def _payload_size_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _validate_payload(payload: Any) -> dict[str, Any] | None:
    """Return a rejection dict naming the failed check, or ``None`` if valid.

    Checks, in order (Req 1.10's three named categories — missing required
    field, oversized, unsupported format):

    1. the payload is a JSON object;
    2. it is at most 10 MB;
    3. ``mode`` is present and is one of :data:`VALID_MODES`;
    4. the mode's own required fields are present;
    5. an attached image declares a supported format and fits the size limit.
    """
    if not isinstance(payload, dict):
        return {"failed_check": "payload_not_an_object", "detail": f"payload type {type(payload).__name__}"}

    size = _payload_size_bytes(payload)
    if size > MAX_PAYLOAD_BYTES:
        return {
            "failed_check": "payload_too_large",
            "detail": f"{size} bytes exceeds the {MAX_PAYLOAD_BYTES}-byte limit",
        }

    mode = payload.get("mode")
    if not mode:
        return {"failed_check": "missing_required_field", "detail": "mode"}
    if mode not in VALID_MODES:
        return {
            "failed_check": "unsupported_mode",
            "detail": f"{mode!r}; supported modes are {sorted(VALID_MODES)}",
        }

    if mode == "intake":
        if not any(payload.get(k) for k in ("message", "readings", "image")):
            return {
                "failed_check": "missing_required_field",
                "detail": "one of message, readings, image",
            }
        image = payload.get("image")
        if isinstance(image, dict):
            image_format = str(image.get("format", "")).lower().lstrip(".")
            if image_format not in SUPPORTED_IMAGE_FORMATS:
                return {
                    "failed_check": "unsupported_image_format",
                    "detail": f"{image_format or '<absent>'}; supported formats are "
                    f"{sorted(SUPPORTED_IMAGE_FORMATS)}",
                }
            declared_bytes = image.get("size_bytes")
            if declared_bytes is not None and int(declared_bytes) > MAX_PAYLOAD_BYTES:
                return {
                    "failed_check": "image_too_large",
                    "detail": f"{declared_bytes} bytes exceeds the {MAX_PAYLOAD_BYTES}-byte limit",
                }

    if mode == "resume":
        if not payload.get("run_id"):
            return {"failed_check": "missing_required_field", "detail": "run_id"}
        if not (payload.get("interruptResponse") or payload.get("resolved_option_id")):
            return {
                "failed_check": "missing_required_field",
                "detail": "one of interruptResponse, resolved_option_id",
            }

    if mode == "chat" and not (payload.get("prompt") or payload.get("message")):
        return {"failed_check": "missing_required_field", "detail": "one of prompt, message"}

    return None


# ---------------------------------------------------------------------------
# Audit / run-index record writers (Req 1.5, 1.6, 1.8, 1.9, 1.10).
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


def _safe_audit(**kwargs: Any) -> None:
    """Append an audit entry, never letting a ledger failure mask the event
    being recorded.

    Req 16.9's "block the tool call on an audit failure" applies to *tool*
    calls, enforced by ``harness/hooks.py``. These are run *lifecycle* records:
    if the ledger is unavailable, failing the whole run here would replace a
    recoverable "we could not write the run record" with "the run did not
    happen", and would also swallow the very failure record we are trying to
    write. The append error is therefore attached to the response instead.
    """
    try:
        append_audit_entry(**kwargs)
    except Exception as exc:  # noqa: BLE001 - see docstring
        app.logger.error("Audit append failed for %s: %r", kwargs.get("tool_name"), exc)


def _record_run_start(
    *,
    run_id: str,
    mode: str,
    trigger_type: str,
    trigger_source_id: str | None,
    started_at: str,
    incident_id: str | None,
) -> None:
    """Write the Req 1.5 run record: ledger entry + queryable index item.

    A ``resume`` invocation continues an **existing** run rather than starting a
    new one, so it appends its own ledger entry (the append-only trail records
    that the run was resumed) but only *updates* the run index item's status —
    it never overwrites the original run's ``started_at``, trigger metadata, or
    resolved configuration identifiers, which belong to the run's actual start.
    """
    config_ids = _resolved_config_ids()
    resuming = mode == "resume"
    _safe_audit(
        run_id=run_id,
        tool_name="entrypoint.run",
        outcome="resumed_started" if resuming else "started",
        inputs={
            "mode": mode,
            "trigger_type": trigger_type,
            "trigger_source_id": trigger_source_id,
            "started_at": started_at,
            "config_ids": config_ids,
        },
        incident_id=incident_id,
    )
    try:
        if resuming:
            state_store.update_run_record(
                run_id,
                status=_STATUS_IN_PROGRESS,
                terminal_status=None,
                resumed_at=started_at,
            )
        else:
            state_store.put_run_record(
                run_id,
                mode=mode,
                trigger_type=trigger_type,
                trigger_source_id=trigger_source_id,
                started_at=started_at,
                status=_STATUS_IN_PROGRESS,
                terminal_status=None,
                incident_id=incident_id,
                config_ids=config_ids,
            )
    except Exception as exc:  # noqa: BLE001 - the ledger entry above is authoritative
        app.logger.error("Run index write failed for run_id=%s: %r", run_id, exc)


def _record_run_outcome(
    *,
    run_id: str,
    outcome: str,
    started_at_monotonic: float,
    incident_id: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record a run's returned outcome (Req 1.5, 12.8's latency column).

    A ``paused`` run has **not** reached a terminal state — it is waiting on a
    coordinator and will continue on a later ``mode=resume`` invocation — so it
    is recorded with ``status="paused"`` and **no** ``terminal_status``. Two
    consequences, both intended:

    - Req 1.7's run list shows no terminal status for it (there isn't one yet).
    - Req 1.8's sweep-skip check treats it as not-terminal, so a sweep tick
      arriving while a sweep sits paused on a coordinator decision is skipped.
      That is Req 1.8 read literally ("has not reached a terminal state"), and
      it is also the safer behaviour: starting a second sweep over the same
      reach while the first is mid-decision would race two runs on one
      incident.
    """
    latency_ms = int((time.monotonic() - started_at_monotonic) * 1000)
    is_terminal = outcome != "paused"
    _safe_audit(
        run_id=run_id,
        tool_name="entrypoint.run",
        outcome=(f"terminal: {outcome}" if is_terminal else "paused"),
        inputs={"outcome": outcome, "latency_ms": latency_ms, **(extra or {})},
        incident_id=incident_id,
    )
    try:
        state_store.update_run_record(
            run_id,
            status=outcome,
            terminal_status=outcome if is_terminal else None,
            finished_at=_now().isoformat() if is_terminal else None,
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 - the ledger entry above is authoritative
        app.logger.error("Run index update failed for run_id=%s: %r", run_id, exc)


def _record_rejection(payload: Any, rejection: dict[str, Any]) -> dict[str, Any]:
    """Req 1.10: reject without starting a run, record it, name the failed check.

    The rejection record is written under a synthetic ``rejected-*`` run id (no
    run was started, so there is no run id to attribute it to) and carries the
    trigger source id when the payload supplied one, so a rejected payload is
    still traceable to its sender.
    """
    record_id = f"rejected-{uuid.uuid4().hex}"
    trigger_source_id = payload.get("trigger_source_id") if isinstance(payload, dict) else None
    _safe_audit(
        run_id=record_id,
        tool_name="entrypoint.rejection",
        outcome=f"rejected: {rejection['failed_check']}",
        inputs={
            "failed_check": rejection["failed_check"],
            "detail": rejection.get("detail"),
            "trigger_source_id": trigger_source_id,
            "mode": payload.get("mode") if isinstance(payload, dict) else None,
        },
    )
    return {
        "ok": False,
        "run_started": False,
        "reason": "payload_rejected",
        "failed_check": rejection["failed_check"],
        "detail": rejection.get("detail"),
        "record_id": record_id,
    }


def _record_duplicate_suppression(
    *, trigger_source_id: str, original_run_id: str | None, mode: str
) -> dict[str, Any]:
    """Req 1.9: no new run; the record references the original payload's run."""
    record_id = f"duplicate-{uuid.uuid4().hex}"
    _safe_audit(
        run_id=record_id,
        tool_name="entrypoint.duplicate",
        outcome="duplicate_suppressed",
        inputs={
            "trigger_source_id": trigger_source_id,
            "original_run_id": original_run_id,
            "mode": mode,
        },
    )
    return {
        "ok": True,
        "run_started": False,
        "reason": "duplicate_trigger_source_id",
        "trigger_source_id": trigger_source_id,
        "original_run_id": original_run_id,
        "record_id": record_id,
    }


def _record_sweep_skip(
    *, scheduled_time: str, in_progress_run_id: str | None
) -> dict[str, Any]:
    """Req 1.8: skip the tick; the record identifies the skipped scheduled time
    and the in-progress run."""
    record_id = f"skipped-{uuid.uuid4().hex}"
    _safe_audit(
        run_id=record_id,
        tool_name="entrypoint.skip",
        outcome="sweep_skipped",
        inputs={
            "skipped_scheduled_time": scheduled_time,
            "in_progress_run_id": in_progress_run_id,
        },
    )
    return {
        "ok": True,
        "run_started": False,
        "reason": "sweep_already_in_progress",
        "skipped_scheduled_time": scheduled_time,
        "in_progress_run_id": in_progress_run_id,
        "record_id": record_id,
    }


def _record_failure(
    *,
    run_id: str,
    mode: str,
    cause: str,
    incident_id: str | None,
    started_at_monotonic: float,
) -> dict[str, Any]:
    """Req 1.6: write the failure record, then notify the coordinator.

    The notification goes through ``Escalation_Service`` (never a second,
    separate alerting channel — design.md's Error Handling "General pattern"),
    so an infrastructure failure reaches the coordinator on the same one screen
    as a substantive decision.
    """
    detected_at = _now()
    latency_ms = int((time.monotonic() - started_at_monotonic) * 1000)
    _safe_audit(
        run_id=run_id,
        tool_name="entrypoint.failure",
        outcome=f"failed: {cause}",
        inputs={
            "mode": mode,
            "failure_cause": cause,
            "detected_at": detected_at.isoformat(),
            "latency_ms": latency_ms,
        },
        incident_id=incident_id,
    )
    try:
        state_store.update_run_record(
            run_id,
            status="failed",
            terminal_status="failed",
            failure_cause=cause,
            finished_at=detected_at.isoformat(),
            latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Run index update failed for run_id=%s: %r", run_id, exc)

    notified = _notify_run_failure(run_id=run_id, mode=mode, cause=cause, incident_id=incident_id)
    return {
        "ok": False,
        "run_started": True,
        "run_id": run_id,
        "mode": mode,
        "outcome": "failed",
        "failure_cause": cause,
        "failure_notified": notified,
        "notification_budget_seconds": FAILURE_NOTIFICATION_BUDGET_SECONDS,
    }


def _notify_run_failure(
    *, run_id: str, mode: str, cause: str, incident_id: str | None
) -> bool:
    """Deliver the Req 1.6 failure notification through ``Escalation_Service``.

    Uses ``escalation_type="run_failure"`` (rendered by the ``_generic``
    template) and ``default_action="hold"`` — see this module's docstring,
    deviation 1. The deadline is the WARNING-band response deadline from the
    escalation policy, so no deadline value is invented here.
    """
    deadline_minutes = escalation_policy.RESPONSE_DEADLINE_MINUTES["WARNING"].value
    try:
        result = escalation_service.create_escalation(
            # One escalation per failed run, replayed (never duplicated) if this
            # path is retried for the same run (Req 15.2).
            idempotency_key=f"run_failure:{run_id}",
            run_id=run_id,
            escalation_type="run_failure",
            decision_summary=f"ThunAI run {run_id} ({mode}) failed: {cause}"[:200],
            reason=(
                "A triggered run did not reach a terminal state successfully, so I am "
                "telling you rather than retrying silently."
            ),
            stakes=(
                f"Hazard work this run would have done did not complete. Failure cause: {cause}."
            ),
            options=[
                {"option_id": "acknowledge", "label": "Acknowledge"},
                {"option_id": "retrigger", "label": "Trigger another run now"},
            ],
            default_action="hold",
            response_deadline_minutes=deadline_minutes,
            incident_id=incident_id,
            context={"failure_cause": cause, "mode": mode, "failed_run_id": run_id},
        )
        return bool(result.get("notified"))
    except Exception as exc:  # noqa: BLE001 - a failed notification must not mask the failure
        app.logger.error("Failure notification failed for run_id=%s: %r", run_id, exc)
        return False


# ---------------------------------------------------------------------------
# Mode handlers.
# ---------------------------------------------------------------------------


def _river_reach_id(payload: dict[str, Any]) -> str:
    return str(
        payload.get("river_reach_id")
        or os.environ.get(_RIVER_REACH_ENV_VAR)
        or _DEFAULT_RIVER_REACH_ID
    )


def _gather_readings(as_of: datetime) -> dict[str, dict[str, Any]]:
    """Read every configured reading type for the ``rule_engine`` node.

    Shapes each reading as ``{"value", "unit", "ts"}`` — ``agents/rule_engine.py``'s
    declared input contract. A reading the provider cannot supply is simply
    omitted, which that node treats as absent and marks unavailable with a
    reason (Req 2.5) rather than failing the evaluation.
    """
    from integrations.sensor_provider import get_sensor_provider

    provider = get_sensor_provider()
    readings: dict[str, dict[str, Any]] = {}
    for reading_type in THRESHOLDS:
        try:
            latest = provider.get_latest_reading(reading_type, as_of)
        except Exception as exc:  # noqa: BLE001 - Req 2.5/3.6: absence is data, not a crash
            app.logger.warning("Reading %s unavailable: %r", reading_type, exc)
            continue
        if not latest:
            continue
        readings[reading_type] = {
            "value": latest.get("value"),
            "unit": latest.get("unit"),
            "ts": latest.get("timestamp"),
        }
    return readings


def _last_known_band(incident_id: str) -> str:
    """The reach's most recently recorded band, for Req 2.9's total-outage path."""
    try:
        incident = state_store.get_incident(incident_id)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Prior incident read failed for %s: %r", incident_id, exc)
        return "NORMAL"
    return incident.severity_band if incident is not None else "NORMAL"


def _build_incident_ctx(
    payload: dict[str, Any], *, run_id: str, mode: str, incident_id: str, as_of: datetime
) -> dict[str, Any]:
    """Build the ``invocation_state`` threaded to every graph node.

    Carries the run/trigger metadata (``run_id`` is what ``harness/hooks.py``
    keys every per-run cap and every audit entry off — see that module's
    verification note item 4), the ``rule_engine`` node's declared inputs, and
    the mode-specific payload fields the agent nodes read.
    """
    river_reach_id = _river_reach_id(payload)
    ctx: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "trigger_type": payload.get("trigger_type", mode),
        "trigger_source_id": payload.get("trigger_source_id"),
        "river_reach_id": river_reach_id,
        "incident_id": incident_id,
        "as_of": as_of.isoformat(),
        "now": as_of,
        "readings": _gather_readings(as_of),
        "last_known_band": _last_known_band(incident_id),
    }
    for key in ("message", "image", "request_id", "language", "prompt", "readings_override"):
        if payload.get(key) is not None:
            ctx[key] = payload[key]
    # An explicit readings payload (a sensor-reading trigger, Req 1.3) wins over
    # the provider read, so the run evaluates exactly what was delivered.
    if isinstance(payload.get("readings"), dict) and payload["readings"]:
        ctx["readings"] = payload["readings"]
    return ctx


async def _run_graph(payload: dict[str, Any], *, run_id: str, mode: str) -> dict[str, Any]:
    """Run ``Incident_Graph`` start-to-completion-or-pause (Design Question 2)."""
    from tools.incident_tools import incident_id_for_reach

    entry = NODE_RULE_ENGINE if mode == "sweep" else NODE_INTAKE
    incident_id = incident_id_for_reach(_river_reach_id(payload))
    as_of = _now()
    ctx = _build_incident_ctx(payload, run_id=run_id, mode=mode, incident_id=incident_id, as_of=as_of)

    graph = build_incident_graph(entry)
    result = await execute_incident_graph(
        graph, ctx, run_id=run_id, incident_id=incident_id
    )
    return {
        "run_id": run_id,
        "mode": mode,
        "incident_id": incident_id,
        "outcome": result.outcome,
        "execution_order": list(result.execution_order),
        "node_status": dict(result.node_status),
        "paused_node_id": result.paused_node_id,
        "interrupt_id": result.interrupt_id,
        "halt_reason": result.halt_reason,
        "failed_node_id": result.failed_node_id,
        "failure_reason": result.failure_reason,
        # The Observability_Layer (task 22) reads the run's Strands
        # ``result.metrics`` off this ``RunResult``; it is stripped from the
        # response before it leaves ``invoke`` (see ``_OBSERVABILITY_RESULT_KEY``).
        _OBSERVABILITY_RESULT_KEY: result,
    }


async def _run_resume(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Replay the paused node of an earlier run with the recorded decision.

    design.md §3.2's resume path. Two shapes, exactly as
    ``surface/escalation_service.py`` builds them:

    - ``interruptResponse`` present (a ``HumanInTheLoop``-paused node): replay
      ``agent([{"interruptResponse": {"interruptId": ..., "response": ...}}])``
      against a freshly constructed node built from the same static config.
    - ``resolved_option_id`` only (a deterministic-gate pause, no SDK interrupt
      object): invoke the node with the recorded decision added to its persisted
      inputs.

    Missing or unreadable run progress raises a ``resume-integrity`` escalation
    (Req 15.11, design.md's Error Handling table) and returns without replaying
    anything, rather than guessing at the paused node.
    """
    progress = state_store.get_run_progress(run_id)
    paused = next((item for item in progress if item.get("status") == "paused"), None)
    if paused is None:
        return await _resume_integrity_failure(
            run_id=run_id,
            detail="no paused node recorded for this run" if progress else "no run progress recorded",
            payload=payload,
        )

    node_id = str(paused.get("node_id") or (paused.get("payload") or {}).get("node_id") or "")
    persisted = dict((paused.get("payload") or {}).get("inputs") or {})
    incident_id = (paused.get("payload") or {}).get("incident_id") or payload.get("incident_id")
    if not node_id:
        return await _resume_integrity_failure(
            run_id=run_id, detail="paused run progress names no node", payload=payload
        )

    # Reconstruct the paused node from the same static config the pausing run
    # built it from (no model call to construct it — design.md §3.2). Both
    # entry points declare the identical node set, so either build yields the
    # same executor for a given node id; the sweep entry is used for both.
    graph = build_incident_graph(NODE_RULE_ENGINE)
    node = graph.nodes.get(node_id)
    if node is None:
        return await _resume_integrity_failure(
            run_id=run_id, detail=f"node {node_id!r} is not in the declared graph", payload=payload
        )
    executor = node.executor

    ctx: dict[str, Any] = {
        **persisted,
        "run_id": run_id,
        "mode": "resume",
        "incident_id": incident_id,
        "resumed_node_id": node_id,
    }

    interrupt_response = payload.get("interruptResponse")
    if interrupt_response:
        raw = await executor.invoke_async([{"interruptResponse": interrupt_response}], ctx)
    else:
        ctx["resolved_option_id"] = payload.get("resolved_option_id")
        raw = await executor.invoke_async("", ctx)

    status = getattr(raw, "status", None)
    state_store.persist_run_progress(
        run_id,
        node_id,
        "resumed",
        {"node_id": node_id, "resumed_at": _now().isoformat()},
    )
    return {
        "run_id": run_id,
        "mode": "resume",
        "incident_id": incident_id,
        "outcome": "resumed",
        "resumed_node_id": node_id,
        "node_status": str(status) if status is not None else None,
        "escalation_id": payload.get("escalation_id"),
    }


async def _resume_integrity_failure(
    *, run_id: str, detail: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Req 15.11: raise a ``resume-integrity`` escalation and resume nothing."""
    try:
        escalation_service.create_escalation(
            idempotency_key=f"resume_integrity:{run_id}",
            run_id=run_id,
            escalation_type="resume_integrity",
            decision_summary=f"Cannot resume run {run_id}: {detail}"[:200],
            reason=(
                "The persisted progress for a paused run is missing or unreadable, so I will "
                "not guess which step to replay."
            ),
            stakes="A decision you already responded to has not been carried out.",
            options=[
                {"option_id": "acknowledge", "label": "Acknowledge"},
                {"option_id": "retrigger", "label": "Trigger a fresh run"},
            ],
            default_action="hold",
            response_deadline_minutes=escalation_policy.RESPONSE_DEADLINE_MINUTES["WARNING"].value,
            incident_id=payload.get("incident_id"),
            context={"resume_integrity_detail": detail, "run_id": run_id},
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.error("resume-integrity escalation failed for run_id=%s: %r", run_id, exc)
    return {
        "run_id": run_id,
        "mode": "resume",
        "outcome": "resume_integrity_failure",
        "detail": detail,
    }


async def _run_chat(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """One ad-hoc coordinator question through ``Coordinator_Orchestrator``.

    The orchestrator holds no write tool (Req 4.10) — that is structural in
    ``agents/coordinator_orchestrator.py``, not enforced here. No session
    manager is attached: ``memory/memory_store.py`` (task 19.1) owns
    constructing it, and this entrypoint injects it once that module exists.
    """
    from agents.coordinator_orchestrator import build_coordinator_orchestrator

    prompt = str(payload.get("prompt") or payload.get("message") or "")
    agent = build_coordinator_orchestrator()
    result = await agent.invoke_async(prompt, {"run_id": run_id, "mode": "chat"})
    return {
        "run_id": run_id,
        "mode": "chat",
        "outcome": "complete",
        "response": str(result),
        "stop_reason": getattr(result, "stop_reason", None),
    }


async def _dispatch(payload: dict[str, Any], *, run_id: str, mode: str) -> dict[str, Any]:
    """Route one validated payload to its mode handler (Design Question 1)."""
    if mode in ("sweep", "intake"):
        return await _run_graph(payload, run_id=run_id, mode=mode)
    if mode == "resume":
        return await _run_resume(payload, run_id=run_id)
    return await _run_chat(payload, run_id=run_id)


# ---------------------------------------------------------------------------
# The single entrypoint (Req 19.3).
# ---------------------------------------------------------------------------


@app.entrypoint
def invoke(payload: Any, context: Any = None) -> dict[str, Any]:
    """The one ``/invocations`` handler: validate, dedupe, run, record.

    Args:
        payload: The invocation payload. Must be a JSON object carrying a
            ``mode`` of :data:`VALID_MODES`, plus that mode's own fields
            (``trigger_source_id``/``scheduled_time`` for a sweep;
            ``message``/``readings``/``image`` for an intake;
            ``run_id`` + ``interruptResponse``/``resolved_option_id`` for a
            resume; ``prompt`` for a chat).
        context: The SDK-supplied ``RequestContext`` (its ``session_id`` is
            recorded on the run's response for trace correlation). The
            parameter must be named exactly ``context`` for the SDK to pass it.

    Returns:
        A JSON-serialisable dict. Always includes ``ok`` and ``run_started``;
        a started run additionally includes ``run_id``, ``mode``, and the
        handler's own outcome fields. Never raises — every failure path returns
        a structured error naming its cause, so a trigger Lambda sees the
        reason rather than an opaque HTTP 500.
    """
    problems = validate_cold_start()
    if problems:
        # Req 19.9/19.13: reject the run before the first model invocation,
        # naming each absent value; State_Store is left untouched.
        return {
            "ok": False,
            "run_started": False,
            "reason": "configuration_invalid",
            "problems": problems,
        }

    rejection = _validate_payload(payload)
    if rejection is not None:
        return _record_rejection(payload, rejection)

    mode = str(payload["mode"])
    trigger_type = str(payload.get("trigger_type") or mode)
    trigger_source_id = payload.get("trigger_source_id")
    session_id = getattr(context, "session_id", None)

    # Req 1.8: a sweep tick arriving while a prior sweep is still in progress is
    # skipped, and the skip record names the in-progress run.
    if mode == "sweep":
        try:
            in_progress = state_store.query_in_progress_runs("sweep")
        except Exception as exc:  # noqa: BLE001 - a read failure must not block the sweep
            app.logger.warning("In-progress sweep check failed: %r", exc)
            in_progress = []
        if in_progress:
            return _record_sweep_skip(
                scheduled_time=str(payload.get("scheduled_time") or _now().isoformat()),
                in_progress_run_id=in_progress[0].get("run_id"),
            )

    run_id = str(payload.get("resume_run_id") or payload.get("run_id") or "") if mode == "resume" else ""
    if not run_id:
        run_id = _new_run_id()

    # Req 1.9: a trigger source id already recorded starts no new run; the
    # duplicate-suppression record references the original payload's run.
    # `record_trigger_event` is a conditional write, so two racing invocations
    # for the same id cannot both proceed.
    if trigger_source_id and mode in ("sweep", "intake"):
        try:
            existing = state_store.get_trigger_event(str(trigger_source_id))
            if existing is not None:
                return _record_duplicate_suppression(
                    trigger_source_id=str(trigger_source_id),
                    original_run_id=existing.get("run_id"),
                    mode=mode,
                )
            if not state_store.record_trigger_event(str(trigger_source_id), run_id=run_id):
                existing = state_store.get_trigger_event(str(trigger_source_id)) or {}
                return _record_duplicate_suppression(
                    trigger_source_id=str(trigger_source_id),
                    original_run_id=existing.get("run_id"),
                    mode=mode,
                )
        except Exception as exc:  # noqa: BLE001 - dedupe must not silently drop a real trigger
            app.logger.warning("Trigger dedupe check failed for %s: %r", trigger_source_id, exc)

    started_at = _now()
    started_monotonic = time.monotonic()
    incident_id: str | None = payload.get("incident_id")
    if mode in ("sweep", "intake"):
        from tools.incident_tools import incident_id_for_reach

        incident_id = incident_id_for_reach(_river_reach_id(payload))

    _record_run_start(
        run_id=run_id,
        mode=mode,
        trigger_type=trigger_type,
        trigger_source_id=str(trigger_source_id) if trigger_source_id else None,
        started_at=started_at.isoformat(),
        incident_id=incident_id,
    )

    async def _bounded() -> dict[str, Any]:
        # Req 1.6: the configured maximum run duration is a hard ceiling; a run
        # that exceeds it is a detected failure, not an invocation that hangs.
        return await asyncio.wait_for(
            _dispatch(payload, run_id=run_id, mode=mode), timeout=MAX_RUN_DURATION_SECONDS
        )

    # Req 20.1/20.2/20.3/20.5/20.9: wrap the run so the Observability_Layer
    # records per-run metrics, emits one trace, and checks the budget when the
    # run finishes. ``observe_run`` is total — it never raises — so an
    # emission/recording failure inside it cannot change the run's outcome
    # (Req 20.9). On a failure/timeout path no result is set on the observation,
    # so its wrap-up is a no-op and the entrypoint's own failure record stands.
    with observability.observe_run(
        run_id=run_id, session_id=session_id, incident_id=incident_id
    ) as _obs:
        try:
            result = asyncio.run(_bounded())
        except asyncio.TimeoutError:
            return _record_failure(
                run_id=run_id,
                mode=mode,
                cause=f"run exceeded the maximum run duration of {MAX_RUN_DURATION_SECONDS}s",
                incident_id=incident_id,
                started_at_monotonic=started_monotonic,
            )
        except Exception as exc:  # noqa: BLE001 - Req 1.6: any run error is a recorded failure
            app.logger.exception("Run %s (%s) failed", run_id, mode)
            return _record_failure(
                run_id=run_id,
                mode=mode,
                cause=f"{type(exc).__name__}: {exc}",
                incident_id=incident_id,
                started_at_monotonic=started_monotonic,
            )

        outcome = str(result.get("outcome", "complete"))
        result_incident_id = result.get("incident_id") or incident_id
        latency_ms = int((time.monotonic() - started_monotonic) * 1000)

        # Hand the finished run to the observation so metrics are extracted from
        # its Strands ``result.metrics`` and recorded/traced on ``with`` exit.
        run_result_obj = result.pop(_OBSERVABILITY_RESULT_KEY, None)
        if run_result_obj is not None:
            _obs.set_result(
                run_result_obj, latency_ms=latency_ms, incident_id=result_incident_id
            )

        _record_run_outcome(
            run_id=run_id,
            outcome=outcome,
            started_at_monotonic=started_monotonic,
            incident_id=result_incident_id,
            extra={"execution_order": result.get("execution_order")}
            if result.get("execution_order")
            else None,
        )
        return {
            "ok": outcome != "failed",
            "run_started": True,
            "session_id": session_id,
            "trigger_type": trigger_type,
            "trigger_source_id": str(trigger_source_id) if trigger_source_id else None,
            "started_at": started_at.isoformat(),
            **result,
        }


if __name__ == "__main__":  # pragma: no cover - container entry
    # The AgentCore Runtime contract's port (the SDK's own default).
    app.run()
