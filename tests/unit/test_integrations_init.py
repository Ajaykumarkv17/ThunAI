"""Unit tests for integrations/__init__.py (task 10.4).

Covers check_required_live_resources() naming each specific missing env var,
abort_if_live_resources_missing() raising/not-raising MissingLiveResourceError,
and confirms the three provider factories are importable directly from
`integrations`.
"""

from __future__ import annotations

import pytest

from integrations import (
    MissingLiveResourceError,
    abort_if_live_resources_missing,
    check_required_live_resources,
    get_knowledge_provider,
    get_sensor_provider,
    notification_provider,
)

_ALL_REQUIRED_VARS = (
    "THUNAI_KNOWLEDGE_BASE_ID",
    "THUNAI_SES_SENDER",
    "THUNAI_COGNITO_USER_POOL_ID",
    "THUNAI_STATE_TABLE",
    "THUNAI_AUDIT_TABLE",
    "THUNAI_MEMORY_TABLE",
)


def _set_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_REQUIRED_VARS:
        monkeypatch.setenv(var, f"value-for-{var.lower()}")


def test_check_required_live_resources_empty_when_all_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all(monkeypatch)
    assert check_required_live_resources() == []


def test_check_required_live_resources_names_single_missing_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all(monkeypatch)
    monkeypatch.delenv("THUNAI_KNOWLEDGE_BASE_ID", raising=False)

    problems = check_required_live_resources()

    assert len(problems) == 1
    assert "THUNAI_KNOWLEDGE_BASE_ID" in problems[0]


def test_check_required_live_resources_names_each_of_several_missing_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all(monkeypatch)
    monkeypatch.delenv("THUNAI_SES_SENDER", raising=False)
    monkeypatch.setenv("THUNAI_COGNITO_USER_POOL_ID", "")

    problems = check_required_live_resources()

    assert len(problems) == 2
    joined = " | ".join(problems)
    assert "THUNAI_SES_SENDER" in joined
    assert "THUNAI_COGNITO_USER_POOL_ID" in joined
    # Naming is per-var, not a single generic message.
    assert problems[0] != problems[1]


def test_check_required_live_resources_accepts_explicit_env_mapping() -> None:
    env = {var: "" for var in _ALL_REQUIRED_VARS}
    env["THUNAI_STATE_TABLE"] = "thunai-state"

    problems = check_required_live_resources(env)

    missing_names = {p.split(" ", 1)[0] for p in problems}
    assert missing_names == set(_ALL_REQUIRED_VARS) - {"THUNAI_STATE_TABLE"}


def test_abort_if_live_resources_missing_raises_with_every_missing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all(monkeypatch)
    monkeypatch.delenv("THUNAI_AUDIT_TABLE", raising=False)
    monkeypatch.delenv("THUNAI_MEMORY_TABLE", raising=False)

    with pytest.raises(MissingLiveResourceError) as exc_info:
        abort_if_live_resources_missing()

    message = str(exc_info.value)
    assert "THUNAI_AUDIT_TABLE" in message
    assert "THUNAI_MEMORY_TABLE" in message
    assert len(exc_info.value.problems) == 2


def test_abort_if_live_resources_missing_does_not_raise_when_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all(monkeypatch)

    abort_if_live_resources_missing()  # must not raise


def test_provider_factories_are_importable_from_integrations_package() -> None:
    assert callable(get_sensor_provider)
    assert callable(notification_provider)
    assert callable(get_knowledge_provider)

    from integrations.knowledge_provider import get_knowledge_provider as direct_kp
    from integrations.notification_provider import notification_provider as direct_np
    from integrations.sensor_provider import get_sensor_provider as direct_sp

    assert get_sensor_provider is direct_sp
    assert notification_provider is direct_np
    assert get_knowledge_provider is direct_kp
