"""Unit tests for tools/dispatch_tools.py (task 12.4).

Validates: Requirements 6.1, 6.8; Design §3.1 (Dispatch), §4.4.
"""

from __future__ import annotations

import math
import os

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from schemas.entities import Responder
from tools import dispatch_tools

TABLE_NAME = "thunai-state-dispatch-tools-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Fresh moto-mocked `thunai-state` table for every test, mirroring
    tests/unit/test_state_store.py's fixture."""
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


def _responder(**overrides) -> Responder:
    defaults = dict(
        responder_id="resp-1",
        name="Karthik R.",
        home_coords=(13.08, 80.27),
        equipment=["boat"],
        availability_status="AVAILABLE",
    )
    defaults.update(overrides)
    return Responder(**defaults)


# ---------------------------------------------------------------------------
# haversine_km
# ---------------------------------------------------------------------------


class TestHaversineKm:
    def test_identical_points_are_zero_distance(self):
        assert dispatch_tools.haversine_km((13.08, 80.27), (13.08, 80.27)) == 0.0

    def test_symmetric(self):
        a, b = (13.08, 80.27), (13.05, 80.30)
        assert dispatch_tools.haversine_km(a, b) == pytest.approx(dispatch_tools.haversine_km(b, a))

    def test_known_distance_equator_one_degree_longitude(self):
        # At the equator, 1 degree of longitude is ~111.19 km (matches the
        # IUGG mean-radius haversine calculation this module uses).
        distance = dispatch_tools.haversine_km((0.0, 0.0), (0.0, 1.0))
        expected = math.radians(1.0) * dispatch_tools._EARTH_RADIUS_KM
        assert distance == pytest.approx(expected, rel=1e-6)

    def test_never_negative(self):
        assert dispatch_tools.haversine_km((-33.9, 151.2), (51.5, -0.1)) > 0.0


# ---------------------------------------------------------------------------
# find_candidate_responders
# ---------------------------------------------------------------------------


class TestFindCandidateResponders:
    def test_returns_only_responders_within_radius_sorted_by_distance(self, monkeypatch):
        monkeypatch.setenv(dispatch_tools.DISPATCH_SEARCH_RADIUS_KM_ENV_VAR, "5")
        origin = (13.08, 80.27)
        near = _responder(responder_id="resp-near", home_coords=(13.081, 80.271))
        far = _responder(responder_id="resp-far", home_coords=(14.5, 81.5))
        state_store.put_responder(near)
        state_store.put_responder(far)

        result = dispatch_tools.find_candidate_responders(*origin)

        assert result["ok"] is True
        ids = [c["responder_id"] for c in result["candidates"]]
        assert ids == ["resp-near"]

    def test_excludes_assigned_responders(self):
        state_store.put_responder(
            _responder(responder_id="resp-busy", availability_status="ASSIGNED", active_assignment_id="req-0")
        )

        result = dispatch_tools.find_candidate_responders(13.08, 80.27)

        assert result["candidates"] == []

    def test_truncates_to_max_candidate_count(self, monkeypatch):
        monkeypatch.setenv(dispatch_tools.DISPATCH_SEARCH_RADIUS_KM_ENV_VAR, "50")
        monkeypatch.setenv(dispatch_tools.DISPATCH_MAX_CANDIDATE_COUNT_ENV_VAR, "2")
        for i in range(4):
            state_store.put_responder(
                _responder(responder_id=f"resp-{i}", home_coords=(13.08 + i * 0.001, 80.27))
            )

        result = dispatch_tools.find_candidate_responders(13.08, 80.27)

        assert len(result["candidates"]) == 2

    def test_empty_result_when_none_within_radius(self, monkeypatch):
        monkeypatch.setenv(dispatch_tools.DISPATCH_SEARCH_RADIUS_KM_ENV_VAR, "1")
        state_store.put_responder(_responder(home_coords=(20.0, 90.0)))

        result = dispatch_tools.find_candidate_responders(13.08, 80.27)

        assert result["ok"] is True
        assert result["candidates"] == []
        assert result["search_radius_km"] == 1.0


# ---------------------------------------------------------------------------
# assign_responder
# ---------------------------------------------------------------------------


class TestAssignResponder:
    def test_assigns_available_responder(self):
        state_store.put_responder(_responder())

        result = dispatch_tools.assign_responder("resp-1", "req-1")

        assert result == {
            "ok": True,
            "responder_id": "resp-1",
            "request_id": "req-1",
            "availability_status": "ASSIGNED",
            "active_assignment_count": 1,
        }

    def test_derives_idempotency_key_from_request_id_and_replays_first_outcome(self):
        state_store.put_responder(_responder())

        first = dispatch_tools.assign_responder("resp-1", "req-1")
        second = dispatch_tools.assign_responder("resp-1", "req-1")

        assert first == second  # idempotent: same outcome, no second write

    def test_returns_typed_failure_when_responder_no_longer_available(self):
        state_store.put_responder(
            _responder(availability_status="ASSIGNED", active_assignment_id="req-0")
        )

        result = dispatch_tools.assign_responder("resp-1", "req-2")

        assert result == {
            "ok": False,
            "responder_id": "resp-1",
            "request_id": "req-2",
            "reason": "responder_no_longer_available",
        }

    def test_no_double_assignment_on_racing_calls(self):
        state_store.put_responder(_responder())

        first = dispatch_tools.assign_responder("resp-1", "req-1")
        second = dispatch_tools.assign_responder("resp-1", "req-2")

        assert first["ok"] is True
        assert second["ok"] is False
        assert second["reason"] == "responder_no_longer_available"
