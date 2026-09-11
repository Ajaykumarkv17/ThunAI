"""Unit tests for agents/monitor_agent.py (task 13.2).

Validates: Requirements 3.1-3.10; Design §3.1 (Monitor_Agent), §3.2.

Every test uses a fake "agent" callable (never a live Bedrock call) that
mimics `Agent.__call__`'s `(prompt, *, structured_output_model=...) ->
AgentResult`-shaped contract closely enough for
`agents.monitor_agent._assess()` to consume it: an object exposing a
`.structured_output` attribute. This exercises `run_monitor_sweep()`'s own
deterministic logic (the actual subject of this task) without depending on
model availability/credentials.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from agents import monitor_agent
from memory import state_store
from schemas.decisions import HazardAssessment
from schemas.structured import ValidationFailureFallback
from tools import incident_tools

TABLE_NAME = "thunai-state-monitor-agent-test"
AUDIT_TABLE_NAME = "thunai-audit-monitor-agent-test"

RIVER_REACH_ID = "reach-7"
AS_OF = "2026-09-04T23:00:00+05:30"


class _FakeAgentResult:
    def __init__(self, structured_output):
        self.structured_output = structured_output


class _FakeAgent:
    """A minimal stand-in for `strands.Agent.__call__`'s call shape.

    Raises whatever `raises` is set to (if any) instead of returning, so
    tests can exercise the `safe_structured()` validation-failure path
    without a real `StructuredOutputException`.
    """

    def __init__(self, assessment: HazardAssessment | None = None, raises: Exception | None = None):
        self._assessment = assessment
        self._raises = raises
        self.calls: list[str] = []

    def __call__(self, prompt, *, structured_output_model=None, **kwargs):
        self.calls.append(prompt)
        if self._raises is not None:
            raise self._raises
        return _FakeAgentResult(self._assessment)


def _confident_execute_assessment(rationale: str = "River level rising but within expected range.") -> HazardAssessment:
    return HazardAssessment(
        severity_band="WATCH",
        rate_of_change={},
        anomaly_indicator={},
        confidence=0.95,
        action="execute",
        rationale=rationale,
    )


@pytest.fixture(autouse=True)
def _dynamo_tables():
    """Fresh moto-mocked `thunai-state` + `thunai-audit` tables for every
    test, mirroring tests/unit/test_incident_tools.py's fixture."""
    os.environ["THUNAI_STATE_TABLE"] = TABLE_NAME
    os.environ["THUNAI_AUDIT_TABLE"] = AUDIT_TABLE_NAME
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=AUDIT_TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


def _reading(value: float, ts: str, unit: str = "m", baseline_avg: float | None = 2.0) -> dict:
    return {
        "reading_type": "river_level",
        "as_of": ts,
        "available": True,
        "value": value,
        "unit": unit,
        "source_timestamp": ts,
        "unavailable_reason": None,
        "baseline": (
            {
                "available": True,
                "average": baseline_avg,
                "reading_count": 30,
                "window_days": 30,
                "unavailable_reason": None,
            }
            if baseline_avg is not None
            else {
                "available": False,
                "average": None,
                "reading_count": 0,
                "window_days": 30,
                "unavailable_reason": "no_baseline_readings",
            }
        ),
    }


def _unavailable_reading() -> dict:
    return {
        "reading_type": "river_level",
        "as_of": AS_OF,
        "available": False,
        "value": None,
        "unit": None,
        "source_timestamp": None,
        "unavailable_reason": "provider_unavailable_after_retries",
        "baseline": None,
    }


def _patch_reading_tools(monkeypatch, *, river_level=None, rainfall_rate=None, dam_release=None):
    """Monkeypatch `agents.monitor_agent._READING_TOOLS` so a test controls
    exactly what each reading-type tool call returns, without touching the
    real Sensor_Provider seam."""
    tools = {
        "river_level": (lambda **kwargs: river_level) if river_level is not None else (lambda **kwargs: _unavailable_reading()),
        "rainfall_rate": (lambda **kwargs: rainfall_rate) if rainfall_rate is not None else (lambda **kwargs: _unavailable_reading()),
        "dam_release": (lambda **kwargs: dam_release) if dam_release is not None else (lambda **kwargs: _unavailable_reading()),
    }
    monkeypatch.setattr(monitor_agent, "_READING_TOOLS", tools)


def _rule_engine_result(severity_band: str, triggered_rules: list[str] | None = None) -> dict:
    return {
        "severity_band": severity_band,
        "triggered_rules": triggered_rules or [],
        "rule_set_version": "rs-test-1",
    }


