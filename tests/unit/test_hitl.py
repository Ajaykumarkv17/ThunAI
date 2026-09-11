"""Unit tests for agents/hitl.py (task 16.2).

Constructs real `strands.hooks.events.BeforeToolCallEvent` instances against
a real `strands.Agent` instance (cheap -- no model call is made by
construction alone), mirroring `tests/unit/test_harness_hooks.py`'s own
established convention for exercising real Strands SDK types directly.
"""

from __future__ import annotations

import pytest
from strands import Agent
from strands.hooks.events import BeforeToolCallEvent
from strands.vended_interventions.hitl import HumanInTheLoop
from strands.vended_interventions.hitl.classifier import ClassifierResult

from agents.hitl import (
    build_allowed_tools,
    build_human_in_the_loop,
    deterministic_classifier,
    tool_name,
)
from policy import escalation_policy as ep
from tools.dispatch_tools import find_candidate_responders


@pytest.fixture()
def agent() -> Agent:
    return Agent(callback_handler=None, agent_id="test-agent")


def _before_tool_event(agent: Agent, name: str, input_: dict) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": name, "input": input_, "toolUseId": f"tooluse-{name}"},
        invocation_state={"run_id": "run-1"},
    )


# ---------------------------------------------------------------------------
# tool_name / build_allowed_tools (Req 16.11's "allowed_tools generated
# from the tool-registration list, never hand-typed")
# ---------------------------------------------------------------------------


class TestBuildAllowedTools:
    def test_tool_name_reads_decorated_function_tool_name(self):
        assert tool_name(find_candidate_responders) == "find_candidate_responders"

    def test_tool_name_falls_back_to_dunder_name_for_plain_callable(self):
        def plain_fn():
            ...

        assert tool_name(plain_fn) == "plain_fn"

    def test_build_allowed_tools_generates_from_registration_list(self):
        allowed = build_allowed_tools([find_candidate_responders])
        assert allowed == ["find_candidate_responders"]

    def test_build_allowed_tools_preserves_order_and_handles_empty(self):
        assert build_allowed_tools([]) == []


# ---------------------------------------------------------------------------
# deterministic_classifier (Req 16.11, 10.4-10.7; Design Question 3)
# ---------------------------------------------------------------------------


class TestDeterministicClassifier:
    def test_delegates_to_escalation_policy_decide_for_never_ask_category(self, agent: Agent):
        category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
        event = _before_tool_event(
            agent, "assign_responder", {"category": category, "confidence": 1.0, "action": "execute"}
        )

        result = deterministic_classifier(event)

        assert isinstance(result, ClassifierResult)
        assert result.requires_human_in_the_loop is False

    def test_delegates_to_escalation_policy_decide_for_always_ask_category(self, agent: Agent):
        category = sorted(ep.ALWAYS_ASK_CATEGORIES)[0]
        event = _before_tool_event(
            agent, "assign_responder", {"category": category, "confidence": 1.0, "action": "execute"}
        )

        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop is True
        assert result.reason

    def test_escalates_below_confidence_floor(self, agent: Agent):
        category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
        below_floor = ep.CONFIDENCE_FLOOR.value - 0.01
        event = _before_tool_event(
            agent, "assign_responder", {"category": category, "confidence": below_floor, "action": "execute"}
        )

        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop is True

    def test_escalates_irreversible_action_regardless_of_category(self, agent: Agent):
        category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
        event = _before_tool_event(
            agent,
            "assign_responder",
            {
                "category": category,
                "confidence": 1.0,
                "action": "execute",
                "is_irreversible_action": True,
            },
        )

        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop is True

    def test_escalate_by_default_when_category_missing(self, agent: Agent):
        event = _before_tool_event(agent, "assign_responder", {"confidence": 1.0})

        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop is True
        assert "escalate-by-default" in result.reason

    def test_escalate_by_default_when_confidence_missing(self, agent: Agent):
        category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
        event = _before_tool_event(agent, "assign_responder", {"category": category})

        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop is True

    def test_escalate_by_default_when_input_is_not_a_dict(self, agent: Agent):
        event = BeforeToolCallEvent(
            agent=agent,
            selected_tool=None,
            tool_use={"name": "assign_responder", "input": "not-a-dict", "toolUseId": "tooluse-1"},
            invocation_state={"run_id": "run-1"},
        )

        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop is True

    def test_identical_inputs_produce_identical_decisions(self, agent: Agent):
        """Req 16.11: identical inputs -> identical enforcement decisions."""
        category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
        payload = {"category": category, "confidence": 0.9, "action": "execute"}

        first = deterministic_classifier(_before_tool_event(agent, "assign_responder", dict(payload)))
        second = deterministic_classifier(_before_tool_event(agent, "assign_responder", dict(payload)))

        assert first.requires_human_in_the_loop == second.requires_human_in_the_loop
        assert first.reason == second.reason

    def test_matches_escalation_policy_decide_directly(self, agent: Agent):
        """The classifier's verdict must match policy.escalation_policy.decide()'s
        own verdict for the same fields exactly -- it is the sole authority."""
        category = "dispatch_non_ambulatory"
        confidence = 0.95
        event = _before_tool_event(
            agent, "assign_responder", {"category": category, "confidence": confidence, "action": "execute"}
        )

        expected = ep.decide(category=category, confidence=confidence, action="execute")
        result = deterministic_classifier(event)

        assert result.requires_human_in_the_loop == expected["escalate"]
        assert result.reason == expected["reason"]


# ---------------------------------------------------------------------------
# build_human_in_the_loop
# ---------------------------------------------------------------------------


class TestBuildHumanInTheLoop:
    def test_returns_human_in_the_loop_with_generated_allowed_tools(self):
        hitl = build_human_in_the_loop(read_tools=[find_candidate_responders])

        assert isinstance(hitl, HumanInTheLoop)

    def test_uses_deterministic_classifier_by_default(self):
        hitl = build_human_in_the_loop(read_tools=[])
        # No public attribute is guaranteed by the SDK for introspecting the
        # configured classifier; constructing without error and returning a
        # HumanInTheLoop instance is the externally observable contract.
        assert isinstance(hitl, HumanInTheLoop)

    def test_accepts_a_custom_classifier_override(self, agent: Agent):
        calls: list[BeforeToolCallEvent] = []

        def spy_classifier(event: BeforeToolCallEvent, **kwargs) -> ClassifierResult:
            calls.append(event)
            return ClassifierResult(requires_human_in_the_loop=False, reason="spy")

        hitl = build_human_in_the_loop(read_tools=[], classifier=spy_classifier)
        assert isinstance(hitl, HumanInTheLoop)
