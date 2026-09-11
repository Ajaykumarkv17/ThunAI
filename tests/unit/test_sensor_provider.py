"""Unit tests for integrations/sensor_provider.py (task 10.1).

Validates: Requirements 19.5, 3.1; Design §3.7.

Exercises ``SyntheticSensorProvider`` against the real committed
``seed/hazard_readings/*.jsonl`` fixtures (no fakes needed — task 11.2
already validated those files) and ``get_sensor_provider()``'s selection
logic. ``LiveSensorProvider`` is asserted only for correct type selection —
its methods require a live AgentCore Gateway target and are out of scope for
unit testing here (per the task instructions).
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from integrations.sensor_provider import (
    LiveSensorProvider,
    SyntheticSensorProvider,
    get_sensor_provider,
)

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[2] / "seed" / "hazard_readings"
IST = timezone(timedelta(hours=5, minutes=30))
SERIES_START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=IST)
SERIES_END = datetime(2026, 9, 4, 23, 0, 0, tzinfo=IST)  # 35 days * 24h - 1h, per README


@pytest.fixture()
def provider() -> SyntheticSensorProvider:
    return SyntheticSensorProvider(fixture_path=FIXTURE_DIR)


def test_loads_real_committed_fixture_files(provider: SyntheticSensorProvider) -> None:
    """All three reading types load with the full 840-row series (README "Row count")."""
    for reading_type in ("river_level", "rainfall_rate", "dam_release"):
        rows = provider._readings[reading_type]
        assert len(rows) == 35 * 24
        # Strictly increasing timestamps, per README "Reading frequency".
        for earlier, later in zip(rows, rows[1:]):
            assert later["timestamp"] - earlier["timestamp"] == timedelta(hours=1)


def test_latest_reading_within_window_matches_expected_row(provider: SyntheticSensorProvider) -> None:
    """Requesting an 'as of' timestamp mid-series returns the reading at exactly that hour."""
    as_of = SERIES_START + timedelta(days=10, hours=3)
    reading = provider.get_latest_reading("river_level", as_of)
    assert reading is not None
    assert reading["timestamp"] == as_of
    assert reading["unit"] == "m"
    assert isinstance(reading["value"], float)


def test_latest_reading_between_hourly_ticks_replays_prior_hour(provider: SyntheticSensorProvider) -> None:
    """An 'as of' timestamp between two hourly rows replays the latest one at or before it."""
    as_of = SERIES_START + timedelta(days=10, hours=3, minutes=45)
    reading = provider.get_latest_reading("river_level", as_of)
    assert reading is not None
    assert reading["timestamp"] == SERIES_START + timedelta(days=10, hours=3)


def test_latest_reading_at_final_series_timestamp(provider: SyntheticSensorProvider) -> None:
    """An 'as of' timestamp at/after the series end returns the final (rising-scenario) reading."""
    reading = provider.get_latest_reading("river_level", SERIES_END + timedelta(days=1))
    assert reading is not None
    assert reading["timestamp"] == SERIES_END
    assert reading["value"] >= 5.0  # EVACUATE threshold, per README threshold cross-check


def test_baseline_series_length_after_day_30(provider: SyntheticSensorProvider) -> None:
    """A baseline request positioned after day 30 returns 30 days worth of hourly readings."""
    as_of = SERIES_START + timedelta(days=32)  # well past the 30-day baseline window
    baseline = provider.get_baseline_series("river_level", as_of, days=30)
    assert len(baseline) == 30 * 24
    # Window is (as_of - 30d, as_of] — ascending order, last entry at/near as_of.
    assert baseline[0]["timestamp"] > as_of - timedelta(days=30)
    assert baseline[-1]["timestamp"] <= as_of
    for earlier, later in zip(baseline, baseline[1:]):
        assert earlier["timestamp"] < later["timestamp"]


def test_baseline_series_shorter_when_less_than_30_days_of_history(
    provider: SyntheticSensorProvider,
) -> None:
    """A baseline request positioned only 5 days into the series returns only those 5 days."""
    as_of = SERIES_START + timedelta(days=5)
    baseline = provider.get_baseline_series("river_level", as_of, days=30)
    # Window is (as_of - 30d, as_of]; the series only starts at SERIES_START, so every
    # row from SERIES_START through as_of inclusive falls in the window: 5*24 + 1 rows
    # (the +1 is the SERIES_START row itself, which is still > window_start here).
    assert len(baseline) == 5 * 24 + 1


def test_as_of_before_any_data_returns_none_and_empty_baseline(
    provider: SyntheticSensorProvider,
) -> None:
    """An 'as of' timestamp before the series start yields no latest reading and an empty baseline.

    Chosen behaviour (documented on SensorProvider Protocol): None / [] rather
    than raising, so callers (Req 3.6, 3.8, 3.9) can mark the reading type
    unavailable and continue rather than fail the whole evaluation.
    """
    as_of = SERIES_START - timedelta(days=1)
    assert provider.get_latest_reading("river_level", as_of) is None
    assert provider.get_baseline_series("river_level", as_of, days=30) == []


def test_unknown_reading_type_returns_none_and_empty(provider: SyntheticSensorProvider) -> None:
    """An unrecognised reading type behaves as unavailable rather than raising."""
    assert provider.get_latest_reading("wind_speed", SERIES_START) is None
    assert provider.get_baseline_series("wind_speed", SERIES_START) == []


def test_missing_fixture_directory_yields_empty_series(tmp_path: pathlib.Path) -> None:
    """Constructing against a directory with no fixture files yields empty series, not an error."""
    empty_provider = SyntheticSensorProvider(fixture_path=tmp_path)
    assert empty_provider.get_latest_reading("river_level", SERIES_START) is None


@pytest.mark.parametrize("flag_value", ["1", None])
def test_factory_selects_synthetic_provider_by_default(monkeypatch: pytest.MonkeyPatch, flag_value: str | None) -> None:
    if flag_value is None:
        monkeypatch.delenv("SYNTHETIC_SENSORS", raising=False)
    else:
        monkeypatch.setenv("SYNTHETIC_SENSORS", flag_value)
    monkeypatch.setenv("SENSOR_FIXTURE_PATH", str(FIXTURE_DIR))
    provider = get_sensor_provider()
    assert isinstance(provider, SyntheticSensorProvider)


def test_factory_selects_live_provider_when_flag_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHETIC_SENSORS", "0")
    monkeypatch.setenv("SENSOR_GATEWAY_TARGET", "sensor-live")
    provider = get_sensor_provider()
    assert isinstance(provider, LiveSensorProvider)