# ---------------------------------------------------------------------------
# Incident creation (Req 3.3)
# ---------------------------------------------------------------------------


class TestIncidentCreation:
    def test_creates_incident_on_watch_with_no_open_incident(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF, baseline_avg=3.6),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h", baseline_avg=9.0),
            dam_release=_reading(500.0, AS_OF, unit="m3/s", baseline_avg=480.0),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["ok"] is True
        assert result["outcome"] == "incident_created"
        assert result["created"] is True
        assert result["escalated"] is False

        stored = state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID))
        assert stored is not None
        assert stored.status == "OPEN"
        assert stored.severity_band == "WATCH"

    def test_no_second_incident_created_for_same_reach(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF, baseline_avg=3.6),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h", baseline_avg=9.0),
            dam_release=_reading(500.0, AS_OF, unit="m3/s", baseline_avg=480.0),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH"),
            run_id="run-1",
            agent=fake_agent,
        )
        second = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH"),
            run_id="run-2",
            agent=fake_agent,
        )

        # Second call finds the incident already OPEN -> update path, not a
        # second creation (Req 3.4).
        assert second["outcome"] == "incident_updated"
        assert second["created"] is False
        assert second["updated"] is True


# ---------------------------------------------------------------------------
# Incident update / idempotent no-op (Req 3.4)
# ---------------------------------------------------------------------------


class TestIncidentUpdate:
    def test_update_raises_stored_band_to_higher_of_the_two(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h"),
            dam_release=_reading(500.0, AS_OF, unit="m3/s"),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        # Seed an OPEN incident at WATCH via a direct create call.
        incident_tools.create_or_update_incident(
            river_reach_id=RIVER_REACH_ID,
            severity_band="WATCH",
            readings={"river_level": {"value": 3.6, "unit": "m", "ts": AS_OF}},
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-test-0",
        )

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WARNING", ["river_level_warning"]),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["outcome"] == "incident_updated"
        assert result["severity_band"] == "WARNING"
        stored = state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID))
        assert stored.severity_band == "WARNING"

    def test_repeated_identical_update_is_idempotent_no_op(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h"),
            dam_release=_reading(500.0, AS_OF, unit="m3/s"),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        incident_tools.create_or_update_incident(
            river_reach_id=RIVER_REACH_ID,
            severity_band="WATCH",
            readings={"river_level": {"value": 3.6, "unit": "m", "ts": AS_OF}},
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-test-0",
        )

        first = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=fake_agent,
        )
        second = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-2",
            agent=fake_agent,
        )

        assert first["severity_band"] == second["severity_band"] == "WATCH"
        stored = state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID))
        assert stored.severity_band == "WATCH"
        assert stored.status == "OPEN"


# ---------------------------------------------------------------------------
# NORMAL, no anomaly, no open incident -> no-op (Req 3.5)
# ---------------------------------------------------------------------------


class TestNormalNoAnomaly:
    def test_normal_band_no_anomaly_completes_without_incident_or_escalation(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(2.1, AS_OF, baseline_avg=2.0),
            rainfall_rate=_reading(5.0, AS_OF, unit="mm/h", baseline_avg=5.0),
            dam_release=_reading(400.0, AS_OF, unit="m3/s", baseline_avg=400.0),
        )
        fake_agent = _FakeAgent(
            assessment=HazardAssessment(
                severity_band="NORMAL",
                rate_of_change={},
                anomaly_indicator={},
                confidence=0.95,
                action="execute",
                rationale="River level steady, no meaningful change.",
            )
        )

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("NORMAL"),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["outcome"] == "no_incident"
        assert result["created"] is False
        assert result["escalated"] is False
        assert state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID)) is None


# ---------------------------------------------------------------------------
# Total sensor outage -> data-outage escalation, incidents unchanged (Req 3.6)
# ---------------------------------------------------------------------------


class TestTotalSensorOutage:
    def test_total_outage_escalates_and_never_touches_incident_state(self, monkeypatch):
        _patch_reading_tools(monkeypatch)  # every reading type unavailable
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        # Seed an existing OPEN incident to prove it is left unchanged.
        incident_tools.create_or_update_incident(
            river_reach_id=RIVER_REACH_ID,
            severity_band="WATCH",
            readings={"river_level": {"value": 3.6, "unit": "m", "ts": AS_OF}},
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-test-0",
        )

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["outcome"] == "data_outage"
        assert result["escalated"] is True
        assert result["escalation"]["ok"] is True
        assert not fake_agent.calls  # no model call made on the total-outage path

        stored = state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID))
        assert stored.severity_band == "WATCH"  # unchanged


