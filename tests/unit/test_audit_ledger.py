"""Unit tests for ``memory/audit_ledger.py`` and ``harness/audit.py`` (task 7.1).

Uses ``moto``'s ``@mock_aws`` to provide an in-process fake DynamoDB backend
since no live ``thunai-audit`` table exists yet (the CDK data stack, tasks
21.x, is not implemented). Each test creates the table schema itself,
matching design.md §4.4's key schema (``pk``/``sk`` composite primary key,
plus a ``gsi1`` GSI on ``gsi1pk``/``gsi1sk`` for the per-incident read path).

Validates: Requirements 15.6, 16.8; Design §4.4, §3.6.
"""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from harness.audit import append_audit_entry
from memory.audit_ledger import (
    AuditAppendDuplicateError,
    append_entry,
    get_entries_for_incident,
    get_entries_for_run,
    new_entry_id,
)
from schemas.entities import AuditEntry

_TABLE_NAME = "thunai-audit-test"


def _create_audit_table(region_name: str = "us-west-2") -> None:
    """Create the ``thunai-audit`` table schema (design.md §4.4) in the
    currently-active moto backend."""
    client = boto3.client("dynamodb", region_name=region_name)
    client.create_table(
        TableName=_TABLE_NAME,
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
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture()
def audit_table(monkeypatch: pytest.MonkeyPatch):
    """Stand up a fresh in-process ``thunai-audit`` table for one test."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("THUNAI_AUDIT_TABLE", _TABLE_NAME)
    with mock_aws():
        _create_audit_table()
        yield


def _make_entry(**overrides) -> AuditEntry:
    defaults = dict(
        entry_id=new_entry_id(),
        run_id="run-1",
        timestamp="2026-09-03T14:00:00+00:00",
        tool_call_id="tc-1",
        tool_name="create_or_update_incident",
        inputs={"river_reach_id": "reach-7"},
        outcome="incident_created",
    )
    defaults.update(overrides)
    return AuditEntry(**defaults)


class TestAppendEntry:
    def test_append_new_entry_succeeds(self, audit_table) -> None:
        entry = _make_entry()
        append_entry(entry)

        fetched = get_entries_for_run("run-1")
        assert len(fetched) == 1
        assert fetched[0].entry_id == entry.entry_id
        assert fetched[0].outcome == "incident_created"

    def test_append_duplicate_entry_raises_and_leaves_original_unchanged(
        self, audit_table
    ) -> None:
        entry = _make_entry()
        append_entry(entry)

        # A second append attempt for the identical (run_id, timestamp,
        # entry_id) key must raise rather than silently overwrite (Req 15.6).
        duplicate = _make_entry(
            entry_id=entry.entry_id,
            timestamp=entry.timestamp,
            outcome="a_different_outcome_that_must_not_win",
        )
        with pytest.raises(AuditAppendDuplicateError):
            append_entry(duplicate)

        fetched = get_entries_for_run("run-1")
        assert len(fetched) == 1
        assert fetched[0].outcome == "incident_created"  # unchanged

    def test_get_entries_for_run_returns_oldest_first(self, audit_table) -> None:
        first = _make_entry(timestamp="2026-09-03T14:00:00+00:00", outcome="first")
        second = _make_entry(timestamp="2026-09-03T14:05:00+00:00", outcome="second")
        third = _make_entry(timestamp="2026-09-03T14:10:00+00:00", outcome="third")

        # Append out of chronological order to prove ordering comes from the
        # query, not insertion order.
        append_entry(third)
        append_entry(first)
        append_entry(second)

        fetched = get_entries_for_run("run-1")
        assert [e.outcome for e in fetched] == ["first", "second", "third"]

    def test_get_entries_for_incident_returns_oldest_first(self, audit_table) -> None:
        first = _make_entry(timestamp="2026-09-03T14:00:00+00:00", outcome="first")
        second = _make_entry(timestamp="2026-09-03T14:05:00+00:00", outcome="second")
        unrelated = _make_entry(
            timestamp="2026-09-03T14:02:00+00:00",
            outcome="unrelated",
            run_id="run-2",
        )

        append_entry(second, incident_id="inc-1")
        append_entry(first, incident_id="inc-1")
        append_entry(unrelated, incident_id="inc-2")

        fetched = get_entries_for_incident("inc-1")
        assert [e.outcome for e in fetched] == ["first", "second"]

    def test_get_entries_for_run_empty_when_no_entries(self, audit_table) -> None:
        assert get_entries_for_run("run-nonexistent") == []


class TestAppendAuditEntry:
    def test_calls_redact_fields_on_inputs_before_persisting(self, audit_table) -> None:
        raw_inputs = {"resident_name": "Priya K.", "location": "14 Kanal Street"}

        with patch(
            "harness.audit.redact_fields",
            return_value={"resident_name": "[REDACTED]", "location": "14 Kanal Street"},
        ) as mock_redact:
            append_audit_entry(
                run_id="run-1",
                tool_name="create_incident",
                outcome="incident_created",
                inputs=raw_inputs,
            )

        mock_redact.assert_called_once_with(raw_inputs)

        fetched = get_entries_for_run("run-1")
        assert len(fetched) == 1
        assert fetched[0].inputs == {
            "resident_name": "[REDACTED]",
            "location": "14 Kanal Street",
        }
        # The raw, unredacted value must never have reached the ledger.
        assert "Priya K." not in str(fetched[0].inputs)

    def test_persists_expected_entry_fields(self, audit_table) -> None:
        entry = append_audit_entry(
            run_id="run-1",
            tool_name="assign_responder",
            outcome="assigned",
            tool_call_id="tc-42",
            inputs={"request_id": "req-1"},
            approving_human_id="coord-1",
            model_id="us.amazon.nova-pro-v1:0",
            input_tokens=120,
            output_tokens=30,
        )

        assert entry.tool_call_id == "tc-42"
        assert entry.approving_human_id == "coord-1"
        assert entry.model_id == "us.amazon.nova-pro-v1:0"
        assert entry.input_tokens == 120
        assert entry.output_tokens == 30

        fetched = get_entries_for_run("run-1")
        assert len(fetched) == 1
        assert fetched[0].tool_call_id == "tc-42"

    def test_propagates_underlying_append_failure(self, audit_table) -> None:
        from memory.audit_ledger import AuditAppendError

        with patch(
            "harness.audit.append_entry",
            side_effect=AuditAppendError("boom"),
        ):
            with pytest.raises(AuditAppendError):
                append_audit_entry(
                    run_id="run-1",
                    tool_name="deliver_alert",
                    outcome="sent",
                )

    def test_defaults_inputs_to_empty_dict(self, audit_table) -> None:
        entry = append_audit_entry(
            run_id="run-1",
            tool_name="get_river_level",
            outcome="ok",
        )
        assert entry.inputs == {}
