#!/usr/bin/env python3
"""Set the demo incident to a given severity band, for showing the public page
across NORMAL / WATCH / WARNING / EVACUATE.

Usage:  python scripts/set_band.py WATCH
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import boto3

BANDS = {
    # band -> (river_level, rainfall_rate, dam_release) illustrative current values
    "NORMAL": (2.6, 6.0, 380),
    "WATCH": (3.8, 24.0, 1200),
    "WARNING": (4.5, 38.0, 1950),
    "EVACUATE": (6.1, 55.0, 2850),
}

AREA_COUNTS = {
    "NORMAL": {},
    "WATCH": {"Riverside Colony": 2},
    "WARNING": {"Riverside Colony": 6, "Kanal Street": 3},
    "EVACUATE": {"Riverside Colony": 12, "Kanal Street": 7, "North Ward": 3},
}


def main() -> None:
    band = (sys.argv[1] if len(sys.argv) > 1 else "EVACUATE").upper()
    if band not in BANDS:
        print(f"Unknown band {band!r}. Choose one of {list(BANDS)}.")
        raise SystemExit(1)

    river, rain, dam = BANDS[band]
    now = datetime.now(timezone.utc).isoformat()
    table = boto3.resource(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    ).Table(os.environ.get("THUNAI_STATE_TABLE", "thunai-state"))

    areas = list(AREA_COUNTS[band].keys()) or (["Riverside Colony"] if band != "NORMAL" else [])
    table.put_item(
        Item={
            "pk": "INCIDENT#incident-reach-7",
            "sk": "META",
            "incident_id": "incident-reach-7",
            "river_reach_id": "reach-7",
            "severity_band": band,
            "status": "OPEN",
            "affected_areas": areas,
            "created_at": now,
            "updated_at": now,
            "last_readings": {
                "river_level": {"value": Decimal(str(river)), "unit": "m"},
                "rainfall_rate": {"value": Decimal(str(rain)), "unit": "mm/h"},
                "dam_release": {"value": Decimal(str(dam)), "unit": "m3/s"},
                "area_request_counts": AREA_COUNTS[band],
            },
            "triggered_rule_ids": [],
            "rule_set_version": "demo",
        }
    )
    print(f"Set incident to {band}: river {river}m, rain {rain}mm/h, dam {dam}m3/s.")


if __name__ == "__main__":
    main()
