"""Deterministic generator for seed/seed_dataset.json (design.md §3.9, Req 18.1, 18.2).

This script is committed alongside its output so the synthetic
responders/shelters/resident-points dataset is reproducible and auditable
rather than a black-box fixture, following the same convention as
seed/hazard_readings/_generate_fixtures.py. It is not imported by
application code — scripts/seed.sh (task 11.5) reads the committed
seed_dataset.json directly, never this generator.

Re-run with: ``python seed/_generate_seed_dataset.py``
(uses a fixed seed, so re-running reproduces byte-identical output).

Dataset shape (see seed/README.md for the full contract):
  - 14 responders, matching schemas.entities.Responder exactly, all
    AVAILABLE / active_assignment_count=0 at seed (a fresh seed starts every
    responder available and unassigned).
  - 3 shelters, matching schemas.entities.Shelter exactly, total_capacity
    summing to 120 (50/40/30 split, per design.md §3.9's own example split),
    available_capacity == total_capacity at seed (a fresh seed starts every
    shelter empty).
  - ~900 synthetic aggregate resident points along the monitored river
    reach, used only for aggregate affected-area occupant counts (Req 14.5)
    — never individually exposed, never carrying a name or contact detail
    (Req 18.1, 18.2).

All coordinates are drawn from the same lat/lon region already used by the
committed test fixtures for this entity set (tests/unit/test_state_store.py,
tests/unit/test_entities_schema.py): roughly 11.2-11.3 lat / 79.8-79.9 lon
(Kollidam, Tamil Nadu — matching the "Kollidam Ward-7 Neighbourhood Flood
Committee" framing in design.md's Overview), so this dataset stays
consistent with the entity fixtures already used elsewhere in the repo.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

# Allow running this script directly as well as importing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schemas.entities import Responder, Shelter  # noqa: E402  (see sys.path insert above)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEED = 20260829  # fixed seed for reproducibility (contest window start date)
OUTPUT_PATH = Path(__file__).resolve().parent / "seed_dataset.json"

# Region bounding box for all synthetic coordinates: Kollidam Ward-7 river
# reach, matching the region already used by tests/unit/test_entities_schema.py
# fixtures (11.23-11.24 lat / 79.84-79.85 lon). Widened slightly here so the
# generated points are spread across a plausible ward rather than a point.
LAT_MIN, LAT_MAX = 11.20, 11.30
LON_MIN, LON_MAX = 79.80, 79.90

RESPONDER_COUNT = 14
SHELTER_COUNT = 3
SHELTER_CAPACITY_SPLIT = [50, 40, 30]  # sums to 120, per design.md §3.9's own example
RESIDENT_POINT_COUNT = 900

# Ward-7's stated population is "~900 residents" (design.md Overview). Each
# resident point represents an aggregate household/cluster occupant count,
# not an individual resident record (Req 18.1, 18.2, 14.5 — aggregate-only).
# With 900 points averaging ~1 occupant each, the aggregate population
# implied by this dataset lands close to that stated ~900, while still
# allowing some points to represent small multi-occupant households (the
# occupant_count distribution below is weighted so most points are 1, a
# meaningful minority are 2-3, and a few are 4-6 — a plausible household-size
# mix for a river-ward setting).
OCCUPANT_COUNT_CHOICES = [1, 1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 6]

# Ward areas used for affected_areas / area_id labelling, consistent with the
# "Ward-7 South" / "Riverside Colony" style names already used in design.md's
# own example escalation templates (§3.4, §3.5).
AREAS = [
    "Ward-7 North",
    "Ward-7 South",
    "Riverside Colony",
    "Kanal Street",
    "Old Ferry Road",
]

EQUIPMENT_MIX: list[list[str]] = [
    ["boat"],
    ["4x4"],
    ["boat", "4x4"],
    ["none"],
]

SYNTHETIC_FIRST_NAMES = [
    "Karthik",
    "Meena",
    "Suresh",
    "Lakshmi",
    "Arun",
    "Priya",
    "Manoj",
    "Divya",
    "Ravi",
    "Kavya",
    "Senthil",
    "Anitha",
    "Vijay",
    "Deepa",
]
SYNTHETIC_LAST_INITIALS = ["R.", "K.", "S.", "M.", "N.", "T.", "V.", "P."]


def _rand_coord(rng: random.Random) -> tuple[float, float]:
    lat = round(rng.uniform(LAT_MIN, LAT_MAX), 5)
    lon = round(rng.uniform(LON_MIN, LON_MAX), 5)
    return (lat, lon)


def _build_responders(rng: random.Random) -> list[dict]:
    responders: list[dict] = []
    for i in range(RESPONDER_COUNT):
        name = f"{SYNTHETIC_FIRST_NAMES[i]} {SYNTHETIC_LAST_INITIALS[i % len(SYNTHETIC_LAST_INITIALS)]}"
        equipment = EQUIPMENT_MIX[i % len(EQUIPMENT_MIX)]
        responder = Responder(
            responder_id=f"resp-{i + 1:02d}",
            name=name,
            home_coords=_rand_coord(rng),
            equipment=list(equipment),
            availability_status="AVAILABLE",
            active_assignment_id=None,
            active_assignment_count=0,
        )
        responders.append(responder.model_dump())
    return responders


def _build_shelters(rng: random.Random) -> list[dict]:
    shelter_names = [
        "Ward-7 Community Hall",
        "Riverside Colony School",
        "Kanal Street Marriage Hall",
    ]
    shelters: list[dict] = []
    for i in range(SHELTER_COUNT):
        capacity = SHELTER_CAPACITY_SPLIT[i]
        shelter = Shelter(
            shelter_id=f"shelter-{i + 1}",
            name=shelter_names[i],
            coords=_rand_coord(rng),
            total_capacity=capacity,
            available_capacity=capacity,  # fresh seed: nobody placed yet
        )
        shelters.append(shelter.model_dump())
    return shelters


def _build_resident_points(rng: random.Random) -> list[dict]:
    points: list[dict] = []
    for i in range(RESIDENT_POINT_COUNT):
        area = AREAS[i % len(AREAS)]
        occupant_count = rng.choice(OCCUPANT_COUNT_CHOICES)
        points.append(
            {
                "point_id": f"rp-{i + 1:04d}",
                "area_id": area,
                "coords": list(_rand_coord(rng)),
                "occupant_count": occupant_count,
            }
        )
    return points


def build_dataset() -> dict:
    rng = random.Random(SEED)
    responders = _build_responders(rng)
    shelters = _build_shelters(rng)
    resident_points = _build_resident_points(rng)

    assert len(responders) == RESPONDER_COUNT
    assert len(shelters) == SHELTER_COUNT
    assert sum(s["total_capacity"] for s in shelters) == 120
    assert len(resident_points) == RESIDENT_POINT_COUNT

    return {
        "responders": responders,
        "shelters": shelters,
        "resident_points": resident_points,
    }


def main() -> None:
    dataset = build_dataset()
    OUTPUT_PATH.write_text(json.dumps(dataset, indent=2) + "\n", encoding="utf-8")
    total_occupants = sum(p["occupant_count"] for p in dataset["resident_points"])
    print(
        f"Wrote {OUTPUT_PATH} — "
        f"{len(dataset['responders'])} responders, "
        f"{len(dataset['shelters'])} shelters "
        f"(total_capacity={sum(s['total_capacity'] for s in dataset['shelters'])}), "
        f"{len(dataset['resident_points'])} resident points "
        f"(aggregate occupant total={total_occupants})."
    )


if __name__ == "__main__":
    main()
