"""Unit tests for agents/incident_graph.py::build_incident_graph (task 15.2).

Validates: Requirements 4.1, 4.3, 4.4, 4.9; Design §3.2;
docs/spikes/15-graph-api-findings.md §3/§5.

Two layers of tests:

1. **Branch conditions** — the named ``Callable[[GraphState], bool]`` functions
   are pure functions of a ``GraphState`` and can be tested directly with real
   SDK result types (``AgentResult``/``NodeResult``) wrapping the real decision
   Pydantic models, plus the deterministic ``RuleEngineNode`` dict-on-``.state``
   shape. No live model call is needed.
2. **Declared topology** — ``build_incident_graph`` produces a real
   ``strands.multiagent.graph.Graph``; its node set, edges (and their attached
   conditions), entry point, and Req 4.4 limits are asserted against the built
   object. Agent construction needs the two model-id env vars set (no network),
   provided by the ``_model_env`` fixture.
"""

from __future__ import annotations

import pytest
from strands.agent.agent_result import AgentResult
from strands.multiagent.base import NodeResult, Status
from strands.multiagent.graph import GraphState
from strands.telemetry.metrics import EventLoopMetrics

from agents import incident_graph
from agents.config import (
    EXECUTION_TIMEOUT_LIMIT,
    MAX_NODE_EXECUTIONS_LIMIT,
    NODE_TIMEOUT_LIMIT,
)
from agents.incident_graph import (
    GRAPH_NODE_IDS,
    NODE_ALERT,
    NODE_DISPATCH,
    NODE_INTAKE,
    NODE_MONITOR,
    NODE_RULE_ENGINE,
    NODE_SAFETY_QA,
    build_incident_graph,
    dispatch_is_irreversible,
    intake_dispatch_eligible,
    monitor_raised_incident,
)
from schemas.decisions import DispatchDecision, EmergencyRequest, HazardAssessment


# ---------------------------------------------------------------------------
# Helpers to build a GraphState carrying a prior node's result, matching the
# two real shapes _node_decision handles: structured_output (LLM agents) and
# a dict on .state (the deterministic RuleEngineNode).
# ---------------------------------------------------------------------------
def _state_with(node_id: str, *, structured=None, state=None) -> GraphState:
    agent_result = AgentResult(
        stop_reason="end_turn",
        message={"role": "assistant", "content": [{"text": "x"}]},
        metrics=EventLoopMetrics(),
        state=state if state is not None else {},
        structured_output=structured,
    )
    node_result = NodeResult(result=agent_result, status=Status.COMPLETED)
    return GraphState(results={node_id: node_result})


def _hazard(band: str) -> HazardAssessment:
    return HazardAssessment(
        severity_band=band,
        rate_of_change={},
        anomaly_indicator={},
        confidence=0.9,
        action="execute",
        rationale="test",
    )


def _emergency_request(*, dispatch_eligible: bool) -> EmergencyRequest:
    return EmergencyRequest(
        request_category="RESCUE",
        location_reference="Ward 5, near the temple",
        mobility_assistance=False,
        medical_need=False,
        source_language="en",
        urgency_band="ROUTINE",
        confidence=0.9,
        action="execute",
        category="routine_dispatch_ambulatory",
        rationale="test",
        dispatch_eligible=dispatch_eligible,
    )


def _dispatch_decision(*, irreversible: bool) -> DispatchDecision:
    return DispatchDecision(
        selected_responder_id="responder-1",
        selection_rationale="nearest available",
        confidence=0.9,
        action="execute",
        category="routine_dispatch_ambulatory",
        is_irreversible_action=irreversible,
    )


# ---------------------------------------------------------------------------
# monitor_raised_incident (alert branch gate) — Req 4.3, 4.9
# ---------------------------------------------------------------------------
def test_monitor_raised_incident_true_when_band_above_normal():
    state = _state_with(NODE_MONITOR, structured=_hazard("WATCH"))
    assert monitor_raised_incident(state) is True


