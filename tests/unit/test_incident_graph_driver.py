"""Unit tests for agents/incident_graph.py::execute_incident_graph (task 15.3).

Validates: Requirements 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9; Design §3.2;
docs/spikes/15-graph-api-findings.md §5.

The pause-aware driver walks the *declared* topology of a built ``Graph`` and
assigns ThunAI's richer status/outcome vocabulary. These tests build the real
ThunAI node/edge topology with **fake node executors** (tiny ``MultiAgentBase``
subclasses that return a pre-programmed result, raise, sleep, or pause), so the
whole graph runs in-process with **no live model call and no network** (design
principle 5 / Req 21.10). Persistence and audit are captured with in-process
fakes injected via ``execute_incident_graph``'s ``persist_progress`` /
``append_audit`` seams.

Each fake executor carries its own ``node_id``, so the default node runner
(``executor.invoke_async``) drives them exactly as it would the real nodes;
no ``node_runner`` override is needed except where a test wants to force a
runner-level behaviour independent of the executor.
"""

from __future__ import annotations

import asyncio
from typing import Any

from strands.agent.agent_result import AgentResult
from strands.multiagent import GraphBuilder
from strands.multiagent.base import (
    Interrupt,
    MultiAgentBase,
    MultiAgentResult,
    NodeResult,
    Status,
)
from strands.telemetry.metrics import EventLoopMetrics

from agents import incident_graph
from agents.incident_graph import (
    NODE_ALERT,
    NODE_DISPATCH,
    NODE_INTAKE,
    NODE_MONITOR,
    NODE_RULE_ENGINE,
    NODE_SAFETY_QA,
    OUTCOME_COMPLETE,
    OUTCOME_FAILED,
    OUTCOME_HALTED,
    OUTCOME_PARTIAL,
    OUTCOME_PAUSED,
    STATUS_FAILED,
    STATUS_NOT_EXECUTED,
    STATUS_PAUSED,
    STATUS_SKIPPED,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    RunResult,
    dispatch_is_irreversible,
    execute_incident_graph,
    intake_dispatch_eligible,
    monitor_raised_incident,
)
from schemas.decisions import DispatchDecision, EmergencyRequest, HazardAssessment


# ---------------------------------------------------------------------------
# Fake node executors — MultiAgentBase subclasses with no model call.
# ---------------------------------------------------------------------------
def _agent_result(*, structured: Any = None, state: Any = None, stop_reason: str = "end_turn", interrupts=None) -> AgentResult:
    return AgentResult(
        stop_reason=stop_reason,
        message={"role": "assistant", "content": [{"text": "x"}]},
        metrics=EventLoopMetrics(),
        state=state if state is not None else {},
        structured_output=structured,
        interrupts=interrupts or [],
    )


class _FakeNode(MultiAgentBase):
    """A deterministic fake graph node returning a pre-built result.

    ``behavior`` selects what happens when the driver invokes it:
      - ``"succeed"``: return a COMPLETED MultiAgentResult wrapping the
        provided ``structured``/``state`` payload (drives branch conditions).
      - ``"raise"``: raise ``RuntimeError(reason)`` (a hard node failure).
      - ``"sleep"``: ``await asyncio.sleep(delay)`` then succeed (to exceed a
        per-node timeout when the driver's node_timeout is small).
      - ``"pause_agent"``: return an AgentResult with stop_reason='interrupt'
        and an Interrupt carrying ``interrupt_id`` (Strands Agent pause).
      - ``"pause_gate"``: return a NodeResult with Status.INTERRUPTED and no
        SDK interrupt object (deterministic-gate pause).
    """

    def __init__(self, node_id: str, behavior: str = "succeed", *, structured=None, state=None, reason="boom", delay=0.2, interrupt_id="int-1"):
        super().__init__()
        self.node_id = node_id
        self.name = node_id
        self.id = node_id
        self._behavior = behavior
        self._structured = structured
        self._state = state
        self._reason = reason
        self._delay = delay
        self._interrupt_id = interrupt_id

    async def invoke_async(self, task, invocation_state=None, **kwargs) -> MultiAgentResult:  # type: ignore[override]
        if self._behavior == "raise":
            raise RuntimeError(self._reason)
        if self._behavior == "sleep":
            await asyncio.sleep(self._delay)
        if self._behavior == "pause_agent":
            agent_result = _agent_result(
                stop_reason="interrupt",
                interrupts=[Interrupt(id=self._interrupt_id, name="hitl")],
            )
            return MultiAgentResult(
                status=Status.INTERRUPTED,
                results={self.node_id: NodeResult(result=agent_result, status=Status.INTERRUPTED)},
            )
        if self._behavior == "pause_gate":
            agent_result = _agent_result()
            return MultiAgentResult(
                status=Status.INTERRUPTED,
                results={self.node_id: NodeResult(result=agent_result, status=Status.INTERRUPTED)},
            )
        # succeed
        agent_result = _agent_result(structured=self._structured, state=self._state)
        return MultiAgentResult(
            status=Status.COMPLETED,
            results={self.node_id: NodeResult(result=agent_result, status=Status.COMPLETED)},
        )


