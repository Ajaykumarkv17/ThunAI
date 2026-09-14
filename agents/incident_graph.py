"""``Incident_Graph`` declared topology (Req 4.1, 4.3, 4.4, 4.9; design.md §3.2).

This module owns the **declared structure** of ThunAI's orchestration graph:
the fixed node set, the directed edges (with their branch conditions), the
single per-run entry point, and the three execution limits of Req 4.4. It
exposes exactly one public entry point for task 15.2:

    ``build_incident_graph(entry=..., *, limits=...) -> Graph``

The pause-aware driver (``execute_incident_graph``) that *runs* this declared
structure with ThunAI's richer status/outcome vocabulary is **task 15.3** and
is intentionally not implemented here; this file is laid out so 15.3 can add
that driver directly below ``build_incident_graph`` without touching the
node/edge factory. The two responsibilities are kept apart on purpose: what
the graph *is* (declared once, fixed for the run — Req 4.1) versus how a run
of it is *driven* (15.3).

Verification note (mandatory per tasks.md 15's "verify first" banner; the
15.1 spike already did the deep dive, so this is a single focused
confirmation, not a repeat):
    Installed version confirmed: ``strands-agents==1.55.0`` (matches
    `pyproject.toml`'s pin and `agents/rule_engine.py`/`agents/config.py`'s
    own verification notes). Read the installed
    ``strands/multiagent/graph.py`` directly to re-confirm the exact surface
    this module builds on, for this pin:
        - ``strands.multiagent.GraphBuilder`` exposes, with the signatures
          the 15.1 findings recorded (``docs/spikes/15-graph-api-findings.md``
          §1/§2): ``add_node(executor, node_id=None) -> GraphNode``,
          ``add_edge(from_node, to_node, condition=None) -> GraphEdge``,
          ``set_entry_point(node_id) -> GraphBuilder``,
          ``set_execution_timeout(timeout: float) -> GraphBuilder``,
          ``set_node_timeout(timeout: float) -> GraphBuilder``,
          ``set_max_node_executions(max_executions: int) -> GraphBuilder``,
          and ``build() -> Graph``. All present and unchanged at 1.55.0.
        - The conditional-edge ``condition`` callable receives a
          ``strands.multiagent.graph.GraphState`` (the legacy
          ``Callable[[GraphState], bool]`` form), **not** the design's
          assumed ``ctx.node_output(...)`` object. Confirmed: ``GraphState``
          carries ``results: dict[str, NodeResult]`` (``graph.py``), so a
          prior node's output is read as ``state.results["<id>"].result``.
          This module therefore uses **named condition functions** (never
          ``lambda ctx:``) with explicit ``None``-guards, exactly as the
          15.1 findings §3/§5 require ("docs win").

    Deviations from design.md §3.2's worked code block, each recorded here
    (per the "docs win" rule):
        1. **Conditional edges rewritten (15.1 Open Q3, findings §3).**
           design.md §3.2 writes ``condition=lambda ctx:
           ctx.node_output("monitor").severity_band != "NORMAL"``. That
           ``ctx``/``node_output`` accessor does not exist. Every edge
           condition here is instead a named ``Callable[[GraphState], bool]``
           that reads ``state.results["<id>"].result`` and pulls the typed
           decision off it via ``_node_decision`` (see below), returning
           ``False`` whenever the prior node's output is missing or the
           field is absent, so a condition is always safe to evaluate even
           mid-partial-run.
        2. **Structured-output accessor made explicit (15.1 findings §3
           note 1).** The findings left "the exact accessor when the
           Monitor/Intake/Dispatch nodes are built" open. Confirmed by
           reading the built agents and the SDK: LLM ``Agent`` nodes that
           declare ``structured_output_model=`` expose their parsed Pydantic
           decision on ``AgentResult.structured_output`` (``Intake_Agent`` →
           ``EmergencyRequest`` with ``dispatch_eligible``; ``Monitor_Agent``
           → ``HazardAssessment`` with ``severity_band``; ``Dispatch_Agent``
           → ``DispatchDecision`` with ``is_irreversible_action``), while the
           deterministic ``RuleEngineNode`` puts its payload on
           ``AgentResult.state`` (a dict with ``severity_band``).
           ``_node_decision`` checks ``structured_output`` first, then a
           dict-shaped ``state``, so one accessor serves both node kinds.
        3. **Two builds from one factory (design.md §3.2, unchanged
           intent).** ``build_incident_graph(entry="rule_engine")`` (sweep)
           and ``build_incident_graph(entry="intake")`` (intake) are produced
           from the *same* node/edge factory functions, differing only in the
           ``set_entry_point`` call, so Req 4.1's "node set and directed
           edges remain unchanged for the duration of that run" holds
           per-run while both legitimate starting conditions are served.
        4. **Limits sourced from `agents/config.py` (Req 4.4).** The three
           limits are resolved via ``agents.config.get_graph_execution_limits``
           (defaults 600/120/25, ranges 60-1800 / 10-600 / 6-100) rather than
           hardcoded literals as in the design snippet, so a deployer can tune
           them and an out-of-range value is rejected at build time.

Edge topology (design.md §3.1 "agent roles + graph edges" and §3.2):
    rule_engine → monitor
    monitor → alert     (only when monitor raised an incident, band != NORMAL)
    monitor → intake    (sweep cross-references a new hazard against open requests)
    intake → dispatch   (only when intake produced a dispatch-eligible request)
    alert → safety_qa
    dispatch → safety_qa (only when the dispatch decision is an Irreversible_Action)

Req 4.9 ("exactly one of alert vs dispatch reachable") is satisfied by the
mutually-exclusive branch conditions on ``monitor → alert`` and
``intake → dispatch``: on a pure sweep the intake/dispatch branch's condition
never fires, and on a pure intake run the monitor/alert condition never fires;
whichever branch is unreachable has its nodes recorded ``skipped`` by the
15.3 driver (findings §5, design.md §3.2). The default OR edge semantics
(findings §3 note 2) are correct here — no node needs *all* upstream deps, so
no ``all_dependencies_complete`` AND-condition is used.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from strands.multiagent import GraphBuilder
from strands.multiagent.base import MultiAgentResult, NodeResult, Status
from strands.multiagent.graph import Graph, GraphState

from agents.alert_agent import build_alert_agent
from agents.config import get_graph_execution_limits
from agents.dispatch_agent import build_dispatch_agent
from agents.intake_agent import build_intake_agent
from agents.monitor_agent import build_monitor_agent
from agents.rule_engine import RuleEngineNode
from agents.safety_qa_agent import build_safety_qa_agent

# ---------------------------------------------------------------------------
# Declared node ids (Req 4.1: a fixed, declared node set). Defined as
# module-level constants so the edge factory, the entry-point validation, and
# the 15.3 driver all reference one spelling rather than scattered string
# literals.
# ---------------------------------------------------------------------------
NODE_RULE_ENGINE = "rule_engine"
NODE_MONITOR = "monitor"
NODE_INTAKE = "intake"
NODE_DISPATCH = "dispatch"
NODE_ALERT = "alert"
NODE_SAFETY_QA = "safety_qa"

#: Every declared node id, in the order the nodes are added to the builder.
GRAPH_NODE_IDS: tuple[str, ...] = (
    NODE_RULE_ENGINE,
    NODE_MONITOR,
    NODE_INTAKE,
    NODE_DISPATCH,
    NODE_ALERT,
    NODE_SAFETY_QA,
)

#: The two legitimate per-run entry points (design.md §3.2): a routine sweep
#: begins at the deterministic ``rule_engine`` node; an inbound resident
#: message begins at ``intake``.
EntryPoint = Literal["rule_engine", "intake"]
VALID_ENTRY_POINTS: frozenset[str] = frozenset({NODE_RULE_ENGINE, NODE_INTAKE})

#: The severity band that means "no incident" (design.md §3.2 / §3.3,
#: `agents/rule_engine.py::SEVERITY_ORDER`). The alert branch is reachable
#: only when the monitor raised the band *above* this.
NORMAL_BAND = "NORMAL"


# ---------------------------------------------------------------------------
# Structured-output accessor shared by every branch condition (deviation 2).
# ---------------------------------------------------------------------------
def _node_decision(state: GraphState, node_id: str) -> Any | None:
    """Return the typed decision object a prior node produced, or ``None``.

    A conditional-edge callable receives the graph's ``GraphState``; a prior
    node's result lives at ``state.results[node_id].result`` (an
    ``AgentResult`` for both LLM ``Agent`` nodes and the deterministic
    ``RuleEngineNode``). This helper extracts the *decision* object the
    branch conditions read fields off of, tolerating every shape that can
    legitimately occur mid-run:

    - node not yet executed / no result recorded → ``None``
    - the recorded result is an ``Exception`` (a failed node) → ``None``
    - an LLM ``Agent`` node with ``structured_output_model`` set → its parsed
      Pydantic model on ``AgentResult.structured_output``
    - the ``RuleEngineNode`` (deterministic) → its payload dict on
      ``AgentResult.state``
    - anything else (plain text result, unexpected shape) → ``None``

    Returning ``None`` rather than raising means every branch condition is
    safe to evaluate at any point in a partial run: a missing/failed upstream
    node simply makes the branch not-yet-reachable, never an error.

    Args:
        state: The graph execution state passed to the edge condition.
        node_id: The upstream node whose decision is wanted.

    Returns:
        The decision object (a Pydantic model or a dict), or ``None`` when no
        usable decision is available yet.
    """
    node_result = state.results.get(node_id)
    if node_result is None:
        return None

    result = getattr(node_result, "result", None)
    if result is None or isinstance(result, Exception):
        return None

    structured = getattr(result, "structured_output", None)
    if structured is not None:
        return structured

    node_state = getattr(result, "state", None)
    if isinstance(node_state, dict):
        return node_state

    return None


def _decision_field(decision: Any, field: str) -> Any | None:
    """Read ``field`` off a decision object that may be a model or a dict.

    ``structured_output`` decisions are Pydantic models (attribute access);
    the ``RuleEngineNode`` payload is a plain dict (item access). This reads
    whichever applies and returns ``None`` when the field is absent, so a
    condition never raises on a partially-populated decision.
    """
    if decision is None:
        return None
    if isinstance(decision, dict):
        return decision.get(field)
    return getattr(decision, field, None)


# ---------------------------------------------------------------------------
# Named branch conditions (deviation 1). Each is a `Callable[[GraphState],
# bool]` — the confirmed legacy signature — with explicit None-guards so it is
# directly unit-testable without a live graph run.
# ---------------------------------------------------------------------------
def monitor_raised_incident(state: GraphState) -> bool:
    """Alert branch gate: monitor created/updated an incident above NORMAL.

    Reachable only when the ``monitor`` node's assessment carries a
    ``severity_band`` other than ``NORMAL`` (design.md §3.2: "only reachable
    when Monitor created/updated an incident at WATCH+"). Returns ``False``
    when the monitor node has not run, failed, or produced no band — i.e. no
    incident to alert about.

    **Validates: Requirements 4.3, 4.9**
    """
    band = _decision_field(_node_decision(state, NODE_MONITOR), "severity_band")
    if not isinstance(band, str):
        return False
    return band != NORMAL_BAND


def intake_dispatch_eligible(state: GraphState) -> bool:
    """Dispatch branch gate: intake produced a dispatch-eligible request.

    Reachable only when the ``intake`` node's request is flagged
    ``dispatch_eligible`` (design.md §3.2). Returns ``False`` when intake has
    not run, failed, or produced a non-dispatch-eligible request.

    **Validates: Requirements 4.3, 4.9**
    """
    eligible = _decision_field(_node_decision(state, NODE_INTAKE), "dispatch_eligible")
    return eligible is True


def dispatch_is_irreversible(state: GraphState) -> bool:
    """Safety-QA gate on the dispatch branch: the decision is irreversible.

    The ``dispatch → safety_qa`` edge fires only when the dispatch decision
    is an ``Irreversible_Action`` (design.md §3.1/§3.2), matching Req 9.6's
    "review a proposed Irreversible_Action before execution". Returns
    ``False`` when dispatch has not run, failed, or is not irreversible.
    """
    irreversible = _decision_field(
        _node_decision(state, NODE_DISPATCH), "is_irreversible_action"
    )
    return irreversible is True


# ---------------------------------------------------------------------------
# The node/edge factory (design.md §3.2). Split into two helpers so the two
# builds (sweep entry vs intake entry) share one declaration of nodes and one
# declaration of edges — Req 4.1's "unchanged node set and edges per run".
# ---------------------------------------------------------------------------
def _add_nodes(builder: GraphBuilder) -> None:
    """Add the six declared nodes to ``builder`` (design.md §3.1/§3.2).

    Each specialist ``Agent`` is built fresh here (no session manager, per
    Req 4.13 and each builder's own docstring); ``RuleEngineNode`` is the
    deterministic zero-model node. This is the single place the node set is
    declared, so both entry-point builds get an identical node set.
    """
    builder.add_node(RuleEngineNode(), NODE_RULE_ENGINE)
    builder.add_node(build_monitor_agent(), NODE_MONITOR)
    builder.add_node(build_intake_agent(), NODE_INTAKE)
    builder.add_node(build_dispatch_agent(), NODE_DISPATCH)
    builder.add_node(build_alert_agent(), NODE_ALERT)
    builder.add_node(build_safety_qa_agent(), NODE_SAFETY_QA)


def _add_edges(builder: GraphBuilder) -> None:
    """Add the declared directed edges to ``builder`` (design.md §3.1/§3.2).

    The single place the edge set is declared, so both entry-point builds get
    an identical edge topology. Conditional edges use the named
    ``Callable[[GraphState], bool]`` conditions above (deviation 1), never
    ``lambda ctx:``. Unconditional edges (``rule_engine → monitor``,
    ``monitor → intake``, ``alert → safety_qa``) pass no ``condition``.
    """
    builder.add_edge(NODE_RULE_ENGINE, NODE_MONITOR)
    # Alert branch: only reachable when Monitor created/updated an incident at
    # WATCH+ (band != NORMAL).
    builder.add_edge(NODE_MONITOR, NODE_ALERT, condition=monitor_raised_incident)
    # monitor -> intake: a sweep that reveals a new hazard can be
    # cross-referenced against open requests (design.md §3.2).
    builder.add_edge(NODE_MONITOR, NODE_INTAKE)
    # Dispatch branch: only reachable when Intake produced a dispatch-eligible
    # request.
    builder.add_edge(NODE_INTAKE, NODE_DISPATCH, condition=intake_dispatch_eligible)
    builder.add_edge(NODE_ALERT, NODE_SAFETY_QA)
    # dispatch -> safety_qa only when the decision is an Irreversible_Action.
    builder.add_edge(NODE_DISPATCH, NODE_SAFETY_QA, condition=dispatch_is_irreversible)


def build_incident_graph(
    entry: EntryPoint = NODE_RULE_ENGINE,
    *,
    limits: tuple[int, int, int] | None = None,
) -> Graph:
    """Build the declared ``Incident_Graph`` for one entry point (Req 4.1, 4.4).

    Produces a fully-configured, ready-to-run ``Graph`` whose node set,
    directed edges, single entry point, and three execution limits are all
    fixed at build time and unchanged for the duration of a run (Req 4.1).
    Both legitimate entry points are built from the *same* node/edge factory
    (``_add_nodes``/``_add_edges``), so the only difference between the sweep
    graph and the intake graph is which node the run starts at (design.md
    §3.2).

    Args:
        entry: The per-run entry point. ``"rule_engine"`` (the default) for a
            routine sweep; ``"intake"`` for an inbound-resident-message run.
        limits: An optional ``(execution_timeout_s, node_timeout_s,
            max_node_executions)`` triple to wire onto the builder, primarily
            for tests. When ``None`` (the default), the limits are resolved
            from `agents.config.get_graph_execution_limits` (Req 4.4 defaults
            600 / 120 / 25, each validated against its configurable range).

    Returns:
        A built ``strands.multiagent.graph.Graph`` with the entry point set
        and the Req 4.4 limits configured.

    Raises:
        ValueError: If ``entry`` is not one of the two valid entry points, or
            if a provided ``limits`` value is out of its Req 4.4 range (raised
            by `agents.config.get_graph_execution_limits`).
    """
    if entry not in VALID_ENTRY_POINTS:
        raise ValueError(
            f"entry must be one of {sorted(VALID_ENTRY_POINTS)}; got {entry!r}"
        )

    execution_timeout_s, node_timeout_s, max_node_executions = (
        limits if limits is not None else get_graph_execution_limits()
    )

    builder = GraphBuilder()
    _add_nodes(builder)
    _add_edges(builder)

    builder.set_entry_point(entry)
    builder.set_execution_timeout(execution_timeout_s)  # Req 4.4
    builder.set_node_timeout(node_timeout_s)  # Req 4.4
    builder.set_max_node_executions(max_node_executions)  # Req 4.4

    return builder.build()


# ---------------------------------------------------------------------------
# Task 15.3 (`execute_incident_graph`, the pause-aware driver) will be added
# below this line. It is deliberately not implemented in task 15.2: this file
# provides only the declared topology (node/edge factory + entry-point
# selection + Req 4.4 limits). See design.md §3.2 and
# docs/spikes/15-graph-api-findings.md §5 for the driver's contract.
# ---------------------------------------------------------------------------

# ===========================================================================
# Task 15.3 — the pause-aware execution driver (`execute_incident_graph`).
#
# Req 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9; design.md §3.2 (Design Question 2,
# the "own thin scheduler" paragraph); docs/spikes/15-graph-api-findings.md §5.
#
# WHY A CUSTOM DRIVER (decision carried from the 15.1 spike, findings §5).
#     `build_incident_graph` above produces a real Strands ``Graph`` and we use
#     it as the *source of truth for the declared topology* (Req 4.1): the node
#     set, the directed edges with their branch ``condition`` callables, the
#     single entry point, and the three Req 4.4 limits. But ThunAI drives that
#     declared graph with an explicit scheduler rather than a bare
#     ``graph(...)`` call, because Req 4.5-4.9 need a per-node status vocabulary
#     ({succeeded, failed, timed_out, skipped, not_executed} + paused) and a run
#     outcome vocabulary ({complete, partial, halted, failed} + paused) that the
#     SDK's native ``GraphResult`` does not expose, and because cross-process
#     pause/resume with only *some* interrupt-capable member nodes was the
#     open item the spike flagged (findings §4/§5). The driver reads the
#     topology and the limits *back off the built ``Graph``* (never a second
#     hardcoded copy), so the numbers it enforces are exactly the numbers
#     ``build_incident_graph`` was configured with (Req 4.4).
#
# VERIFICATION (single focused check per task 15's "verify first" banner; the
#     15.1 spike did the deep dive). Re-confirmed on the installed
#     ``strands-agents==1.55.0`` by reading the installed package:
#       - ``AgentResult.stop_reason`` is a ``strands.types.event_loop.StopReason``
#         literal whose members include exactly ``"interrupt"`` — the design's
#         stated pause trigger (design.md §3.2 item 2, Design Question 3) is the
#         literal string ``"interrupt"``, confirmed present. ``AgentResult`` also
#         carries an ``interrupts`` field.
#       - ``strands.multiagent.base.Interrupt`` is a dataclass with fields
#         ``id: str``, ``name: str``, ``reason``, ``response`` — so the design's
#         ``result.interrupts[0].id`` accessor (design.md §3.2 item 2) is exact:
#         ``.id`` exists. Confirmed rather than assumed.
#       - ``MultiAgentResult`` and ``NodeResult`` both carry ``interrupts`` and a
#         ``Status`` that includes ``INTERRUPTED``; ``RuleEngineNode`` returns a
#         ``MultiAgentResult`` while the LLM ``Agent`` nodes return an
#         ``AgentResult``. The driver's ``_classify_result`` handles both shapes.
#     No docs-vs-design disagreement surfaced in this check; the design's exact
#     accessors hold at this pin, so no deviation is recorded for 15.3 beyond
#     those already recorded for 15.2 above.
# ===========================================================================

# --- Node status vocabulary (Req 4.5/4.6/4.9) -----------------------------
#: Exactly one of these is assigned to every declared node when a run reaches
#: a terminal state (Req 4.5), plus ``STATUS_PAUSED`` for the pause case
#: (design.md §3.2). ``not_executed`` is the initial status of every node
#: before the driver reaches it; a node that never becomes ready keeps it.
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_SKIPPED = "skipped"
STATUS_NOT_EXECUTED = "not_executed"
STATUS_PAUSED = "paused"

#: The closed set Req 4.5 mandates (paused is the design's pause addition).
NODE_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_SUCCEEDED,
        STATUS_FAILED,
        STATUS_TIMED_OUT,
        STATUS_SKIPPED,
        STATUS_NOT_EXECUTED,
        STATUS_PAUSED,
    }
)

# --- Run outcome vocabulary (Req 4.5/4.6/4.7/4.8) -------------------------
OUTCOME_COMPLETE = "complete"
OUTCOME_PARTIAL = "partial"
OUTCOME_HALTED = "halted"
OUTCOME_FAILED = "failed"
OUTCOME_PAUSED = "paused"

#: The closed set Req 4.5 mandates (paused is the design's pause addition).
RUN_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_COMPLETE,
        OUTCOME_PARTIAL,
        OUTCOME_HALTED,
        OUTCOME_FAILED,
        OUTCOME_PAUSED,
    }
)

#: Declared terminal node(s) (design.md §3.1/§3.2 topology): ``safety_qa`` is
#: the only sink (both the alert and dispatch branches converge on it). Req 4.7
#: ("a *terminal* node fails => run outcome failed") is keyed off this set; a
#: node is terminal iff no declared edge leaves it. Computed from the built
#: graph at run time rather than hardcoded, so the set can never drift from the
#: declared topology, but ``NODE_SAFETY_QA`` is the expected member.
_EXPECTED_TERMINAL_NODES: frozenset[str] = frozenset({NODE_SAFETY_QA})


@dataclass
class NodeExecution:
    """The recorded execution of one declared node (Req 4.5).

    Attributes:
        node_id: The declared node id.
        status: One of :data:`NODE_STATUSES`.
        result: The node's ``NodeResult`` when it ran (``None`` when the node
            was skipped/not_executed/errored before producing one). Retained so
            branch conditions can read prior outputs and so Req 4.7 can "retain
            every node output produced before the failure".
        failure_reason: A short human-readable reason when the node failed or
            timed out (Req 4.7 surfaces this to Coordinator_Console); ``None``
            otherwise.
        interrupt_id: For a paused Strands ``Agent`` node, the
            ``result.interrupts[0].id`` value (design.md §3.2 item 2); ``None``
            for a deterministic-gate pause or a non-paused node.
        started: Whether the driver actually invoked this node (``False`` for
            skipped / not_executed nodes — Req 4.8 "start no further node").
    """

    node_id: str
    status: str = STATUS_NOT_EXECUTED
    result: NodeResult | None = None
    failure_reason: str | None = None
    interrupt_id: str | None = None
    started: bool = False


@dataclass
class RunResult:
    """The terminal result of one ``execute_incident_graph`` run (Req 4.5).

    Attributes:
        run_id: The run identifier the caller supplied.
        incident_id: The incident identifier the caller supplied (may be
            ``None`` for a sweep that has not yet created an incident).
        outcome: One of :data:`RUN_OUTCOMES`.
        execution_order: The ordered sequence of node ids in the order the
            driver invoked them (Req 4.2/4.5). Only started nodes appear.
        node_status: Every declared node id mapped to exactly one of
            :data:`NODE_STATUSES` (Req 4.5 "exactly one status per declared
            node").
        node_executions: The full :class:`NodeExecution` record per declared
            node (results, failure reasons, interrupt ids).
        halt_reason: For a ``halted`` run, why it halted (Req 4.8); ``None``
            otherwise.
        last_completed_node: For a ``halted`` run, the last node that completed
            before the halt (Req 4.8); ``None`` when none completed.
        failed_node_id: For a ``failed`` run, the terminal node that failed
            (Req 4.7); ``None`` otherwise.
        failure_reason: For a ``failed`` run, the terminal node's failure
            reason (Req 4.7); ``None`` otherwise.
        paused_node_id: For a ``paused`` run, the node that paused (design.md
            §3.2); ``None`` otherwise.
        interrupt_id: For a ``paused`` run raised by a Strands ``Agent`` node,
            the interrupt id persisted for resume; ``None`` otherwise.
    """

    run_id: str
    incident_id: str | None
    outcome: str
    execution_order: list[str] = field(default_factory=list)
    node_status: dict[str, str] = field(default_factory=dict)
    node_executions: dict[str, NodeExecution] = field(default_factory=dict)
    halt_reason: str | None = None
    last_completed_node: str | None = None
    failed_node_id: str | None = None
    failure_reason: str | None = None
    paused_node_id: str | None = None
    interrupt_id: str | None = None


# ---------------------------------------------------------------------------
# Result classification: normalise a node's raw return (an ``AgentResult`` from
# an LLM ``Agent`` node, a ``MultiAgentResult`` from a ``MultiAgentBase`` node,
# or a raised exception the runner surfaced as a value) into the driver's
# status/interrupt vocabulary. Kept separate so the pause detection (design.md
# §3.2's two pause shapes) lives in exactly one place.
# ---------------------------------------------------------------------------
def _extract_interrupt_id(raw: Any) -> str | None:
    """Return ``raw.interrupts[0].id`` when present, else ``None``.

    Both ``AgentResult`` and ``MultiAgentResult``/``NodeResult`` carry an
    ``interrupts`` list of ``strands.multiagent.base.Interrupt`` (each with a
    ``.id`` field, confirmed at 1.55.0 — see the verification block above).
    A deterministic-gate pause (design.md §3.2's plain-Python ``paused``
    ``NodeResult``) carries no interrupt object, so this returns ``None`` for
    it — which is correct: there is no SDK interrupt id to persist for resume.
    """
    interrupts = getattr(raw, "interrupts", None)
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "id", None)


def _is_paused(raw: Any, node_result: NodeResult | None) -> bool:
    """True when a node reported a pause, in either of design.md §3.2's shapes.

    1. **Strands ``Agent`` interrupt** — the node's ``AgentResult`` (or the
       wrapping ``MultiAgentResult``) has ``stop_reason == "interrupt"`` (the
       confirmed 1.55.0 ``StopReason`` literal), i.e. a ``HumanInTheLoop``
       tool call paused mid-agent-loop.
    2. **Deterministic-gate pause** — a plain-Python ``NodeResult`` whose
       ``status`` is ``Status.INTERRUPTED`` (or a raw result carrying a truthy
       ``interrupts`` list), raised by the escalation gate directly with no SDK
       interrupt object (design.md §3.2's "no SDK interrupt object involved at
       all" branch). ``Status.INTERRUPTED`` is the SDK's own paused marker,
       reused here so a deterministic gate and an ``Agent`` interrupt converge
       on one status.
    """
    if getattr(raw, "stop_reason", None) == "interrupt":
        return True
    if node_result is not None and node_result.status == Status.INTERRUPTED:
        return True
    if getattr(raw, "interrupts", None):
        return True
    return False


@dataclass
class _Classified:
    """Internal: a node's normalised outcome before it is recorded."""

    status: str
    node_result: NodeResult | None
    failure_reason: str | None = None
    interrupt_id: str | None = None


def _unwrap_node_result(raw: Any, node_id: str) -> NodeResult | None:
    """Return the ``NodeResult`` for ``node_id`` from a node's raw return.

    A ``MultiAgentBase`` node returns a ``MultiAgentResult`` whose ``results``
    dict holds one or more ``NodeResult`` entries (``RuleEngineNode`` keys its
    single entry by ``self.name``); an LLM ``Agent`` node returns an
    ``AgentResult`` directly, which this wraps into a ``NodeResult`` so every
    recorded node has a uniform ``NodeResult`` shape the branch conditions
    (which read ``state.results[id].result``) can consume.
    """
    if isinstance(raw, MultiAgentResult):
        results = raw.results or {}
        # Prefer the entry keyed by this node's id; fall back to the sole
        # entry (RuleEngineNode keys by ``name`` == node_id, but be tolerant).
        if node_id in results:
            return results[node_id]
        if len(results) == 1:
            return next(iter(results.values()))
        return None
    if isinstance(raw, NodeResult):
        return raw
    # An ``AgentResult`` (or anything else) — wrap it so downstream code has a
    # uniform ``NodeResult``. Status is filled in by the classifier.
    return NodeResult(result=raw, status=Status.COMPLETED)


def _classify_result(raw: Any, node_id: str) -> _Classified:
    """Normalise a node's successful return value into a :class:`_Classified`.

    Pause detection wins over success: a node that paused is recorded
    ``paused`` even though it "returned" (an interrupt is a return, not a
    raise). A returned/embedded ``Status.FAILED`` ``NodeResult`` is recorded
    ``failed`` (a node that caught its own error and reported it rather than
    raising). Everything else is ``succeeded``.
    """
    node_result = _unwrap_node_result(raw, node_id)

    if _is_paused(raw, node_result):
        # The interrupt id may live on the raw result (an Agent's AgentResult),
        # on the wrapping NodeResult, or on the NodeResult's inner ``result``
        # (a MultiAgentResult wrapping an AgentResult that carries the
        # interrupt) — check all three, first hit wins.
        inner = getattr(node_result, "result", None) if node_result else None
        interrupt_id = (
            _extract_interrupt_id(raw)
            or (_extract_interrupt_id(node_result) if node_result else None)
            or (_extract_interrupt_id(inner) if inner is not None else None)
        )
        return _Classified(
            status=STATUS_PAUSED,
            node_result=node_result,
            interrupt_id=interrupt_id,
        )

    # A node that reported its own failure via NodeResult.status rather than
    # raising (or an embedded Exception result).
    if node_result is not None:
        inner = getattr(node_result, "result", None)
        if node_result.status == Status.FAILED or isinstance(inner, Exception):
            reason = str(inner) if isinstance(inner, Exception) else "node reported FAILED"
            return _Classified(
                status=STATUS_FAILED, node_result=node_result, failure_reason=reason
            )

    return _Classified(status=STATUS_SUCCEEDED, node_result=node_result)


# ---------------------------------------------------------------------------
# The default node runner. Invokes a node's executor uniformly whether it is a
# ``MultiAgentBase`` (``invoke_async``) or a Strands ``Agent`` (``invoke_async``
# too — both expose it), enforcing the per-node timeout (Req 4.4). Injectable
# so tests can drive the graph with in-process fakes and no live model call
# (design principle 5 / Req 21.10).
# ---------------------------------------------------------------------------
NodeRunner = Callable[[Any, dict[str, Any]], Any]
"""A callable ``(executor, invocation_state) -> raw_result``. May be async or
sync; the driver awaits it if it returns an awaitable. Raising is how a runner
signals a hard node failure (mapped to ``failed``); returning normally lets
:func:`_classify_result` decide succeeded/failed/paused."""


async def _default_node_runner(executor: Any, invocation_state: dict[str, Any]) -> Any:
    """Invoke a node's ``invoke_async`` (Req 4.4/design), handling both node types.

    The two node kinds have DIFFERENT ``invoke_async`` signatures in the
    pinned ``strands-agents==1.55.0`` SDK, so the driver must call each the
    way that SDK version declares:

    - ``MultiAgentBase`` subclasses (``RuleEngineNode``):
      ``invoke_async(task, invocation_state)`` — ``invocation_state`` is the
      second POSITIONAL parameter.
    - Strands ``Agent`` (the LLM nodes: monitor/intake/dispatch/alert/
      safety_qa): ``invoke_async(prompt=None, *, invocation_state=None, ...)``
      — ``invocation_state`` is KEYWORD-ONLY. Passing it positionally raises
      ``TypeError: invoke_async() takes from 1 to 2 positional arguments but 3
      were given`` (which previously failed every LLM node at runtime).

    ThunAI's nodes all read everything they need from ``invocation_state`` and
    ignore the prompt/task, so an empty prompt is passed in both cases.
    """
    from strands.multiagent.base import MultiAgentBase

    if isinstance(executor, MultiAgentBase):
        # MultiAgentBase: invocation_state is positional arg #2. These nodes
        # ignore the task, so an empty string is fine here.
        return await executor.invoke_async("", invocation_state)
    # Strands Agent (the LLM nodes): invocation_state is keyword-only in the
    # pinned SDK, AND the call needs a real user prompt (an empty/None prompt
    # is rejected by Bedrock's ConverseStream). Each agent has a role-specific
    # system prompt + tools; we hand it a concise task prompt built from the
    # shared invocation_state so it can fetch specifics via its own tools and
    # produce its structured decision.
    agent_name = getattr(executor, "name", None) or getattr(executor, "agent_id", "")
    prompt = _build_node_prompt(str(agent_name), invocation_state)
    return await executor.invoke_async(prompt, invocation_state=invocation_state)


def _build_node_prompt(agent_name: str, ctx: dict[str, Any]) -> str:
    """Build the user prompt for one LLM ``Agent`` node from the shared context.

    The graph threads one ``invocation_state`` (``incident_ctx``) to every
    node. Each specialist agent needs a task prompt naming what to assess;
    the agent's own tools (river-level/rainfall/dam-release readers, candidate-
    responder finder, etc.) fetch the specifics, and its
    ``structured_output_model`` shapes the decision the graph branches on.
    """
    reach = ctx.get("river_reach_id", "the monitored reach")
    as_of = ctx.get("as_of", "now")
    incident_id = ctx.get("incident_id", "")
    readings = ctx.get("readings", {})
    message = ctx.get("message", "")
    language = ctx.get("language", "English")

    if agent_name == "monitor_agent":
        return (
            f"Assess the flood hazard for river reach {reach!r} as of {as_of}. "
            f"Latest readings: {readings}. Use your reading tools and the prior "
            f"sweep to judge whether this is a meaningful change worth a human's "
            f"attention. Return your hazard assessment (severity band, confidence, "
            f"proposed action, one-sentence rationale)."
        )
    if agent_name == "intake_agent":
        return (
            f"Triage this inbound resident message for reach {reach!r}: "
            f"{message!r} (language: {language}). Resolve location and language, "
            f"classify the request, and produce the structured emergency request "
            f"including whether it is dispatch-eligible."
        )
    if agent_name == "dispatch_agent":
        return (
            f"Decide responder dispatch for incident {incident_id!r} on reach "
            f"{reach!r}. Use your candidate-responder tool to find who is "
            f"available and produce a dispatch decision, flagging any "
            f"irreversible action for human approval."
        )
    if agent_name == "alert_agent":
        return (
            f"Compose ONE short recommended-action sentence in {language} for "
            f"incident {incident_id!r} on reach {reach!r}, based on the current "
            f"severity and readings {readings}."
        )
    if agent_name == "safety_qa_agent":
        return (
            f"Review the proposed public-facing content for incident "
            f"{incident_id!r} against the safety rubric and return your safety "
            f"review (pass/fail with any violated policy ids)."
        )
    # Fallback: a generic assessment prompt so an unrecognised agent still runs.
    return (
        f"Process the current incident context for reach {reach!r} as of {as_of} "
        f"and return your structured decision."
    )


async def _run_one_node(
    executor: Any,
    invocation_state: dict[str, Any],
    node_timeout_s: float,
    runner: NodeRunner,
) -> tuple[str, Any]:
    """Run one node under the per-node timeout (Req 4.4/4.6).

    Returns a ``(disposition, value)`` tuple where ``disposition`` is one of
    ``"ok"`` (``value`` is the raw node return), ``"timeout"`` (``value`` is
    the elapsed-seconds float), or ``"error"`` (``value`` is the exception).
    A per-node timeout is *not* a run-level halt — Req 4.6 treats a per-node
    timeout exactly like a per-node failure (continue other branches), so it is
    surfaced here as a node-level disposition, distinct from the overall-run
    timeout the caller checks separately (Req 4.8).
    """
    try:
        maybe = runner(executor, invocation_state)
        awaitable = maybe if inspect.isawaitable(maybe) else _wrap_value(maybe)
        raw = await asyncio.wait_for(awaitable, timeout=node_timeout_s)
        return ("ok", raw)
    except asyncio.TimeoutError:
        return ("timeout", node_timeout_s)
    except Exception as exc:  # noqa: BLE001 - a node's hard failure (Req 4.6/4.7)
        # Log the concrete cause so a container run surfaces WHY a node failed
        # (the run-outcome audit entry records only the status, not the trace).
        import logging
        import traceback

        logging.getLogger("thunai.incident_graph").error(
            "NODE FAILURE in %r: %s\n%s",
            getattr(executor, "name", type(executor).__name__),
            repr(exc),
            traceback.format_exc(),
        )
        return ("error", exc)


async def _wrap_value(value: Any) -> Any:
    """Await-adapter for a synchronous runner return value."""
    return value


# ---------------------------------------------------------------------------
# Topology introspection: read the declared node set, edges, entry point(s),
# and Req 4.4 limits back off the *built* ``Graph`` so the driver enforces the
# same numbers ``build_incident_graph`` configured (Req 4.4) and never keeps a
# second hardcoded copy of the topology.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Topology:
    """The declared structure the driver walks, read off the built ``Graph``."""

    node_ids: tuple[str, ...]
    executors: dict[str, Any]
    #: node_id -> list of (successor_id, condition) for each outgoing edge.
    successors: dict[str, list[tuple[str, Any]]]
    #: node_id -> set of direct predecessor node ids (edges pointing at it).
    predecessors: dict[str, frozenset[str]]
    entry_points: tuple[str, ...]
    terminal_nodes: frozenset[str]
    execution_timeout_s: float
    node_timeout_s: float
    max_node_executions: int


def _read_topology(graph: Graph) -> _Topology:
    """Extract the declared topology + Req 4.4 limits from a built ``Graph``.

    Reads ``graph.nodes`` (id -> ``GraphNode`` with ``.executor``),
    ``graph.edges`` (``GraphEdge`` with ``.from_node``/``.to_node``/
    ``.condition``), ``graph.entry_points``, and the three limit attributes
    (``execution_timeout``/``node_timeout``/``max_node_executions``). Nodes are
    ordered by ``GRAPH_NODE_IDS`` (the module's declared add-order) so a fixed,
    deterministic tie-break exists whenever more than one node is ready at once
    (Req 4.2). A node with no outgoing edge is terminal (Req 4.7).
    """
    node_ids = tuple(nid for nid in GRAPH_NODE_IDS if nid in graph.nodes)
    executors = {nid: graph.nodes[nid].executor for nid in node_ids}

    successors: dict[str, list[tuple[str, Any]]] = {nid: [] for nid in node_ids}
    predecessors: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for edge in graph.edges:
        src = edge.from_node.node_id
        dst = edge.to_node.node_id
        successors.setdefault(src, []).append((dst, edge.condition))
        predecessors.setdefault(dst, set()).add(src)
    # Keep each successor list in the module's declared node order for a
    # deterministic walk (Req 4.2), independent of the set-iteration order of
    # ``graph.edges``.
    order_index = {nid: i for i, nid in enumerate(GRAPH_NODE_IDS)}
    for src in successors:
        successors[src].sort(key=lambda pair: order_index.get(pair[0], len(order_index)))

    terminal_nodes = frozenset(nid for nid in node_ids if not successors.get(nid))
    entry_points = tuple(
        nid for nid in GRAPH_NODE_IDS if nid in {n.node_id for n in graph.entry_points}
    )

    # Req 4.4: enforce the same numbers the builder configured. A built graph
    # always has them set (build_incident_graph wires all three), but fall back
    # to the config defaults if a caller passes a graph with a limit left None.
    default_exec, default_node, default_max = get_graph_execution_limits()
    return _Topology(
        node_ids=node_ids,
        executors=executors,
        successors={k: v for k, v in successors.items() if k in node_ids},
        predecessors={nid: frozenset(predecessors.get(nid, set())) for nid in node_ids},
        entry_points=entry_points,
        terminal_nodes=terminal_nodes,
        execution_timeout_s=float(
            graph.execution_timeout if graph.execution_timeout is not None else default_exec
        ),
        node_timeout_s=float(
            graph.node_timeout if graph.node_timeout is not None else default_node
        ),
        max_node_executions=int(
            graph.max_node_executions
            if graph.max_node_executions is not None
            else default_max
        ),
    )


def _nodes_consuming_only(topo: _Topology, failed_node: str) -> set[str]:
    """Return every node reachable *only* through ``failed_node`` (Req 4.6).

    Req 4.6: when a non-terminal node fails, "record every node that consumes
    only that node's output with status not_executed", while continuing every
    branch that does not consume that node's output. A node "consumes only" the
    failed node's output when *every* path from an entry point to it must pass
    through the failed node — i.e. it is unreachable once the failed node is
    removed from the topology. This computes that set by a forward reachability
    check from the entry points with ``failed_node`` deleted: any node that was
    otherwise reachable but is now unreachable consumes only the failed node's
    output (directly or transitively) and is recorded ``not_executed``.

    Nodes on branches that do *not* depend on the failed node stay reachable
    and are therefore *not* in the returned set, so the driver keeps executing
    them (Req 4.6's "continue every branch that does not consume that node's
    output").
    """
    reachable_without = _reachable_from_entries(topo, excluded=failed_node)
    reachable_with = _reachable_from_entries(topo, excluded=None)
    # Nodes that were reachable but become unreachable once failed_node is
    # removed — these consume only the failed node's output (transitively).
    return {
        nid
        for nid in reachable_with
        if nid not in reachable_without and nid != failed_node
    }


def _reachable_from_entries(topo: _Topology, *, excluded: str | None) -> set[str]:
    """Forward reachability over the *structural* edges, ignoring conditions.

    Used only for the Req 4.6 "consumes only that node's output" computation,
    which is a purely structural question ("is there any path avoiding the
    failed node?"), so edge conditions are deliberately not evaluated here —
    a branch that is structurally independent of the failed node must not be
    marked ``not_executed`` even if its own condition happens to be false.
    """
    reachable: set[str] = set()
    queue: deque[str] = deque(
        nid for nid in topo.entry_points if nid != excluded
    )
    while queue:
        nid = queue.popleft()
        if nid in reachable or nid == excluded:
            continue
        reachable.add(nid)
        for succ, _cond in topo.successors.get(nid, []):
            if succ != excluded and succ not in reachable:
                queue.append(succ)
    return reachable


def _build_graph_state(executions: dict[str, NodeExecution]) -> GraphState:
    """Build a ``GraphState`` carrying every node result recorded so far.

    The branch ``condition`` callables (``monitor_raised_incident`` etc.) read
    ``state.results[id].result``; this assembles that ``results`` dict from the
    driver's own per-node records so a condition sees exactly the outputs
    produced up to this point in the run — the same view the SDK's native
    executor would hand a condition, reconstructed from the driver's ledger.
    """
    results = {
        nid: ex.result
        for nid, ex in executions.items()
        if ex.result is not None
    }
    return GraphState(results=results)


def _edge_open(condition: Any, state: GraphState) -> bool:
    """Evaluate one edge's ``condition`` against the current ``GraphState``.

    An unconditional edge (``condition is None``) is always open. A conditional
    edge is open iff its named ``Callable[[GraphState], bool]`` returns truthy
    (the confirmed legacy signature — findings §3). The condition functions are
    all None-guarded, so evaluating one against a partial state is always safe.
    """
    if condition is None:
        return True
    return bool(condition(state))


# ---------------------------------------------------------------------------
# Persistence seams (Req 4.5, 4.7, 4.8, 15.5). Imported lazily inside the
# helpers rather than at module top so that constructing/inspecting a graph
# (and the branch-condition unit tests) never require the DynamoDB-backed
# ``memory``/``harness`` modules to import cleanly. The two persistence
# functions are also injectable on ``execute_incident_graph`` so tests drive
# them with in-process fakes (no network) per design principle 5 / Req 21.10.
# ---------------------------------------------------------------------------
def _persist_run_progress(
    run_id: str, node_id: str, status: str, payload: dict[str, Any]
) -> None:
    """Persist one node's run progress to ``State_Store`` (Req 15.5).

    Thin adapter over ``memory.state_store.persist_run_progress`` so the driver
    does not re-implement persistence (it reuses the ``RUN#{run_id}`` /
    ``NODE#{node_id}`` item shape that module already owns). On pause, the
    payload carries which node paused, its inputs, and the ``interruptId`` —
    exactly design.md §3.2 item 2's contract — keyed under the incident's
    ``Idempotency_Key`` (the caller passes ``run_id`` == the incident's
    ``Idempotency_Key`` for a paused escalation, per design.md §3.2).
    """
    from memory.state_store import persist_run_progress

    persist_run_progress(run_id, node_id, status, payload)


def _append_run_outcome_audit(
    run_id: str,
    incident_id: str | None,
    outcome: str,
    execution_order: list[str],
    node_status: dict[str, str],
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append the terminal run-outcome entry to ``Audit_Ledger`` (Req 4.5/4.7/4.8).

    Reuses ``harness.audit.append_audit_entry`` (which redacts, builds the
    ``AuditEntry``, and delegates to ``memory.audit_ledger.append_entry``) so
    the ordered node execution sequence, the per-node statuses, and the run
    outcome are recorded through the one append-only path the rest of the
    system reads (Req 4.5 "make readable in Coordinator_Console"; Req 12.7's
    per-incident read is served by passing ``incident_id``). ``extra`` carries
    the outcome-specific fields Req 4.7/4.8 require (failed node id + reason;
    halt reason + last completed node).
    """
    from harness.audit import append_audit_entry

    inputs: dict[str, Any] = {
        "execution_order": list(execution_order),
        "node_status": dict(node_status),
    }
    if extra:
        inputs.update(extra)
    append_audit_entry(
        run_id=run_id,
        tool_name="incident_graph.run_outcome",
        outcome=outcome,
        inputs=inputs,
        incident_id=incident_id,
    )


async def execute_incident_graph(
    graph: Graph,
    incident_ctx: dict[str, Any],
    *,
    run_id: str,
    incident_id: str | None = None,
    node_runner: NodeRunner | None = None,
    persist_progress: Callable[[str, str, str, dict[str, Any]], None] | None = None,
    append_audit: Callable[..., None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> RunResult:
    """Drive a built ``Incident_Graph`` to a terminal state (Req 4.2, 4.4-4.9).

    The pause-aware thin scheduler of design.md §3.2. It walks the declared
    topology of ``graph`` (read back off the built ``Graph`` — Req 4.1/4.4),
    invoking each node whose incoming edge condition is open, in the module's
    fixed declared node order so identical inputs yield an identical execution
    order (Req 4.2). It stops the whole run the moment any node reports a pause
    (design.md §3.2), enforces the overall-run timeout and max-node-execution
    count (Req 4.4/4.8), continues independent branches when a non-terminal
    node fails (Req 4.6), fails the run when a terminal node fails (Req 4.7),
    and records every unreachable-branch node ``skipped`` (Req 4.9). On a
    terminal state it assigns exactly one status per declared node and exactly
    one run outcome (Req 4.5) and persists them to ``Audit_Ledger``.

    Args:
        graph: A built ``Graph`` from :func:`build_incident_graph`. Its node
            set, edges, entry point, and the three Req 4.4 limits are read back
            off it; the driver never keeps a second copy of the topology.
        incident_ctx: The shared runtime context threaded to every node as its
            ``invocation_state`` (design.md §3.2 carries ``incident_ctx`` via
            ``invocation_state``, not a bespoke ``ctx`` object). For the
            ``rule_engine`` node this must carry ``readings``/``now``/
            ``last_known_band`` per that node's contract.
        run_id: The run identifier. For a paused escalation this is the
            incident's ``Idempotency_Key`` (design.md §3.2), so run-progress
            persists under that key.
        incident_id: The incident identifier when known, for the per-incident
            audit index (Req 12.7) and Req 4.7's surfaced failure.
        node_runner: Optional injectable ``(executor, invocation_state) ->
            raw`` used to invoke each node; defaults to
            :func:`_default_node_runner`. Tests pass an in-process fake so no
            live model call happens (design principle 5).
        persist_progress: Optional injectable run-progress persister; defaults
            to :func:`_persist_run_progress`.
        append_audit: Optional injectable terminal-audit appender; defaults to
            :func:`_append_run_outcome_audit`.
        now: Optional injectable monotonic-ish clock returning a timezone-aware
            ``datetime``, for the overall-run timeout (Req 4.8); defaults to
            ``datetime.now(timezone.utc)``. Injectable so a test can force a
            deadline breach deterministically without sleeping.

    Returns:
        A :class:`RunResult` with the run outcome, the ordered execution
        sequence, exactly one status per declared node, and the outcome-
        specific fields (halt reason / failed node / paused node).
    """
    runner = node_runner or _default_node_runner
    persist = persist_progress or _persist_run_progress
    audit = append_audit or _append_run_outcome_audit
    clock = now or (lambda: datetime.now(timezone.utc))

    topo = _read_topology(graph)

    # Every declared node starts ``not_executed`` (Req 4.5): a node the driver
    # never reaches keeps this status, which is exactly Req 4.6's "consumes
    # only [a failed node]'s output" outcome and the default for any node left
    # unvisited.
    executions: dict[str, NodeExecution] = {
        nid: NodeExecution(node_id=nid) for nid in topo.node_ids
    }
    execution_order: list[str] = []
    last_completed_node: str | None = None
    started_count = 0
    start_time = clock()

    def _elapsed() -> float:
        return (clock() - start_time).total_seconds()

    # Ready-set walk. A node is ready when it has not run yet and at least one
    # incoming edge is open (OR semantics — findings §3 note 2), or it is an
    # entry point (no predecessors to gate it). Entry points are seeded ready.
    # We recompute readiness each iteration against the accumulated results so
    # a branch condition sees every upstream output produced so far.
    def _ready_nodes() -> list[str]:
        state = _build_graph_state(executions)
        ready: list[str] = []
        for nid in topo.node_ids:
            if executions[nid].started:
                continue
            # A declared entry point is always ready to start, regardless of
            # whether the topology also declares edges into it: the run begins
            # *at* the entry point (Req 4.1's "single entry point"), so any
            # incoming edge from a node that never runs on this entry does not
            # gate it. (E.g. the ``intake`` entry point still has a declared
            # ``monitor -> intake`` edge, but on an intake-entry run monitor
            # never runs, so that edge must not block intake from starting.)
            if nid in topo.entry_points:
                ready.append(nid)
                continue
            preds = topo.predecessors[nid]
            if not preds:
                continue
            # Ready iff at least one incoming edge from an already-succeeded
            # predecessor is open (OR semantics).
            for pred in preds:
                if executions[pred].status != STATUS_SUCCEEDED:
                    continue
                for succ, cond in topo.successors.get(pred, []):
                    if succ == nid and _edge_open(cond, state):
                        ready.append(nid)
                        break
                if nid in ready:
                    break
        return ready

    def _finalize(
        outcome: str,
        *,
        halt_reason: str | None = None,
        failed_node_id: str | None = None,
        failure_reason: str | None = None,
        paused_node_id: str | None = None,
        interrupt_id: str | None = None,
    ) -> RunResult:
        # Skipped-vs-not_executed classification applies only when the run
        # reached a genuine *terminal* state (Req 4.5's status set). A paused
        # run is non-terminal — it will resume and its downstream nodes may yet
        # run — so its un-run nodes stay ``not_executed`` rather than being
        # prematurely recorded ``skipped``.
        if outcome != OUTCOME_PAUSED:
            _assign_skipped_vs_not_executed(topo, executions)
        node_status = {nid: executions[nid].status for nid in topo.node_ids}
        result = RunResult(
            run_id=run_id,
            incident_id=incident_id,
            outcome=outcome,
            execution_order=execution_order,
            node_status=node_status,
            node_executions=executions,
            halt_reason=halt_reason,
            last_completed_node=last_completed_node,
            failed_node_id=failed_node_id,
            failure_reason=failure_reason,
            paused_node_id=paused_node_id,
            interrupt_id=interrupt_id,
        )
        return result

    while True:
        # Req 4.8: overall-run timeout / max-node-execution halt is checked
        # *before* starting any further node ("start no further node").
        if _elapsed() > topo.execution_timeout_s:
            reason = (
                f"overall run execution timeout exceeded "
                f"({_elapsed():.1f}s > {topo.execution_timeout_s:.0f}s)"
            )
            result = _finalize(
                OUTCOME_HALTED, halt_reason=reason
            )
            audit(
                run_id,
                incident_id,
                OUTCOME_HALTED,
                execution_order,
                result.node_status,
                extra={"halt_reason": reason, "last_completed_node": last_completed_node},
            )
            return result
        if started_count >= topo.max_node_executions:
            reason = (
                f"maximum node execution count reached "
                f"({started_count} >= {topo.max_node_executions})"
            )
            result = _finalize(OUTCOME_HALTED, halt_reason=reason)
            audit(
                run_id,
                incident_id,
                OUTCOME_HALTED,
                execution_order,
                result.node_status,
                extra={"halt_reason": reason, "last_completed_node": last_completed_node},
            )
            return result

        ready = _ready_nodes()
        if not ready:
            break  # no more nodes can run -> terminal state reached

        # Deterministic pick: the first ready node in declared node order
        # (Req 4.2). Running one node per iteration (rather than a batch) keeps
        # the recorded ``execution_order`` a single, deterministic sequence and
        # lets a pause stop the run immediately (design.md §3.2).
        node_id = ready[0]
        ex = executions[node_id]
        ex.started = True
        started_count += 1
        execution_order.append(node_id)

        disposition, value = await _run_one_node(
            topo.executors[node_id], incident_ctx, topo.node_timeout_s, runner
        )

        is_terminal = node_id in topo.terminal_nodes

        if disposition == "timeout":
            ex.status = STATUS_TIMED_OUT
            ex.failure_reason = (
                f"per-node execution timeout exceeded ({topo.node_timeout_s:.0f}s)"
            )
            if is_terminal:
                return _fail_run(
                    node_id, ex, executions, execution_order, run_id, incident_id, audit, _finalize
                )
            _mark_consumers_not_executed(topo, executions, node_id)
            continue

        if disposition == "error":
            ex.status = STATUS_FAILED
            ex.failure_reason = str(value)
            if is_terminal:
                return _fail_run(
                    node_id, ex, executions, execution_order, run_id, incident_id, audit, _finalize
                )
            _mark_consumers_not_executed(topo, executions, node_id)
            continue

        # disposition == "ok": classify the returned value.
        classified = _classify_result(value, node_id)
        ex.result = classified.node_result

        if classified.status == STATUS_PAUSED:
            ex.status = STATUS_PAUSED
            ex.interrupt_id = classified.interrupt_id
            # design.md §3.2 items 2-3: persist run progress under the incident
            # Idempotency_Key (which node paused, its inputs, the interruptId),
            # stop advancing further nodes, return a paused outcome.
            persist(
                run_id,
                node_id,
                STATUS_PAUSED,
                {
                    "node_id": node_id,
                    "inputs": _persistable_inputs(incident_ctx),
                    "interrupt_id": classified.interrupt_id,
                    "incident_id": incident_id,
                },
            )
            return _finalize(
                OUTCOME_PAUSED,
                paused_node_id=node_id,
                interrupt_id=classified.interrupt_id,
            )

        if classified.status == STATUS_FAILED:
            ex.status = STATUS_FAILED
            ex.failure_reason = classified.failure_reason
            if is_terminal:
                return _fail_run(
                    node_id, ex, executions, execution_order, run_id, incident_id, audit, _finalize
                )
            _mark_consumers_not_executed(topo, executions, node_id)
            continue

        # Succeeded.
        ex.status = STATUS_SUCCEEDED
        last_completed_node = node_id
        # Persist per-node progress so a later resume knows this step is done
        # (Req 15.5/15.11); best-effort relative to the terminal audit write.
        persist(run_id, node_id, STATUS_SUCCEEDED, {"node_id": node_id})

    # Reached a terminal state with no pending nodes.
    any_failed = any(
        executions[nid].status in {STATUS_FAILED, STATUS_TIMED_OUT}
        for nid in topo.node_ids
    )
    outcome = OUTCOME_PARTIAL if any_failed else OUTCOME_COMPLETE
    result = _finalize(outcome)
    audit(run_id, incident_id, outcome, execution_order, result.node_status)
    return result


def _fail_run(
    node_id: str,
    ex: NodeExecution,
    executions: dict[str, NodeExecution],
    execution_order: list[str],
    run_id: str,
    incident_id: str | None,
    audit: Callable[..., None],
    finalize: Callable[..., RunResult],
) -> RunResult:
    """Terminal-node failure/timeout => run outcome ``failed`` (Req 4.7).

    Retains every node output produced before the failure (they are already on
    ``executions``), surfaces the incident id + failed node id + failure reason
    (both on the returned :class:`RunResult` and in the audit entry), and
    records the run ``failed``.
    """
    result = finalize(
        OUTCOME_FAILED,
        failed_node_id=node_id,
        failure_reason=ex.failure_reason,
    )
    audit(
        run_id,
        incident_id,
        OUTCOME_FAILED,
        execution_order,
        result.node_status,
        extra={
            "failed_node_id": node_id,
            "failure_reason": ex.failure_reason,
            "incident_id": incident_id,
        },
    )
    return result


def _mark_consumers_not_executed(
    topo: _Topology, executions: dict[str, NodeExecution], failed_node: str
) -> None:
    """Record every node consuming only ``failed_node``'s output as not_executed
    (Req 4.6), leaving independent branches untouched so they keep running."""
    for nid in _nodes_consuming_only(topo, failed_node):
        if not executions[nid].started and executions[nid].status == STATUS_NOT_EXECUTED:
            executions[nid].status = STATUS_NOT_EXECUTED  # explicit: stays not_executed
            executions[nid].failure_reason = (
                f"not executed: consumes only the output of failed node {failed_node!r}"
            )


def _assign_skipped_vs_not_executed(
    topo: _Topology, executions: dict[str, NodeExecution]
) -> None:
    """Split the un-run declared nodes into ``skipped`` vs ``not_executed``.

    Two distinct reasons a declared node never ran, with two distinct statuses:

    - **``skipped`` (Req 4.9)** — the node's *branch was not taken*: either its
      guarding branch condition stayed closed (e.g. monitor returned NORMAL so
      the ``monitor -> alert`` edge never opened), or the node's whole branch
      is structurally unreachable from the entry point actually used (e.g. on
      an ``intake``-entry run the entire ``rule_engine``/``monitor``/``alert``
      branch never runs). Req 4.9 requires *every node of the unreachable
      branch* to be recorded ``skipped``.
    - **``not_executed`` (Req 4.6)** — the node consumes *only* the output of a
      node that failed or timed out, so it could not run for a reason other
      than an untaken branch. These were already tagged with a ``failure_reason``
      by :func:`_mark_consumers_not_executed`; they are left ``not_executed``.

    The distinguishing marker is the ``failure_reason`` stamped by
    :func:`_mark_consumers_not_executed`: an un-run node carrying it stays
    ``not_executed`` (Req 4.6); every other still-un-run node becomes
    ``skipped`` (Req 4.9), covering both the closed-condition case and the
    structurally-unreachable-branch case uniformly.
    """
    for nid in topo.node_ids:
        ex = executions[nid]
        if ex.started or ex.status != STATUS_NOT_EXECUTED:
            continue
        if ex.failure_reason:  # marked by _mark_consumers_not_executed -> keep
            continue
        ex.status = STATUS_SKIPPED


def _persistable_inputs(incident_ctx: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON/DynamoDB-safe view of a paused node's inputs (Req 15.5).

    design.md §3.2 item 2 persists "what its inputs were" so a resume in a
    different process can reconstruct the node. ``incident_ctx`` may carry
    non-serialisable values (e.g. a ``datetime`` under ``now``); those are
    stringified so the run-progress item is always persistable, while the
    ``state_store`` layer handles float->Decimal conversion itself.
    """
    safe: dict[str, Any] = {}
    for key, value in incident_ctx.items():
        if isinstance(value, datetime):
            safe[key] = value.isoformat()
        else:
            safe[key] = value
    return safe
