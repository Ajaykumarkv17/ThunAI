"""``create_escalation``: persists an ``EscalationRecord`` for a pending
human decision (Req 11.1; design.md §3.4, §3.5).

Verification note (mandatory per tasks.md 12.7, "verify first"):
    Checked against the current Strands Agents docs (`strands-agents`
    installed at `1.55.0` per `pyproject.toml`, matching every other
    already-verified tool module in this codebase) for the `@tool` decorator
    contract, cross-checked against design.md §3.2/§3.4's own pseudocode for
    exactly how this function is invoked.

    **Finding: this function is deliberately NOT `@tool`-decorated.**
    Every other module under `tools/` (`sensor_tools.py`, `incident_tools.py`,
    `dispatch_tools.py`, `alert_tools.py`) wraps its functions with
    `@tool` because those functions are called *by the model*, inside a
    Strands `Agent`'s tool-selection loop — the model reads the decorated
    function's docstring/signature to decide when and how to call it.
    `create_escalation` is different in kind, not just in degree: design.md
    §3.2 states plainly that "the deterministic gate (§3.4) decides
    `ask_human` and calls `Escalation_Service.create_escalation(...)`
    directly — a plain Python call, not a Strands SDK primitive", and
    §3.4's own pseudocode (`Gate->>ESvc: create_escalation(EscalationRecord)`
    in the sequence diagram) shows the deterministic
    `policy/escalation_policy.py::decide()` authority — pure Python, zero
    model calls, per that module's own docstring — as the caller. No model
    ever sees this function's name, docstring, or signature, because it is
    never registered on any `Agent(tools=[...])` list anywhere in this
    design; `harness/hooks.py::WRITE_TOOLS` lists the string
    `"create_escalation"` only so `ApprovalGateHook`'s allowlist vocabulary
    stays consistent with every write-tool name named anywhere in the
    design (see that module's own docstring), not because this function is
    ever reached through a model's tool-call loop that hook could intercept.
    Decorating a plain deterministic-gate-to-service call with `@tool` would
    misrepresent it to the SDK as model-callable and add an unused input
    schema for a caller that will never be a model. This module therefore
    exports `create_escalation` as an ordinary function, matching the
    "plain Python function" contract design.md itself specifies, while
    still following every other established plain-dict-return convention
    (`ok` field, typed non-raising failure `reason`, idempotent writes via
    an explicit `idempotency_key`) that this codebase's `@tool`-decorated
    modules also use — that convention is about how *any* write-performing
    function in this codebase reports outcomes, not something specific to
    `@tool`.

Persistence-only scope (per this task's explicit instructions): this
function's job ends at "persist the `EscalationRecord`". It does **not**
send the out-of-band notification (Req 11.2 — that belongs to
`surface/escalation_service.py`, task 16.3, not yet built) and it does not
wire the resume path (`agents/hitl.py`, task 16.2, not yet built). A future
`surface/escalation_service.py` is expected to call this function first
(to persist the record) and then perform the notification send as a
separate, subsequent step — exactly as design.md §3.2's numbered pause-point
description separates "persists the Escalation_Record (Req 11.1)" from
"fires the notification (Req 11.2)" as two distinct actions taken by the
same caller.

Idempotency (Req 15.2, Property 2's generator explicitly includes
``create_escalation`` among the write functions tested for idempotent
replay): wrapped in ``memory.state_store.idempotent_write`` keyed by a
caller-supplied ``idempotency_key``, following the exact pattern every
other write tool in this codebase already uses
(``tools/dispatch_tools.py::assign_responder``,
``tools/alert_tools.py::deliver_alert``,
``tools/incident_tools.py::create_or_update_incident``) — a retried call for
the same key (e.g. a resumed run replaying the same gate decision after a
crash before the record was durably recorded) never creates a second
``EscalationRecord`` and simply replays the first call's outcome, satisfying
Req 11.9's "at most one Escalation_Record per routine incident sweep run"
even under a retry.

Field-shape validation (Req 11.1's literal wording: "a decision summary of
one sentence not exceeding 200 characters", "between 2 and 5 available
options each carrying a distinct option identifier"): this module checks
those two constraints itself, *before* constructing the ``EscalationRecord``
Pydantic model and *before* calling ``idempotent_write`` (so an invalid call
never consumes the idempotency key), returning a typed, non-raising
``{"ok": False, "reason": ...}`` result rather than letting a
``pydantic.ValidationError`` propagate — matching every other tool module's
"never raise for an expected, recoverable outcome" convention documented
throughout this codebase (e.g. ``memory/state_store.py``'s own stated
philosophy). ``schemas.entities.EscalationRecord`` itself already declares
``decision_summary: str = Field(max_length=200)`` and
``options: list[EscalationOption] = Field(min_length=2, max_length=5)``, so
this module's own checks are a defence-in-depth mirror of those same
constraints, checked early enough to short-circuit before any write is
attempted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from memory import state_store
from schemas.entities import EscalationOption, EscalationRecord

__all__ = ["create_escalation"]


def _coerce_options(options: list[Any]) -> list[EscalationOption]:
    coerced: list[EscalationOption] = []
    for option in options:
        if isinstance(option, EscalationOption):
            coerced.append(option)
        else:
            coerced.append(EscalationOption(**option))
    return coerced


def create_escalation(
    *,
    idempotency_key: str,
    run_id: str,
    escalation_type: str,
    decision_summary: str,
    reason: str,
    stakes: str,
    options: list[dict[str, str]] | list[EscalationOption],
    default_action: str,
    response_deadline_minutes: int,
    incident_id: str | None = None,
    interrupt_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a new ``EscalationRecord`` with status ``OPEN`` (Req 11.1).

    Called only by the deterministic escalation gate
    (``policy/escalation_policy.py::decide()``'s ``ASK_HUMAN`` verdict,
    design.md §3.2/§3.4) — never by a model's tool-call loop. See this
    module's own docstring for why this is a plain function rather than a
    Strands ``@tool``.

    The caller is expected to have already obtained ``default_action`` and
    ``response_deadline_minutes`` from ``policy.escalation_policy.decide()``'s
    own return value (its ``"default_action"`` and
    ``"response_deadline_minutes"`` fields) for the escalating decision, so
    this function never re-derives an autonomy threshold itself — it only
    persists what the single autonomy authority already decided (Req
    10.8).

    Args:
        idempotency_key: A caller-supplied key unique to the escalating
            decision (e.g. derived from the incident id, the run id, and the
            escalation type), so a retried call for the same decision never
            creates a second ``EscalationRecord`` and instead replays the
            first call's outcome (Req 15.2, Property 2). Must be non-empty.
        run_id: The Incident_Graph run identifier the paused decision
            belongs to, so a later resume can locate this run's persisted
            progress (Req 11.5).
        escalation_type: The template key this record renders under (e.g.
            ``"dispatch_assignment"``, ``"alert_release"``) — must match a
            key in ``surface/escalation_service.py``'s ``TEMPLATES`` once
            that module exists (design.md §3.5); this module does not
            itself validate the key against that table, since
            ``surface/escalation_service.py`` (task 16.3) is not yet built.
        decision_summary: One sentence, at most 200 characters, describing
            what ThunAI decided (Req 11.1).
        reason: Why a human is being asked instead of ThunAI acting alone
            (Req 11.1, 11.3).
        stakes: The stakes of the decision, including any quantity (Req
            11.1, 11.3).
        options: Between 2 and 5 available options, each either an
            ``EscalationOption`` or a plain
            ``{"option_id": str, "label": str}`` dict, each carrying a
            distinct ``option_id`` (Req 11.1).
        default_action: The single default action drawn from
            ``policy.escalation_policy.DEFAULT_ACTION`` for
            ``escalation_type`` (Req 11.1) — applied by the (not-yet-built)
            timeout sweeper if no human responds by the deadline.
        response_deadline_minutes: The number of whole minutes from now
            until the response deadline, drawn from
            ``policy.escalation_policy.decide()``'s own
            ``response_deadline_minutes`` (Req 11.1). Used to compute the
            persisted absolute, timezone-aware ``response_deadline``.
        incident_id: The incident this escalation concerns, if any.
        interrupt_id: The Strands ``HumanInTheLoop`` interrupt identifier,
            set only when the pausing node used that interrupt/resume
            mechanism (design.md §3.2's note: "if that node was mid-tool-call
            inside a Strands `Agent` using `HumanInTheLoop`"). ``None`` when
            the pause was raised by the deterministic gate directly with no
            SDK interrupt object involved.
        context: Escalation-type-specific persisted values needed to render
            this escalation's hand-authored template (e.g. ``responder_name``,
            ``location`` for ``"dispatch_assignment"``) — copied verbatim
            from already-persisted ``State_Store`` values, never from a
            model call (Req 11.3). Defaults to an empty dict.

    Returns:
        On success (including a replay of a prior call for the same
        ``idempotency_key``):
        ``{"ok": True, "escalation_id": str, "status": "OPEN",
        "response_deadline": str, "already_existed": bool}``.

        On a validation failure (never raised):
        ``{"ok": False, "reason": "missing_idempotency_key"}`` when
        ``idempotency_key`` is empty;
        ``{"ok": False, "reason": "decision_summary_too_long", "limit": 200}``
        when ``decision_summary`` exceeds 200 characters;
        ``{"ok": False, "reason": "invalid_options_count", "count": int}``
        when ``options`` has fewer than 2 or more than 5 entries;
        ``{"ok": False, "reason": "duplicate_option_ids"}`` when two or
        more options share the same ``option_id``.
    """
    if not idempotency_key:
        return {"ok": False, "reason": "missing_idempotency_key"}

    if len(decision_summary) > 200:
        return {"ok": False, "reason": "decision_summary_too_long", "limit": 200}

    if not (2 <= len(options) <= 5):
        return {"ok": False, "reason": "invalid_options_count", "count": len(options)}

    coerced_options = _coerce_options(list(options))
    option_ids = [option.option_id for option in coerced_options]
    if len(set(option_ids)) != len(option_ids):
        return {"ok": False, "reason": "duplicate_option_ids"}

    already_existed = state_store._get_idempotency_record(idempotency_key) is not None

    def _create() -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        deadline = (now + timedelta(minutes=response_deadline_minutes)).isoformat()
        record = EscalationRecord(
            escalation_id=str(uuid.uuid4()),
            incident_id=incident_id,
            escalation_type=escalation_type,
            status="OPEN",
            decision_summary=decision_summary,
            reason=reason,
            stakes=stakes,
            options=coerced_options,
            default_action=default_action,
            response_deadline=deadline,
            interrupt_id=interrupt_id,
            run_id=run_id,
            context=dict(context or {}),
        )
        state_store.put_escalation_record(record)
        return {
            "ok": True,
            "escalation_id": record.escalation_id,
            "status": record.status,
            "response_deadline": record.response_deadline,
        }

    outcome = state_store.idempotent_write(idempotency_key, _create)
    return {**outcome, "already_existed": already_existed}
