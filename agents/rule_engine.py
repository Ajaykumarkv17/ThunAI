"""Deterministic ``Rule_Engine`` custom multi-agent graph node (Req 2, design.md §3.3).

Implements ``RuleEngineNode``: a zero-model-call evaluator of the
safety-critical hazard thresholds defined in `policy.rule_engine_rules`.
This is the **only** source of threshold values this module consults — no
threshold, staleness limit, or rule-set version is duplicated or hardcoded
here (task 4.1 instructions).

Verification note (mandatory per tasks.md 4.1, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (matches
    `pyproject.toml`'s pin; `pip show strands-agents`). Read
    ``strands/multiagent/base.py`` directly from the installed package
    (the authoritative source for this exact pin, since the SDK ships
    weekly and design.md was pinned to 1.54.0) rather than relying solely on
    the docs site, which can drift from what is actually installed. Findings
    and every deviation from design.md §3.3's assumed shape are recorded
    below.

    Confirmed still current, matching design.md's assumed imports:
        - ``strands.multiagent.base.MultiAgentBase`` — abstract base class
          for a custom graph node. Subclasses MUST implement
          ``async def invoke_async(self, task, invocation_state, **kwargs)
          -> MultiAgentResult`` (still abstract in the installed base
          class). ``__call__`` (sync) is provided by the base class and
          delegates to ``invoke_async`` via ``run_async`` — callers may
          invoke a node either way; this node implements only the required
          async method.
        - ``strands.multiagent.base.MultiAgentResult`` — dataclass with
          ``status: Status`` and ``results: dict[str, NodeResult]`` (plus
          usage/metrics/execution_time/interrupts fields this node leaves at
          their defaults, since it performs 0 model invocations and never
          interrupts).
        - ``strands.multiagent.base.NodeResult`` — dataclass wrapping
          ``result: AgentResult | MultiAgentResult | Exception`` plus
          ``status: Status``.
        - ``strands.multiagent.base.Status`` — enum with members
          ``PENDING``, ``EXECUTING``, ``COMPLETED``, ``FAILED``,
          ``INTERRUPTED``. Design.md's assumed ``Status.COMPLETED`` member
          exists unchanged.
        - ``strands.agent.agent_result.AgentResult`` — still the module path
          design.md assumes. Confirmed its dataclass fields:
          ``stop_reason: StopReason``, ``message: Message``,
          ``metrics: EventLoopMetrics`` (**not** ``None`` — see deviation
          below), ``state: Any``, plus optional ``interrupts``,
          ``structured_output``, ``checkpoint``.
        - ``strands.types.content.Message`` / ``ContentBlock`` — still
          current; ``Message`` is a ``TypedDict`` with ``content: list[
          ContentBlock]`` and ``role: Role`` (plain dict construction, not a
          class constructor call — see deviation below), so this module
          builds it as a dict literal rather than
          ``Message(role=..., content=[...])``.

    Deviations from design.md §3.3's worked code block, each recorded here
    because it changes the exact call shape (per tasks.md's "when the docs
    and the design disagree, the docs win"):
        1. ``AgentResult.metrics`` is a required, non-Optional
           ``EventLoopMetrics`` field in the installed SDK (design.md's
           snippet passes ``metrics=None``, which the installed dataclass
           accepts positionally/by-keyword with no runtime type
           enforcement, but is not type-correct and is avoided here).
           This node has no real event-loop metrics to report (it makes
           0 model invocations), so it passes a fresh, empty
           ``EventLoopMetrics()`` instance — accurately representing "no
           model activity occurred" rather than a placeholder ``None``.
        2. ``strands.types.content.Message`` is a ``TypedDict``, not a
           dataclass/class with a keyword constructor — design.md's
           ``Message(role="assistant", content=[...])`` call syntax would
           raise ``TypeError`` (``TypedDict`` is not callable that way at
           runtime in the way a regular class is; it is only usable as a
           type annotation plus a plain-dict literal). This module builds
           the message as a plain dict literal:
           ``{"role": "assistant", "content": [...]}``. Likewise
           ``ContentBlock`` is used only as a plain dict entry
           ``{"text": ...}``, matching how the SDK's own internals populate
           ``Message["content"]`` elsewhere in the codebase.
        3. ``MultiAgentBase.__init__`` now exists and performs required base
           setup (``self._invocation_start_time = None``, used by the base
           class's own execution-time bookkeeping helpers). Design.md's
           snippet does not show an ``__init__`` at all; this module adds
           one that calls ``super().__init__()`` before setting
           node-specific attributes, so the base class's bookkeeping state
           is correctly initialised (omitting the ``super().__init__()``
           call would leave ``_invocation_start_time`` unset and raise
           ``AttributeError`` the first time any base-class helper method
           that reads it is invoked).
        4. ``MultiAgentBase`` declares a class-level ``id: str`` attribute
           (used for session management elsewhere in the SDK, per its own
           docstring: "Unique MultiAgent id for session management, etc.").
           Design.md's snippet sets only ``name = "rule_engine"``. This
           module sets ``id = "rule_engine"`` as well (mirroring ``name``)
           so any future session-management integration that reads
           ``self.id`` finds a stable value rather than an unset attribute.
        5. ``invoke_async``'s ``task`` parameter is typed
           ``MultiAgentInput`` (``str | list[ContentBlock] |
           list[InterruptResponseContent]``) in the installed SDK. This
           node performs no model call and needs no prompt text at all, so
           it accepts (and ignores) whatever ``task`` value the graph driver
           passes, exactly as design.md's snippet does (it never reads
           ``task``, only ``invocation_state``). Documented here rather than
           left as a silent implication.
        6. ``THRESHOLDS``/``STALENESS_LIMIT_S``/``RULE_SET_VERSION`` are
           imported directly from `policy.rule_engine_rules`, matching
           design.md's import line exactly. No deviation on that import.

    Input-shape choice (per task instructions, "accept whatever input shape
    is natural for a graph node"): design.md's own worked example reads
    ``invocation_state["readings"]``, ``invocation_state["now"]``, and
    ``invocation_state["last_known_band"]`` — i.e. via the ``invocation_state``
    dict that `Incident_Graph`'s pause-aware driver (design.md §3.2, task 15,
    not yet implemented) is expected to thread through every node. This
    module follows that exact convention rather than inventing a different
    shape, since `Incident_Graph` will construct this node's caller and must
    match whatever contract this node declares. ``now`` is read from
    ``invocation_state["now"]`` rather than a real clock call, exactly per
    the task's determinism requirement (an injectable "now" for testability,
    needed by Property 1/Property 4, tasks 4.2-4.3).

Req 2.10 (rule-set load failure) and Monitor_Agent's consumption of this
node's `unverified` flag (Req 2.9/2.10, design §3.1 Monitor_Agent) are noted
here for the implementer of `agents/monitor_agent.py` (task 13.2, not part
of this task): Req 2.9 states that on total outage, ``Rule_Engine`` "SHALL
apply no decrease to the severity band of an open incident" — this node
implements that by returning ``invocation_state["last_known_band"]``
unchanged (never computing a fresh NORMAL) and setting ``unverified=True``
in its result payload, so ``Monitor_Agent`` can read ``unverified`` and
decide whether the total-outage condition itself is a decision requiring
escalation (Req 3.6, "escalate a data-outage decision to the coordinator")
without this node making that escalation call itself — this node has no
knowledge of `Escalation_Service` and no model calls, deterministic-only,
per Req 2.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from strands.agent.agent_result import AgentResult
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status
from strands.telemetry.metrics import EventLoopMetrics
from strands.types.multiagent import MultiAgentInput

from policy.rule_engine_rules import RULE_SET_VERSION, STALENESS_LIMIT_S, THRESHOLDS, ReadingSpec

# Ordered severity bands, ascending (Req 2.3: "the highest band triggered by
# any individual reading"). Sourced structurally from the rule engine's own
# design, not hardcoded anywhere else in this module.
SEVERITY_ORDER: list[str] = ["NORMAL", "WATCH", "WARNING", "EVACUATE"]

# Distinct unavailability causes (Req 2.5): "absent, is non-numeric, falls
# outside the configured valid range for its unit, or carries a source
# timestamp older than the configured staleness limit". Exposed as named
# constants so the node result's `reason` field is a closed, testable set of
# strings rather than ad hoc text.
REASON_ABSENT = "absent"
REASON_NON_NUMERIC = "non_numeric"
REASON_OUT_OF_RANGE = "out_of_range"
REASON_STALE = "stale"


@dataclass(frozen=True)
class _AvailabilityResult:
    """Internal helper: whether one reading is usable, and why not if not."""

    available: bool
    reason: str | None = None


def _is_numeric(value: Any) -> bool:
    """True when `value` is a real number (int/float), excluding bool.

    `bool` is deliberately excluded even though `isinstance(True, int)` is
    `True` in Python — a boolean sensor value is not a meaningful hazard
    reading and should be treated as non-numeric (Req 2.5).
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _in_range(value: float, valid_range: tuple[float, float]) -> bool:
    """True when `value` falls within the inclusive `valid_range`."""
    low, high = valid_range
    return low <= value <= high


