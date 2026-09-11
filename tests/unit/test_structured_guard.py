"""Unit tests for schemas/structured.py::safe_structured() (task 2.4).

Covers the success path, the two caught validation-failure exception types
(`StructuredOutputException` and `pydantic.ValidationError`), and confirms
an unrelated/unexpected exception is NOT swallowed by the guard (Req 10.11
names structured-output validation failure specifically; it does not extend
to arbitrary errors).
"""

import pytest
from pydantic import ValidationError

from schemas.decisions import HazardAssessment
from schemas.structured import ValidationFailureFallback, safe_structured
from strands.types.exceptions import StructuredOutputException

VALID_HAZARD_KWARGS = dict(
    severity_band="WATCH",
    rate_of_change={"river_level": 0.3},
    anomaly_indicator={"river_level": 1.4},
    confidence=0.9,
    action="execute",
    rationale="River level is rising faster than usual near the ward.",
    unavailable_readings=[],
)


def test_success_path_returns_instance_unchanged():
    expected = HazardAssessment(**VALID_HAZARD_KWARGS)
    result = safe_structured(lambda: expected, HazardAssessment)
    assert result is expected


def test_structured_output_exception_is_caught_and_synthesizes_ask_human():
    def raising():
        raise StructuredOutputException("model returned malformed JSON: {bad")

    result = safe_structured(raising, HazardAssessment)

    assert isinstance(result, ValidationFailureFallback)
    assert result.action == "ask_human"
    assert result.confidence == 0.0
    assert result.category == "validation_failure"
    assert result.is_irreversible_action is False
    assert "malformed JSON" in result.raw_output
    assert "HazardAssessment" in result.error_detail


def test_pydantic_validation_error_is_caught_and_synthesizes_ask_human():
    def raising():
        # Trigger a real ValidationError via direct model_validate, as a
        # non-Agent call site would.
        HazardAssessment.model_validate({"severity_band": "NOT_A_BAND"})

    result = safe_structured(raising, HazardAssessment)

    assert isinstance(result, ValidationFailureFallback)
    assert result.action == "ask_human"
    assert result.confidence == 0.0
    assert result.category == "validation_failure"
    assert result.is_irreversible_action is False
    assert result.raw_output
    assert "HazardAssessment" in result.error_detail


def test_unrelated_exception_is_not_swallowed():
    def raising():
        raise RuntimeError("network timeout talking to the model provider")

    with pytest.raises(RuntimeError, match="network timeout"):
        safe_structured(raising, HazardAssessment)


def test_unrelated_exception_type_validation_error_is_not_confused_with_other_errors():
    # Sanity check: a bare ValueError (not a pydantic ValidationError) must
    # not be caught either.
    def raising():
        raise ValueError("some unrelated programming error")

    with pytest.raises(ValueError, match="some unrelated programming error"):
        safe_structured(raising, HazardAssessment)
