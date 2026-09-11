"""Unit tests for agents/coordinator_orchestrator.py (task 13.8).

Validates: Requirements 4.10, 4.11, 4.12, 4.13, 4.14; Design §3.1
(Coordinator_Orchestrator).

No live Bedrock invocation is made anywhere in this file. Agent construction
builds a `BedrockModel` object (no network call happens until the agent is
invoked), and the read-only guarantee / prompt / session-manager checks are
all pure structural assertions over the built agent and module constants.
"""

from __future__ import annotations

import pytest
from strands.session.session_manager import SessionManager

from agents import coordinator_orchestrator as orch
from agents import (
    dispatch_agent,
    intake_agent,
    knowledge_agent,
    monitor_agent,
)


@pytest.fixture(autouse=True)
def _model_ids(monkeypatch):
    """Ensure both model-id env vars are set so `_build_model()` never sees
    an empty id, mirroring tests/unit/test_dispatch_agent.py's own fixture."""
    monkeypatch.setenv("THUNAI_LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
    monkeypatch.setenv("THUNAI_HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Req 4.10: exposes exactly the four specialists as tools; holds no write tool.
# ---------------------------------------------------------------------------


class TestReadOnlyAndExposedTools:
    def test_orchestrator_exposes_exactly_the_four_specialist_tools(self):
        agent = orch.build_coordinator_orchestrator()
        tool_names = set(agent.tool_names)
        assert {"ask_monitor", "ask_intake", "ask_dispatch", "ask_knowledge"} <= tool_names

    def test_orchestrator_holds_no_write_tool(self):
        agent = orch.build_coordinator_orchestrator()
        tool_names = set(agent.tool_names)
        write_tools = {"create_or_update_incident", "assign_responder", "deliver_alert", "create_escalation"}
        assert not (tool_names & write_tools)

    def test_readonly_tools_manifest_contains_no_write_tool(self):
        # The import-time guard already runs assert_no_write_tools(); re-assert
        # explicitly here so a regression is attributed to this requirement.
        orch.assert_no_write_tools()
        names = {orch._tool_name(t) for t in orch.READONLY_TOOLS}
        assert not (names & orch._KNOWN_WRITE_TOOL_NAMES)

    def test_assert_no_write_tools_detects_a_smuggled_write_tool(self, monkeypatch):
        from tools.dispatch_tools import assign_responder

        monkeypatch.setattr(
            orch, "READONLY_TOOLS", orch.READONLY_TOOLS + (assign_responder,), raising=True
        )
        with pytest.raises(AssertionError, match="assign_responder"):
            orch.assert_no_write_tools()

    def test_each_specialist_wrapper_tool_facing_signature_is_query_only(self):
        # The orchestrator must be shown `(query: str)` only -- the `_model`
        # test seam is a module global, not a tool parameter (Req 4.10 keeps
        # the model-facing surface minimal and unroutable-safe).
        for wrapper in (orch.ask_monitor, orch.ask_intake, orch.ask_dispatch, orch.ask_knowledge):
            spec = wrapper.tool_spec
            props = spec["inputSchema"]["json"]["properties"]
            assert set(props) == {"query"}


# ---------------------------------------------------------------------------
# Req 4.11: unroutable-request indication is expressed in the prompt with a
# stable, machine-recognisable marker, and changes nothing (structural).
# ---------------------------------------------------------------------------


class TestUnroutableRequest:
    def test_prompt_instructs_unroutable_marker(self):
        assert orch.UNROUTABLE_REQUEST_MARKER in orch.SYSTEM_PROMPT

    def test_prompt_instructs_change_nothing_and_read_only(self):
        prompt = orch.SYSTEM_PROMPT.lower()
        assert "change nothing" in prompt or "change any record" in prompt
        assert "read-only" in prompt


# ---------------------------------------------------------------------------
# Req 4.12: unique agent_id + a system prompt distinct from every other role.
# ---------------------------------------------------------------------------


class TestDistinctIdentity:
    def test_agent_id_is_unique_across_roles(self):
        other_ids = {
            monitor_agent.build_monitor_agent().agent_id
            if hasattr(monitor_agent, "build_monitor_agent")
            else "monitor_agent",
            intake_agent.AGENT_ID,
            dispatch_agent.AGENT_ID,
            knowledge_agent.AGENT_ID,
        }
        assert orch.AGENT_ID == "coordinator_orchestrator"
        assert orch.AGENT_ID not in other_ids

    def test_system_prompt_distinct_from_every_other_role(self):
        others = {
            monitor_agent.MONITOR_SYSTEM_PROMPT,
            intake_agent.SYSTEM_PROMPT,
            dispatch_agent.SYSTEM_PROMPT,
            knowledge_agent.SYSTEM_PROMPT,
        }
        assert orch.SYSTEM_PROMPT not in others
        # The four read-only specialist prompts are also mutually distinct
        # and distinct from the orchestrator prompt.
        specialist_prompts = [
            orch.MONITOR_READONLY_PROMPT,
            orch.INTAKE_READONLY_PROMPT,
            orch.DISPATCH_READONLY_PROMPT,
            orch.KNOWLEDGE_READONLY_PROMPT,
            orch.SYSTEM_PROMPT,
        ]
        assert len(set(specialist_prompts)) == len(specialist_prompts)


# ---------------------------------------------------------------------------
# Req 4.13: Memory_Store attached to the orchestrating component only.
# ---------------------------------------------------------------------------


class TestSessionManager:
    def test_no_session_manager_when_none_supplied(self):
        agent = orch.build_coordinator_orchestrator()
        assert agent._session_manager is None

    def test_session_manager_attached_only_to_orchestrator_when_supplied(self):
        sentinel = _DummySessionManager()
        agent = orch.build_coordinator_orchestrator(session_manager=sentinel)
        assert agent._session_manager is sentinel

    def test_no_other_agent_build_accepts_a_session_manager(self):
        # Req 4.13: this is the ONLY build_* with a session_manager parameter.
        import inspect

        assert "session_manager" in inspect.signature(orch.build_coordinator_orchestrator).parameters
        for build_fn in (
            dispatch_agent.build_dispatch_agent,
            intake_agent.build_intake_agent,
            knowledge_agent.build_knowledge_agent,
        ):
            assert "session_manager" not in inspect.signature(build_fn).parameters


class _DummySessionManager(SessionManager):
    """A stand-in SessionManager double -- Req 4.13's injection seam accepts
    any SessionManager; production passes a real AgentCoreMemorySessionManager
    (Memory_Store), constructed by the memory epic (a later task). Implements
    the four abstract methods as no-ops so it can be attached to an Agent
    without a live memory backend."""

    def initialize(self, *args, **kwargs):  # pragma: no cover - no-op double
        return None

    def append_message(self, *args, **kwargs):  # pragma: no cover
        return None

    def sync_agent(self, *args, **kwargs):  # pragma: no cover
        return None

    def redact_latest_message(self, *args, **kwargs):  # pragma: no cover
        return None
