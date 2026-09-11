"""Unit tests for tools/knowledge_tools.py (task 12.6).

Validates: Requirements 8.1, 8.1a; Design §3.1 (Knowledge), §3.7, Open
Question 6.

`retrieve_passages` wraps `integrations.get_knowledge_provider()`
(`BedrockKBKnowledgeProvider`, task 10.3). These tests mock the provider
factory (and, transitively, the underlying `KnowledgeProvider`) rather than
touching the live `bedrock-agent-runtime` API, matching
`tests/unit/test_knowledge_provider.py`'s own no-live-AWS-calls convention.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from integrations.knowledge_provider import KnowledgeProviderConfigurationError, Passage
from tools.knowledge_tools import (
    DEFAULT_KNOWLEDGE_MAX_PASSAGE_COUNT,
    retrieve_passages,
)


def _call(tool_fn, **kwargs):
    """Invoke a `@tool`-decorated function directly with plain keyword
    arguments, matching `tests/unit/test_sensor_tools.py`'s established
    direct-invocation helper.
    """
    return tool_fn(**kwargs)


SAMPLE_PASSAGES = [
    Passage(
        text="Move to higher ground if the river rises above the warning mark.",
        score=0.87,
        source_id="s3://thunai-kb-docs/water_safety_en.md",
        source_version="2026-09-v1",
        document_id="doc-water-safety-en",
        metadata={"source_version": "2026-09-v1", "topic": "water_safety"},
    ),
    Passage(
        text="Keep a packed evacuation kit ready near the door.",
        score=0.61,
        source_id="s3://thunai-kb-docs/evacuation_kit_en.md",
        source_version=None,
        document_id="doc-evac-kit-en",
        metadata={},
    ),
]


def test_retrieve_passages_returns_ok_with_scored_passages(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_provider = MagicMock()
    mock_provider.retrieve.return_value = SAMPLE_PASSAGES
    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", lambda: mock_provider)

    result = _call(retrieve_passages, query="Is it safe to cross the bridge right now?")

    assert result["ok"] is True
    assert result["query"] == "Is it safe to cross the bridge right now?"
    assert result["passage_count"] == 2
    assert result["no_relevant_passages_found"] is False
    assert result["reason"] is None

    first, second = result["passages"]
    assert first["text"] == "Move to higher ground if the river rises above the warning mark."
    assert first["score"] == pytest.approx(0.87)
    assert first["source_id"] == "s3://thunai-kb-docs/water_safety_en.md"
    assert first["source_version"] == "2026-09-v1"
    assert first["document_id"] == "doc-water-safety-en"
    assert second["source_version"] is None

    mock_provider.retrieve.assert_called_once_with(
        "Is it safe to cross the bridge right now?", max_results=DEFAULT_KNOWLEDGE_MAX_PASSAGE_COUNT
    )


def test_retrieve_passages_respects_explicit_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_provider = MagicMock()
    mock_provider.retrieve.return_value = []
    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", lambda: mock_provider)

    _call(retrieve_passages, query="evacuation kit contents", max_results=3)

    mock_provider.retrieve.assert_called_once_with("evacuation kit contents", max_results=3)


def test_retrieve_passages_respects_env_override_for_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_provider = MagicMock()
    mock_provider.retrieve.return_value = []
    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", lambda: mock_provider)
    monkeypatch.setenv("THUNAI_KNOWLEDGE_MAX_PASSAGE_COUNT", "2")

    _call(retrieve_passages, query="flood safety")

    mock_provider.retrieve.assert_called_once_with("flood safety", max_results=2)


def test_retrieve_passages_signals_no_relevant_passages_found_on_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_provider = MagicMock()
    mock_provider.retrieve.return_value = []
    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", lambda: mock_provider)

    result = _call(retrieve_passages, query="an obscure question with no match")

    assert result["ok"] is True
    assert result["passages"] == []
    assert result["passage_count"] == 0
    assert result["no_relevant_passages_found"] is True
    assert result["reason"] is None


def test_retrieve_passages_reports_unavailable_when_provider_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_config_error():
        raise KnowledgeProviderConfigurationError("THUNAI_KNOWLEDGE_BASE_ID is unset")

    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", _raise_config_error)

    result = _call(retrieve_passages, query="water safety")

    assert result["ok"] is False
    assert result["passages"] == []
    assert result["passage_count"] == 0
    assert result["no_relevant_passages_found"] is True
    assert result["reason"] == "knowledge_base_unavailable"


def test_retrieve_passages_reports_unavailable_on_retrieve_call_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_provider = MagicMock()
    mock_provider.retrieve.side_effect = RuntimeError("boto3 ClientError: ThrottlingException")
    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", lambda: mock_provider)

    result = _call(retrieve_passages, query="shelter guidance")

    assert result["ok"] is False
    assert result["reason"] == "knowledge_base_unavailable"


def test_retrieve_passages_reports_unavailable_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def _slow_retrieve(query, *, max_results=5):
        time.sleep(0.5)
        return SAMPLE_PASSAGES

    mock_provider = MagicMock()
    mock_provider.retrieve.side_effect = _slow_retrieve
    monkeypatch.setattr("tools.knowledge_tools.get_knowledge_provider", lambda: mock_provider)
    monkeypatch.setenv("THUNAI_KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS", "0.05")

    result = _call(retrieve_passages, query="evacuation basics")

    assert result["ok"] is False
    assert result["reason"] == "knowledge_base_unavailable"


def test_retrieve_passages_is_decorated_and_carries_docstring() -> None:
    assert hasattr(retrieve_passages, "tool_spec")
    assert retrieve_passages.__doc__
    assert "Args:" in retrieve_passages.__doc__
    assert "Returns:" in retrieve_passages.__doc__