def _hazard(band: str) -> HazardAssessment:
    return HazardAssessment(
        severity_band=band, rate_of_change={}, anomaly_indicator={},
        confidence=0.9, action="execute", rationale="t",
    )


def _emergency_request(*, dispatch_eligible: bool) -> EmergencyRequest:
    return EmergencyRequest(
        request_category="RESCUE", location_reference="Ward 5", mobility_assistance=False,
        medical_need=False, source_language="en", urgency_band="ROUTINE", confidence=0.9,
        action="execute", category="routine_dispatch_ambulatory", rationale="t",
        dispatch_eligible=dispatch_eligible,
    )


def _dispatch_decision(*, irreversible: bool) -> DispatchDecision:
    return DispatchDecision(
        selected_responder_id="r-1", selection_rationale="nearest", confidence=0.9,
        action="execute", category="routine_dispatch_ambulatory",
        is_irreversible_action=irreversible,
    )


def _build_fake_graph(nodes: dict[str, _FakeNode], *, entry: str = NODE_RULE_ENGINE, limits=(600, 120, 25)):
    """Assemble the real ThunAI topology with the given fake node executors.

    Mirrors ``incident_graph._add_edges`` exactly (same edges, same named
    conditions) but adds the caller's fake executors instead of real agents,
    so the driver reads back the genuine declared topology and Req 4.4 limits
    from a real built ``Graph`` while every node is an in-process fake.
    """
    builder = GraphBuilder()
    builder.add_node(nodes[NODE_RULE_ENGINE], NODE_RULE_ENGINE)
    builder.add_node(nodes[NODE_MONITOR], NODE_MONITOR)
    builder.add_node(nodes[NODE_INTAKE], NODE_INTAKE)
    builder.add_node(nodes[NODE_DISPATCH], NODE_DISPATCH)
    builder.add_node(nodes[NODE_ALERT], NODE_ALERT)
    builder.add_node(nodes[NODE_SAFETY_QA], NODE_SAFETY_QA)
    builder.add_edge(NODE_RULE_ENGINE, NODE_MONITOR)
    builder.add_edge(NODE_MONITOR, NODE_ALERT, condition=monitor_raised_incident)
    builder.add_edge(NODE_MONITOR, NODE_INTAKE)
    builder.add_edge(NODE_INTAKE, NODE_DISPATCH, condition=intake_dispatch_eligible)
    builder.add_edge(NODE_ALERT, NODE_SAFETY_QA)
    builder.add_edge(NODE_DISPATCH, NODE_SAFETY_QA, condition=dispatch_is_irreversible)
    builder.set_entry_point(entry)
    et, nt, mx = limits
    builder.set_execution_timeout(et)
    builder.set_node_timeout(nt)
    builder.set_max_node_executions(mx)
    return builder.build()


class _Recorder:
    """Captures persist_progress + append_audit calls (in-process, no network)."""

    def __init__(self):
        self.progress: list[tuple[str, str, str, dict]] = []
        self.audits: list[dict] = []

    def persist(self, run_id, node_id, status, payload):
        self.progress.append((run_id, node_id, status, payload))

    def audit(self, run_id, incident_id, outcome, execution_order, node_status, *, extra=None):
        self.audits.append(
            {
                "run_id": run_id,
                "incident_id": incident_id,
                "outcome": outcome,
                "execution_order": list(execution_order),
                "node_status": dict(node_status),
                "extra": dict(extra) if extra else {},
            }
        )


