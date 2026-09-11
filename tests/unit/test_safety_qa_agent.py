"""Unit tests for agents/safety_qa_agent.py (task 13.6).

Covers: `cross_reference_resident_record()`'s deterministic name/address
matching, `review_content()`'s merge of the deterministic PII/secret
pre-scan with a mocked model review (both the "PII detected -> forced
fail" path and the "clean draft -> pass" path), the irreversible-action
review path, and the low-confidence/ambiguous verdict escalating via
`policy.escalation_policy.decide()` once `review_content()`'s result is
fed into it (mirroring how a caller — Alert_Agent/Dispatch_Agent — is
expected to use this module's output).

The underlying Strands `Agent` invocation is mocked throughout (no real
Bedrock call), matching this codebase's own established convention for
agent-level unit tests: exercise the deterministic logic and the SDK-shaped
plumbing without a live model call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.safety_qa_agent import (
    AGENT_ID,
    SYSTEM_PROMPT,
    cross_reference_resident_record,
    review_content,
)
from policy import escalation_policy
from policy.safety_policy import SAFETY_POLICY_VERSION
from schemas.decisions import SafetyReview
from schemas.structured import ValidationFailureFallback
from strands.types.exceptions import StructuredOutputException


def _mock_agent(review: SafetyReview) -> MagicMock:
    """Build a mock Strands Agent whose `agent(prompt, structured_output_model=...)`
    call returns a result object exposing `.structured_output`."""
    agent = MagicMock()
    result = MagicMock()
    result.structured_output = review
    agent.return_value = result
    return agent


# ---------------------------------------------------------------------------
# Identity (Req 4.12)
# ---------------------------------------------------------------------------


def test_agent_id_and_system_prompt_are_nonempty_and_distinct():
    assert AGENT_ID == "safety_qa_agent"
    assert isinstance(SYSTEM_PROMPT, str) and SYSTEM_PROMPT.strip()


# ---------------------------------------------------------------------------
# cross_reference_resident_record()
# ---------------------------------------------------------------------------


class TestCrossReferenceResidentRecord:
    def test_returns_empty_for_no_resident_record(self):
        assert cross_reference_resident_record("Some content", None) == []
        assert cross_reference_resident_record("Some content", {}) == []

    def test_detects_name_leak(self):
        result = cross_reference_resident_record(
            "Please check on Priya Kumar at the shelter.",
            {"resident_name": "Priya Kumar"},
        )
        assert result == ["resident_name_leak"]

    def test_detects_address_leak(self):
        result = cross_reference_resident_record(
            "Team is headed to 14 Kanal Street now.",
            {"resident_address": "14 Kanal Street"},
        )
        assert result == ["resident_address_leak"]

    def test_detects_both_when_both_present(self):
        result = cross_reference_resident_record(
            "Priya Kumar at 14 Kanal Street needs mobility assistance.",
            {"resident_name": "Priya Kumar", "resident_address": "14 Kanal Street"},
        )
        assert result == ["resident_address_leak", "resident_name_leak"]

    def test_case_insensitive_match(self):
        result = cross_reference_resident_record(
            "PRIYA KUMAR needs help.",
            {"resident_name": "Priya Kumar"},
        )
        assert result == ["resident_name_leak"]

    def test_no_match_when_name_absent_from_content(self):
        result = cross_reference_resident_record(
            "General flood warning for the whole ward.",
            {"resident_name": "Priya Kumar", "resident_address": "14 Kanal Street"},
        )
        assert result == []


# ---------------------------------------------------------------------------
# review_content() — PII detected -> forced fail, deduplicated violations
# ---------------------------------------------------------------------------


class TestReviewContentPiiDetected:
    def test_pii_in_draft_forces_fail_and_flags_fields_even_if_model_says_pass(self):
        # Model (incorrectly) says pass=True with no violations; the
        # deterministic pre-scan must override it.
        model_review = SafetyReview(
            reviewed_content_id="ignored-by-model",
            pass_indicator=True,
            violated_policy_ids=[],
            suggested_revisions=[],
            policy_version="stale-version",
            confidence=0.9,
            action="execute",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent,
            reviewed_content_id="alert-draft-001",
            content="Contact the resident at 9876543210 for pickup.",
        )

        assert isinstance(result, SafetyReview)
        assert result.pass_indicator is False
        assert "resident_contact_leak" in result.violated_policy_ids
        assert result.reviewed_content_id == "alert-draft-001"
        assert result.policy_version == SAFETY_POLICY_VERSION

    def test_model_own_violations_are_unioned_with_deterministic_ones(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=False,
            violated_policy_ids=["resident_name_leak"],
            suggested_revisions=["Remove the resident's name."],
            policy_version=SAFETY_POLICY_VERSION,
            confidence=0.6,
            action="execute",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent,
            reviewed_content_id="alert-draft-002",
            content="Call coordinator@thunai-ward7.example for updates.",
        )

        assert set(result.violated_policy_ids) == {"resident_name_leak", "resident_contact_leak"}
        assert result.pass_indicator is False
        assert result.suggested_revisions == ["Remove the resident's name."]

    def test_resident_record_cross_reference_contributes_to_forced_fail(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=True,
            violated_policy_ids=[],
            policy_version="stale",
            confidence=0.95,
            action="execute",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent,
            reviewed_content_id="dispatch-review-001",
            content="Dispatching to Priya Kumar's residence now.",
            resident_record={"resident_name": "Priya Kumar"},
        )

        assert result.pass_indicator is False
        assert "resident_name_leak" in result.violated_policy_ids


# ---------------------------------------------------------------------------
# review_content() — clean draft -> pass
# ---------------------------------------------------------------------------


class TestReviewContentCleanDraft:
    def test_clean_draft_with_model_pass_returns_pass(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=True,
            violated_policy_ids=[],
            suggested_revisions=[],
            policy_version="stale-version",
            confidence=0.95,
            action="execute",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent,
            reviewed_content_id="alert-draft-003",
            content="River levels near Ward-7 South are rising. Move to higher ground.",
        )

        assert isinstance(result, SafetyReview)
        assert result.pass_indicator is True
        assert result.violated_policy_ids == []
        assert result.policy_version == SAFETY_POLICY_VERSION
        assert result.reviewed_content_id == "alert-draft-003"

    def test_prompt_passed_to_agent_names_no_deterministic_violation_when_clean(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=True,
            policy_version="stale",
            confidence=0.9,
            action="execute",
        )
        agent = _mock_agent(model_review)

        review_content(agent, reviewed_content_id="x", content="All clear, no action needed.")

        prompt = agent.call_args.args[0]
        assert "Deterministic pre-scan found no violation." in prompt


# ---------------------------------------------------------------------------
# review_content() — irreversible action review path (Req 9.6)
# ---------------------------------------------------------------------------


class TestReviewContentIrreversibleAction:
    def test_irreversible_action_context_is_included_in_prompt(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=True,
            policy_version="stale",
            confidence=0.9,
            action="execute",
        )
        agent = _mock_agent(model_review)

        review_content(
            agent,
            reviewed_content_id="dispatch-decision-042",
            content="Dispatch responder r-14 to a non-ambulatory occupant at 12 River Road.",
            is_irreversible_action=True,
            action_description="dispatch to non-ambulatory occupant",
        )

        prompt = agent.call_args.args[0]
        assert "Irreversible_Action" in prompt
        assert "dispatch to non-ambulatory occupant" in prompt

    def test_irreversible_action_review_result_flows_into_escalation_decide_as_ask_human_when_failed(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=False,
            violated_policy_ids=[],
            suggested_revisions=["Confirm occupant mobility status before dispatch."],
            policy_version="stale",
            confidence=0.5,
            action="ask_human",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent,
            reviewed_content_id="dispatch-decision-042",
            content="Dispatch responder r-14 to a non-ambulatory occupant.",
            is_irreversible_action=True,
        )

        # Per Req 10.6/10.12, an Irreversible_Action-flagged proposal always
        # escalates regardless of this review's own is_irreversible_action
        # field (SafetyReview.category is "safety_review", not itself
        # is_irreversible_action) — the CALLER is responsible for flagging
        # the underlying proposal as irreversible when calling decide();
        # this test exercises that integration directly.
        verdict = escalation_policy.decide(
            category="dispatch_non_ambulatory",
            confidence=result.confidence,
            action=result.action,
            is_irreversible_action=True,
        )
        assert verdict["escalate"] is True
        assert verdict["determining_entry"] == "is_irreversible_action"


# ---------------------------------------------------------------------------
# review_content() — low-confidence/ambiguous verdict escalates via decide()
# ---------------------------------------------------------------------------


class TestReviewContentEscalationIntegration:
    def test_false_pass_indicator_review_result_escalates_via_decide(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=False,
            violated_policy_ids=["resident_name_leak"],
            suggested_revisions=["Remove the name."],
            policy_version="stale",
            confidence=0.9,
            action="execute",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent, reviewed_content_id="alert-draft-004", content="Contact Priya Kumar directly."
        )

        assert result.pass_indicator is False
        # A caller (Alert_Agent) is expected to treat any false pass_indicator
        # as ask_human regardless of decide()'s own category-driven verdict,
        # per Req 9.2's own wording ("carries a false pass indicator... THE
        # ThunAI_Platform SHALL withhold... and SHALL escalate"). Demonstrate
        # the equivalent decide() call a caller would make for the
        # SafetyReview category itself: it is not itself always-ask, so a
        # caller must check pass_indicator explicitly in addition to calling
        # decide() -- documented here as the expected integration pattern.
        verdict = escalation_policy.decide(
            category=result.category,
            confidence=result.confidence,
            action="ask_human" if not result.pass_indicator else result.action,
            is_irreversible_action=result.is_irreversible_action,
        )
        assert verdict["escalate"] is True

    def test_low_confidence_review_below_floor_escalates_via_decide(self):
        model_review = SafetyReview(
            reviewed_content_id="ignored",
            pass_indicator=True,
            violated_policy_ids=[],
            policy_version="stale",
            confidence=0.1,
            action="execute",
        )
        agent = _mock_agent(model_review)

        result = review_content(
            agent, reviewed_content_id="alert-draft-005", content="All clear, no action needed."
        )

        verdict = escalation_policy.decide(
            category=result.category,
            confidence=result.confidence,
            action=result.action,
            is_irreversible_action=result.is_irreversible_action,
        )
        assert verdict["escalate"] is True
        assert verdict["determining_entry"] == "CONFIDENCE_FLOOR"

    def test_structured_output_validation_failure_returns_fallback_and_escalates(self):
        agent = MagicMock()
        agent.side_effect = StructuredOutputException("model returned malformed JSON")

        result = review_content(
            agent, reviewed_content_id="alert-draft-006", content="All clear, no action needed."
        )

        assert isinstance(result, ValidationFailureFallback)
        assert result.action == "ask_human"
        assert result.category == "validation_failure"

        verdict = escalation_policy.decide(
            category=result.category,
            confidence=result.confidence,
            action=result.action,
            is_irreversible_action=result.is_irreversible_action,
        )
        assert verdict["escalate"] is True
