"""Responder assignment read/respond API (Req 13.1-13.10).

Serves the two endpoints the Responder_Interface calls:
    GET  /responders/{id}/assignments                      -> Assignment[]
    POST /responders/{id}/assignments/{assignmentId}/respond {action}

Assignments are persisted in ``thunai-state`` under ``pk = ASSIGNMENT#{id}``,
``sk = META``, each carrying ``assignedResponderId`` and the current lifecycle
``state``. Each response advances the assignment through the
``Request_Lifecycle`` machine (Design §4.3); an impermissible transition is
rejected with HTTP 409 and the permitted set named (Req 13.9).

This module talks to DynamoDB directly (self-contained, no dependency on the
core state_store's typed helpers) so the responder surface can be deployed and
demoed independently.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

_TABLE_NAME = os.environ.get("THUNAI_STATE_TABLE", "thunai-state")
_table = boto3.resource("dynamodb").Table(_TABLE_NAME)

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Content-Type": "application/json",
}

#: Lifecycle transitions: state -> {action: next_state} (Design §4.3).
_TRANSITIONS: dict[str, dict[str, str]] = {
    "AWAITING_ACK": {"accept": "ACCEPTED", "decline": "DECLINED"},
    "ACCEPTED": {"en-route": "EN_ROUTE"},
    "EN_ROUTE": {"on-scene": "ON_SCENE"},
    "ON_SCENE": {"completed": "VERIFICATION"},
    "VERIFICATION": {},
    "DECLINED": {},
}

#: Which timestamp attribute each action stamps, for the assignment timeline.
_TIMESTAMP_ATTR: dict[str, str] = {
    "accept": "acceptedAt",
    "decline": "declinedAt",
    "en-route": "enRouteAt",
    "on-scene": "onSceneAt",
    "completed": "completedAt",
}

#: Fields carried to the frontend Assignment shape (responder/types.ts).
_ASSIGNMENT_FIELDS = (
    "assignmentId",
    "requestId",
    "assignedResponderId",
    "locationReference",
    "occupantCount",
    "mobilityAssistance",
    "medicalNeed",
    "equipmentRequirement",
    "acknowledgementDeadline",
    "state",
    # Per-stage transition timestamps (ISO-8601) for the timeline stepper.
    "acceptedAt",
    "enRouteAt",
    "onSceneAt",
    "completedAt",
    "declinedAt",
)


def _response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": _CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def _to_assignment(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: item.get(k) for k in _ASSIGNMENT_FIELDS}
    # DynamoDB returns numbers as Decimal; coerce occupantCount to int.
    if out.get("occupantCount") is not None:
        try:
            out["occupantCount"] = int(out["occupantCount"])
        except (TypeError, ValueError):
            out["occupantCount"] = 0
    return out


def _assignments_for(responder_id: str) -> list[dict[str, Any]]:
    """Every ASSIGNMENT item for one responder."""
    resp = _table.scan(
        FilterExpression=Attr("pk").begins_with("ASSIGNMENT#")
        & Attr("assignedResponderId").eq(responder_id)
    )
    return [_to_assignment(it) for it in resp.get("Items", [])]


def _all_assignments() -> list[dict[str, Any]]:
    """Every ASSIGNMENT item — the coordinator's active-dispatches view."""
    resp = _table.scan(FilterExpression=Attr("pk").begins_with("ASSIGNMENT#"))
    items = [_to_assignment(it) for it in resp.get("Items", [])]
    # Newest / most-active first: open states before completed/declined.
    order = {"AWAITING_ACK": 0, "ACCEPTED": 1, "EN_ROUTE": 2, "ON_SCENE": 3, "VERIFICATION": 4, "DECLINED": 5}
    items.sort(key=lambda a: order.get(str(a.get("state")), 9))
    return items


def _get_assignment(assignment_id: str) -> dict[str, Any] | None:
    resp = _table.get_item(Key={"pk": f"ASSIGNMENT#{assignment_id}", "sk": "META"})
    return resp.get("Item")


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """API Gateway proxy entry point for the responder surface."""
    event = event or {}
    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method")
        or "GET"
    ).upper()
    path = event.get("path") or event.get("resource") or ""
    params = event.get("pathParameters") or {}
    responder_id = params.get("responderId") or params.get("id") or ""

    if method == "OPTIONS":
        return _response(204, {})

    try:
        if method == "POST" and "respond" in path:
            assignment_id = params.get("assignmentId") or ""
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8")
            body = json.loads(raw) if isinstance(raw, str) else raw
            return _respond(responder_id, assignment_id, body.get("action"))

        # Coordinator's active-dispatches view: GET /assignments (all).
        if method == "GET" and "responders" not in path:
            return _response(200, _all_assignments())

        if method == "GET":
            return _response(200, _assignments_for(responder_id))

        return _response(404, {"ok": False, "reason": "not_found", "path": path})
    except Exception as exc:  # noqa: BLE001
        return _response(500, {"ok": False, "reason": "internal_error", "detail": str(exc)})


def _respond(responder_id: str, assignment_id: str, action: str | None) -> dict[str, Any]:
    """Advance one assignment's lifecycle by a responder action (Req 13.2/13.9)."""
    item = _get_assignment(assignment_id)
    if item is None:
        return _response(404, {"ok": False, "reason": "assignment_not_found"})
    if str(item.get("assignedResponderId")) != responder_id:
        return _response(403, {"ok": False, "reason": "not_your_assignment"})

    current = str(item.get("state", ""))
    permitted = _TRANSITIONS.get(current, {})
    if action not in permitted:
        return _response(
            409,
            {
                "ok": False,
                "reason": "invalid_transition",
                "attempted": action,
                "permitted": list(permitted.keys()),
            },
        )

    next_state = permitted[action]
    # Stamp the time of this transition so the UI can render a timeline.
    stamp_attr = _TIMESTAMP_ATTR.get(action)
    now_iso = datetime.now(timezone.utc).isoformat()
    if stamp_attr:
        _table.update_item(
            Key={"pk": f"ASSIGNMENT#{assignment_id}", "sk": "META"},
            UpdateExpression="SET #s = :ns, #ts = :now",
            ExpressionAttributeNames={"#s": "state", "#ts": stamp_attr},
            ExpressionAttributeValues={":ns": next_state, ":now": now_iso},
        )
    else:
        _table.update_item(
            Key={"pk": f"ASSIGNMENT#{assignment_id}", "sk": "META"},
            UpdateExpression="SET #s = :ns",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":ns": next_state},
        )
    return _response(
        200,
        {"ok": True, "assignmentId": assignment_id, "state": next_state, "at": now_iso},
    )