# ---------------------------------------------------------------------------
# Partial outage -> assessment marks unavailable readings (Req 3.8)
# ---------------------------------------------------------------------------


class TestPartialOutage:
    def test_partial_outage_marks_unavailable_readings_and_proceeds(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF, baseline_avg=3.6),
            rainfall_rate=None,  # unavailable
            dam_release=_reading(500.0, AS_OF, unit="m3/s", baseline_avg=480.0),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["outcome"] == "incident_created"
        assert result["assessment"]["unavailable_readings"] == ["rainfall_rate"]


# ---------------------------------------------------------------------------
# Missing baseline / no prior sweep -> unavailable rate-of-change/anomaly (Req 3.9)
# ---------------------------------------------------------------------------


class TestMissingBaselineAndPriorSweep:
    def test_missing_baseline_marks_anomaly_indicator_unavailable(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF, baseline_avg=None),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h"),
            dam_release=_reading(500.0, AS_OF, unit="m3/s"),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["assessment"]["anomaly_indicator"]["river_level"] is None

    def test_no_prior_sweep_marks_rate_of_change_unavailable(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h"),
            dam_release=_reading(500.0, AS_OF, unit="m3/s"),
        )
        fake_agent = _FakeAgent(assessment=_confident_execute_assessment())

        # No prior incident recorded for this reach at all -> get_prior_sweep_readings
        # reports unavailable, so rate_of_change must be None for every type.
        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=fake_agent,
        )

        assert all(v is None for v in result["assessment"]["rate_of_change"].values())


# ---------------------------------------------------------------------------
# NORMAL with an existing open incident -> downgrade, keep open (Req 3.10)
# ---------------------------------------------------------------------------


class TestNormalDowngradeWithOpenIncident:
    def test_normal_band_updates_and_keeps_incident_open(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(2.1, AS_OF, baseline_avg=2.0),
            rainfall_rate=_reading(5.0, AS_OF, unit="mm/h", baseline_avg=5.0),
            dam_release=_reading(400.0, AS_OF, unit="m3/s", baseline_avg=400.0),
        )
        fake_agent = _FakeAgent(
            assessment=HazardAssessment(
                severity_band="NORMAL",
                rate_of_change={},
                anomaly_indicator={},
                confidence=0.95,
                action="execute",
                rationale="River level back to normal.",
            )
        )

        incident_tools.create_or_update_incident(
            river_reach_id=RIVER_REACH_ID,
            severity_band="WATCH",
            readings={"river_level": {"value": 3.6, "unit": "m", "ts": AS_OF}},
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-test-0",
        )

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("NORMAL"),
            run_id="run-1",
            agent=fake_agent,
        )

        assert result["outcome"] == "incident_updated_normal_downgrade"
        stored = state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID))
        assert stored.status == "OPEN"  # never closed
        # Never lowered below the previously-stored WATCH band by this
        # NORMAL-band sweep, per higher_severity_band's "never lower it" rule.
        assert stored.severity_band == "WATCH"


# ---------------------------------------------------------------------------
# Structured-output validation failure -> ask-human, no incident touch (Req 10.11)
# ---------------------------------------------------------------------------


class TestValidationFailure:
    def test_validation_failure_escalates_without_touching_incident_state(self, monkeypatch):
        _patch_reading_tools(
            monkeypatch,
            river_level=_reading(3.8, AS_OF),
            rainfall_rate=_reading(10.0, AS_OF, unit="mm/h"),
            dam_release=_reading(500.0, AS_OF, unit="m3/s"),
        )

        def _raising(prompt, *, structured_output_model=None, **kwargs):
            raise ValueError("boom")

        # safe_structured() only catches StructuredOutputException/ValidationError;
        # patch _assess directly so this test exercises the escalation branch
        # without depending on a real Strands exception type.
        monkeypatch.setattr(
            monitor_agent,
            "_assess",
            lambda agent, prompt: ValidationFailureFallback(raw_output="<bad>", error_detail="boom"),
        )

        result = monitor_agent.run_monitor_sweep(
            river_reach_id=RIVER_REACH_ID,
            as_of=AS_OF,
            rule_engine_result=_rule_engine_result("WATCH", ["river_level_watch"]),
            run_id="run-1",
            agent=object(),
        )

        assert result["outcome"] == "escalated_validation_failure"
        assert result["escalated"] is True
        assert state_store.get_incident(incident_tools.incident_id_for_reach(RIVER_REACH_ID)) is None
