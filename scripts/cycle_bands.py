#!/usr/bin/env python3
"""Cycle the public status band through NORMAL -> WATCH -> WARNING -> EVACUATE,
changing every N seconds, for a live demo of the resident status page.

Reuses the exact per-band readings and area counts from set_band.py so the
public page shows a consistent, escalating flood picture.

Usage:
    python scripts/cycle_bands.py                # 5s per band, one pass
    python scripts/cycle_bands.py --interval 3   # 3s per band
    python scripts/cycle_bands.py --loop         # keep looping until Ctrl-C
    python scripts/cycle_bands.py --loop --interval 5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3

BANDS = ["NORMAL", "WATCH", "WARNING", "EVACUATE"]

# band -> (river_level m, rainfall_rate mm/h, dam_release m3/s) — matches set_band.py
READINGS = {
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


def _table():
    return boto3.resource(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    ).Table(os.environ.get("THUNAI_STATE_TABLE", "thunai-state"))


def set_band(table, band: str) -> None:
    river, rain, dam = READINGS[band]
    now = datetime.now(timezone.utc).isoformat()
    areas = list(AREA_COUNTS[band].keys()) or (
        ["Riverside Colony"] if band != "NORMAL" else []
    )
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
            # GSI keys so the coordinator incident list also sees it.
            "gsi1pk": "INCIDENT_BY_SEVERITY",
            "gsi1sk": f"{BANDS.index(band)}#{now}",
            "triggered_rule_ids": [],
            "rule_set_version": "demo",
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cycle public status bands for a demo.")
    parser.add_argument("--interval", type=float, default=10.0, help="Seconds per band (default 10).")
    parser.add_argument("--loop", action="store_true", help="Keep cycling until Ctrl-C.")
    args = parser.parse_args()

    table = _table()
    print(
        f"Cycling bands every {args.interval:g}s "
        f"({'looping — Ctrl-C to stop' if args.loop else 'one pass'})…"
    )
    try:
        while True:
            for band in BANDS:
                set_band(table, band)
                river, rain, dam = READINGS[band]
                print(f"  {band:9s}  river {river}m · rain {rain}mm/h · dam {dam}m3/s")
                time.sleep(args.interval)
            if not args.loop:
                break
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)

    print("Done. Refresh the public status page to watch it change.")


if __name__ == "__main__":
    main()
