#!/usr/bin/env python3
"""Reset the demo escalation state to a clean, presentable set.

1. Resolves every currently OPEN escalation (including any auto-generated
   "cannot resume" records), looping until the inbox is empty.
2. Creates a fresh, realistic set of demo escalations with the REAL 30-minute
   response deadline (policy default), so the Decision Inbox reflects genuine
   product behaviour for a live demo.

Run:  python scripts/demo_reset.py
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("THUNAI_STATE_TABLE", "thunai-state")
os.environ.setdefault("THUNAI_AUDIT_TABLE", "thunai-audit")
os.environ.setdefault("AWS_REGION", "us-east-1")

from datetime import datetime, timezone  # noqa: E402

from memory import state_store  # noqa: E402
from schemas.entities import Incident  # noqa: E402
from policy.rule_engine_rules import RULE_SET_VERSION  # noqa: E402
from surface.escalation_service import create_escalation, resolve_escalation  # noqa: E402


def _refresh_demo_incident() -> None:
    """(Re)write the demo EVACUATE incident with CURRENT readings + timestamps.

    Keeps the public Resident_Status_Page showing a live, non-stale flood: a
    fresh ``updated_at`` (so the reading is inside the 15-min staleness limit),
    all three hazard readings (incl. dam release), and per-area request counts.
    """
    now = datetime.now(timezone.utc).isoformat()
    incident = Incident(
        incident_id="incident-reach-7",
        river_reach_id="reach-7",
        severity_band="EVACUATE",
        status="OPEN",
        affected_areas=["Riverside Colony", "Kanal Street", "North Ward"],
        created_at=now,
        updated_at=now,
        last_readings={
            "river_level": {"value": 6.1, "unit": "m"},
            "rainfall_rate": {"value": 55, "unit": "mm/h"},
            "dam_release": {"value": 2850, "unit": "m3/s"},
            "area_request_counts": {
                "Riverside Colony": 12,
                "Kanal Street": 7,
                "North Ward": 3,
            },
        },
        triggered_rule_ids=["river_level_evacuate", "rainfall_rate_evacuate", "dam_release_evacuate"],
        rule_set_version=RULE_SET_VERSION,
    )
    state_store.put_incident(incident)
    print("  refreshed demo incident (current readings + counts).")

#: The real product default deadline (policy RESPONSE_DEADLINE_MINUTES).
DEMO_DEADLINE_MINUTES = 30

DEMO_ESCALATIONS = [
    {
        "idempotency_key": f"demo:evacuate:{int(time.time())}",
        "run_id": "demo",
        "escalation_type": "hazard_monitoring",
        "decision_summary": (
            "Send an evacuation advisory to Ward-7 riverside residents?"
        ),
        "reason": "The river is at 6.1 m and still rising with heavy rainfall (55 mm/h), "
        "past the evacuation threshold. A mass advisory needs your approval.",
        "stakes": "~450 residents in the riverside affected area.",
        "options": [
            {"option_id": "approve", "label": "Approve evacuation advisory"},
            {"option_id": "hold", "label": "Hold and keep monitoring"},
        ],
        "default_action": "hold",
        "response_deadline_minutes": DEMO_DEADLINE_MINUTES,
        "incident_id": "incident-reach-7",
    },
    {
        "idempotency_key": f"demo:boat:{int(time.time())}",
        "run_id": "demo",
        "escalation_type": "dispatch",
        "decision_summary": (
            "Dispatch a boat rescue to 2 residents needing mobility help at Riverside Lane?"
        ),
        "reason": "This commits a limited rescue boat to one location — an "
        "irreversible call that needs your approval before it goes out.",
        "stakes": "1 rescue boat, 2 residents, ETA 12 min.",
        "options": [
            {"option_id": "approve", "label": "Approve boat dispatch"},
            {"option_id": "decline", "label": "Decline, reassign"},
        ],
        "default_action": "hold",
        "response_deadline_minutes": DEMO_DEADLINE_MINUTES,
        "incident_id": "incident-reach-7",
    },
]


#: Cognito sub of the demo responder user (responder@thunai.demo).
_DEMO_RESPONDER_ID = os.environ.get(
    "THUNAI_DEMO_RESPONDER_ID", "24385488-3061-70d1-b6b8-91f813f7d8b8"
)


def _refresh_demo_assignment() -> None:
    """(Re)write a demo responder assignment with a fresh acknowledge deadline."""
    import boto3
    from datetime import timedelta

    table = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
        os.environ.get("THUNAI_STATE_TABLE", "thunai-state")
    )
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    table.put_item(
        Item={
            "pk": "ASSIGNMENT#REQ-2451",
            "sk": "META",
            "assignmentId": "REQ-2451",
            "requestId": "REQ-2451",
            "assignedResponderId": _DEMO_RESPONDER_ID,
            "locationReference": "Riverside Lane, near the bridge",
            "occupantCount": 2,
            "mobilityAssistance": True,
            "medicalNeed": False,
            "equipmentRequirement": "Rescue boat",
            "acknowledgementDeadline": deadline,
            "state": "AWAITING_ACK",
        }
    )
    print("  refreshed responder assignment REQ-2451.")


def _clear_assignments() -> None:
    """Remove all ASSIGNMENT items so the live handshake creates a fresh one."""
    import boto3

    table = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
        os.environ.get("THUNAI_STATE_TABLE", "thunai-state")
    )
    from boto3.dynamodb.conditions import Attr

    resp = table.scan(FilterExpression=Attr("pk").begins_with("ASSIGNMENT#"))
    for item in resp.get("Items", []):
        table.delete_item(Key={"pk": item["pk"], "sk": item.get("sk", "META")})
    print(f"  cleared {len(resp.get('Items', []))} assignment(s).")


def _drain_open(max_rounds: int = 6) -> int:
    """Resolve every OPEN escalation, looping to catch resume-failure records."""
    total = 0
    for _ in range(max_rounds):
        open_records = state_store.query_open_escalations()
        if not open_records:
            break
        for rec in open_records:
            opt = rec.options[0].option_id if rec.options else "hold"
            try:
                resolve_escalation(rec.escalation_id, opt, responding_human_id="demo-reset")
                total += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  (skip {rec.escalation_id}: {exc})")
        time.sleep(1)
    return total


def main() -> None:
    print("Draining open escalations…")
    drained = _drain_open()
    print(f"  resolved {drained} record(s).")

    print("Creating fresh 30-minute demo escalations…")
    for spec in DEMO_ESCALATIONS:
        r = create_escalation(**spec)
        print(f"  created {r.get('ok')}: {r.get('escalation_id')}  ({spec['decision_summary'][:40]}…)")

    print("Refreshing public flood status…")
    _refresh_demo_incident()

    # Clear any prior responder assignments so the live coordinator->responder
    # handshake (approving the dispatch escalation) is what creates one during
    # the demo. Set THUNAI_SEED_ASSIGNMENT=1 to pre-seed one instead.
    if os.environ.get("THUNAI_SEED_ASSIGNMENT") == "1":
        print("Seeding responder assignment…")
        _refresh_demo_assignment()
    else:
        print("Clearing responder assignments (created live on approval)…")
        _clear_assignments()

    remaining = state_store.query_open_escalations()
    print(f"Done. Inbox now shows {len(remaining)} open escalation(s), 30-min deadline.")


if __name__ == "__main__":
    main()
