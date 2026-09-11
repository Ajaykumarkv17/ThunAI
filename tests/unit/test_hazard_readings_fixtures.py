"""Validation tests for seed/hazard_readings/*.jsonl (task 11.2).

Validates: Requirements 3.1, 17.3, 19.5; Design §3.9.

These tests load the committed synthetic hazard-reading fixtures and assert
the structural and scenario invariants described in
seed/hazard_readings/README.md: exactly 35 days of hourly coverage, strictly
increasing/evenly-spaced timestamps, no missing/null readings, and a day
31-35 river_level trajectory that crosses into the WATCH/WARNING/EVACUATE
bands by the final reading.

policy/rule_engine_rules.py (task 3.2) is now implemented, so the EVACUATE
threshold cross-check imports the real THRESHOLDS table from that module
directly instead of hardcoding an assumed value. See the "Threshold
cross-check" section in seed/hazard_readings/README.md.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta

import pytest

from policy.rule_engine_rules import THRESHOLDS

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[2] / "seed" / "hazard_readings"
READING_TYPES = ("river_level", "rainfall_rate", "dam_release")
TOTAL_DAYS = 35
HOURS_PER_DAY = 24
EXPECTED_ROW_COUNT = TOTAL_DAYS * HOURS_PER_DAY  # 840
RISING_START_HOUR = 30 * HOURS_PER_DAY  # day 31 begins at hour index 720


def _evacuate_threshold(reading_type: str) -> float:
    spec = THRESHOLDS[reading_type]
    return next(rule.threshold for rule in spec.rules_ascending if rule.band == "EVACUATE")


RIVER_LEVEL_EVACUATE_M = _evacuate_threshold("river_level")
RAINFALL_RATE_EVACUATE_MM_H = _evacuate_threshold("rainfall_rate")
DAM_RELEASE_EVACUATE_M3_S = _evacuate_threshold("dam_release")


def _load_jsonl(reading_type: str) -> list[dict]:
    path = FIXTURE_DIR / f"{reading_type}.jsonl"
    assert path.exists(), f"missing fixture file: {path}"
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                pytest.fail(f"{path.name}:{line_no} is not valid JSON: {exc}")
    return rows


@pytest.mark.parametrize("reading_type", READING_TYPES)
def test_row_count_covers_exactly_35_days(reading_type: str) -> None:
    rows = _load_jsonl(reading_type)
    assert len(rows) == EXPECTED_ROW_COUNT, (
        f"{reading_type}.jsonl has {len(rows)} rows, expected "
        f"{EXPECTED_ROW_COUNT} (35 days x 24 hourly readings)"
    )


@pytest.mark.parametrize("reading_type", READING_TYPES)
def test_no_missing_or_null_readings(reading_type: str) -> None:
    rows = _load_jsonl(reading_type)
    required_fields = ("reading_id", "reading_type", "timestamp", "value", "unit")
    for i, row in enumerate(rows):
        for field in required_fields:
            assert field in row, f"{reading_type}.jsonl row {i} missing field '{field}'"
            assert row[field] is not None, f"{reading_type}.jsonl row {i} has null '{field}'"
        assert isinstance(row["value"], (int, float)), (
            f"{reading_type}.jsonl row {i} 'value' is not numeric: {row['value']!r}"
        )
        assert row["reading_type"] == reading_type


@pytest.mark.parametrize("reading_type", READING_TYPES)
def test_timestamps_strictly_increasing_and_hourly(reading_type: str) -> None:
    rows = _load_jsonl(reading_type)
    timestamps = [datetime.fromisoformat(row["timestamp"]) for row in rows]

    for i in range(1, len(timestamps)):
        delta = timestamps[i] - timestamps[i - 1]
        assert delta == timedelta(hours=1), (
            f"{reading_type}.jsonl: gap between row {i - 1} and row {i} is "
            f"{delta}, expected exactly 1 hour"
        )

    # Confirm the timezone offset convention (+05:30, IST) used elsewhere in
    # the codebase (schemas/entities.py test fixtures).
    for i, row in enumerate(rows):
        assert row["timestamp"].endswith("+05:30"), (
            f"{reading_type}.jsonl row {i} timestamp missing '+05:30' offset: "
            f"{row['timestamp']!r}"
        )

    # Exactly 35 days of coverage, start to end inclusive.
    span = timestamps[-1] - timestamps[0]
    assert span == timedelta(days=TOTAL_DAYS - 1, hours=HOURS_PER_DAY - 1), (
        f"{reading_type}.jsonl spans {span}, expected "
        f"{timedelta(days=TOTAL_DAYS - 1, hours=HOURS_PER_DAY - 1)} "
        f"(35 days of hourly coverage start-to-end inclusive)"
    )


@pytest.mark.parametrize("reading_type", READING_TYPES)
def test_reading_ids_unique_within_file(reading_type: str) -> None:
    rows = _load_jsonl(reading_type)
    ids = [row["reading_id"] for row in rows]
    assert len(ids) == len(set(ids)), f"{reading_type}.jsonl has duplicate reading_id values"


def test_baseline_river_level_stays_well_under_watch_threshold() -> None:
    """Days 1-30 should fluctuate mildly and stay well clear of any WATCH band,
    so the 30-day baseline used by Monitor_Agent's anomaly-ratio calculation
    (Req 3.2, 3.9, 17.3) reflects a genuine steady state.
    """
    rows = _load_jsonl("river_level")
    baseline_values = [row["value"] for row in rows[:RISING_START_HOUR]]
    assert len(baseline_values) == RISING_START_HOUR
    assert max(baseline_values) < 3.0, (
        f"baseline river_level max {max(baseline_values)} m is not well under "
        "the assumed WATCH threshold"
    )
    assert min(baseline_values) > 0.0


def test_days_31_to_35_river_level_trends_upward_crossing_evacuate() -> None:
    """The day 31-35 rising-river demo scenario must exercise the escalation
    path end-to-end: river_level should trend upward and the final reading
    must exceed the real EVACUATE threshold from policy/rule_engine_rules.py.
    """
    rows = _load_jsonl("river_level")
    rising_values = [row["value"] for row in rows[RISING_START_HOUR:]]
    expected_rising_hours = 5 * HOURS_PER_DAY
    assert len(rising_values) == expected_rising_hours

    # Overall trend is upward: the final reading is well above the first
    # rising-period reading.
    assert rising_values[-1] > rising_values[0]

    # "Monotonic-ish": allow minor natural dips but require the overall
    # trajectory to be non-decreasing across most consecutive steps.
    increases = sum(1 for a, b in zip(rising_values, rising_values[1:]) if b >= a)
    assert increases / (len(rising_values) - 1) > 0.8, (
        "rising-river scenario is not sufficiently monotonic (too many "
        "consecutive decreases for a physically plausible rising trend)"
    )

    # Final reading crosses into EVACUATE.
    assert rising_values[-1] > RIVER_LEVEL_EVACUATE_M, (
        f"final river_level reading {rising_values[-1]} m does not exceed the "
        f"real EVACUATE threshold {RIVER_LEVEL_EVACUATE_M} m"
    )


def test_days_31_to_35_rainfall_and_dam_release_also_rise() -> None:
    """Rainfall rate and dam release should rise in step with river level,
    since rising rainfall is a plausible physical driver of a rising river
    and increased dam release is a plausible contributor to the scenario.
    """
    for reading_type in ("rainfall_rate", "dam_release"):
        rows = _load_jsonl(reading_type)
        rising_values = [row["value"] for row in rows[RISING_START_HOUR:]]
        assert rising_values[-1] > rising_values[0], (
            f"{reading_type} does not rise over the day 31-35 demo scenario"
        )


def test_days_31_to_35_rainfall_and_dam_release_cross_real_evacuate() -> None:
    """rainfall_rate and dam_release's final rising-scenario readings must
    exceed the real EVACUATE thresholds from policy/rule_engine_rules.py,
    mirroring the river_level cross-check above.
    """
    expected = {
        "rainfall_rate": RAINFALL_RATE_EVACUATE_MM_H,
        "dam_release": DAM_RELEASE_EVACUATE_M3_S,
    }
    for reading_type, evacuate_threshold in expected.items():
        rows = _load_jsonl(reading_type)
        rising_values = [row["value"] for row in rows[RISING_START_HOUR:]]
        assert rising_values[-1] > evacuate_threshold, (
            f"final {reading_type} reading {rising_values[-1]} does not exceed "
            f"the real EVACUATE threshold {evacuate_threshold}"
        )
