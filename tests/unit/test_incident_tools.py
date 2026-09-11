"""Unit tests for tools/incident_tools.py (task 12.2).

Validates: Requirements 3.3, 3.4, 3.7; Design §3.2, §4.4.
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from tools import incident_tools

TABLE_NAME = "thunai-state-incident-tools-test"

READINGS = {"river_level": {"value": 2.4, "unit": "m", "ts": "2026-09-01T10:00:00+05:30"}}
READINGS_2 = {"river_level": {"value": 2.9, "unit": "m", "ts": "2026-09-01T11:00:00+05:30"}}


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Fresh moto-mocked `thunai-state` table for every test, mirroring
    tests/unit/test_state_store.py's / test_dispatch_tools.py's fixture."""
    os.environ["THUNAI_STATE_TABLE"] = TABLE_NAME
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
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


# ---------------------------------------------------------------------------
# incident_id_for_reach / higher_severity_band
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_incident_id_for_reach_is_deterministic(self):
        assert incident_tools.incident_id_for_reach("reach-7") == incident_tools.incident_id_for_reach("reach-7")

    def test_incident_id_for_reach_differs_by_reach(self):
        assert incident_tools.incident_id_for_reach("reach-7") != incident_tools.incident_id_for_reach("reach-8")

    def test_higher_severity_band_never_returns_lower(self):
        assert incident_tools.higher_severity_band("WARNING", "WATCH") == "WARNING"
        assert incident_tools.higher_severity_band("WATCH", "WARNING") == "WARNING"

    def test_higher_severity_band_normal_never_wins(self):
        assert incident_tools.higher_severity_band("WARNING", "NORMAL") == "WARNING"

    def test_higher_severity_band_equal_bands(self):
        assert incident_tools.higher_severity_band("EVACUATE", "EVACUATE") == "EVACUATE"


# ---------------------------------------------------------------------------
# create_or_update_incident: creation path (Req 3.3)
# ---------------------------------------------------------------------------


class TestCreateIncident:
    def test_creates_incident_when_none_open_and_band_not_normal(self):
        result = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WATCH",
            readings=READINGS,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )

        assert result["ok"] is True
        assert result["created"] is True
        assert result["updated"] is False
        assert result["severity_band"] == "WATCH"

        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        assert stored is not None
        assert stored.status == "OPEN"
        assert stored.severity_band == "WATCH"
        assert stored.last_readings == READINGS

    def test_no_creation_when_normal_band_and_no_open_incident(self):
        result = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="NORMAL",
            readings=READINGS,
            triggered_rule_ids=[],
            rule_set_version="rs-1",
        )

        assert result == {
            "ok": True,
            "created": False,
            "updated": False,
            "incident_id": incident_tools.incident_id_for_reach("reach-7"),
            "severity_band": "NORMAL",
            "reason": "no_open_incident_and_normal_band",
        }
        assert state_store.get_incident(incident_tools.incident_id_for_reach("reach-7")) is None

    def test_invalid_severity_band_rejected_without_write(self):
        result = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="BOGUS",
            readings=READINGS,
            triggered_rule_ids=[],
            rule_set_version="rs-1",
        )

        assert result == {"ok": False, "reason": "invalid_severity_band", "severity_band": "BOGUS"}
        assert state_store.get_incident(incident_tools.incident_id_for_reach("reach-7")) is None

    def test_second_call_for_same_reach_and_band_takes_the_update_path(self):
        """Once the first call creates the OPEN incident, a second call for
        the same reach naturally becomes an update (Req 3.4) -- it must not
        create a second incident, and it must apply the higher-band rule."""
        first = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WATCH",
            readings=READINGS,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )
        second = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WATCH",
            readings=READINGS_2,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )

        assert first["created"] is True
        assert second["created"] is False
        assert second["updated"] is True
        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        # Exactly one incident item exists for this reach (no second incident created).
        assert stored is not None
        assert stored.last_readings == READINGS_2


# ---------------------------------------------------------------------------
# create_or_update_incident: update path (Req 3.4 -- higher-band wins,
# idempotent update / Property 11)
# ---------------------------------------------------------------------------