def _check_availability(
    reading: dict[str, Any] | None,
    spec: ReadingSpec,
    now: datetime,
    staleness_limit_s: int,
) -> _AvailabilityResult:
    """Determine availability and, if unavailable, the specific cause (Req 2.5).

    Checks are ordered so the reported cause is the first genuine problem
    found: absent, then non-numeric, then out-of-range, then stale. A
    reading could in principle fail more than one check (e.g. an
    out-of-range value that is also stale); reporting the first one keeps
    the reason set closed and deterministic without implying priority
    among real-world causes.

    Args:
        reading: The raw reading dict (``{"value":..., "unit":..., "ts":...}``)
            for one reading type, or `None` if absent entirely.
        spec: The `ReadingSpec` (valid range, etc.) for this reading type.
        now: The evaluation instant, injected by the caller (never a live
            clock read inside this function) for determinism.
        staleness_limit_s: Maximum age, in seconds, before a reading is
            considered stale.

    Returns:
        An `_AvailabilityResult` describing whether the reading is usable.
    """
    if reading is None:
        return _AvailabilityResult(available=False, reason=REASON_ABSENT)

    value = reading.get("value")
    if not _is_numeric(value):
        return _AvailabilityResult(available=False, reason=REASON_NON_NUMERIC)

    if not _in_range(value, spec.valid_range):
        return _AvailabilityResult(available=False, reason=REASON_OUT_OF_RANGE)

    timestamp = reading.get("ts")
    if timestamp is None or (now - timestamp).total_seconds() > staleness_limit_s:
        return _AvailabilityResult(available=False, reason=REASON_STALE)

    return _AvailabilityResult(available=True, reason=None)