def _run(graph, ctx=None, *, run_id="run-1", incident_id="inc-1", rec: _Recorder | None = None, now=None) -> tuple[RunResult, _Recorder]:
    rec = rec or _Recorder()
    ctx = ctx if ctx is not None else {"readings": {}, "now": "t", "last_known_band": "NORMAL"}
    result = asyncio.run(
        execute_incident_graph(
            graph,
            ctx,
            run_id=run_id,
            incident_id=incident_id,
            persist_progress=rec.persist,
            append_audit=rec.audit,
            now=now,
        )
    )
    return result, rec


# ---------------------------------------------------------------------------
# Node factory presets for a fully-succeeding sweep with the alert branch open
# (monitor raises WATCH) and the dispatch branch closed (intake not eligible).
# ---------------------------------------------------------------------------
def _alert_branch_nodes(**overrides) -> dict[str, _FakeNode]:
    nodes = {
        NODE_RULE_ENGINE: _FakeNode(NODE_RULE_ENGINE, state={"severity_band": "WATCH"}),
        NODE_MONITOR: _FakeNode(NODE_MONITOR, structured=_hazard("WATCH")),
        NODE_INTAKE: _FakeNode(NODE_INTAKE, structured=_emergency_request(dispatch_eligible=False)),
        NODE_DISPATCH: _FakeNode(NODE_DISPATCH, structured=_dispatch_decision(irreversible=False)),
        NODE_ALERT: _FakeNode(NODE_ALERT, state={"delivered": True}),
        NODE_SAFETY_QA: _FakeNode(NODE_SAFETY_QA, state={"passed": True}),
    }
    nodes.update(overrides)
    return nodes


# ===========================================================================
# Req 4.5 / determinism (4.2): a clean full run.
# ===========================================================================
def test_complete_run_alert_branch_only_req_4_5_4_9():
    """Monitor raises WATCH (alert branch open), intake not dispatch-eligible
    (dispatch branch closed): alert branch runs to safety_qa; dispatch is
    skipped (Req 4.9); run outcome complete (Req 4.5)."""
    result, rec = _run(_build_fake_graph(_alert_branch_nodes()))

    assert result.outcome == OUTCOME_COMPLETE
    assert result.node_status[NODE_RULE_ENGINE] == STATUS_SUCCEEDED
    assert result.node_status[NODE_MONITOR] == STATUS_SUCCEEDED
    assert result.node_status[NODE_ALERT] == STATUS_SUCCEEDED
    assert result.node_status[NODE_INTAKE] == STATUS_SUCCEEDED
    assert result.node_status[NODE_SAFETY_QA] == STATUS_SUCCEEDED
    # Dispatch branch closed (intake not eligible) => dispatch skipped (Req 4.9).
    assert result.node_status[NODE_DISPATCH] == STATUS_SKIPPED
    # Exactly one status per declared node from the closed set (Req 4.5).
    assert set(result.node_status) == set(incident_graph.GRAPH_NODE_IDS)
    for status in result.node_status.values():
        assert status in incident_graph.NODE_STATUSES
    # Terminal audit written exactly once with the ordered sequence (Req 4.5).
    assert len(rec.audits) == 1
    assert rec.audits[0]["outcome"] == OUTCOME_COMPLETE
    assert rec.audits[0]["execution_order"] == result.execution_order
    # rule_engine and monitor always precede their successors (Req 4.2 order).
    order = result.execution_order
    assert order.index(NODE_RULE_ENGINE) < order.index(NODE_MONITOR)
    assert order.index(NODE_MONITOR) < order.index(NODE_ALERT)


def test_execution_order_is_deterministic_across_runs_req_4_2():
    """Req 4.2: identical input + identical node outcomes => identical order."""
    order_a, _ = _run(_build_fake_graph(_alert_branch_nodes()))
    order_b, _ = _run(_build_fake_graph(_alert_branch_nodes()))
    assert order_a.execution_order == order_b.execution_order
    assert order_a.node_status == order_b.node_status


