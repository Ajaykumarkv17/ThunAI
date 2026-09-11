"""Unit tests for agents/config.py (task 13.1).

Covers `get_model_id_for_role()` (known/unknown roles), `MODEL_FOR_ROLE`'s
exact role coverage, and `validate_config()`'s pass/fail behaviour, using
the mapping/env-value parameters `validate_config()` exposes so the module
is testable without mutating real process environment state where possible.
"""

import pytest

from agents.config import (
    AGENT_ROLES,
    MODEL_FOR_ROLE,
    get_model_id_for_role,
    validate_config,
)


def test_get_model_id_for_role_returns_correct_id_for_known_role():
    assert get_model_id_for_role("intake_agent") == MODEL_FOR_ROLE["intake_agent"]
    assert get_model_id_for_role("dispatch_agent") == MODEL_FOR_ROLE["dispatch_agent"]


def test_get_model_id_for_role_raises_on_unknown_role():
    with pytest.raises(ValueError, match="Unknown agent role"):
        get_model_id_for_role("not_a_real_role")


def test_model_for_role_covers_exactly_the_seven_declared_roles():
    assert set(MODEL_FOR_ROLE) == AGENT_ROLES
    assert len(AGENT_ROLES) == 7


def test_validate_config_passes_with_complete_valid_config():
    validate_config(
        model_for_role={role: "some.model-id:0" for role in AGENT_ROLES},
        low_cost_model_id="us.amazon.nova-lite-v1:0",
        high_capability_model_id="us.amazon.nova-pro-v1:0",
    )


def test_validate_config_uses_real_env_by_default(monkeypatch):
    monkeypatch.setattr("agents.config.LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
    monkeypatch.setattr("agents.config.HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")
    monkeypatch.setattr(
        "agents.config.MODEL_FOR_ROLE",
        {role: "us.amazon.nova-lite-v1:0" for role in AGENT_ROLES},
    )
    validate_config()


def test_validate_config_raises_naming_missing_model_id_env_vars():
    with pytest.raises(ValueError) as exc_info:
        validate_config(
            model_for_role={role: "some.model-id:0" for role in AGENT_ROLES},
            low_cost_model_id="",
            high_capability_model_id="",
        )
    message = str(exc_info.value)
    assert "THUNAI_LOW_COST_MODEL_ID" in message
    assert "THUNAI_HIGH_CAPABILITY_MODEL_ID" in message


def test_validate_config_raises_naming_every_missing_role():
    incomplete_mapping = {"monitor_agent": "some.model-id:0"}
    with pytest.raises(ValueError) as exc_info:
        validate_config(
            model_for_role=incomplete_mapping,
            low_cost_model_id="us.amazon.nova-lite-v1:0",
            high_capability_model_id="us.amazon.nova-pro-v1:0",
        )
    message = str(exc_info.value)
    assert "missing role(s)" in message
    for role in AGENT_ROLES - {"monitor_agent"}:
        assert role in message


def test_validate_config_raises_naming_extra_roles():
    mapping = {role: "some.model-id:0" for role in AGENT_ROLES}
    mapping["not_a_real_role"] = "some.model-id:0"
    with pytest.raises(ValueError) as exc_info:
        validate_config(
            model_for_role=mapping,
            low_cost_model_id="us.amazon.nova-lite-v1:0",
            high_capability_model_id="us.amazon.nova-pro-v1:0",
        )
    assert "unrecognised role(s)" in str(exc_info.value)
    assert "not_a_real_role" in str(exc_info.value)


def test_validate_config_raises_naming_role_with_empty_model_id():
    mapping = {role: "some.model-id:0" for role in AGENT_ROLES}
    mapping["alert_agent"] = ""
    with pytest.raises(ValueError) as exc_info:
        validate_config(
            model_for_role=mapping,
            low_cost_model_id="us.amazon.nova-lite-v1:0",
            high_capability_model_id="us.amazon.nova-pro-v1:0",
        )
    assert "alert_agent" in str(exc_info.value)


def test_validate_config_reports_multiple_problems_at_once():
    with pytest.raises(ValueError) as exc_info:
        validate_config(
            model_for_role={"monitor_agent": "some.model-id:0"},
            low_cost_model_id="",
            high_capability_model_id="us.amazon.nova-pro-v1:0",
        )
    message = str(exc_info.value)
    assert "THUNAI_LOW_COST_MODEL_ID" in message
    assert "missing role(s)" in message
