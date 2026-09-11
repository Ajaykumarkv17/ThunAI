"""Unit tests for harness/hooks.py (task 8.2).

Constructs real `strands.hooks.events` instances (per `agents/rule_engine.py`
and `tests/unit/test_rule_engine_node.py`'s own precedent of exercising real
Strands objects directly rather than mocking the SDK's own types) against a
real `strands.Agent` instance (cheap to construct — no model call is made by
construction alone, matching `harness/hooks.py`'s own module docstring's
verification note). Only `harness.audit.append_audit_entry` — the one
network/DynamoDB-touching dependency — is mocked, via `unittest.mock.patch`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from strands import Agent
from strands.hooks.events import AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent

from harness.hooks import (
    ApprovalGateHook,
    AuditHook,
    NotificationCapHook,
    SpendCapHook,
    ToolCallCapHook,
)
from memory.audit_ledger import AuditAppendError


@pytest.fixture()
def agent() -> Agent:
    return Agent(callback_handler=None, agent_id="test-agent")


def _before_tool_event(agent: Agent, name: str, input_: dict, run_id: str = "run-1", **extra_state) -> BeforeToolCallEvent:
    invocation_state = {"run_id": run_id, **extra_state}
    return BeforeToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": name, "input": input_, "toolUseId": f"tooluse-{name}"},
        invocation_state=invocation_state,
    )


def _after_tool_event(
    agent: Agent,
    name: str,
    input_: dict,
    result: dict,
    run_id: str = "run-1",
    cancel_message: str | None = None,
) -> AfterToolCallEvent:
    return AfterToolCallEvent(
        agent=agent,
        selected_tool=None,
        tool_use={"name": name, "input": input_, "toolUseId": f"tooluse-{name}"},
        invocation_state={"run_id": run_id},
        result=result,
        cancel_message=cancel_message,
    )


# ---------------------------------------------------------------------------
# ApprovalGateHook (Req 16.1, 16.2, 16.3)
# ---------------------------------------------------------------------------


class TestApprovalGateHook:
    def test_blocks_write_tool_with_no_recorded_approval(self, agent: Agent):
        hook = ApprovalGateHook()
        event = _before_tool_event(agent, "assign_responder", {"category": "dispatch_non_ambulatory"})

        hook.check(event)

        assert event.cancel_tool
        assert "assign_responder" in event.cancel_tool
        assert "human approval" in event.cancel_tool

    def test_does_not_touch_read_only_tool(self, agent: Agent):
        hook = ApprovalGateHook()
        event = _before_tool_event(agent, "find_candidate_responders", {})

        hook.check(event)

        assert event.cancel_tool is False

    def test_allows_write_tool_with_recorded_approval(self, agent: Agent):
        hook = ApprovalGateHook()
        event = _before_tool_event(
            agent,
            "assign_responder",
            {"category": "dispatch_non_ambulatory"},
            approved_tool_call_ids={"tooluse-assign_responder"},
        )

        hook.check(event)

        assert event.cancel_tool is False

    def test_allows_write_tool_in_never_ask_category(self, agent: Agent):
        hook = ApprovalGateHook()
        event = _before_tool_event(
            agent, "create_or_update_incident", {"category": "routine_dispatch_ambulatory"}
        )

        hook.check(event)

        assert event.cancel_tool is False


# ---------------------------------------------------------------------------
# ToolCallCapHook (Req 16.4, 16.7)
# ---------------------------------------------------------------------------


class TestToolCallCapHook:
    def test_allows_calls_under_the_cap(self, agent: Agent):
        hook = ToolCallCapHook(max_calls=3)
        for _ in range(3):
            event = _before_tool_event(agent, "assign_responder", {})
            hook.check(event)
            assert event.cancel_tool is False
        assert hook.breaches == []

    @patch("harness.hooks.append_audit_entry")
    def test_blocks_and_records_breach_once_cap_exceeded(self, mock_append, agent: Agent):
        hook = ToolCallCapHook(max_calls=2)
        for _ in range(2):
            hook.check(_before_tool_event(agent, "assign_responder", {}))

        breaching_event = _before_tool_event(agent, "assign_responder", {})
        hook.check(breaching_event)

        assert breaching_event.cancel_tool
        assert len(hook.breaches) == 1
        assert hook.breaches[0]["cap_type"] == "tool_call_cap"
        mock_append.assert_called_once()
        assert mock_append.call_args.kwargs["outcome"] == "limit_breach: tool_call_cap exceeded"

    def test_cap_is_scoped_per_run_id(self, agent: Agent):
        hook = ToolCallCapHook(max_calls=1)
        hook.check(_before_tool_event(agent, "assign_responder", {}, run_id="run-a"))
        event_run_b = _before_tool_event(agent, "assign_responder", {}, run_id="run-b")
        hook.check(event_run_b)
        assert event_run_b.cancel_tool is False

    def test_rejects_out_of_range_constructor_value(self):
        with pytest.raises(ValueError):
            ToolCallCapHook(max_calls=0)
        with pytest.raises(ValueError):
            ToolCallCapHook(max_calls=51)


# ---------------------------------------------------------------------------
# SpendCapHook (Req 16.5, 16.7)
# ---------------------------------------------------------------------------


class TestSpendCapHook:
    def _model_event(self, agent: Agent, projected_tokens: int, run_id: str = "run-1") -> BeforeModelCallEvent:
        return BeforeModelCallEvent(
            agent=agent,
            invocation_state={"run_id": run_id},
            projected_input_tokens=projected_tokens,
        )

    def test_allows_calls_under_the_spend_cap(self, agent: Agent):
        hook = SpendCapHook(max_minor_units=1000)
        event = self._model_event(agent, projected_tokens=1000)
        hook.check(event)
        assert event.cancel is False

    @patch("harness.hooks.append_audit_entry")
    def test_blocks_once_spend_cap_exceeded(self, mock_append, agent: Agent):
        hook = SpendCapHook(max_minor_units=1)
        # First call: projected cost is (5000/1000)*1.0 = 5.0 minor units,
        # already >= the cap of 1, but the cap check happens BEFORE
        # accumulating this call's cost, so the first call is allowed and
        # the second call (now over cap) is blocked.
        first = self._model_event(agent, projected_tokens=5000)
        hook.check(first)
        assert first.cancel is False

        second = self._model_event(agent, projected_tokens=100)
        hook.check(second)

        assert second.cancel
        assert len(hook.breaches) == 1
        mock_append.assert_called_once()

    def test_rejects_out_of_range_constructor_value(self):
        with pytest.raises(ValueError):
            SpendCapHook(max_minor_units=0)
        with pytest.raises(ValueError):
            SpendCapHook(max_minor_units=100_001)


# ---------------------------------------------------------------------------
# NotificationCapHook (Req 16.6, 16.7)
# ---------------------------------------------------------------------------


class TestNotificationCapHook:
    def test_allows_notifications_under_the_cap(self, agent: Agent):
        hook = NotificationCapHook(max_notifications=2)
        for _ in range(2):
            event = _before_tool_event(agent, "deliver_alert", {})
            hook.check(event)
            assert event.cancel_tool is False

    @patch("harness.hooks.append_audit_entry")
    def test_blocks_once_notification_cap_exceeded(self, mock_append, agent: Agent):
        hook = NotificationCapHook(max_notifications=1)
        hook.check(_before_tool_event(agent, "deliver_alert", {}))

        breaching = _before_tool_event(agent, "deliver_alert", {})
        hook.check(breaching)

        assert breaching.cancel_tool
        assert len(hook.breaches) == 1
        mock_append.assert_called_once()

    def test_does_not_touch_non_notification_tool(self, agent: Agent):
        hook = NotificationCapHook(max_notifications=0 + 1)
        event = _before_tool_event(agent, "assign_responder", {})
        hook.check(event)
        assert event.cancel_tool is False

    def test_rejects_out_of_range_constructor_value(self):
        with pytest.raises(ValueError):
            NotificationCapHook(max_notifications=0)
        with pytest.raises(ValueError):
            NotificationCapHook(max_notifications=101)


# ---------------------------------------------------------------------------
# AuditHook (Req 16.8, 16.9, 16.10)
# ---------------------------------------------------------------------------


class TestAuditHook:
    @patch("harness.hooks.append_audit_entry")
    def test_redacts_then_appends_with_correct_outcome_on_success(self, mock_append, agent: Agent):
        hook = AuditHook()
        result = {"toolUseId": "t1", "status": "success", "content": [{"text": "done"}]}
        event = _after_tool_event(
            agent,
            "assign_responder",
            {"resident_name": "Priya", "responder_id": "r1"},
            result,
        )

        hook.after(event)

        mock_append.assert_called_once()
        kwargs = mock_append.call_args.kwargs
        assert kwargs["inputs"]["resident_name"] == "[REDACTED]"
        assert kwargs["inputs"]["responder_id"] == "r1"
        assert "success" in kwargs["outcome"]
        assert kwargs["tool_name"] == "assign_responder"
        assert kwargs["run_id"] == "run-1"

    @patch("harness.hooks.append_audit_entry")
    def test_records_blocked_outcome_when_cancel_message_set(self, mock_append, agent: Agent):
        hook = AuditHook()
        result = {"toolUseId": "t1", "status": "error", "content": [{"text": "cancelled"}]}
        event = _after_tool_event(
            agent,
            "assign_responder",
            {},
            result,
            cancel_message="run limit of 5 reached",
        )

        hook.after(event)

        kwargs = mock_append.call_args.kwargs
        assert kwargs["outcome"] == "blocked: run limit of 5 reached"

    @patch("harness.hooks.append_audit_entry", side_effect=AuditAppendError("boom"))
    def test_blocks_write_tool_result_when_audit_append_fails(self, mock_append, agent: Agent):
        hook = AuditHook()
        result = {"toolUseId": "t1", "status": "success", "content": [{"text": "done"}]}
        event = _after_tool_event(agent, "assign_responder", {}, result)

        hook.after(event)

        assert event.result["status"] == "error"
        assert "Audit capture failed" in event.result["content"][0]["text"]
        # Two append attempts: the original (failed) append, and the
        # second minimal audit_capture_failure record (also failing here,
        # since the mock always raises) — both are swallowed, never raised.
        assert mock_append.call_count == 2

    @patch("harness.hooks.append_audit_entry")
    def test_does_not_block_result_for_read_only_tool_when_audit_append_fails(self, mock_append, agent: Agent):
        mock_append.side_effect = AuditAppendError("boom")
        hook = AuditHook()
        result = {"toolUseId": "t1", "status": "success", "content": [{"text": "done"}]}
        event = _after_tool_event(agent, "find_candidate_responders", {}, result)

        hook.after(event)

        # Read-only tool: result is left exactly as the tool produced it,
        # even though the audit append failed (Req 16.9 only names write
        # tool calls).
        assert event.result["status"] == "success"