def _band_for_reading(value: float, spec: ReadingSpec) -> tuple[str, list[str]]:
    """Return the highest severity band `value` meets or exceeds, plus the
    triggered rule identifiers accumulated while scanning.

    Scans `spec.rules_ascending` in ascending threshold order and keeps
    overwriting `band` with each rule whose threshold is met or exceeded —
    since the list is ascending, the last (and therefore highest) rule met
    wins, and every rule met along the way is recorded as triggered. This is
    the "structurally monotonic" evaluation design.md §3.3 describes
    (Property 4, task 4.3): a larger `value` meets every rule a smaller
    value met, plus possibly more, so the returned band never decreases as
    `value` increases, for a fixed `spec`.

    Args:
        value: The numeric reading value, already confirmed available.
        spec: The `ReadingSpec` for this reading type.

    Returns:
        A tuple of (severity band, list of triggered rule ids for this
        reading type only).
    """
    band = "NORMAL"
    triggered: list[str] = []
    for rule in spec.rules_ascending:
        if value >= rule.threshold:
            band = rule.band
            triggered.append(rule.rule_id)
    return band, triggered


def evaluate_rule_engine(
    readings: dict[str, dict[str, Any]],
    now: datetime,
    last_known_band: str,
) -> dict[str, Any]:
    """Pure, deterministic core of the `Rule_Engine` evaluation (Req 2.1-2.3, 2.5, 2.9).

    Factored out of `RuleEngineNode.invoke_async` so the deterministic logic
    is directly unit-testable (and property-testable by tasks 4.2-4.4)
    without constructing a `MultiAgentResult`/`AgentResult` wrapper for every
    test case. `RuleEngineNode.invoke_async` calls this function and wraps
    its return value into the Strands result types.

    A pure function of its three arguments plus the module-level
    `policy.rule_engine_rules.THRESHOLDS` / `STALENESS_LIMIT_S` — no hidden
    state, no randomness, no wall-clock reliance (Property 1 determinism,
    task 4.2), since `now` is always supplied by the caller.

    Args:
        readings: Mapping of reading type (e.g. ``"river_level"``) to a dict
            with at least ``"value"`` and ``"ts"`` (a `datetime`); a reading
            type absent from this mapping is treated as an absent reading.
        now: The evaluation instant used for staleness checks. Never read
            from a real clock inside this function; the caller decides what
            "now" means, so identical inputs always produce identical
            output (Property 1).
        last_known_band: The most recently computed severity band from
            available readings, used verbatim as the returned band on total
            outage (Req 2.9: "return the most recent severity band computed
            from available readings" and "apply no decrease" to an open
            incident's band). Must be one of `SEVERITY_ORDER`.

    Returns:
        A dict payload with the fields the node result payload must carry
        per the task instructions: ``severity_band``, ``triggered_rules``,
        ``availability`` (per-reading-type dict of
        ``{"available": bool, "reason": str | None}``), ``model_invocations``
        (always ``0``), ``rule_set_version``, ``unverified`` (bool), and
        ``evaluation_ts`` (``now.isoformat()``).
    """
    triggered: list[str] = []
    band_per_reading: dict[str, str] = {}
    availability: dict[str, dict[str, Any]] = {}

    for reading_type, spec in THRESHOLDS.items():
        reading = readings.get(reading_type)
        result = _check_availability(reading, spec, now, STALENESS_LIMIT_S)
        if not result.available:
            availability[reading_type] = {"available": False, "reason": result.reason}
            continue

        availability[reading_type] = {"available": True, "reason": None}
        band, reading_triggered = _band_for_reading(reading["value"], spec)
        band_per_reading[reading_type] = band
        triggered.extend(reading_triggered)

    available_bands = [
        band_per_reading[reading_type]
        for reading_type in band_per_reading
        if availability[reading_type]["available"]
    ]

    if not available_bands:
        # Req 2.9/2.10: total outage. Never silently report NORMAL -- carry
        # forward the last known band and mark the result unverified, so a
        # false-negative "all clear" is never produced from a total sensor
        # outage.
        severity = last_known_band
        unverified = True
        # No reading contributed a band this evaluation, so no rule ids are
        # freshly triggered; `triggered` remains whatever partial-outage
        # readings (if any) contributed above, which is none in this branch
        # since `available_bands` is empty only when every reading type is
        # unavailable.
    else:
        severity = max(available_bands, key=SEVERITY_ORDER.index)  # highest band wins, Req 2.3
        unverified = False

    return {
        "severity_band": severity,
        "triggered_rules": triggered,
        "availability": availability,
        "model_invocations": 0,
        "rule_set_version": RULE_SET_VERSION,
        "unverified": unverified,
        "evaluation_ts": now.isoformat(),
    }