def test_dispatch_branch_reachable_runs_safety_qa_terminal():
    """Intake dispatch-eligible + irreversible dispatch => dispatch->safety_qa
    edge opens; monitor NORMAL so alert branch closed and alert skipped."""
    nodes = _alert_branch_nodes()
    nodes[NODE_RULE_ENGINE] = _FakeNode(NODE_RULE_ENGINE, state={"severity_band": "NORMAL"})
    nodes[NODE_MONITOR] = _FakeNode(NODE_MONITOR, structured=_hazard("NORMAL"))
    nodes[NODE_INTAKE] = _FakeNode(NODE_INTAKE, structured=_emergency_request(dispatch_eligible=True))
    nodes[NODE_DISPATCH] = _FakeNode(NODE_DISPATCH, structured=_dispatch_decision(irreversible=True))

    result, _ = _run(_build_fake_graph(nodes))
    assert result.outcome == OUTCOME_COMPLETE
    assert result.node_status[NODE_DISPATCH] == STATUS_SUCCEEDED
    assert result.node_status[NODE_SAFETY_QA] == STATUS_SUCCEEDED
    # Alert branch unreachable (monitor NORMAL) => alert skipped (Req 4.9).
    assert result.node_status[NODE_ALERT] == STATUS_SKIPPED


# ===========================================================================
# Req 4.6 — non-terminal failure: continue independent branches, mark
# only-consumers not_executed, run outcome partial.
# ===========================================================================
def test_non_terminal_failure_marks_only_consumers_not_executed_req_4_6():
    """Monitor fails (non-terminal). Its only structural consumers (alert,
    intake, and everything downstream) become not_executed; run outcome
    partial. rule_engine already succeeded and is retained."""
    nodes = _alert_branch_nodes(monitor=_FakeNode(NODE_MONITOR, behavior="raise", reason="monitor down"))
    result, rec = _run(_build_fake_graph(nodes))

    assert result.outcome == OUTCOME_PARTIAL
    assert result.node_status[NODE_RULE_ENGINE] == STATUS_SUCCEEDED
    assert result.node_status[NODE_MONITOR] == STATUS_FAILED
    # Everything downstream of monitor consumes only monitor's output here.
    assert result.node_status[NODE_ALERT] == STATUS_NOT_EXECUTED
    assert result.node_status[NODE_INTAKE] == STATUS_NOT_EXECUTED
    assert result.node_status[NODE_DISPATCH] == STATUS_NOT_EXECUTED
    assert result.node_status[NODE_SAFETY_QA] == STATUS_NOT_EXECUTED
    assert rec.audits[0]["outcome"] == OUTCOME_PARTIAL


def test_non_terminal_timeout_marks_timed_out_and_partial_req_4_6():
    """A non-terminal node exceeding the per-node timeout is recorded
    timed_out (not failed) and the run outcome is partial (Req 4.6)."""
    nodes = _alert_branch_nodes(monitor=_FakeNode(NODE_MONITOR, behavior="sleep", delay=1.0))
    # node_timeout = 1 second minimum allowed by config? min is 10; but the
    # driver reads the built graph's node_timeout. Build with a tiny timeout by
    # bypassing config range via direct attribute set after build.
    graph = _build_fake_graph(nodes)
    graph.node_timeout = 0.05  # force a fast per-node timeout for the sleeping node
    result, _ = _run(graph)

    assert result.node_status[NODE_MONITOR] == STATUS_TIMED_OUT
    assert result.outcome == OUTCOME_PARTIAL
    assert "timeout" in (result.node_executions[NODE_MONITOR].failure_reason or "")


def test_independent_branch_continues_when_other_branch_node_fails_req_4_6():
    """intake fails (non-terminal) but the alert branch does not consume
    intake's output, so alert->safety_qa still runs; only dispatch (intake's
    sole consumer) becomes not_executed; outcome partial."""
    nodes = _alert_branch_nodes(
        intake=_FakeNode(NODE_INTAKE, behavior="raise", reason="intake parse error"),
    )
    result, _ = _run(_build_fake_graph(nodes))

    assert result.node_status[NODE_INTAKE] == STATUS_FAILED
    assert result.node_status[NODE_DISPATCH] == STATUS_NOT_EXECUTED  # consumes only intake
    # Alert branch is independent of intake -> keeps running to the terminal node.
    assert result.node_status[NODE_ALERT] == STATUS_SUCCEEDED
    assert result.node_status[NODE_SAFETY_QA] == STATUS_SUCCEEDED
    assert result.outcome == OUTCOME_PARTIAL


