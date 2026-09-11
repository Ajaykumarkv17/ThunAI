"""Harness-facing audit-append wrapper: ``append_audit_entry`` (Req 16.8, 15.6).

This is the function every hook class (eventually ``harness/hooks.py
::AuditHook``, task 8.2, not yet implemented) calls after every tool
invocation, per design.md §3.6:

    def after(self, event: AfterToolCallEvent) -> None:
        redacted_input = redact_fields(event.tool_use["input"])          # Req 16.10
        ok = append_audit_entry(
            run_id=event.run_id, tool_call_id=event.tool_use["toolUseId"],
            tool_name=event.tool_use["name"], inputs=redacted_input,
            outcome=event.result if not event.cancelled else f"blocked: {event.cancel_tool}",
            approving_human_id=approval_store.approver_for(event.run_id, event.tool_use["toolUseId"]),
            model_id=event.model_id, input_tokens=event.input_tokens, output_tokens=event.output_tokens,
        )

This module's ``append_audit_entry`` takes the tool-call-shaped keyword
arguments design.md's sketch above shows (rather than a pre-built
``AuditEntry``), constructs the :class:`schemas.entities.AuditEntry`,
redacts ``inputs`` before persisting, and delegates the actual DynamoDB
write to ``memory.audit_ledger.append_entry``. Redaction itself is task
8.1's job (``harness/redaction.py::redact_fields``) — this module imports
and calls that function rather than re-implementing any redaction logic.

Verification note (mandatory per tasks.md 7.1, "verify first"): this module
has no boto3/Strands API surface of its own — it is a thin composition of
``harness.redaction.redact_fields`` (pure Python) and
``memory.audit_ledger.append_entry`` (already verified against the current
AWS documentation in ``memory/audit_ledger.py``'s own verification note).
No new external API needed re-verification here.

Design deviation from design.md's literal sketch, stated explicitly: the
sketch above shows ``AuditHook.after()`` treating ``append_audit_entry`` as
returning a boolean (``ok = append_audit_entry(...)``) and then checking
``if not ok`` to decide whether to block the tool call. This module's
``append_audit_entry`` instead **raises** on failure and returns ``None`` on
success, per the task prompt's explicit instruction ("this function should
... raise (not swallow) any exception from the underlying append, so a
caller higher up (the eventual AuditHook) can react") and per Req 16.9's
own wording ("IF the Audit_Ledger append for a write tool call does not
succeed, THEN THE Harness SHALL block that tool call") — the *Harness*
(``AuditHook``, task 8.2) is the component responsible for deciding to
block on failure, not this function. Task 8.2's ``AuditHook.after()`` will
therefore wrap this call in a ``try/except (AuditAppendError)`` rather than
checking a boolean return value; this is a narrower, more Pythonic
reconciliation of the same Req 16.8/16.9 behaviour the sketch describes, not
a change to what gets enforced.
"""

from __future__ import annotations

from datetime import datetime, timezone

from harness.redaction import redact_fields
from memory.audit_ledger import append_entry, new_entry_id
from schemas.entities import AuditEntry

__all__ = ["append_audit_entry"]


def append_audit_entry(
    *,
    run_id: str,
    tool_name: str,
    outcome: str,
    tool_call_id: str | None = None,
    inputs: dict | None = None,
    approving_human_id: str | None = None,
    model_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    incident_id: str | None = None,
) -> AuditEntry:
    """Construct, redact, and persist one ``Audit_Ledger`` entry.

    Called by ``harness/hooks.py::AuditHook`` (task 8.2) after every tool
    call — including every blocked tool call (Req 16.8) — within 1 second of
    that tool call completing or being blocked.

    Redacts ``inputs`` via :func:`harness.redaction.redact_fields` *before*
    constructing the :class:`AuditEntry`, so the redacted (never the raw)
    value is what reaches ``memory.audit_ledger.append_entry`` and therefore
    the persisted ``Audit_Ledger`` item, a log record, or a trace attribute
    (Req 16.10). This function performs no redaction logic itself — that is
    ``harness/redaction.py``'s job (task 8.1) exclusively; this function only
    calls it.

    Args:
        run_id: The current run identifier.
        tool_name: The tool's name, exactly as it appears in the tool-call
            event (e.g. ``event.tool_use["name"]``).
        outcome: A short outcome description — the tool's result summary
            when the call executed, or a ``"blocked: <reason>"`` string when
            the Harness cancelled the call (design.md §3.6's
            ``event.result if not event.cancelled else f"blocked:
            {event.cancel_tool}"`` construction is the caller's
            responsibility, not this function's; this function simply
            persists whatever string it is given).
        tool_call_id: The tool call's identifier (e.g.
            ``event.tool_use["toolUseId"]``), when available. ``None`` for a
            record that is not tied to one specific tool call (not expected
            in normal Harness use, but not disallowed by ``AuditEntry``).
        inputs: The tool call's input arguments, before redaction. Defaults
            to an empty dict when omitted (matching
            ``AuditEntry.inputs``'s own default).
        approving_human_id: The identifier of the human who approved this
            call, when a recorded approval applied. ``None`` when no
            approval was involved (Req 16.8).
        model_id: The identifier of the model that produced the tool call.
        input_tokens: The input token count of the model invocation that
            produced the tool call, when known.
        output_tokens: The output token count of the model invocation that
            produced the tool call, when known.
        incident_id: When this tool call is attributable to a specific
            incident, the incident identifier to index the entry under (see
            ``memory.audit_ledger.append_entry``'s ``incident_id``
            parameter and Req 12.7's per-incident audit-trail read path).
            ``None`` for a call not attributable to one incident (e.g. a
            Coordinator_Orchestrator read-only tool call).

    Returns:
        The persisted :class:`AuditEntry`, for a caller that wants to log or
        inspect what was written (e.g. in a test).

    Raises:
        memory.audit_ledger.AuditAppendError: Propagated, unmodified, from
            ``memory.audit_ledger.append_entry`` if the underlying append
            fails for any reason — including
            ``memory.audit_ledger.AuditAppendDuplicateError`` for the
            append-only-violation case. This function does not catch or
            swallow that exception; per Req 16.9, the caller (the eventual
            ``AuditHook``) is responsible for reacting to it by blocking the
            triggering write-tool call.
    """
    redacted_inputs = redact_fields(inputs or {})

    entry = AuditEntry(
        entry_id=new_entry_id(),
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        inputs=redacted_inputs,
        outcome=outcome,
        approving_human_id=approving_human_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    append_entry(entry, incident_id=incident_id)
    return entry
