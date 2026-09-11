"""Unit tests for schemas/decisions.py (task 2.1).

Constructs a valid instance of each of the five typed decision models and
confirms that invalid `confidence` values and invalid `action` values raise
`pydantic.ValidationError`.
"""

import pytest
from pydantic import ValidationError

from schemas.decisions import (
    AlertDraft,
    DispatchDecision,
    EmergencyRequest,
    HazardAssessment,
    SafetyReview,
)

VALID_KWARGS = {
    HazardAssessment: dict(
        severity_band="WATCH",
        rate_of_change={"river_level": 0.3, "rainfall_rate": None},
        anomaly_indicator={"river_level": 1.4, "rainfall_rate": None},
        confidence=0.9,
        action="execute",
        rationale="River level is rising faster than usual near the ward.",
        unavailable_readings=["rainfall_rate"],
    ),
    EmergencyRequest: dict(
        request_category="RESCUE",
        occupant_count=3,
        location_reference="12th Street, near the temple",
        location_candidates=[],
        mobility_assistance=False,
        medical_need="unknown",
        source_language="ta",
        urgency_band="URGENT",
        confidence=0.85,
        action="execute",
        category="routine_dispatch_ambulatory",
        rationale="Family of three requests rescue near rising water.",
        dispatch_eligible=True,
    ),
    DispatchDecision: dict(
        selected_responder_id="resp_01",
        alternative_responder_ids=["resp_02", "resp_03"],
        selection_rationale="Closest available responder with a boat.",
        confidence=0.92,
        action="execute",
        category="routine_dispatch_ambulatory",
    ),
    AlertDraft: dict(
        channel="sms",
        language="ta",
        affected_areas=["Ward 7 riverside"],
        recommended_action="Move to higher ground now.",
        nearest_shelter="Ward 7 Community Hall",
        validity_start="2026-09-03T14:30:00+05:30",
        validity_end="2026-09-03T20:30:00+05:30",
        confidence=0.8,
        action="execute",
        is_irreversible_action=False,
        audience_count=120,
    ),
    SafetyReview: dict(
        reviewed_content_id="alert_draft_001",
        pass_indicator=True,
        policy_version="v1",
    ),
}


@pytest.mark.parametrize("model_cls", list(VALID_KWARGS.keys()))
def test_valid_instance_constructs(model_cls):
    instance = model_cls(**VALID_KWARGS[model_cls])
    assert 0.0 <= instance.confidence <= 1.0
    assert instance.action in ("execute", "ask_human")
    assert isinstance(instance.is_irreversible_action, bool)
    assert instance.category is not None


@pytest.mark.parametrize("model_cls", list(VALID_KWARGS.keys()))
@pytest.mark.parametrize("bad_confidence", [1.5, -0.1])
def test_invalid_confidence_raises(model_cls, bad_confidence):
    kwargs = {**VALID_KWARGS[model_cls], "confidence": bad_confidence}
    with pytest.raises(ValidationError):
        model_cls(**kwargs)


@pytest.mark.parametrize("model_cls", list(VALID_KWARGS.keys()))
def test_invalid_action_raises(model_cls):
    kwargs = {**VALID_KWARGS[model_cls], "action": "delete_everything"}
    with pytest.raises(ValidationError):
        model_cls(**kwargs)


def test_emergency_request_is_always_ask_property():
    kwargs = {**VALID_KWARGS[EmergencyRequest], "mobility_assistance": True}
    req = EmergencyRequest(**kwargs)
    assert req.is_always_ask is True

    kwargs_false = {
        **VALID_KWARGS[EmergencyRequest],
        "mobility_assistance": False,
        "medical_need": False,
    }
    req_false = EmergencyRequest(**kwargs_false)
    assert req_false.is_always_ask is False


def test_rationale_max_length_enforced():
    long_rationale = "x" * 201
    kwargs = {**VALID_KWARGS[HazardAssessment], "rationale": long_rationale}
    with pytest.raises(ValidationError):
        HazardAssessment(**kwargs)