def test_monitor_raised_incident_false_when_band_normal():
    state = _state_with(NODE_MONITOR, structured=_hazard("NORMAL"))
    assert monitor_raised_incident(state) is False


def test_monitor_raised_incident_false_when_monitor_not_run():
    # Empty state — monitor node has produced nothing yet.
    assert monitor_raised_incident(GraphState()) is False


def test_monitor_raised_incident_false_when_node_failed():
    node_result = NodeResult(result=RuntimeError("boom"), status=Status.FAILED)
    state = GraphState(results={NODE_MONITOR: node_result})
    assert monitor_raised_incident(state) is False


# ---------------------------------------------------------------------------
# intake_dispatch_eligible (dispatch branch gate) — Req 4.3, 4.9
# ---------------------------------------------------------------------------
def test_intake_dispatch_eligible_true_when_flagged():
    state = _state_with(
        NODE_INTAKE, structured=_emergency_request(dispatch_eligible=True)
    )
    assert intake_dispatch_eligible(state) is True


def test_intake_dispatch_eligible_false_when_not_flagged():
    state = _state_with(
        NODE_INTAKE, structured=_emergency_request(dispatch_eligible=False)
    )
    assert intake_dispatch_eligible(state) is False


def test_intake_dispatch_eligible_false_when_intake_not_run():
    assert intake_dispatch_eligible(GraphState()) is False


# ---------------------------------------------------------------------------
# dispatch_is_irreversible (safety_qa gate on dispatch branch)
# ---------------------------------------------------------------------------
def test_dispatch_is_irreversible_true_when_irreversible():
    state = _state_with(NODE_DISPATCH, structured=_dispatch_decision(irreversible=True))
    assert dispatch_is_irreversible(state) is True


def test_dispatch_is_irreversible_false_when_reversible():
    state = _state_with(
        NODE_DISPATCH, structured=_dispatch_decision(irreversible=False)
    )
    assert dispatch_is_irreversible(state) is False


def test_dispatch_is_irreversible_false_when_dispatch_not_run():
    assert dispatch_is_irreversible(GraphState()) is False


# ---------------------------------------------------------------------------
# _node_decision reads the RuleEngineNode's dict-on-.state shape too, so a
# condition works uniformly across deterministic and LLM nodes.
# ---------------------------------------------------------------------------
def test_node_decision_reads_dict_state_payload():
    state = _state_with(NODE_RULE_ENGINE, state={"severity_band": "WARNING"})
    decision = incident_graph._node_decision(state, NODE_RULE_ENGINE)
    assert decision == {"severity_band": "WARNING"}
    assert incident_graph._decision_field(decision, "severity_band") == "WARNING"


def test_mutually_exclusive_branch_conditions_req_4_9():
    """Req 4.9: on a pure sweep only the alert branch fires; on a pure intake
    run only the dispatch branch fires — the two branch conditions never both
    hold for the same single-branch input."""
    sweep = _state_with(NODE_MONITOR, structured=_hazard("EVACUATE"))
    assert monitor_raised_incident(sweep) is True
    assert intake_dispatch_eligible(sweep) is False  # intake never ran

    intake = _state_with(
        NODE_INTAKE, structured=_emergency_request(dispatch_eligible=True)
    )
    assert intake_dispatch_eligible(intake) is True
    assert monitor_raised_incident(intake) is False  # monitor never ran


