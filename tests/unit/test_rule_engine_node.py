"""Unit tests for agents/rule_engine.py::RuleEngineNode (task 4.1).

Covers: NORMAL band well below WATCH, correct band selection at/above each
threshold for each reading type individually, "highest band wins" across
reading types, each of the three unavailability causes individually
degrading evaluation without blocking it, the total-outage `unverified`
case, and `model_invocations == 0` on every result shape.

Uses `evaluate_rule_engine()` (the pure function `RuleEngineNode.invoke_async`
delegates to) for most cases, since it is directly testable without
constructing async/Strands result wrappers, plus a handful of tests that
exercise `RuleEngineNode.invoke_async` itself to confirm it wires that
payload correctly into the Strands `MultiAgentResult`/`AgentResult` shape.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from strands.multiagent.base import MultiAgentResult, Status

from agents.rule_engine import (
    REASON_ABSENT,
    REASON_NON_NUMERIC,
    REASON_OUT_OF_RANGE,
    REASON_STALE,
    RuleEngineNode,
    evaluate_rule_engine,
)
from policy.rule_engine_rules import RULE_SET_VERSION, THRESHOLDS

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _reading(value: float, ts: datetime = NOW) -> dict:
    return {"value": value, "unit": "irrelevant", "ts": ts}


def _all_normal_readings(ts: datetime = NOW) -> dict[str, dict]:
    """One reading per configured type, each well below its WATCH threshold."""
    readings = {}
    for reading_type, spec in THRESHOLDS.items():
        watch_threshold = spec.rules_ascending[0].threshold
        readings[reading_type] = _reading(watch_threshold / 2, ts)
    return readings


# ---------------------------------------------------------------------------
# NORMAL band
# ---------------------------------------------------------------------------


def test_all_readings_well_below_watch_yields_normal():
    readings = _all_normal_readings()
    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")

    assert result["severity_band"] == "NORMAL"
    assert result["triggered_rules"] == []
    assert result["unverified"] is False
    assert result["model_invocations"] == 0
    assert result["rule_set_version"] == RULE_SET_VERSION


# ---------------------------------------------------------------------------
# Band selection at/above each threshold, per reading type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reading_type", list(THRESHOLDS.keys()))
@pytest.mark.parametrize("band_index", [0, 1, 2])  # WATCH, WARNING, EVACUATE
def test_band_selection_at_and_above_each_threshold(reading_type, band_index):
    spec = THRESHOLDS[reading_type]
    rule = spec.rules_ascending[band_index]

    readings = _all_normal_readings()

    # Exactly at threshold.
    readings[reading_type] = _reading(rule.threshold)
    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")
    assert result["severity_band"] == rule.band
    assert rule.rule_id in result["triggered_rules"]

    # Above threshold.
    readings[reading_type] = _reading(rule.threshold + 0.01 * max(rule.threshold, 1.0))
    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")
    assert result["severity_band"] == rule.band
    assert rule.rule_id in result["triggered_rules"]


# ---------------------------------------------------------------------------
# Highest band wins across different reading types
# ---------------------------------------------------------------------------


def test_highest_band_wins_across_reading_types():
    readings = _all_normal_readings()

    river_warning = THRESHOLDS["river_level"].rules_ascending[1]  # WARNING
    rainfall_watch = THRESHOLDS["rainfall_rate"].rules_ascending[0]  # WATCH

    readings["river_level"] = _reading(river_warning.threshold)
    readings["rainfall_rate"] = _reading(rainfall_watch.threshold)

    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")

    assert result["severity_band"] == "WARNING"
    assert river_warning.rule_id in result["triggered_rules"]
    assert rainfall_watch.rule_id in result["triggered_rules"]


# ---------------------------------------------------------------------------
# Each unavailability cause individually degrades evaluation without
# blocking it (Req 2.5, 2.9)
# ---------------------------------------------------------------------------


def test_absent_reading_is_marked_unavailable_and_does_not_block():
    readings = _all_normal_readings()
    del readings["rainfall_rate"]

    river_watch = THRESHOLDS["river_level"].rules_ascending[0]
    readings["river_level"] = _reading(river_watch.threshold)

    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")

    assert result["availability"]["rainfall_rate"] == {"available": False, "reason": REASON_ABSENT}
    assert result["severity_band"] == "WATCH"
    assert result["unverified"] is False


def test_out_of_range_reading_is_marked_unavailable_and_does_not_block():
    readings = _all_normal_readings()
    out_of_range_value = THRESHOLDS["dam_release"].valid_range[1] + 1000.0
    readings["dam_release"] = _reading(out_of_range_value)

    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")

    assert result["availability"]["dam_release"] == {"available": False, "reason": REASON_OUT_OF_RANGE}
    assert result["severity_band"] == "NORMAL"
    assert result["unverified"] is False


def test_non_numeric_reading_is_marked_unavailable_and_does_not_block():
    readings = _all_normal_readings()
    readings["dam_release"] = _reading("not-a-number")  # type: ignore[arg-type]

    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")

    assert result["availability"]["dam_release"] == {"available": False, "reason": REASON_NON_NUMERIC}
    assert result["severity_band"] == "NORMAL"
    assert result["unverified"] is False


def test_stale_reading_is_marked_unavailable_and_does_not_block():
    readings = _all_normal_readings()
    stale_ts = NOW - timedelta(minutes=16)  # STALENESS_LIMIT_S is 15 minutes
    river_watch = THRESHOLDS["river_level"].rules_ascending[0]
    readings["river_level"] = _reading(river_watch.threshold)  # would trigger WATCH if fresh
    readings["rainfall_rate"] = _reading(readings["rainfall_rate"]["value"], stale_ts)

    result = evaluate_rule_engine(readings, NOW, last_known_band="NORMAL")

    assert result["availability"]["rainfall_rate"] == {"available": False, "reason": REASON_STALE}
    # river_level is fresh and available, so evaluation still proceeds and
    # uses it -- the stale rainfall reading does not block the rest.
    assert result["severity_band"] == "WATCH"
    assert result["unverified"] is False


# ---------------------------------------------------------------------------
# Total outage -> unverified, last known band carried forward, never NORMAL
# by default (Req 2.9/2.10)
# ---------------------------------------------------------------------------


def test_total_outage_returns_unverified_and_last_known_band():
    readings: dict[str, dict] = {}  # every reading type absent

    result = evaluate_rule_engine(readings, NOW, last_known_band="WARNING")

    assert result["unverified"] is True
    assert result["severity_band"] == "WARNING"
    assert result["triggered_rules"] == []
    for reading_type in THRESHOLDS:
        assert result["availability"][reading_type]["available"] is False
    assert result["model_invocations"] == 0


def test_total_outage_never_silently_reports_normal_when_last_known_is_higher():
    """A total outage must not produce a false-negative NORMAL reading."""
    readings = {
        reading_type: _reading(float("nan")) for reading_type in THRESHOLDS
    }
    # NaN is numeric (float) but fails the in-range check for every spec
    # below, so every reading type becomes unavailable via out-of-range.
    result = evaluate_rule_engine(readings, NOW, last_known_band="EVACUATE")

    assert result["unverified"] is True
    assert result["severity_band"] == "EVACUATE"


# ---------------------------------------------------------------------------
# model_invocations == 0 on every result shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "readings,last_known_band",
    [
        ({}, "NORMAL"),
        ({}, "EVACUATE"),
        (
            {
                reading_type: (lambda spec: _reading(spec.rules_ascending[0].threshold))(spec)
                for reading_type, spec in THRESHOLDS.items()
            },
            "NORMAL",
        ),
    ],
)
def test_model_invocations_is_always_zero(readings, last_known_band):
    result = evaluate_rule_engine(readings, NOW, last_known_band=last_known_band)
    assert result["model_invocations"] == 0


# ---------------------------------------------------------------------------
# RuleEngineNode.invoke_async wiring: Strands MultiAgentResult/AgentResult shape
# ---------------------------------------------------------------------------


def test_invoke_async_returns_completed_multiagent_result_with_zero_model_invocations():
    node = RuleEngineNode()
    readings = _all_normal_readings()
    invocation_state = {"readings": readings, "now": NOW, "last_known_band": "NORMAL"}

    result = asyncio.run(node.invoke_async(task="ignored", invocation_state=invocation_state))

    assert isinstance(result, MultiAgentResult)
    assert result.status == Status.COMPLETED
    assert "rule_engine" in result.results

    node_result = result.results["rule_engine"]
    assert node_result.status == Status.COMPLETED

    agent_result = node_result.result
    assert agent_result.state["model_invocations"] == 0
    assert agent_result.state["severity_band"] == "NORMAL"
    assert agent_result.state["rule_set_version"] == RULE_SET_VERSION
    assert agent_result.stop_reason == "end_turn"


def test_invoke_async_total_outage_via_missing_invocation_state_keys_raises():
    """A graph invocation that omits a required invocation_state key fails loudly."""
    node = RuleEngineNode()
    with pytest.raises(KeyError):
        asyncio.run(node.invoke_async(task="ignored", invocation_state={}))


def test_invoke_async_is_deterministic_across_two_calls():
    """Sanity check ahead of Property 1 (task 4.2): identical inputs, identical output."""
    node = RuleEngineNode()
    readings = _all_normal_readings()
    river_warning = THRESHOLDS["river_level"].rules_ascending[1]
    readings["river_level"] = _reading(river_warning.threshold)
    invocation_state = {"readings": readings, "now": NOW, "last_known_band": "NORMAL"}

    result_a = asyncio.run(node.invoke_async(task="x", invocation_state=dict(invocation_state)))
    result_b = asyncio.run(node.invoke_async(task="x", invocation_state=dict(invocation_state)))

    state_a = result_a.results["rule_engine"].result.state
    state_b = result_b.results["rule_engine"].result.state

    assert state_a["severity_band"] == state_b["severity_band"]
    assert state_a["triggered_rules"] == state_b["triggered_rules"]