class RuleEngineNode(MultiAgentBase):
    """Deterministic threshold evaluator. Performs 0 model invocations (Req 2.1).

    A custom Strands multi-agent graph node (subclasses `MultiAgentBase`)
    suitable for `GraphBuilder.add_node(RuleEngineNode(), "rule_engine")`
    (design.md §3.2, task 15.2, not yet implemented). Never calls a
    language model; `evaluate_rule_engine()` above does all real work as a
    pure function, and this class is only the thin adapter that satisfies
    the `MultiAgentBase` contract so the node composes correctly inside a
    Strands `Graph`.

    Input contract (`invocation_state`, per design.md §3.3's own worked
    example): the graph driver must supply, under the `invocation_state`
    dict passed to `invoke_async`/`__call__`:
        - ``"readings"``: ``dict[str, dict[str, Any]]`` keyed by reading
          type, each with at least ``"value"`` and ``"ts"`` (a `datetime`).
        - ``"now"``: a `datetime`, the evaluation instant (never read from a
          real clock inside this node -- always injected, for determinism).
        - ``"last_known_band"``: the most recently computed severity band
          string, used verbatim on total outage (Req 2.9).
    """

    name = "rule_engine"
    id = "rule_engine"

    def __init__(self) -> None:
        """Initialise base `MultiAgentBase` bookkeeping state.

        `MultiAgentBase.__init__` sets `_invocation_start_time = None`,
        required by the base class's own execution-time helpers; this node
        holds no additional state of its own (it is a pure function
        wrapper), so no further initialisation is needed.
        """
        super().__init__()

    async def invoke_async(
        self,
        task: MultiAgentInput,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> MultiAgentResult:
        """Evaluate the current hazard readings and return a `MultiAgentResult`.

        Args:
            task: Unused. This node performs no model call and needs no
                prompt text; accepted only to satisfy the `MultiAgentBase`
                abstract signature the graph driver invokes uniformly across
                every node type.
            invocation_state: Must contain ``"readings"``, ``"now"``, and
                ``"last_known_band"`` per the class docstring's input
                contract. Defaults to an empty dict if `None` is passed,
                matching `MultiAgentBase.__call__`'s own default-handling
                convention, though a real graph invocation must supply the
                three required keys or a `KeyError` is raised naming the
                missing key.
            **kwargs: Unused; accepted only for signature compatibility with
                `MultiAgentBase.invoke_async`.

        Returns:
            A `MultiAgentResult` with `status=Status.COMPLETED` and a single
            entry in `results` keyed by `self.name`, whose `NodeResult.result`
            is an `AgentResult` carrying the evaluation payload in
            `AgentResult.state` (severity band, triggered rules,
            availability, `model_invocations=0`, rule set version,
            `unverified` flag, evaluation timestamp).
        """
        if invocation_state is None:
            invocation_state = {}

        readings: dict[str, dict[str, Any]] = invocation_state["readings"]
        now: datetime = invocation_state["now"]
        last_known_band: str = invocation_state["last_known_band"]

        payload = evaluate_rule_engine(readings, now, last_known_band)

        message = {
            "role": "assistant",
            "content": [
                {
                    "text": (
                        f"Rule_Engine: severity={payload['severity_band']} "
                        f"rule_set={payload['rule_set_version']} "
                        f"triggered={payload['triggered_rules']} "
                        f"unverified={payload['unverified']}"
                    )
                }
            ],
        }

        agent_result = AgentResult(
            stop_reason="end_turn",
            message=message,
            metrics=EventLoopMetrics(),
            state=payload,
        )

        return MultiAgentResult(
            status=Status.COMPLETED,
            results={self.name: NodeResult(result=agent_result, status=Status.COMPLETED)},
        )
