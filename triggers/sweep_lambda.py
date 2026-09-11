"""EventBridge Scheduler target: start a hazard sweep run (Req 1.1, 1.8; design.md §5.1).

This is the AWS-native front door for the scheduled hazard sweep. An
EventBridge Scheduler schedule fires this Lambda on a fixed cadence; the
handler builds a ``mode=sweep`` payload and invokes the single AgentCore
Runtime (``surface/entrypoint.py``) through ``bedrock-agentcore``'s
``invoke_agent_runtime`` (boto3), exactly as ``surface/escalation_service.py``
already does for the resume path.

Why the runtime does the work, not this Lambda (design.md §5.1, Architecture
diagram): the autonomous loop — reading sensors, running ``Rule_Engine``, the
specialist agents, the Incident_Graph — lives inside the AgentCore Runtime
behind the one ``/invocations`` endpoint (Req 19.3). A trigger Lambda's only
job is to translate an AWS event into that one invocation and record what it
decided to do; it holds no agent logic of its own.

In-progress-sweep skip (Req 1.8)
--------------------------------
Req 1.8: "IF a previous hazard sweep run has not reached a terminal state when
a scheduled hazard sweep tick fires, THEN THE ThunAI_Platform SHALL skip the
new run and SHALL record a skip entry identifying the skipped scheduled time
and the in-progress run identifier."

The skip is enforced here, at the trigger boundary, *before* spending an
``invoke_agent_runtime`` call, by reading
``memory.state_store.query_in_progress_runs("sweep")`` — the same non-terminal
run query ``surface/entrypoint.py`` uses for its own defence-in-depth copy of
this check. When an in-progress sweep is found, this handler writes a skip
record to ``Audit_Ledger`` (via ``harness.audit.append_audit_entry``) naming
the skipped scheduled time and the in-progress run identifier, and returns
without invoking the runtime. The entrypoint keeps its own copy of the same
check so that a race (two ticks, or a manual invocation) that slips past this
Lambda's read is still caught server-side; the two are complementary, not
redundant — this one avoids the wasted runtime invocation in the common case.

Verification note: docs/MCP verification was explicitly skipped for this task
per the task note ("do not refer any docs"). The ``invoke_agent_runtime``
call shape (``agentRuntimeArn``, ``runtimeSessionId`` [33+ chars],
``payload=json.dumps(...)``, ``qualifier="DEFAULT"``) is reused verbatim from
``surface/escalation_service.py::_default_resume_invoker``, which was itself
verified against the installed ``boto3==1.43.90`` when that module was written.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from harness.audit import append_audit_entry
from memory import state_store

__all__ = ["handler", "SWEEP_TRIGGER_TYPE"]

#: The ``trigger_type`` (and non-terminal-run filter) a sweep run is recorded
#: under, matching ``surface/entrypoint.py``'s own sweep-skip query.
SWEEP_TRIGGER_TYPE = "sweep"

_RUNTIME_ARN_ENV_VAR = "THUNAI_RUNTIME_ARN"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    """A fresh 39-character ``runtimeSessionId`` (the AgentCore 33+-char floor,
    reused from ``surface/escalation_service.py::_new_runtime_session_id``)."""
    return f"sweep-{uuid.uuid4().hex}"


def _scheduled_time(event: dict[str, Any]) -> str:
    """The tick's scheduled time, for the skip record (Req 1.8).

    An EventBridge Scheduler event carries the schedule fire time as ``time``
    (the top-level event envelope field); a ``<aws.scheduler.scheduled-time>``
    template value may also be injected into the event payload. Falls back to
    the current time when the trigger delivered neither.
    """
    return str(
        event.get("scheduled_time")
        or event.get("time")
        or (event.get("detail") or {}).get("scheduled_time")
        or _now_iso()
    )


def _trigger_source_id(event: dict[str, Any], scheduled_time: str) -> str:
    """A stable identifier for this scheduled tick (Req 1.9 dedupe key).

    A Scheduler tick has no natural payload id, so the scheduled fire time is
    the dedupe identity — two deliveries of the same tick share it, a later
    tick does not. Prefixed so it never collides with an ingestion trigger id.
    """
    return str(event.get("trigger_source_id") or f"sweep-tick:{scheduled_time}")


def _invoke_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke the AgentCore Runtime with ``payload`` (mode=sweep).

    Raises:
        RuntimeError: If ``THUNAI_RUNTIME_ARN`` is not configured.
    """
    arn = os.environ.get(_RUNTIME_ARN_ENV_VAR, "")
    if not arn:
        raise RuntimeError(f"{_RUNTIME_ARN_ENV_VAR} is not configured")

    client = boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    session_id = _new_session_id()
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload),
        qualifier="DEFAULT",
    )
    return {"runtime_session_id": session_id, "status_code": response.get("statusCode")}


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """EventBridge Scheduler target entry point (Req 1.1, 1.8).

    Args:
        event: The Scheduler-delivered event. Optional/`None`-tolerant so the
            handler never crashes on an empty tick; the scheduled time and a
            derived trigger-source id are extracted from whatever is present.
        context: The Lambda context object (unused; accepted for the AWS
            handler signature).

    Returns:
        A JSON-serialisable dict. On a skip: ``{"ok": True, "run_started":
        False, "reason": "sweep_already_in_progress", ...}``. On a started
        sweep: ``{"ok": True, "run_started": True, "mode": "sweep", ...}``
        carrying the runtime session id. Never raises — a configuration or
        invocation failure is returned as ``{"ok": False, ...}`` so the
        Scheduler's own retry/DLQ policy sees a structured result.
    """
    event = event or {}
    scheduled_time = _scheduled_time(event)
    trigger_source_id = _trigger_source_id(event, scheduled_time)

    # Req 1.8: skip the tick if a prior sweep has not reached a terminal state.
    try:
        in_progress = state_store.query_in_progress_runs(SWEEP_TRIGGER_TYPE)
    except Exception as exc:  # noqa: BLE001 - a read failure must not block the sweep entirely
        in_progress = []
        _safe_audit(
            run_id=f"sweep-tick:{scheduled_time}",
            tool_name="sweep_lambda.in_progress_check",
            outcome="check_failed",
            inputs={"error": f"{type(exc).__name__}: {exc}", "scheduled_time": scheduled_time},
        )

    if in_progress:
        in_progress_run_id = in_progress[0].get("run_id")
        _safe_audit(
            run_id=f"skipped-{uuid.uuid4().hex}",
            tool_name="sweep_lambda.skip",
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
        }

    payload: dict[str, Any] = {
        "mode": "sweep",
        "trigger_type": SWEEP_TRIGGER_TYPE,
        "trigger_source_id": trigger_source_id,
        "scheduled_time": scheduled_time,
    }
    river_reach_id = event.get("river_reach_id") or (event.get("detail") or {}).get("river_reach_id")
    if river_reach_id:
        payload["river_reach_id"] = str(river_reach_id)

    try:
        invocation = _invoke_runtime(payload)
    except Exception as exc:  # noqa: BLE001 - return the cause, never an opaque crash
        _safe_audit(
            run_id=f"sweep-tick:{scheduled_time}",
            tool_name="sweep_lambda.invoke",
            outcome="invoke_failed",
            inputs={
                "scheduled_time": scheduled_time,
                "trigger_source_id": trigger_source_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return {
            "ok": False,
            "run_started": False,
            "reason": "runtime_invocation_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "scheduled_time": scheduled_time,
        }

    return {
        "ok": True,
        "run_started": True,
        "mode": "sweep",
        "scheduled_time": scheduled_time,
        "trigger_source_id": trigger_source_id,
        **invocation,
    }


def _safe_audit(**kwargs: Any) -> None:
    """Append an audit entry, never letting a ledger failure crash the trigger.

    Mirrors ``surface/entrypoint.py::_safe_audit``: these are run *lifecycle*
    records (a skip, a failed invocation), so an audit-ledger hiccup must not
    turn a recoverable event into an unhandled Lambda error.
    """
    try:
        append_audit_entry(**kwargs)
    except Exception:  # noqa: BLE001 - see docstring
        pass
