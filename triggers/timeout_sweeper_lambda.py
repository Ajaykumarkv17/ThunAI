"""EventBridge rule (1-min cadence) target: apply the default action to
past-deadline escalations (Req 11.6; design.md Design Question 2).

An EventBridge rule fires this Lambda once a minute. The handler finds every
``EscalationRecord`` whose response deadline has passed and applies the
``Escalation_Policy`` default action to it, entirely by delegating to
``surface/escalation_service.py::sweep_timed_out_escalations`` — the function
that module's own docstring already names this Lambda as the caller of ("the
function ``triggers/timeout_sweeper_lambda.py`` (task 20.3) calls once per
invocation, per design.md §5.1's 1-minute EventBridge sweep").

This module deliberately holds **no** timeout/resolution logic of its own: the
whole point of design.md Design Question 2 is that nothing blocks inside the
AgentCore Runtime waiting on a human, so the timeout that resolves an unanswered
escalation by its default action is driven from *outside* the runtime, by this
scheduled sweep. All of that behaviour — querying OPEN escalations, comparing
each one's ``response_deadline`` to now, flipping the past-deadline ones to
``RESOLVED_BY_DEFAULT``, recording the timeout and applied default action in
``Audit_Ledger``, notifying the coordinator, and invoking the runtime's resume
path — already lives in ``escalation_service.sweep_timed_out_escalations`` /
``apply_timeout_default`` (task 16.3). This handler is the thin EventBridge
adapter around it, matching the same "trigger Lambda translates an AWS event
into one call and records the outcome, holding no domain logic" shape as
``sweep_lambda.py`` and ``ingestion_lambda.py``.

Verification note: docs/MCP verification was explicitly skipped per the task
note. An EventBridge scheduled-rule event is a fixed ``{"source":
"aws.events", "detail-type": "Scheduled Event", ...}`` envelope with no
per-invocation fields this handler needs — it acts on the current time and the
current OPEN-escalation set, not on anything in the event body.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from surface import escalation_service

__all__ = ["handler"]


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """EventBridge 1-minute rule target entry point (Req 11.6).

    Args:
        event: The EventBridge scheduled-rule event (unused beyond an optional
            ``now`` override a test/manual invocation may inject; the sweep
            acts on the current OPEN-escalation set, not on event content).
        context: The Lambda context object (unused; accepted for the AWS
            handler signature).

    Returns:
        A JSON-serialisable summary: ``{"ok": True, "swept": <n>, "results":
        [...]}`` where ``results`` is ``escalation_service
        .sweep_timed_out_escalations``'s per-escalation result dicts (one per
        past-deadline OPEN escalation found this tick; empty when none was due).
        On an unexpected error, ``{"ok": False, "reason": ..., "error": ...}``
        — never raises, so the EventBridge retry/DLQ policy sees a structured
        result rather than an opaque crash.
    """
    # A test or manual invocation may pin the comparison clock; a real
    # EventBridge tick supplies none, so the sweep uses the current UTC time.
    now: datetime | None = None
    if isinstance(event, dict) and event.get("now"):
        try:
            parsed = datetime.fromisoformat(str(event["now"]))
            now = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            now = None

    try:
        results = escalation_service.sweep_timed_out_escalations(now=now)
    except Exception as exc:  # noqa: BLE001 - return the cause, never an opaque crash
        return {
            "ok": False,
            "reason": "timeout_sweep_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {"ok": True, "swept": len(results), "results": results}
