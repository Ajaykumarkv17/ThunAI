"""Unit tests for agents/knowledge_agent.py (task 13.7).

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9; Design
§3.1 (Knowledge).

Every test drives `answer_question` directly with a stubbed
`produce_answer` callable (mirroring `agents/dispatch_agent.py`'s and
`agents/intake_agent.py`'s own established "never invoke a live Bedrock
model in this suite" convention), against moto-mocked `thunai-state`/
`thunai-audit` tables (needed because `answer_question` calls
`tools.escalation_tools.create_escalation` and
`harness.audit.append_audit_entry` on every branch).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from agents import knowledge_agent
from memory import audit_ledger, state_store
from schemas.structured import ValidationFailureFallback

STATE_TABLE_NAME = "thunai-state-knowledge-agent-test"
AUDIT_TABLE_NAME = "thunai-audit-knowledge-agent-test"


def _create_table(client, table_name: str) -> None:
    client.create_table(
        TableName=table_name,
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


@pytest.fixture(autouse=True)
def _dynamo_tables(monkeypatch):
    monkeypatch.setenv("THUNAI_STATE_TABLE", STATE_TABLE_NAME)
    monkeypatch.setenv("THUNAI_AUDIT_TABLE", AUDIT_TABLE_NAME)
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("THUNAI_CONFIGURED_LANGUAGES", raising=False)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        _create_table(client, STATE_TABLE_NAME)
        _create_table(client, AUDIT_TABLE_NAME)
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


def _passage(**overrides) -> dict:
    defaults = dict(
        text="Move to higher ground if the river rises above the warning mark.",
        score=0.87,
        source_id="s3://thunai-kb-docs/water_safety_en.md",
        source_version="2026-09-v1",
        document_id="doc-water-safety-en",
    )
    defaults.update(overrides)
    return defaults


def _mock_retrieve_ok(monkeypatch, passages: list[dict]) -> None:
    monkeypatch.setattr(
        knowledge_agent,
        "retrieve_passages",
        lambda query, max_results=None: {
            "ok": True,
            "query": query,
            "passages": passages,
            "passage_count": len(passages),
            "no_relevant_passages_found": len(passages) == 0,
            "reason": None,
        },
    )


def _mock_retrieve_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        knowledge_agent,
        "retrieve_passages",
        lambda query, max_results=None: {
            "ok": False,
            "query": query,
            "passages": [],
            "passage_count": 0,
            "no_relevant_passages_found": True,
            "reason": "knowledge_base_unavailable",
        },
    )


def _mock_detect_language(monkeypatch, *, detected="en", outside=False) -> None:
    monkeypatch.setattr(
        knowledge_agent.intake_tools,
        "detect_language",
        lambda text: {
            "ok": True,
            "detected_language": detected,
            "is_outside_configured_languages": outside,
        },
    )


def _composed(**overrides) -> knowledge_agent._ComposedAnswer:
    defaults = dict(
        answer_text="Move to higher ground now.",
        cited_source_ids=["s3://thunai-kb-docs/water_safety_en.md"],
        requires_advisory_disclaimer=False,
        confidence=0.9,
    )
    defaults.update(overrides)
    return knowledge_agent._ComposedAnswer(**defaults)


# ---------------------------------------------------------------------------
# Identity (Req 4.12)
# ---------------------------------------------------------------------------


def test_agent_id_and_system_prompt_are_nonempty_and_distinct():
    assert knowledge_agent.AGENT_ID == "knowledge_agent"
    assert isinstance(knowledge_agent.SYSTEM_PROMPT, str) and knowledge_agent.SYSTEM_PROMPT.strip()


def test_build_knowledge_agent_has_no_session_manager_and_no_tools():
    agent = knowledge_agent.build_knowledge_agent()
    assert agent.agent_id == "knowledge_agent"
    # No tools registered -- the model never calls retrieve_passages itself.
    assert list(agent.tool_names) == []


# ---------------------------------------------------------------------------
# select_grounding_passages (Req 8.6)
# ---------------------------------------------------------------------------


class TestSelectGroundingPassages:
    def test_keeps_passages_at_or_above_floor(self):
        passages = [_passage(score=0.9), _passage(score=0.5)]
        result = knowledge_agent.select_grounding_passages(passages, relevance_floor=0.5)
        assert len(result) == 2

    def test_drops_passages_below_floor(self):
        passages = [_passage(score=0.9), _passage(score=0.2)]
        result = knowledge_agent.select_grounding_passages(passages, relevance_floor=0.5)
        assert len(result) == 1
        assert result[0]["score"] == 0.9

    def test_empty_input_returns_empty(self):
        assert knowledge_agent.select_grounding_passages([], relevance_floor=0.5) == []

    def test_never_mutates_input_list(self):
        passages = [_passage(score=0.9)]
        original = list(passages)
        knowledge_agent.select_grounding_passages(passages, relevance_floor=0.5)
        assert passages == original

    def test_default_floor_used_when_omitted(self, monkeypatch):
        monkeypatch.setenv(knowledge_agent.RELEVANCE_FLOOR_ENV_VAR, "0.8")
        passages = [_passage(score=0.7), _passage(score=0.85)]
        result = knowledge_agent.select_grounding_passages(passages)
        assert len(result) == 1
        assert result[0]["score"] == 0.85


# ---------------------------------------------------------------------------
# citations_are_grounded_in_offered_passages (Req 8.3)
# ---------------------------------------------------------------------------


class TestCitationsAreGrounded:
    def test_no_defects_when_every_citation_was_offered(self):
        offered = [_passage(source_id="a"), _passage(source_id="b")]
        defects = knowledge_agent.citations_are_grounded_in_offered_passages(["a"], offered)
        assert defects == []

    def test_flags_citation_not_among_offered_passages(self):
        offered = [_passage(source_id="a")]
        defects = knowledge_agent.citations_are_grounded_in_offered_passages(["a", "z"], offered)
        assert len(defects) == 1
        assert "'z'" in defects[0]

    def test_empty_citations_produce_no_defects(self):
        offered = [_passage(source_id="a")]
        assert knowledge_agent.citations_are_grounded_in_offered_passages([], offered) == []


# ---------------------------------------------------------------------------
# answer_question: grounded answer with citation (Req 8.1, 8.2, 8.3, 8.9)
# ---------------------------------------------------------------------------


class TestGroundedAnswer:
    def test_grounded_answer_carries_citation_and_source_version(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a", source_version="v1")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        produce_answer = MagicMock(
            return_value=_composed(cited_source_ids=["doc-a"], confidence=0.9)
        )

        result = knowledge_agent.answer_question(
            question_id="q-1",
            run_id="run-1",
            question="Is it safe to cross the bridge right now?",
            produce_answer=produce_answer,
        )

        assert result["ok"] is True
        assert result["outcome"] == "answered"
        answer = result["answer"]
        assert answer.answer_text == "Move to higher ground now."
        assert len(answer.citations) == 1
        assert answer.citations[0].source_id == "doc-a"
        assert answer.citations[0].source_version == "v1"
        assert answer.category == "knowledge_answer_grounded"
        assert answer.action == "execute"
        assert result["citation_defects"] == []

        # produce_answer was given only the cleared passage.
        grounding_context = produce_answer.call_args.args[0]
        assert [p["source_id"] for p in grounding_context["passages"]] == ["doc-a"]

    def test_low_confidence_grounded_answer_sets_action_ask_human(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        result = knowledge_agent.answer_question(
            question_id="q-2",
            run_id="run-1",
            question="Some question",
            produce_answer=lambda ctx: _composed(cited_source_ids=["doc-a"], confidence=0.1),
        )

        assert result["answer"].action == "ask_human"

    def test_citation_not_offered_is_flagged_but_still_answers(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        result = knowledge_agent.answer_question(
            question_id="q-3",
            run_id="run-1",
            question="Some question",
            produce_answer=lambda ctx: _composed(cited_source_ids=["doc-a", "not-offered"]),
        )

        assert result["outcome"] == "answered"
        assert len(result["citation_defects"]) == 1
        # Only the actually-offered citation is included on the answer.
        assert [c.source_id for c in result["answer"].citations] == ["doc-a"]


# ---------------------------------------------------------------------------
# answer_question: no-answer / relevance-floor routing (Req 8.6)
# ---------------------------------------------------------------------------


class TestNoAnswerRouting:
    def test_no_passage_above_floor_escalates_without_calling_produce_answer(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.1)])
        _mock_detect_language(monkeypatch, detected="en", outside=False)
        produce_answer = MagicMock()

        result = knowledge_agent.answer_question(
            question_id="q-4",
            run_id="run-1",
            question="An obscure question",
            produce_answer=produce_answer,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "knowledge_no_answer"
        produce_answer.assert_not_called()

    def test_empty_passages_escalates_no_answer(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        result = knowledge_agent.answer_question(
            question_id="q-5",
            run_id="run-1",
            question="An obscure question",
            produce_answer=lambda ctx: _composed(),
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "knowledge_no_answer"


# ---------------------------------------------------------------------------
# answer_question: retrieval failure/unavailable (Req 8.7)
# ---------------------------------------------------------------------------


class TestRetrievalUnavailable:
    def test_retrieval_failure_escalates_without_calling_produce_answer(self, monkeypatch):
        _mock_retrieve_unavailable(monkeypatch)
        _mock_detect_language(monkeypatch, detected="en", outside=False)
        produce_answer = MagicMock()

        result = knowledge_agent.answer_question(
            question_id="q-6",
            run_id="run-1",
            question="Any question",
            produce_answer=produce_answer,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "knowledge_unavailable"
        produce_answer.assert_not_called()


# ---------------------------------------------------------------------------
# answer_question: validation failure (Req 10.11)
# ---------------------------------------------------------------------------


class TestValidationFailure:
    def test_validation_failure_escalates(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9)])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        fallback = ValidationFailureFallback(raw_output="garbage", error_detail="boom")
        result = knowledge_agent.answer_question(
            question_id="q-7",
            run_id="run-1",
            question="Any question",
            produce_answer=lambda ctx: fallback,
        )

        assert result["outcome"] == "escalated"
        assert result["escalation_type"] == "validation_failure"


# ---------------------------------------------------------------------------
# Language handling (Req 8.4, 8.5)
# ---------------------------------------------------------------------------


class TestLanguageHandling:
    def test_answers_in_detected_language_when_within_configured_set(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="ta", outside=False)

        captured = {}

        def _produce(ctx):
            captured.update(ctx)
            return _composed(cited_source_ids=["doc-a"])

        result = knowledge_agent.answer_question(
            question_id="q-8", run_id="run-1", question="தமிழ் கேள்வி", produce_answer=_produce
        )

        assert captured["answer_language"] == "ta"
        assert captured["question_language_recognised"] is True
        assert result["answer"].answer_language == "ta"
        assert result["answer"].question_language_recognised is True

    def test_falls_back_to_default_language_when_outside_configured_set(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="unknown", outside=True)

        captured = {}

        def _produce(ctx):
            captured.update(ctx)
            return _composed(cited_source_ids=["doc-a"])

        result = knowledge_agent.answer_question(
            question_id="q-9", run_id="run-1", question="12345 !!!", produce_answer=_produce
        )

        assert captured["answer_language"] == knowledge_agent.intake_tools.DEFAULT_LANGUAGE
        assert captured["question_language_recognised"] is False
        assert result["answer"].question_language_recognised is False


# ---------------------------------------------------------------------------
# Advisory disclaimer (Req 8.8)
# ---------------------------------------------------------------------------


class TestAdvisoryDisclaimer:
    def test_disclaimer_prefixed_when_model_flags_advisory_category(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        result = knowledge_agent.answer_question(
            question_id="q-10",
            run_id="run-1",
            question="Should I try to fix the cracked wall myself?",
            produce_answer=lambda ctx: _composed(
                cited_source_ids=["doc-a"], requires_advisory_disclaimer=True
            ),
        )

        assert result["answer"].answer_text.startswith(knowledge_agent.ADVISORY_DISCLAIMER)

    def test_no_disclaimer_when_not_flagged(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        result = knowledge_agent.answer_question(
            question_id="q-11",
            run_id="run-1",
            question="Is the water level rising?",
            produce_answer=lambda ctx: _composed(
                cited_source_ids=["doc-a"], requires_advisory_disclaimer=False
            ),
        )

        assert not result["answer"].answer_text.startswith(knowledge_agent.ADVISORY_DISCLAIMER)


# ---------------------------------------------------------------------------
# Multiple candidate passages with relevance scores (Req 8.1, 8.2, 8.9)
# ---------------------------------------------------------------------------


class TestMultipleCandidatePassages:
    def test_only_passages_clearing_floor_are_offered_and_others_ignored(self, monkeypatch):
        passages = [
            _passage(score=0.9, source_id="doc-high"),
            _passage(score=0.6, source_id="doc-mid"),
            _passage(score=0.2, source_id="doc-low"),
        ]
        _mock_retrieve_ok(monkeypatch, passages)
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        captured = {}

        def _produce(ctx):
            captured.update(ctx)
            return _composed(cited_source_ids=["doc-high", "doc-mid"])

        result = knowledge_agent.answer_question(
            question_id="q-12", run_id="run-1", question="Flood safety question", produce_answer=_produce
        )

        offered_ids = {p["source_id"] for p in captured["passages"]}
        assert offered_ids == {"doc-high", "doc-mid"}
        assert result["citation_defects"] == []
        assert {c.source_id for c in result["answer"].citations} == {"doc-high", "doc-mid"}


# ---------------------------------------------------------------------------
# Audit-ledger recording (Req 8.9): every branch writes the question, the
# detected language, the retrieved passage ids with their scores, and the
# returned answer / no-answer outcome.
# ---------------------------------------------------------------------------


class TestAuditLedgerRecording:
    def _last_entry_for(self, run_id: str):
        entries = audit_ledger.get_entries_for_run(run_id)
        knowledge_entries = [e for e in entries if e.tool_name == "knowledge_agent"]
        assert knowledge_entries, "no knowledge_agent audit entry was written"
        return knowledge_entries[-1]

    def test_answered_branch_records_question_language_and_passage_scores(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.9, source_id="doc-a")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        knowledge_agent.answer_question(
            question_id="q-audit-1",
            run_id="run-audit-1",
            question="Is the bridge safe?",
            produce_answer=lambda ctx: _composed(cited_source_ids=["doc-a"]),
        )

        entry = self._last_entry_for("run-audit-1")
        assert entry.outcome == "answered"
        assert entry.inputs["question"] == "Is the bridge safe?"
        assert entry.inputs["detected_language"] == "en"
        assert entry.inputs["retrieved_passages"] == [{"source_id": "doc-a", "score": 0.9}]

    def test_no_answer_branch_records_retrieved_passage_scores(self, monkeypatch):
        _mock_retrieve_ok(monkeypatch, [_passage(score=0.1, source_id="doc-low")])
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        knowledge_agent.answer_question(
            question_id="q-audit-2",
            run_id="run-audit-2",
            question="Obscure question",
            produce_answer=lambda ctx: _composed(),
        )

        entry = self._last_entry_for("run-audit-2")
        assert entry.outcome == "escalated:knowledge_no_answer"
        assert entry.inputs["retrieved_passages"] == [{"source_id": "doc-low", "score": 0.1}]

    def test_unavailable_branch_records_outcome(self, monkeypatch):
        _mock_retrieve_unavailable(monkeypatch)
        _mock_detect_language(monkeypatch, detected="en", outside=False)

        knowledge_agent.answer_question(
            question_id="q-audit-3",
            run_id="run-audit-3",
            question="Any question",
            produce_answer=lambda ctx: _composed(),
        )

        entry = self._last_entry_for("run-audit-3")
        assert entry.outcome == "escalated:knowledge_unavailable"
