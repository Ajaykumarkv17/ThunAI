"""Unit tests for tools/sensor_tools.py (task 12.1).

Validates: Requirements 3.1; Design §2.1, §3.7.

Exercises `get_river_level` / `get_rainfall_rate` / `get_dam_release` over
the real committed `seed/hazard_readings/*.jsonl` fixtures via
`SyntheticSensorProvider` (SYNTHETIC_SENSORS=1, the default) — no fakes
needed. Each `@tool`-decorated function is called through its `.func`
underlying callable so tests exercise the plain Python logic directly
(matching the pattern used across this codebase's other `@tool`/pure-logic
unit tests, e.g. `test_rule_engine_rules.py`).
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from tools.sensor_tools import get_dam_release, get_rainfall_rate, get_river_level

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[2] / "seed" / "hazard_readings"
IST = timezone(timedelta(hours=5, minutes=30))
SERIES_START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=IST)
SERIES_END = datetime(2026, 9, 4, 23, 0, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def _synthetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHETIC_SENSORS", "1")
    monkeypatch.setenv("SENSOR_FIXTURE_PATH", str(FIXTURE_DIR))


def _call(tool_fn, **kwargs):
    """Invoke a `@tool`-decorated function directly with plain keyword
    arguments. Strands' `DecoratedFunctionTool.__call__` supports this direct
    invocation style (confirmed interactively against the installed
    `strands-agents==1.55.0`), so tests exercise the tool exactly as an
    agent's tool executor would, without needing a live `Agent`/model call.
    """
    return tool_fn(**kwargs)


def test_get_river_level_returns_available_reading_mid_series() -> None:
    as_of = (SERIES_START + timedelta(days=10, hours=3)).isoformat()
    result = _call(get_river_level, as_of=as_of)
    assert result["reading_type"] == "river_level"
    assert result["available"] is True
    assert result["unit"] == "m"
    assert isinstance(result["value"], float)
    assert result["unavailable_reason"] is None
    assert result["baseline"] is None


def test_get_rainfall_rate_returns_available_reading_at_series_end() -> None:
    result = _call(get_rainfall_rate, as_of=SERIES_END.isoformat())
    assert result["available"] is True
    assert result["unit"] == "mm/h"
    assert result["value"] >= 50.0  # EVACUATE-band final reading, per fixture README


def test_get_dam_release_unavailable_before_series_start() -> None:
    as_of = (SERIES_START - timedelta(days=1)).isoformat()
    result = _call(get_dam_release, as_of=as_of)
    assert result["available"] is False
    assert result["value"] is None
    assert result["unit"] is None
    assert result["unavailable_reason"] == "no_reading_at_or_before_as_of"


def test_get_river_level_with_baseline_reports_average() -> None:
    as_of = (SERIES_START + timedelta(days=32)).isoformat()
    result = _call(get_river_level, as_of=as_of, include_baseline=True, baseline_days=30)
    assert result["available"] is True
    baseline = result["baseline"]
    assert baseline is not None
    assert baseline["available"] is True
    assert baseline["reading_count"] == 30 * 24
    assert baseline["window_days"] == 30
    assert isinstance(baseline["average"], float)


def test_get_river_level_baseline_unavailable_before_series_start() -> None:
    as_of = (SERIES_START - timedelta(days=1)).isoformat()
    result = _call(get_river_level, as_of=as_of, include_baseline=True)
    baseline = result["baseline"]
    assert baseline is not None
    assert baseline["available"] is False
    assert baseline["reading_count"] == 0
    assert baseline["unavailable_reason"] == "no_baseline_readings"


def test_tools_are_decorated_and_carry_docstrings() -> None:
    for tool_fn in (get_river_level, get_rainfall_rate, get_dam_release):
        assert hasattr(tool_fn, "tool_spec")
        assert tool_fn.__doc__ and "Args:" in tool_fn.__doc__ and "Returns:" in tool_fn.__doc__