class TestUpdateIncident:
    def _seed_open_incident(self, band: str = "WATCH") -> None:
        incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band=band,
            readings=READINGS,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )

    def test_updates_existing_open_incident_with_higher_band(self):
        self._seed_open_incident("WATCH")

        result = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WARNING",
            readings=READINGS_2,
            triggered_rule_ids=["river_level_warning"],
            rule_set_version="rs-1",
        )

        assert result["ok"] is True
        assert result["created"] is False
        assert result["updated"] is True
        assert result["severity_band"] == "WARNING"

        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        assert stored.severity_band == "WARNING"
        assert stored.last_readings == READINGS_2

    def test_never_lowers_stored_band(self):
        self._seed_open_incident("WARNING")

        result = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WATCH",  # lower than the stored WARNING
            readings=READINGS_2,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )

        assert result["severity_band"] == "WARNING"
        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        assert stored.severity_band == "WARNING"

    def test_normal_sweep_keeps_incident_open_without_lowering_band(self):
        """Req 3.10: severity NORMAL, open incident exists -- update readings,
        keep the incident open, never close it, never lower the band."""
        self._seed_open_incident("WARNING")

        result = incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="NORMAL",
            readings=READINGS_2,
            triggered_rule_ids=[],
            rule_set_version="rs-1",
        )

        assert result["updated"] is True
        assert result["severity_band"] == "WARNING"

        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        assert stored.status == "OPEN"
        assert stored.severity_band == "WARNING"
        assert stored.last_readings == READINGS_2  # current readings still refreshed

    def test_idempotent_update_repeated_identical_tuple_leaves_incident_unchanged(self):
        """Property 11 generator: repeated identical
        (river_reach_id, readings, severity_band) tuples applied 2-5 times."""
        self._seed_open_incident("WATCH")

        outcomes = [
            incident_tools.create_or_update_incident(
                river_reach_id="reach-7",
                severity_band="WARNING",
                readings=READINGS_2,
                triggered_rule_ids=["river_level_warning"],
                rule_set_version="rs-1",
            )
            for _ in range(4)
        ]

        assert all(outcome == outcomes[0] for outcome in outcomes)
        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        assert stored.severity_band == "WARNING"
        assert stored.last_readings == READINGS_2

    def test_distinct_readings_after_identical_update_still_writes(self):
        """Sanity check that the idempotency key is content-derived, not
        just reach+band, matching this task's "higher-band wins" instruction
        combined with Req 3.4's freshness requirement for readings."""
        self._seed_open_incident("WATCH")

        incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WARNING",
            readings=READINGS_2,
            triggered_rule_ids=["river_level_warning"],
            rule_set_version="rs-1",
        )
        readings_3 = {"river_level": {"value": 3.5, "unit": "m", "ts": "2026-09-01T12:00:00+05:30"}}
        incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WARNING",
            readings=readings_3,
            triggered_rule_ids=["river_level_warning"],
            rule_set_version="rs-1",
        )

        stored = state_store.get_incident(incident_tools.incident_id_for_reach("reach-7"))
        assert stored.last_readings == readings_3


# ---------------------------------------------------------------------------
# get_prior_sweep_readings (Req 3.1, 3.9)
# ---------------------------------------------------------------------------


class TestGetPriorSweepReadings:
    def test_unavailable_when_no_prior_sweep_recorded(self):
        result = incident_tools.get_prior_sweep_readings("reach-9")

        assert result == {
            "ok": True,
            "available": False,
            "river_reach_id": "reach-9",
            "readings": {},
            "severity_band": None,
            "updated_at": None,
            "unavailable_reason": "no_prior_sweep_recorded",
        }

    def test_returns_last_recorded_sweep_readings(self):
        incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WATCH",
            readings=READINGS,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )

        result = incident_tools.get_prior_sweep_readings("reach-7")

        assert result["ok"] is True
        assert result["available"] is True
        assert result["readings"] == READINGS
        assert result["severity_band"] == "WATCH"
        assert result["unavailable_reason"] is None

    def test_reflects_most_recent_update_not_original_create(self):
        incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WATCH",
            readings=READINGS,
            triggered_rule_ids=["river_level_watch"],
            rule_set_version="rs-1",
        )
        incident_tools.create_or_update_incident(
            river_reach_id="reach-7",
            severity_band="WARNING",
            readings=READINGS_2,
            triggered_rule_ids=["river_level_warning"],
            rule_set_version="rs-1",
        )

        result = incident_tools.get_prior_sweep_readings("reach-7")

        assert result["readings"] == READINGS_2
        assert result["severity_band"] == "WARNING"


# ---------------------------------------------------------------------------
# @tool decoration sanity (matches tests/unit/test_sensor_tools.py's pattern)
# ---------------------------------------------------------------------------


def test_tools_are_decorated_and_carry_docstrings():
    for tool_fn in (incident_tools.create_or_update_incident, incident_tools.get_prior_sweep_readings):
        assert hasattr(tool_fn, "tool_spec")
        assert tool_fn.__doc__ and "Args:" in tool_fn.__doc__ and "Returns:" in tool_fn.__doc__
