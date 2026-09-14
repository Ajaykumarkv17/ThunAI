"""Public Resident_Status_Page read API (Req 14.1-14.9).

Serves ``GET /public/status`` — the aggregate, non-identifying public snapshot
the unauthenticated Resident_Status_Page renders. Every value is aggregate by
construction (severity band, per-area request counts, shelter capacity, the
published rule set); no resident name, contact, or household location is ever
returned.

Assembled from State_Store (incidents + shelters) and the static published
rule set (``policy/rule_engine_rules.py``). Returns the exact camelCase shape
``frontend/src/resident/types.ts::ResidentStatus`` expects.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from memory import state_store
from policy.rule_engine_rules import STALENESS_LIMIT_S, THRESHOLDS

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type": "application/json",
}

_SEVERITY_ORDER = ["NORMAL", "WATCH", "WARNING", "EVACUATE"]

#: When set, seeded demo incidents are always presented as freshly-read so a
#: static record does not age past the staleness limit mid-demo. Leave unset in
#: a real deployment so genuine reading staleness is surfaced (Req 14.8).
_DEMO_MODE = os.environ.get("THUNAI_DEMO_MODE", "") == "1"

#: The reach the public page summarises when no incident names another.
_DEFAULT_INCIDENT_ID = "incident-reach-7"

#: Unit labels shown publicly, per reading type.
_UNIT_LABEL = {"river_level": "m", "rainfall_rate": "mm/h", "dam_release": "m³/s"}
_THRESHOLD_LABEL = {
    "river_level": "River level",
    "rainfall_rate": "Rainfall rate",
    "dam_release": "Dam release",
}


def _response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": _CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def _open_request_count(incident_id: str) -> int:
    """Live count of open emergency requests attributed to this incident."""
    if not incident_id:
        return 0
    total = 0
    for state in ("NEW", "TRIAGED", "ASSIGNED", "IN_PROGRESS"):
        try:
            for req in state_store.query_requests_by_state(state):
                if str(req.get("incident_id") or "") == incident_id:
                    total += 1
        except Exception:  # noqa: BLE001 - a read failure must not 500 the page
            continue
    return total


def _reading_levels(last_readings: dict[str, Any]) -> list[dict[str, Any]]:
    """Per reading type: the current value vs the EVACUATE threshold (plainly).

    This is what a resident actually wants to see — "is the river above the
    danger line right now?" — rather than the internal rule-set dump.
    """
    levels: list[dict[str, Any]] = []
    for reading_type, spec in THRESHOLDS.items():
        evac = next(
            (r for r in spec.rules_ascending if r.band == "EVACUATE"),
            spec.rules_ascending[-1] if spec.rules_ascending else None,
        )
        if evac is None:
            continue
        unit = _UNIT_LABEL.get(reading_type, spec.unit)
        reading = last_readings.get(reading_type) if isinstance(last_readings, dict) else None
        current = None
        if isinstance(reading, dict):
            current = reading.get("value")
        # Coerce to float for the threshold comparison (DynamoDB may return
        # Decimal, and demo data may carry strings) — never crash on a bad type.
        current_num = None
        if current is not None:
            try:
                current_num = float(current)
            except (TypeError, ValueError):
                current_num = None
        levels.append(
            {
                "label": _THRESHOLD_LABEL.get(reading_type, reading_type),
                "currentValue": current_num if current_num is not None else current,
                "thresholdValue": evac.threshold,
                "unit": unit,
                "exceeded": (current_num is not None and current_num >= evac.threshold),
            }
        )
    return levels


def _build_status() -> dict[str, Any]:
    """Assemble the public ResidentStatus snapshot from State_Store."""
    now = datetime.now(timezone.utc)

    # Severity + affected areas: prefer the most severe open incident.
    severity = "NORMAL"
    affected_areas: list[dict[str, Any]] = []
    reading_ts = now.isoformat()
    reading_stale = False

    try:
        incidents = state_store.query_incidents_by_severity()
    except Exception:  # noqa: BLE001 - a read failure must not 500 the public page
        incidents = []

    top = incidents[0] if incidents else None
    if top is None:
        try:
            top = state_store.get_incident(_DEFAULT_INCIDENT_ID)
        except Exception:  # noqa: BLE001
            top = None

    last_readings: dict[str, Any] = {}
    if top is not None:
        severity = top.severity_band
        reading_ts = getattr(top, "updated_at", None) or reading_ts
        last_readings = getattr(top, "last_readings", {}) or {}
        # Demo mode: present the reading as current so a static seeded incident
        # does not age past the staleness limit during a live demo. Set
        # THUNAI_DEMO_MODE=1 on the resident Lambda for this behaviour; unset in
        # a real deployment so genuine staleness is surfaced honestly (Req 14.8).
        if _DEMO_MODE:
            reading_ts = now.isoformat()
        # Per-area request counts: prefer a counts map stored on the incident's
        # last_readings ("area_request_counts"); otherwise fall back to the
        # live open-request tally attributed to this incident, split evenly.
        area_counts = {}
        if isinstance(last_readings, dict):
            area_counts = last_readings.get("area_request_counts") or {}
        total_requests = _open_request_count(getattr(top, "incident_id", ""))
        areas = top.affected_areas or []
        affected_areas = []
        for idx, area in enumerate(areas):
            if area in area_counts:
                count = int(area_counts[area])
            elif areas:
                # Spread the live total across areas (first area gets remainder).
                base = total_requests // len(areas)
                count = base + (total_requests % len(areas) if idx == 0 else 0)
            else:
                count = 0
            affected_areas.append({"area": area, "requestCount": count})
        # Staleness: reading older than the configured limit.
        try:
            age = (now - datetime.fromisoformat(reading_ts)).total_seconds()
            reading_stale = age > STALENESS_LIMIT_S
        except (TypeError, ValueError):
            reading_stale = False

    # Shelters: aggregate capacity only (no PII).
    shelters_out: list[dict[str, Any]] = []
    try:
        for s in state_store.query_shelters():
            shelters_out.append(
                {
                    "shelterId": s.shelter_id,
                    "name": s.name,
                    "location": s.name,
                    "availableCapacity": max(0, s.available_capacity),
                }
            )
    except Exception:  # noqa: BLE001
        shelters_out = []

    levels = _reading_levels(last_readings)

    return {
        "severity": severity if severity in _SEVERITY_ORDER else "NORMAL",
        "severityReason": _severity_reason(severity, levels),
        "affectedAreas": affected_areas,
        "shelters": shelters_out,
        "levels": levels,
        "readingTimestamp": reading_ts,
        "readingStale": reading_stale,
    }


def _severity_reason(severity: str, levels: list[dict[str, Any]]) -> str:
    """A factual, deterministic one-line reason for the current band.

    Built mechanically from which readings crossed their danger threshold — it
    is NEVER model-generated text (the public page shows only persisted facts).
    Empty when NORMAL or when no reading is above threshold.
    """
    if severity == "NORMAL":
        return ""
    exceeded = [lvl for lvl in levels if lvl.get("exceeded")]
    if not exceeded:
        return ""
    parts = [
        f"{lvl['label']} ({lvl['currentValue']} {lvl['unit']})"
        for lvl in exceeded
        if lvl.get("currentValue") is not None
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        subject = parts[0]
        verb = "has"
    else:
        subject = ", ".join(parts[:-1]) + f" and {parts[-1]}"
        verb = "have"
    return f"{subject} {verb} exceeded the danger threshold."


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """API Gateway proxy entry point for the public resident status.

    Routes:
        OPTIONS *          -> CORS preflight (204)
        GET /public/status -> the aggregate ResidentStatus snapshot
    """
    event = event or {}
    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method")
        or "GET"
    ).upper()

    if method == "OPTIONS":
        return _response(204, {})

    try:
        return _response(200, _build_status())
    except Exception as exc:  # noqa: BLE001 - never 502 the public page
        return _response(500, {"error": "internal_error", "detail": str(exc)})