# ---------------------------------------------------------------------------
# build_incident_graph — declared topology, entry points, and Req 4.4 limits.
# ---------------------------------------------------------------------------
@pytest.fixture
def _model_env(monkeypatch):
    """Provide the two model-id env vars (no network) so the agent builders
    inside build_incident_graph can construct their BedrockModel instances."""
    monkeypatch.setenv("THUNAI_LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
    monkeypatch.setenv("THUNAI_HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    # Rebind the config module's model-for-role map, which was frozen from env
    # at import time, so get_model_id_for_role returns non-empty ids.
    import agents.config as config

    monkeypatch.setattr(config, "LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
    monkeypatch.setattr(config, "HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")
    monkeypatch.setattr(
        config,
        "MODEL_FOR_ROLE",
        {role: "us.amazon.nova-lite-v1:0" for role in config.AGENT_ROLES},
    )


def _edge_tuples(graph):
    return {
        (
            e.from_node.node_id,
            e.to_node.node_id,
            e.condition.__name__ if e.condition else None,
        )
        for e in graph.edges
    }


def test_build_incident_graph_declares_all_six_nodes(_model_env):
    graph = build_incident_graph()
    assert set(graph.nodes.keys()) == set(GRAPH_NODE_IDS)
    assert len(GRAPH_NODE_IDS) == 6


def test_build_incident_graph_declares_expected_edges(_model_env):
    graph = build_incident_graph()
    assert _edge_tuples(graph) == {
        (NODE_RULE_ENGINE, NODE_MONITOR, None),
        (NODE_MONITOR, NODE_ALERT, "monitor_raised_incident"),
        (NODE_MONITOR, NODE_INTAKE, None),
        (NODE_INTAKE, NODE_DISPATCH, "intake_dispatch_eligible"),
        (NODE_ALERT, NODE_SAFETY_QA, None),
        (NODE_DISPATCH, NODE_SAFETY_QA, "dispatch_is_irreversible"),
    }


def test_build_incident_graph_default_entry_is_rule_engine(_model_env):
    graph = build_incident_graph()
    assert [n.node_id for n in graph.entry_points] == [NODE_RULE_ENGINE]


def test_build_incident_graph_intake_entry(_model_env):
    graph = build_incident_graph("intake")
    assert [n.node_id for n in graph.entry_points] == [NODE_INTAKE]


def test_both_entry_builds_share_identical_node_and_edge_sets_req_4_1(_model_env):
    sweep = build_incident_graph("rule_engine")
    intake = build_incident_graph("intake")
    assert set(sweep.nodes.keys()) == set(intake.nodes.keys())
    assert _edge_tuples(sweep) == _edge_tuples(intake)


def test_build_incident_graph_rejects_unknown_entry(_model_env):
    with pytest.raises(ValueError, match="entry must be one of"):
        build_incident_graph("dispatch")  # type: ignore[arg-type]


def test_build_incident_graph_wires_req_4_4_default_limits(_model_env):
    graph = build_incident_graph()
    assert graph.execution_timeout == EXECUTION_TIMEOUT_LIMIT.default == 600
    assert graph.node_timeout == NODE_TIMEOUT_LIMIT.default == 120
    assert graph.max_node_executions == MAX_NODE_EXECUTIONS_LIMIT.default == 25


def test_build_incident_graph_wires_explicit_in_range_limits(_model_env):
    graph = build_incident_graph(limits=(900, 300, 50))
    assert graph.execution_timeout == 900
    assert graph.node_timeout == 300
    assert graph.max_node_executions == 50


# ---------------------------------------------------------------------------
# Req 4.4 configurable ranges — GraphLimit.resolve enforcement.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "limit,default,minimum,maximum",
    [
        (EXECUTION_TIMEOUT_LIMIT, 600, 60, 1800),
        (NODE_TIMEOUT_LIMIT, 120, 10, 600),
        (MAX_NODE_EXECUTIONS_LIMIT, 25, 6, 100),
    ],
)
def test_graph_limit_defaults_and_ranges_match_req_4_4(
    limit, default, minimum, maximum
):
    assert limit.default == default
    assert limit.minimum == minimum
    assert limit.maximum == maximum
    assert limit.resolve() == default
    assert limit.resolve(minimum) == minimum
    assert limit.resolve(maximum) == maximum


@pytest.mark.parametrize(
    "limit",
    [EXECUTION_TIMEOUT_LIMIT, NODE_TIMEOUT_LIMIT, MAX_NODE_EXECUTIONS_LIMIT],
)
def test_graph_limit_rejects_out_of_range(limit):
    with pytest.raises(ValueError, match="must be in range"):
        limit.resolve(limit.minimum - 1)
    with pytest.raises(ValueError, match="must be in range"):
        limit.resolve(limit.maximum + 1)
