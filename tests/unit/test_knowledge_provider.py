"""Unit tests for integrations/knowledge_provider.py (task 10.3).

`BedrockKBKnowledgeProvider` is the ONLY `KnowledgeProvider` implementation
(LIVE ONLY, no synthetic backend). Per the module's own verification notes,
`moto` (moto==5.2.3, pinned in pyproject.toml's `test` extra) does not cover
`bedrock-agent-runtime`, so these tests use a constructor-injected
`unittest.mock.MagicMock` in place of a real boto3 client rather than `moto`.
No real AWS calls are made.
"""

from unittest.mock import MagicMock

import pytest

from integrations.knowledge_provider import (
    BedrockKBKnowledgeProvider,
    KnowledgeProviderConfigurationError,
    Passage,
    get_knowledge_provider,
)

SAMPLE_RETRIEVE_RESPONSE = {
    "retrievalResults": [
        {
            "content": {"text": "Move to higher ground if the river rises above the warning mark.", "type": "TEXT"},
            "location": {"type": "S3", "s3Location": {"uri": "s3://thunai-kb-docs/water_safety_en.md"}},
            "metadata": {"source_version": "2026-09-v1", "topic": "water_safety", "language": "en"},
            "score": 0.87,
            "documentId": "doc-water-safety-en",
        },
        {
            "content": {"text": "Keep a packed evacuation kit ready near the door.", "type": "TEXT"},
            "location": {"type": "S3", "s3Location": {"uri": "s3://thunai-kb-docs/evacuation_kit_en.md"}},
            # No custom metadata attached yet for this document (ingestion sidecar not yet wired).
            "metadata": {},
            "score": 0.61,
            "documentId": "doc-evac-kit-en",
        },
    ]
}


def _provider_with_mock_client(kb_id: str = "KB1234567890") -> tuple[BedrockKBKnowledgeProvider, MagicMock]:
    mock_client = MagicMock()
    provider = BedrockKBKnowledgeProvider(
        knowledge_base_id=kb_id, region_name="us-west-2", client=mock_client
    )
    return provider, mock_client


def test_retrieve_calls_bedrock_agent_runtime_with_correct_request_shape():
    provider, mock_client = _provider_with_mock_client(kb_id="KB1234567890")
    mock_client.retrieve.return_value = SAMPLE_RETRIEVE_RESPONSE

    provider.retrieve("Is it safe to cross the bridge right now?", max_results=3)

    mock_client.retrieve.assert_called_once_with(
        knowledgeBaseId="KB1234567890",
        retrievalQuery={"text": "Is it safe to cross the bridge right now?"},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 3}},
    )


def test_retrieve_uses_default_max_results_of_five():
    provider, mock_client = _provider_with_mock_client()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    provider.retrieve("What should be in an evacuation kit?")

    _, kwargs = mock_client.retrieve.call_args
    assert kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 5


def test_retrieve_maps_response_into_typed_passages_with_source_version_when_present():
    provider, mock_client = _provider_with_mock_client()
    mock_client.retrieve.return_value = SAMPLE_RETRIEVE_RESPONSE

    passages = provider.retrieve("flood safety")

    assert len(passages) == 2
    first = passages[0]
    assert isinstance(first, Passage)
    assert first.text == "Move to higher ground if the river rises above the warning mark."
    assert first.score == pytest.approx(0.87)
    assert first.source_id == "s3://thunai-kb-docs/water_safety_en.md"
    assert first.source_version == "2026-09-v1"
    assert first.document_id == "doc-water-safety-en"
    assert first.metadata["topic"] == "water_safety"


def test_retrieve_maps_missing_source_version_to_none_rather_than_raising():
    provider, mock_client = _provider_with_mock_client()
    mock_client.retrieve.return_value = SAMPLE_RETRIEVE_RESPONSE

    passages = provider.retrieve("evacuation kit contents")

    second = passages[1]
    assert second.source_version is None
    assert second.source_id == "s3://thunai-kb-docs/evacuation_kit_en.md"


def test_retrieve_falls_back_to_document_id_when_location_has_no_recognised_sub_object():
    provider, mock_client = _provider_with_mock_client()
    mock_client.retrieve.return_value = {
        "retrievalResults": [
            {
                "content": {"text": "some passage"},
                "location": {"type": "CUSTOM"},
                "metadata": {},
                "score": 0.5,
                "documentId": "doc-fallback",
            }
        ]
    }

    passages = provider.retrieve("query")

    assert passages[0].source_id == "doc-fallback"


def test_retrieve_falls_back_to_unknown_when_no_location_and_no_document_id():
    provider, mock_client = _provider_with_mock_client()
    mock_client.retrieve.return_value = {
        "retrievalResults": [
            {"content": {"text": "some passage"}, "location": {}, "metadata": {}, "score": 0.5}
        ]
    }

    passages = provider.retrieve("query")

    assert passages[0].source_id == "unknown"


def test_retrieve_returns_empty_list_when_no_results():
    provider, mock_client = _provider_with_mock_client()
    mock_client.retrieve.return_value = {"retrievalResults": []}

    assert provider.retrieve("obscure question") == []


def test_constructor_raises_clear_error_when_knowledge_base_id_is_empty():
    with pytest.raises(KnowledgeProviderConfigurationError, match="THUNAI_KNOWLEDGE_BASE_ID"):
        BedrockKBKnowledgeProvider(knowledge_base_id="", client=MagicMock())


def test_constructor_raises_clear_error_when_knowledge_base_id_is_none_like_empty_string():
    with pytest.raises(KnowledgeProviderConfigurationError):
        BedrockKBKnowledgeProvider(knowledge_base_id="", region_name="us-west-2", client=MagicMock())


def test_get_knowledge_provider_factory_raises_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("THUNAI_KNOWLEDGE_BASE_ID", raising=False)

    with pytest.raises(KnowledgeProviderConfigurationError):
        get_knowledge_provider()


def test_get_knowledge_provider_factory_returns_bedrock_provider_when_env_var_set(monkeypatch):
    monkeypatch.setenv("THUNAI_KNOWLEDGE_BASE_ID", "KB1234567890")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    provider = get_knowledge_provider()

    assert isinstance(provider, BedrockKBKnowledgeProvider)