# ===========================================================================
# Req 4.7 — terminal-node failure: run outcome failed, retain prior outputs,
# surface incident + failed node + reason.
# ===========================================================================
def test_terminal_node_failure_sets_failed_and_surfaces_req_4_7():
    nodes = _alert_branch_nodes(
        safety_qa=_FakeNode(NODE_SAFETY_QA, behavior="raise", reason="safety review crashed"),
    )
    result, rec = _run(_build_fake_graph(nodes))

    assert result.outcome == OUTCOME_FAILED
    assert result.failed_node_id == NODE_SAFETY_QA
    assert result.failure_reason == "safety review crashed"
    # Prior outputs retained (Req 4.7): rule_engine/monitor/alert succeeded.
    assert result.node_status[NODE_RULE_ENGINE] == STATUS_SUCCEEDED
    assert result.node_status[NODE_ALERT] == STATUS_SUCCEEDED
    # Audit surfaces incident id + failed node id + reason.
    audit = rec.audits[0]
    assert audit["outcome"] == OUTCOME_FAILED
    assert audit["extra"]["failed_node_id"] == NODE_SAFETY_QA
    assert audit["extra"]["failure_reason"] == "safety review crashed"
    assert audit["extra"]["incident_id"] == "inc-1"


# ===========================================================================
# Req 4.8 — halt on overall timeout / max node executions.
# ===========================================================================
def test_halt_on_overall_timeout_starts_no_further_node_req_4_8():
    """A clock that jumps past the overall execution timeout after the first
    node halts the run before starting any further node (Req 4.8)."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    ticks = iter([base, base + timedelta(seconds=10_000), base + timedelta(seconds=10_001)])

    def _now():
        try:
            return next(ticks)
        except StopIteration:
            return base + timedelta(seconds=10_001)

    result, rec = _run(_build_fake_graph(_alert_branch_nodes()), now=_now)
    assert result.outcome == OUTCOME_HALTED
    assert result.halt_reason is not None
    assert "timeout" in result.halt_reason
    assert rec.audits[0]["outcome"] == OUTCOME_HALTED
    assert "last_completed_node" in rec.audits[0]["extra"]


def test_halt_on_max_node_executions_req_4_8():
    """A max-node-execution cap below the number of runnable nodes halts the
    run once the cap is reached, starting no further node (Req 4.8)."""
    graph = _build_fake_graph(_alert_branch_nodes(), limits=(600, 120, 6))
    graph.max_node_executions = 2  # force the cap after 2 nodes
    result, rec = _run(graph)

    assert result.outcome == OUTCOME_HALTED
    assert "maximum node execution count" in (result.halt_reason or "")
    assert len(result.execution_order) == 2
    assert rec.audits[0]["extra"]["last_completed_node"] is not None


# ===========================================================================
# Req 4.9 — exactly one branch reachable: the other branch is skipped.
# ===========================================================================
def test_intake_entry_only_dispatch_branch_skips_alert_req_4_9():
    """Entry at intake (no monitor run) with a dispatch-eligible, irreversible
    request: dispatch branch runs; the alert branch (monitor/alert) is
    unreachable and every one of its nodes is skipped (Req 4.9)."""
    nodes = _alert_branch_nodes()
    nodes[NODE_INTAKE] = _FakeNode(NODE_INTAKE, structured=_emergency_request(dispatch_eligible=True))
    nodes[NODE_DISPATCH] = _FakeNode(NODE_DISPATCH, structured=_dispatch_decision(irreversible=True))
    graph = _build_fake_graph(nodes, entry=NODE_INTAKE)
    result, _ = _run(graph)

    assert result.node_status[NODE_INTAKE] == STATUS_SUCCEEDED
    assert result.node_status[NODE_DISPATCH] == STATUS_SUCCEEDED
    assert result.node_status[NODE_SAFETY_QA] == STATUS_SUCCEEDED
    # Alert branch unreachable from the intake entry (Req 4.9): every node of
    # the unreachable branch is recorded skipped.
    assert result.node_status[NODE_RULE_ENGINE] == STATUS_SKIPPED
    assert result.node_status[NODE_MONITOR] == STATUS_SKIPPED
    assert result.node_status[NODE_ALERT] == STATUS_SKIPPED


# ===========================================================================
# Pause — both shapes: Agent interrupt and deterministic-gate. Run stops,
# progress persisted under run_id (Idempotency_Key), outcome paused.
# ===========================================================================
def test_agent_interrupt_pause_persists_progress_and_stops_req_pause():
    """A Strands Agent node with stop_reason='interrupt' pauses the run:
    outcome paused, no further node started, run progress persisted with the
    interruptId (design.md §3.2 items 2-3)."""
    nodes = _alert_branch_nodes(
        safety_qa=_FakeNode(NODE_SAFETY_QA, behavior="pause_agent", interrupt_id="int-42"),
    )
    result, rec = _run(_build_fake_graph(nodes), run_id="idem-key-1")

    assert result.outcome == OUTCOME_PAUSED
    assert result.paused_node_id == NODE_SAFETY_QA
    assert result.interrupt_id == "int-42"
    assert result.node_status[NODE_SAFETY_QA] == STATUS_PAUSED
    # Run-progress persisted under the incident Idempotency_Key (run_id).
    paused_progress = [p for p in rec.progress if p[2] == STATUS_PAUSED]
    assert len(paused_progress) == 1
    run_id, node_id, status, payload = paused_progress[0]
    assert run_id == "idem-key-1"
    assert node_id == NODE_SAFETY_QA
    assert payload["interrupt_id"] == "int-42"
    assert payload["node_id"] == NODE_SAFETY_QA
    assert "inputs" in payload
    # A paused run does NOT write a terminal run-outcome audit (it is not
    # terminal — it will resume later).
    assert rec.audits == []


def test_deterministic_gate_pause_persists_without_interrupt_id():
    """A deterministic-gate pause (NodeResult status INTERRUPTED, no SDK
    interrupt object) pauses the run with interrupt_id None (design.md §3.2's
    'no SDK interrupt object involved at all' branch)."""
    nodes = _alert_branch_nodes(
        safety_qa=_FakeNode(NODE_SAFETY_QA, behavior="pause_gate"),
    )
    result, rec = _run(_build_fake_graph(nodes), run_id="idem-key-2")

    assert result.outcome == OUTCOME_PAUSED
    assert result.paused_node_id == NODE_SAFETY_QA
    assert result.interrupt_id is None
    paused_progress = [p for p in rec.progress if p[2] == STATUS_PAUSED]
    assert len(paused_progress) == 1
    assert paused_progress[0][3]["interrupt_id"] is None


def test_pause_stops_advancing_further_nodes():
    """When monitor pauses (mid-topology), no downstream node runs; they stay
    not_executed and the run returns paused immediately."""
    nodes = _alert_branch_nodes(
        monitor=_FakeNode(NODE_MONITOR, behavior="pause_agent", interrupt_id="int-9"),
    )
    result, _ = _run(_build_fake_graph(nodes))

    assert result.outcome == OUTCOME_PAUSED
    assert result.paused_node_id == NODE_MONITOR
    assert result.node_status[NODE_ALERT] == STATUS_NOT_EXECUTED
    assert result.node_status[NODE_INTAKE] == STATUS_NOT_EXECUTED
    assert result.node_status[NODE_SAFETY_QA] == STATUS_NOT_EXECUTED


# ===========================================================================
# Req 4.4 — the driver reads limits back off the built graph.
# ===========================================================================
def test_driver_reads_limits_from_built_graph_req_4_4():
    """The topology reader picks up the exact limits build wired, not a second
    hardcoded copy (Req 4.4)."""
    graph = _build_fake_graph(_alert_branch_nodes(), limits=(900, 300, 50))
    topo = incident_graph._read_topology(graph)
    assert topo.execution_timeout_s == 900
    assert topo.node_timeout_s == 300
    assert topo.max_node_executions == 50
    assert topo.terminal_nodes == frozenset({NODE_SAFETY_QA})
