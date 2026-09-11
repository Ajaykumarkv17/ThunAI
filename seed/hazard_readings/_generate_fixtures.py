"""Deterministic generator for seed/hazard_readings/*.jsonl (design.md §3.9, Req 3.1, 17.3, 19.5).

This script is committed alongside its output so the 35-day synthetic hazard
series is reproducible and auditable rather than a black-box fixture. It is
not imported by application code — `SyntheticSensorProvider` (task 10.1)
reads the committed `.jsonl` files directly, never this generator.

Re-run with: ``python seed/hazard_readings/_generate_fixtures.py``
(uses a fixed seed, so re-running reproduces byte-identical output).

Scenario shape (see seed/hazard_readings/README.md for the full contract):
  - Days 1-30 (hours 0-719):   baseline period, mild plausible fluctuation.
  - Days 31-35 (hours 720-839): rising-river demo scenario. river_level ramps
    from baseline into WATCH -> WARNING -> EVACUATE by the final reading;
    rainfall_rate and dam_release ramp up in step as physically plausible
    drivers/contributors.
  - Frequency: hourly, 24 readings/day, 840 readings per reading type total.
  - Timestamps: ISO-8601 with a fixed +05:30 offset (IST), matching the
    convention already used in schemas/entities.py test fixtures.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running this script directly (``python
# seed/hazard_readings/_generate_fixtures.py``) as well as importing it, by
# ensuring the repo root is on sys.path so ``policy`` resolves either way.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from policy.rule_engine_rules import THRESHOLDS as REAL_THRESHOLDS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=IST)
HOURS_PER_DAY = 24
BASELINE_DAYS = 30
RISING_DAYS = 5
TOTAL_DAYS = BASELINE_DAYS + RISING_DAYS  # 35
BASELINE_HOURS = BASELINE_DAYS * HOURS_PER_DAY  # 720
RISING_HOURS = RISING_DAYS * HOURS_PER_DAY  # 120
TOTAL_HOURS = TOTAL_DAYS * HOURS_PER_DAY  # 840

# Real severity thresholds, imported directly from
# policy/rule_engine_rules.py::THRESHOLDS (task 3.2), used to make the
# rising scenario cross them by construction with a ~10% safety margin above
# each EVACUATE threshold.
REAL_EVACUATE_THRESHOLDS = {
    reading_type: next(
        rule.threshold for rule in spec.rules_ascending if rule.band == "EVACUATE"
    )
    for reading_type, spec in REAL_THRESHOLDS.items()
}
# {"river_level": 5.0, "rainfall_rate": 50.0, "dam_release": 2500.0}

_MARGIN = 1.10  # ~10% above the real EVACUATE threshold, per reading type

RIVER_LEVEL_END = round(REAL_EVACUATE_THRESHOLDS["river_level"] * _MARGIN, 1)  # 5.5 m
RAINFALL_RATE_END = round(REAL_EVACUATE_THRESHOLDS["rainfall_rate"] * _MARGIN, 1)  # 55.0 mm/h
DAM_RELEASE_END = round(REAL_EVACUATE_THRESHOLDS["dam_release"] * _MARGIN, 1)  # 2750.0 m3/s

SEED = 20260801  # fixed seed -> byte-identical regeneration


def _timestamps() -> list[str]:
    return [(START + timedelta(hours=i)).isoformat() for i in range(TOTAL_HOURS)]


def _baseline_river_level(rng: random.Random, hour_index: int) -> float:
    import math

    # Diurnal oscillation in a 1.8-2.4 m band, centre 2.1 m, plus small noise.
    diurnal = 0.3 * math.sin(2 * math.pi * (hour_index % 24) / 24)
    noise = rng.uniform(-0.05, 0.05)
    return round(2.1 + diurnal + noise, 3)


def _baseline_rainfall_rate(rng: random.Random) -> float:
    # Mostly low, with occasional light-rain bursts (~15% of hours).
    if rng.random() < 0.15:
        return round(rng.uniform(1.0, 8.0), 2)
    return round(rng.uniform(0.0, 0.5), 2)


def _baseline_dam_release(rng: random.Random) -> float:
    # Steady with small variation around 55 m3/s.
    return round(55.0 + rng.uniform(-3.0, 3.0), 2)


def _rising_series(
    rng: random.Random,
    start_value: float,
    end_value: float,
    noise_span: float,
    floor_tolerance: float,
) -> list[float]:
    """Monotonic-ish ramp from start_value to end_value over RISING_HOURS steps.

    Uses an accelerating curve (exponent 0.8) so the rise is gentle at first
    and steeper approaching the final EVACUATE-crossing reading, with small
    random noise and a floor that permits minor natural dips without letting
    the series drift far from the intended trend.
    """
    values: list[float] = []
    previous = start_value
    for i in range(RISING_HOURS):
        frac = i / (RISING_HOURS - 1)
        target = start_value + (end_value - start_value) * (frac**0.8)
        noisy = target + rng.uniform(-noise_span, noise_span)
        # Allow a small natural backslide but keep the overall trend rising.
        value = max(noisy, previous - floor_tolerance)
        value = round(value, 3)
        values.append(value)
        previous = value
    # Force the final reading to be unambiguously above end_value's intent.
    values[-1] = round(end_value, 3)
    return values


def generate() -> dict[str, list[dict]]:
    rng = random.Random(SEED)
    timestamps = _timestamps()

    river_level_baseline = [_baseline_river_level(rng, i) for i in range(BASELINE_HOURS)]
    rainfall_rate_baseline = [_baseline_rainfall_rate(rng) for _ in range(BASELINE_HOURS)]
    dam_release_baseline = [_baseline_dam_release(rng) for _ in range(BASELINE_HOURS)]

    river_level_rising = _rising_series(
        rng, river_level_baseline[-1], RIVER_LEVEL_END, noise_span=0.02, floor_tolerance=0.015
    )
    rainfall_rate_rising = _rising_series(
        rng, max(rainfall_rate_baseline[-1], 1.0), RAINFALL_RATE_END, noise_span=0.4, floor_tolerance=0.3
    )
    dam_release_rising = _rising_series(
        rng, dam_release_baseline[-1], DAM_RELEASE_END, noise_span=0.8, floor_tolerance=0.6
    )

    series = {
        "river_level": river_level_baseline + river_level_rising,
        "rainfall_rate": rainfall_rate_baseline + rainfall_rate_rising,
        "dam_release": dam_release_baseline + dam_release_rising,
    }
    units = {"river_level": "m", "rainfall_rate": "mm/h", "dam_release": "m3/s"}
    prefixes = {"river_level": "rl", "rainfall_rate": "rf", "dam_release": "dr"}

    rows_by_type: dict[str, list[dict]] = {}
    for reading_type, values in series.items():
        assert len(values) == TOTAL_HOURS
        rows = []
        for i, (ts, value) in enumerate(zip(timestamps, values)):
            rows.append(
                {
                    "reading_id": f"{prefixes[reading_type]}-{i:05d}",
                    "reading_type": reading_type,
                    "timestamp": ts,
                    "value": value,
                    "unit": units[reading_type],
                }
            )
        rows_by_type[reading_type] = rows
    return rows_by_type


def write(rows_by_type: dict[str, list[dict]]) -> None:
    import pathlib

    out_dir = pathlib.Path(__file__).parent
    for reading_type, rows in rows_by_type.items():
        path = out_dir / f"{reading_type}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        print(f"wrote {len(rows)} rows to {path}")


if __name__ == "__main__":
    write(generate())
