"""Unit tests for schemas/entities.py (task 2.2).

Validates: Requirements 15.1, 11.1, 12.4; Design §4.2.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.entities import (
    AuditEntry,
    EscalationOption,
    EscalationRecord,
    Incident,
    Responder,
    Shelter,
)


def _make_escalation_record(**overrides) -> EscalationRecord:
    defaults = dict(
        escalation_id="esc-1",
        incident_id="inc-1",
        escalation_type="dispatch_assignment",
        status="OPEN",
        decision_summary="Dispatch to 14 Kanal Street for 5 people.",
        reason="mobility-assistance requests always need your sign-off before dispatch",
        stakes="this occupant cannot self-evacuate; the boat is the only equipped responder within radius",
        options=[
            EscalationOption(option_id="1", label="Approve dispatch"),
            EscalationOption(option_id="2", label="Choose different responder"),
            EscalationOption(option_id="3", label="Hold"),
        ],
        default_action="hold_assignment",
        response_deadline="2026-09-03T14:52:00+05:30",
        run_id="run-1",
        context={
            "responder_name": "Karthik R.",
            "location": "14 Kanal Street",
            "occupant_count": 5,
            "category": "RESCUE",
            "mobility_note": "One occupant needs mobility assistance. ",
        },
    )
    defaults.update(overrides)
    return EscalationRecord(**defaults)


class TestIncident:
    def test_valid_construction(self) -> None:
        incident = Incident(
            incident_id="inc-1",
            river_reach_id="reach-7",
            severity_band="WARNING",
            status="OPEN",
            affected_areas=["Ward-7 South"],
            created_at="2026-09-03T14:00:00+05:30",
            updated_at="2026-09-03T14:10:00+05:30",
            last_readings={"river_level_m": 3.2},
            triggered_rule_ids=["river_level_warning"],
            rule_set_version="v3",
        )
        assert incident.severity_band == "WARNING"
        assert incident.status == "OPEN"

    def test_invalid_severity_band_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Incident(
                incident_id="inc-1",
                river_reach_id="reach-7",
                severity_band="CRITICAL",  # not in the ordered set
                status="OPEN",
                created_at="2026-09-03T14:00:00+05:30",
                updated_at="2026-09-03T14:10:00+05:30",
                rule_set_version="v3",
            )


class TestResponder:
    def test_valid_construction(self) -> None:
        responder = Responder(
            responder_id="resp-4",
            name="Karthik R.",
            home_coords=(11.23, 79.84),
            equipment=["boat"],
            availability_status="AVAILABLE",
        )
        assert responder.availability_status == "AVAILABLE"
        assert responder.active_assignment_id is None

    def test_negative_active_assignment_count_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Responder(
                responder_id="resp-4",
                name="Karthik R.",
                home_coords=(11.23, 79.84),
                equipment=["boat"],
                availability_status="AVAILABLE",
                active_assignment_count=-1,
            )


class TestShelter:
    def test_valid_construction(self) -> None:
        shelter = Shelter(
            shelter_id="shelter-1",
            name="Ward-7 Community Hall",
            coords=(11.24, 79.85),
            total_capacity=40,
            available_capacity=12,
        )
        assert shelter.available_capacity == 12

    def test_negative_available_capacity_rejected(self) -> None:
        """Req 6.7 / 15.8: available_capacity must never be negative."""
        with pytest.raises(ValidationError):
            Shelter(
                shelter_id="shelter-1",
                name="Ward-7 Community Hall",
                coords=(11.24, 79.85),
                total_capacity=40,
                available_capacity=-1,
            )

    def test_negative_total_capacity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Shelter(
                shelter_id="shelter-1",
                name="Ward-7 Community Hall",
                coords=(11.24, 79.85),
                total_capacity=-1,
                available_capacity=0,
            )


class TestEscalationOption:
    def test_valid_construction(self) -> None:
        option = EscalationOption(option_id="1", label="Approve dispatch")
        assert option.option_id == "1"


class TestEscalationRecord:
    def test_valid_construction(self) -> None:
        record = _make_escalation_record()
        assert record.status == "OPEN"
        assert 2 <= len(record.options) <= 5

    def test_requires_at_least_two_options(self) -> None:
        with pytest.raises(ValidationError):
            _make_escalation_record(
                options=[EscalationOption(option_id="1", label="Approve dispatch")]
            )

    def test_rejects_more_than_five_options(self) -> None:
        options = [
            EscalationOption(option_id=str(i), label=f"Option {i}") for i in range(6)
        ]
        with pytest.raises(ValidationError):
            _make_escalation_record(options=options)

    def test_decision_summary_max_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            _make_escalation_record(decision_summary="x" * 201)

    def test_template_fields_returns_expected_subset(self) -> None:
        """Req 11.3: template_fields() must return ONLY persisted values
        needed for rendering, excluding internal bookkeeping fields."""
        record = _make_escalation_record()
        fields = record.template_fields()

        # Expected persisted/derived values are present.
        assert fields["reason"] == record.reason
        assert fields["stakes"] == record.stakes
        assert fields["deadline_local"] == "14:52"
        assert fields["default_action_human"] == (
            "hold the assignment and keep searching for capacity"
        )
        assert fields["responder_name"] == "Karthik R."
        assert fields["location"] == "14 Kanal Street"
        assert fields["occupant_count"] == 5

        # Internal-only bookkeeping fields must never appear in the template
        # payload handed to a human-facing notification.
        excluded_keys = {
            "escalation_id",
            "run_id",
            "interrupt_id",
            "status",
            "resolved_option_id",
            "responding_human_id",
            "resolution_timestamp",
            "resolved_by_default",
            "options",
            "response_deadline",
            "default_action",
        }
        assert excluded_keys.isdisjoint(fields.keys())

    def test_template_fields_alert_release_type(self) -> None:
        record = _make_escalation_record(
            escalation_type="alert_release",
            default_action="hold",
            context={
                "severity_band": "EVACUATE",
                "affected_areas": "Ward-7 South, Riverside Colony",
                "audience_count": 310,
            },
        )
        fields = record.template_fields()
        assert fields["severity_band"] == "EVACUATE"
        assert fields["audience_count"] == 310
        assert fields["default_action_human"] == "hold (nothing is sent by default)"


class TestAuditEntry:
    def test_valid_construction(self) -> None:
        entry = AuditEntry(
            entry_id="audit-1",
            run_id="run-1",
            timestamp="2026-09-03T14:00:00+05:30",
            tool_call_id="tc-1",
            tool_name="create_or_update_incident",
            inputs={"river_reach_id": "reach-7"},
            outcome="incident_created",
            approving_human_id=None,
            model_id="us.amazon.nova-pro-v1:0",
            input_tokens=120,
            output_tokens=45,
        )
        assert entry.tool_name == "create_or_update_incident"

    def test_negative_token_counts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditEntry(
                entry_id="audit-1",
                run_id="run-1",
                timestamp="2026-09-03T14:00:00+05:30",
                tool_name="create_or_update_incident",
                outcome="incident_created",
                input_tokens=-1,
            )
