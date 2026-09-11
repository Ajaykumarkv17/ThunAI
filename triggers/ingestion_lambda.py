"""API Gateway -> Lambda: resident-message / sensor-reading / hazard-image intake.

Tasks 20.2. Requirements 1.2, 1.3, 1.4, 1.9, 1.10; design.md Architecture,
Error Handling table.

This is the AWS-native front door for every *inbound* trigger a person or a
sensor pushes (as opposed to the scheduled sweep, which ``sweep_lambda.py``
owns): a resident message (Req 1.2), a sensor reading (Req 1.3), or an
uploaded hazard image (Req 1.4). It parses the API Gateway proxy event,
validates the payload, suppresses a 24-hour duplicate, and — only when the
payload is accepted and new — invokes the single AgentCore Runtime
(``surface/entrypoint.py``) with ``mode=intake`` through
``bedrock-agentcore``'s ``invoke_agent_runtime`` (boto3).

Payload validation (Req 1.10)
-----------------------------
Req 1.10: "IF a received payload is missing a required field, exceeds the
maximum accepted size, or is in an unsupported format, THEN THE
ThunAI_Platform SHALL reject the payload with a response indicating which
validation check failed and SHALL record the rejection, WITHOUT starting a
run." This handler rejects — before any ``invoke_agent_runtime`` call —
a payload that:

- is not a JSON object, or names no intake content (message / readings /
  image), or omits a required field (``missing_required_field``);
- exceeds 10 MB (``payload_too_large``), or whose attached image exceeds
  10 MB (``image_too_large``);
- attaches an image in an unsupported format (``unsupported_image_format``).

Every rejection returns an HTTP 400 whose body names the failed check and
writes a rejection record to ``Audit_Ledger`` (Req 1.10), and no run is
started. The size/format limits are imported from ``surface/entrypoint.py``
(``MAX_PAYLOAD_BYTES``, ``SUPPORTED_IMAGE_FORMATS``) so the trigger boundary
and the runtime enforce the *same* numbers, and the entrypoint keeps its own
copy of the check as server-side defence-in-depth.

24-hour duplicate suppression (Req 1.9)
---------------------------------------
Req 1.9: "IF a received trigger source identifier matches a payload already
recorded in Audit_Ledger within the preceding 24 hours, THEN THE
ThunAI_Platform SHALL NOT start a new run and SHALL record a
duplicate-suppression entry referencing the identifier of the run started for
the original payload." This handler scans ``Audit_Ledger`` for a prior
``ingestion_lambda.accept`` entry carrying the same ``trigger_source_id``
within the last 24 hours (see :func:`_find_recent_duplicate`). When found, it
returns without invoking the runtime and writes a duplicate-suppression record
referencing the original run's id.

This is deliberately the *Audit_Ledger*-based 24-hour check Req 1.9 describes,
distinct from ``memory/state_store.py``'s 72-hour ``record_trigger_event``
processed-trigger dedupe (Req 15.3/15.4) — see ``memory/state_store.py``'s
module docstring, "Task-scoping note on Req 1.9 vs. Req 15.3/15.4", which
explicitly assigns this 24-hour Audit_Ledger check to this module.

Verification note: docs/MCP verification was explicitly skipped per the task
note. The API Gateway proxy event shape (``body`` [possibly base64-encoded],
``headers``, ``httpMethod``) and the ``invoke_agent_runtime`` call shape are
reused from patterns already verified elsewhere in this codebase.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from harness.audit import append_audit_entry
from memory import audit_ledger
from surface.entrypoint import MAX_PAYLOAD_BYTES, SUPPORTED_IMAGE_FORMATS

__all__ = ["handler", "INTAKE_TRIGGER_TYPE", "DEDUPE_WINDOW_HOURS"]

#: The ``trigger_type`` an ingestion-started run is recorded under.
INTAKE_TRIGGER_TYPE = "intake"

#: Req 1.9: the duplicate-suppression window is the preceding 24 hours.
DEDUPE_WINDOW_HOURS = 24

_RUNTIME_ARN_ENV_VAR = "THUNAI_RUNTIME_ARN"

#: The audit ``tool_name`` written when a payload is accepted and a run is
#: started — the marker :func:`_find_recent_duplicate` scans for.
_ACCEPT_TOOL_NAME = "ingestion_lambda.accept"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    """A fresh 39-character ``runtimeSessionId`` (AgentCore 33+-char floor)."""
    return f"intake-{uuid.uuid4().hex}"


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# API Gateway proxy event parsing.
# ---------------------------------------------------------------------------


def _parse_event(event: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the intake payload dict from an API Gateway proxy event.

    Handles the API Gateway proxy shape: the request body is in ``event["body"]``
    (a JSON string, or base64-encoded when ``isBase64Encoded`` is true). A
    direct (non-proxy) invocation that passes the payload as the event itself
    is also tolerated, so a test or an alternate integration can call the
    handler without wrapping the payload in a proxy envelope.

    Returns:
        The parsed payload as a dict. An unparseable or absent body yields
        ``{}`` (which :func:`_validate_payload` then rejects as
        ``missing_required_field``), never a raised exception.
    """
    if not isinstance(event, dict):
        return {}

    # Proxy integration: the real payload is the (possibly base64) JSON body.
    if "body" in event:
        raw = event.get("body")
        if raw is None:
            return {}
        if event.get("isBase64Encoded"):
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except (ValueError, TypeError):
                return {}
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # Direct invocation: the event already is the payload (drop proxy-only keys).
    return {k: v for k, v in event.items() if k not in ("requestContext", "headers", "httpMethod")}


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

    Checks, in order (Req 1.10's three categories — missing required field,
    oversized, unsupported format):

    1. the payload is a JSON object;
    2. it is at most 10 MB;
    3. it names at least one of message / readings / image (Req 1.2/1.3/1.4);
    4. an attached image declares a supported format and fits the size limit.
    """
    if not isinstance(payload, dict):
        return {"failed_check": "payload_not_an_object", "detail": f"payload type {type(payload).__name__}"}

    size = _payload_size_bytes(payload)
    if size > MAX_PAYLOAD_BYTES:
        return {
            "failed_check": "payload_too_large",
            "detail": f"{size} bytes exceeds the {MAX_PAYLOAD_BYTES}-byte limit",
        }

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
        if declared_bytes is not None:
            try:
                too_big = int(declared_bytes) > MAX_PAYLOAD_BYTES
            except (TypeError, ValueError):
                too_big = False
            if too_big:
                return {
                    "failed_check": "image_too_large",
                    "detail": f"{declared_bytes} bytes exceeds the {MAX_PAYLOAD_BYTES}-byte limit",
                }

    return None


# ---------------------------------------------------------------------------
# 24-hour Audit_Ledger duplicate check (Req 1.9).
# ---------------------------------------------------------------------------


def _find_recent_duplicate(trigger_source_id: str, *, now: datetime) -> dict[str, Any] | None:
    """Return the original accepted-run record for ``trigger_source_id`` if one
    was recorded in ``Audit_Ledger`` within the preceding 24 hours (Req 1.9).

    Scans this trigger's per-run audit partition (``Audit_Ledger`` is keyed
    ``pk = "RUN#{run_id}"``) via ``memory.audit_ledger.get_entries_for_run``
    keyed on the deterministic per-trigger run partition this handler writes
    the accept marker under (see :func:`_accept_run_partition`). Returns the
    original run id and the original acceptance timestamp when a still-in-window
    marker exists, or ``None`` otherwise.
    """
    cutoff = now - timedelta(hours=DEDUPE_WINDOW_HOURS)
    partition = _accept_run_partition(trigger_source_id)
    try:
        entries = audit_ledger.get_entries_for_run(partition)
    except Exception:  # noqa: BLE001 - a read failure must not silently drop a real trigger
        return None

    for entry in entries:
        if entry.tool_name != _ACCEPT_TOOL_NAME:
            continue
        try:
            recorded_at = datetime.fromisoformat(entry.timestamp)
        except (TypeError, ValueError):
            continue
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=timezone.utc)
        if recorded_at < cutoff:
            continue
        original_run_id = (entry.inputs or {}).get("run_id")
        return {"original_run_id": original_run_id, "recorded_at": entry.timestamp}
    return None


def _accept_run_partition(trigger_source_id: str) -> str:
    """The stable ``Audit_Ledger`` run partition the accept marker is written
    under for a given trigger source id, so a later delivery of the same id can
    find it (Req 1.9). Deterministic in the trigger source id — not the (new)
    run id — precisely so the duplicate scan can address it without knowing the
    original run id in advance."""
    return f"INGESTION#{trigger_source_id}"


# ---------------------------------------------------------------------------
# Runtime invocation (mode=intake).
# ---------------------------------------------------------------------------


def _invoke_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke the AgentCore Runtime with ``payload`` (mode=intake).

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


# ---------------------------------------------------------------------------
# HTTP response helpers (API Gateway proxy response shape).
# ---------------------------------------------------------------------------


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway proxy response. ``body`` is also returned inline as
    ``result`` so a direct (non-proxy) caller reads the structured outcome
    without re-parsing the JSON string body."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
        "result": body,
    }


def _trigger_source_id(payload: dict[str, Any]) -> str:
    """The inbound trigger's source identifier (Req 1.9 dedupe key).

    Uses the caller-supplied ``trigger_source_id`` when present (a resident
    message id, a sensor reading id, an image upload id); otherwise derives a
    fresh one so an id-less payload is still traceable but never mistaken for a
    duplicate of another id-less payload."""
    return str(payload.get("trigger_source_id") or payload.get("message_id") or f"intake:{uuid.uuid4().hex}")


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """API Gateway proxy entry point for inbound intake (Req 1.2, 1.3, 1.4, 1.9, 1.10).

    Args:
        event: The API Gateway proxy event (its ``body`` carries the JSON
            payload), or the payload itself for a direct invocation.
        context: The Lambda context object (unused; accepted for the AWS
            handler signature).

    Returns:
        An API Gateway proxy response dict (``statusCode``/``headers``/``body``),
        with the same structured body echoed under ``result``:

        - 400 ``{"reason": "payload_rejected", "failed_check": ...}`` on a
          validation failure (Req 1.10) — no run started, rejection recorded.
        - 200 ``{"run_started": False, "reason":
          "duplicate_trigger_source_id", "original_run_id": ...}`` on a 24-hour
          duplicate (Req 1.9) — no run started, suppression recorded.
        - 202 ``{"run_started": True, "mode": "intake", "run_id": ...}`` on an
          accepted, new payload.
        - 502 ``{"reason": "runtime_invocation_failed"}`` when the runtime
          invocation itself fails.

        Never raises.
    """
    payload = _parse_event(event)
    now = _now()

    # Req 1.10: reject an invalid payload without starting a run.
    rejection = _validate_payload(payload)
    if rejection is not None:
        record_id = f"rejected-{uuid.uuid4().hex}"
        _safe_audit(
            run_id=record_id,
            tool_name="ingestion_lambda.rejection",
            outcome=f"rejected: {rejection['failed_check']}",
            inputs={
                "failed_check": rejection["failed_check"],
                "detail": rejection.get("detail"),
                "trigger_source_id": payload.get("trigger_source_id") if isinstance(payload, dict) else None,
            },
        )
        return _response(
            400,
            {
                "ok": False,
                "run_started": False,
                "reason": "payload_rejected",
                "failed_check": rejection["failed_check"],
                "detail": rejection.get("detail"),
                "record_id": record_id,
            },
        )

    trigger_source_id = _trigger_source_id(payload)

    # Req 1.9: suppress a payload whose trigger source id was already accepted
    # within the preceding 24 hours; the record references the original run.
    duplicate = _find_recent_duplicate(trigger_source_id, now=now)
    if duplicate is not None:
        record_id = f"duplicate-{uuid.uuid4().hex}"
        _safe_audit(
            run_id=record_id,
            tool_name="ingestion_lambda.duplicate",
            outcome="duplicate_suppressed",
            inputs={
                "trigger_source_id": trigger_source_id,
                "original_run_id": duplicate.get("original_run_id"),
                "original_recorded_at": duplicate.get("recorded_at"),
                "window_hours": DEDUPE_WINDOW_HOURS,
            },
        )
        return _response(
            200,
            {
                "ok": True,
                "run_started": False,
                "reason": "duplicate_trigger_source_id",
                "trigger_source_id": trigger_source_id,
                "original_run_id": duplicate.get("original_run_id"),
                "record_id": record_id,
            },
        )

    run_id = _new_run_id()

    # Record the acceptance in Audit_Ledger *before* invoking the runtime, under
    # the deterministic per-trigger partition the duplicate scan reads, so a
    # second delivery of this same trigger source id is caught by Req 1.9 even
    # if it arrives before the runtime finishes the first run.
    _safe_audit(
        run_id=_accept_run_partition(trigger_source_id),
        tool_name=_ACCEPT_TOOL_NAME,
        outcome="accepted",
        inputs={
            "run_id": run_id,
            "trigger_source_id": trigger_source_id,
            "trigger_type": INTAKE_TRIGGER_TYPE,
            "accepted_at": now.isoformat(),
        },
    )

    runtime_payload: dict[str, Any] = {
        "mode": "intake",
        "trigger_type": payload.get("trigger_type") or INTAKE_TRIGGER_TYPE,
        "trigger_source_id": trigger_source_id,
        "run_id": run_id,
    }
    for key in ("message", "readings", "image", "language", "request_id", "river_reach_id"):
        if payload.get(key) is not None:
            runtime_payload[key] = payload[key]

    try:
        invocation = _invoke_runtime(runtime_payload)
    except Exception as exc:  # noqa: BLE001 - return the cause, never an opaque crash
        _safe_audit(
            run_id=run_id,
            tool_name="ingestion_lambda.invoke",
            outcome="invoke_failed",
            inputs={
                "trigger_source_id": trigger_source_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return _response(
            502,
            {
                "ok": False,
                "run_started": False,
                "reason": "runtime_invocation_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "run_id": run_id,
                "trigger_source_id": trigger_source_id,
            },
        )

    return _response(
        202,
        {
            "ok": True,
            "run_started": True,
            "mode": "intake",
            "run_id": run_id,
            "trigger_source_id": trigger_source_id,
            **invocation,
        },
    )


def _safe_audit(**kwargs: Any) -> None:
    """Append an audit entry, never letting a ledger failure crash the trigger.

    Mirrors ``surface/entrypoint.py::_safe_audit``: these are run *lifecycle*
    records, so an audit-ledger hiccup must not turn a recoverable trigger
    event into an unhandled Lambda error.
    """
    try:
        append_audit_entry(**kwargs)
    except Exception:  # noqa: BLE001 - see docstring
        pass
