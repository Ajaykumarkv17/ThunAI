"""``agents/hitl.py``: the single deterministic ``HumanInTheLoop`` classifier,
plus a helper that builds a ``HumanInTheLoop`` intervention with its
``allowed_tools`` generated from an agent's own read-tool registration list
(Req 16.11, 10.4, 10.5, 10.6, 10.7; design.md Design Question 3).

Verification note (mandatory per tasks.md 16.2, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (``pip show
    strands-agents``, matching every other already-verified module in this
    codebase). Re-confirmed directly against the installed package
    (``inspect.signature``/``inspect.getsource``), consistent with
    Spike 16.1's own finding recorded in ``docs/spikes/16-hitl-findings.md``
    (no deviation found there either):

    1. ``strands.vended_interventions.hitl.HumanInTheLoop.__init__`` accepts
       keyword-only ``allowed_tools: list[str] | None``,
       ``classifier: bool | LLMClassifierConfig | HumanInTheLoopClassifier |
       None``, and (among others) ``ask: AskCallback | Literal["stdio"] |
       None`` — confirmed live via
       ``inspect.signature(HumanInTheLoop.__init__)``.
    2. ``strands.vended_interventions.hitl.classifier.ClassifierResult`` is
       ``@dataclass class ClassifierResult: requires_human_in_the_loop: bool;
       reason: str | None = None`` (confirmed via
       ``inspect.getsource(ClassifierResult)``).
    3. ``strands.vended_interventions.hitl.classifier.HumanInTheLoopClassifier``
       is a ``@runtime_checkable`` ``Protocol`` requiring exactly
       ``def __call__(self, event: BeforeToolCallEvent, **kwargs: Any) ->
       ClassifierResult | Awaitable[ClassifierResult]`` (confirmed via
       ``inspect.getsource``) — a single ``event`` argument, not the
       design.md pseudocode's ``(tool_use: dict, agent_output)`` two-argument
       sketch.
    4. ``strands.hooks.events.BeforeToolCallEvent`` carries ``tool_use``
       (a ``ToolUse`` ``TypedDict`` with an ``input`` key holding the tool
       call's own arguments dict) and ``invocation_state`` — confirmed via
       ``strandsagents.com``'s live API reference (``strands.hooks.events``,
       section "BeforeToolCallEvent") and matching
       ``harness/hooks.py``'s own already-recorded verification note for
       the identical dataclass. There is **no** ``agent_output`` attribute
       anywhere on this event — the classifier only ever sees the pending
       tool call's own arguments, exactly the same constraint
       ``harness/hooks.py::ApprovalGateHook`` (task 8.2) and
       ``agents/dispatch_agent.py`` (task 13.4) already document and work
       around.

    **Deviation from design.md's Design Question 3 inline pseudocode,
    recorded here** (docs win, per the "verify first" instruction):
    design.md's sketch is
    ``def deterministic_classifier(tool_use: dict, agent_output) ->
    ClassifierResult: verdict = decide(agent_output.structured_output)``.
    The installed, confirmed ``HumanInTheLoopClassifier`` protocol takes
    exactly one positional argument, ``event: BeforeToolCallEvent`` (plus
    ``**kwargs`` for future extensibility) — there is no separate
    ``agent_output``/``structured_output`` object passed to a classifier at
    all; the *result* of the agent's structured-output call is never
    threaded into ``BeforeToolCallEvent`` (confirmed by
    ``dataclasses.fields(BeforeToolCallEvent)`` carrying no such field, and
    by the live API reference above). :func:`deterministic_classifier`
    below therefore reads the pending write tool's own call arguments —
    ``event.tool_use["input"]`` — for the ``category``/``confidence``/
    ``action``/``is_irreversible_action`` fields ``policy.escalation_policy
    .decide()`` needs, exactly the calling convention
    ``harness/hooks.py::ApprovalGateHook.check()`` already establishes for
    this same structural limitation (that hook reads
    ``tool_use["input"].get("category")``); the caller (whichever agent's
    write tool this classifier gates) is expected to pass its typed
    decision's own ``category``/``confidence``/``action``/
    ``is_irreversible_action`` fields as part of the tool call's own
    arguments — the same expectation ``ApprovalGateHook`` already places on
    every write-tool call.

Escalate-by-default for an unclassifiable call (Req 16.11's "identical
inputs produce identical enforcement decisions" plus this task's own
explicit instruction, mirroring ``harness/hooks.py::ApprovalGateHook``'s own
documented precedent for the identical structural limitation): when a
pending write-tool call's arguments carry no usable ``category``/
``confidence`` pair, :func:`deterministic_classifier` never guesses --  it
returns ``requires_human_in_the_loop=True`` unconditionally. This is a pure
function of the call's own arguments (no run-scoped mutable state, no
randomness, no model call anywhere in this module), so two invocations with
byte-identical ``event.tool_use["input"]`` always produce the identical
``ClassifierResult`` -- Req 16.11 holds by construction, not by a separate
determinism check.

Current wiring status, recorded explicitly rather than silently omitted
(per this task's own instruction to only wire this helper into an existing
specialist agent "if those agent files already exist and follow the
established Agent-construction pattern"): as of this task, **no** already-
implemented specialist agent (``agents/dispatch_agent.py``,
``agents/alert_agent.py``, ``agents/safety_qa_agent.py``) actually gives its
underlying Strands ``Agent`` a write tool to gate. Each of those three
modules' own docstrings independently record the same, deliberate design
decision: the model is given read-only tools only, and every write
(``assign_responder``, ``deliver_alert``, ``create_escalation``) is
performed by a deterministic Python driver function calling the plain tool
function directly, never through the model's own tool-call loop --
``agents/dispatch_agent.py``'s own docstring names this exact module
(``agents/hitl.py``) as the future extension point, "once a future revision
extends ``assign_responder``'s schema to carry the decision's own fields."
Retrofitting a write tool onto any of those three agents' model-facing tool
list today would silently invalidate each module's own already-passing,
already-documented test suite and design decision, which is out of this
task's scope (task 16.2 is "implement ``agents/hitl.py``", not "redesign
``agents/dispatch_agent.py``/``agents/alert_agent.py``/
``agents/safety_qa_agent.py``"). This module therefore exports a complete,
directly usable, unit-tested ``deterministic_classifier`` and
:func:`build_human_in_the_loop` helper today, ready for the first specialist
agent revision that does give its model a write tool to call
``build_human_in_the_loop(read_tools=[...])`` and pass the result as
``interventions=[...]`` -- exactly the call shape design.md's Design
Question 3 pseudocode shows -- without this task inventing a write-tool-
holding agent that does not otherwise exist.

``allowed_tools`` generation (Req 16.11's "no system-prompt-based
enforcement"; design.md's own note: "``allowed_tools`` entries are
generated from the same tool-registration list the agent is built with,
never hand-typed"): :func:`build_allowed_tools` reads each tool's own
``tool_name`` (a ``@tool``-decorated function's ``DecoratedFunctionTool
.tool_name`` attribute, confirmed live: ``find_candidate_responders
.tool_name == "find_candidate_responders"``) rather than a hand-typed
string literal, mirroring ``agents/coordinator_orchestrator.py::
_tool_name()``'s own already-established best-effort name lookup (checked
here independently since that helper is private to that module).
"""

from __future__ import annotations

from typing import Any, Iterable

from strands.hooks.events import BeforeToolCallEvent
from strands.vended_interventions.hitl import HumanInTheLoop
from strands.vended_interventions.hitl.classifier import ClassifierResult, HumanInTheLoopClassifier

from policy import escalation_policy

__all__ = [
    "tool_name",
    "build_allowed_tools",
    "deterministic_classifier",
    "build_human_in_the_loop",
]


# ---------------------------------------------------------------------------
# tool_name / build_allowed_tools: generate allowed_tools from a
# tool-registration list, never hand-typed (design.md Design Question 3).
# ---------------------------------------------------------------------------


def tool_name(candidate: Any) -> str:
    """Best-effort name of a tool object, for building ``allowed_tools``.

    Mirrors ``agents/coordinator_orchestrator.py::_tool_name()``'s own
    lookup order (checked independently here since that helper is private
    to that module): a ``@tool``-decorated function's own ``tool_name``
    attribute first (a ``strands.tools.decorator.DecoratedFunctionTool``
    instance, confirmed live), then a plain callable's ``__name__``, then
    one level of ``__wrapped__`` unwrapping, then the object's own type
    name as a last resort (never raises).

    Args:
        candidate: A ``@tool``-decorated function, a plain callable, or any
            other object naming a tool.

    Returns:
        The tool's name as a string.
    """
    for attr in ("tool_name", "__name__"):
        value = getattr(candidate, attr, None)
        if isinstance(value, str) and value:
            return value
    inner = getattr(candidate, "__wrapped__", None)
    if inner is not None:
        return tool_name(inner)
    return type(candidate).__name__


def build_allowed_tools(read_tools: Iterable[Any]) -> list[str]:
    """Generate a ``HumanInTheLoop(allowed_tools=...)`` list from an agent's
    own read-tool registration list, never hand-typed (design.md Design
    Question 3: "``allowed_tools`` entries are generated from the same
    tool-registration list the agent is built with, never hand-typed").

    ThunAI never uses ``allowed_tools``' negation syntax (``!tool_name``,
    design.md's own note: "ThunAI does not use negation, to keep the
    allow-list the single source of truth for 'does not need human
    approval'") -- every name returned here is a plain, positive tool name.

    Args:
        read_tools: The exact iterable of read-only tool objects (typically
            ``@tool``-decorated functions) the calling agent module already
            passes to ``Agent(tools=[...])`` for its non-write tools -- the
            same list, not a hand-copied duplicate, so a future rename of
            one of those tools cannot silently create an unlisted,
            unguarded ``allowed_tools`` entry (design.md's own "gateway
            tool name prefixing" note makes the identical point for
            gateway-backed tool names).

    Returns:
        A list of tool name strings, in the same order as ``read_tools``.
    """
    return [tool_name(tool) for tool in read_tools]


# ---------------------------------------------------------------------------
# deterministic_classifier: the ONLY function anywhere that decides
# ask-human vs. execute for a HumanInTheLoop-gated write-tool call
# (Req 16.11, 10.4-10.7; design.md Design Question 3). Never an LLM call --
# delegates solely to policy.escalation_policy.decide().
# ---------------------------------------------------------------------------


def deterministic_classifier(event: BeforeToolCallEvent, **kwargs: Any) -> ClassifierResult:
    """The single deterministic ``HumanInTheLoop`` classifier (design.md
    Design Question 3).

    Never calls a language model and never re-implements any autonomy
    threshold of its own -- it reads the pending write-tool call's own
    arguments (``event.tool_use["input"]``, see this module's docstring's
    "verify first" section for why this is the only data a classifier can
    see under the installed ``HumanInTheLoopClassifier`` protocol) and
    delegates the actual ask-human-vs-execute decision entirely to
    ``policy.escalation_policy.decide()`` -- the single autonomy authority
    (Req 10.8: "nothing else in the source tree may hold a threshold value
    used in an autonomy decision").

    The calling agent's write tool is expected to accept, among its own
    arguments, the same ``category``/``confidence``/``action``/
    ``is_irreversible_action`` fields the tool call's producing typed
    decision (one of ``schemas.decisions``' five models) already carries --
    exactly the expectation ``harness/hooks.py::ApprovalGateHook.check()``
    already places on every write-tool call for its own, structurally
    identical ``category`` lookup.

    Args:
        event: The pending tool call under evaluation, supplied by the
            Strands SDK's ``HumanInTheLoop`` intervention handler.
        **kwargs: Accepted for forward-compatibility with the
            ``HumanInTheLoopClassifier`` protocol's ``**kwargs: Any``;
            unused.

    Returns:
        A ``ClassifierResult`` with ``requires_human_in_the_loop=True`` iff
        ``policy.escalation_policy.decide()`` returns ``"escalate": True``
        for the call's own ``category``/``confidence``/``action``/
        ``is_irreversible_action`` fields, or iff those fields are absent
        or unusable (escalate-by-default for an unclassifiable call -- see
        this module's docstring's "Escalate-by-default" section). ``reason``
        is always set to a short, human-readable explanation (``decide()``'s
        own ``"reason"`` on the normal path, or a fixed unclassifiable-call
        message otherwise). Identical ``event.tool_use["input"]`` always
        produces an identical result (Req 16.11) -- this function holds no
        mutable state and makes no model call.
    """
    tool_input = event.tool_use.get("input")
    fields: dict[str, Any] = tool_input if isinstance(tool_input, dict) else {}

    category = fields.get("category")
    confidence = fields.get("confidence")

    if category is None or confidence is None:
        return ClassifierResult(
            requires_human_in_the_loop=True,
            reason=(
                "the pending tool call carried no usable category/confidence field for "
                "Escalation_Policy to evaluate; an unclassifiable write-tool call is "
                "escalate-by-default (Req 16.11)"
            ),
        )

    verdict = escalation_policy.decide(
        category=category,
        confidence=confidence,
        action=fields.get("action"),
        is_irreversible_action=bool(fields.get("is_irreversible_action", False)),
        anomaly_ratio=fields.get("anomaly_ratio"),
        audience_count=fields.get("audience_count"),
        severity_band=fields.get("severity_band", "WATCH"),
    )
    return ClassifierResult(requires_human_in_the_loop=verdict["escalate"], reason=verdict["reason"])


# ---------------------------------------------------------------------------
# build_human_in_the_loop: the one call site every future write-tool-holding
# specialist agent should use (design.md Design Question 3's worked
# example), so allowed_tools is always generated, never hand-typed.
# ---------------------------------------------------------------------------


def build_human_in_the_loop(
    *,
    read_tools: Iterable[Any] = (),
    classifier: HumanInTheLoopClassifier = deterministic_classifier,
) -> HumanInTheLoop:
    """Build the one ``HumanInTheLoop`` intervention a write-tool-holding
    specialist agent should register (design.md Design Question 3).

    ``allowed_tools`` is generated from ``read_tools`` via
    :func:`build_allowed_tools` -- never hand-typed -- and no ``ask=``
    keyword is ever passed (Spike 16.1's own recorded decision, per
    ``docs/spikes/16-hitl-findings.md``: passing ``ask`` would switch
    ``HumanInTheLoop`` to inline blocking mode instead of the SDK's
    interrupt/resume default, breaking the "nothing blocks inside
    ``/invocations``" rule design.md's Design Question 2 requires).

    Args:
        read_tools: The agent's own read-only tool registration list
            (see :func:`build_allowed_tools`). Every tool named here
            bypasses :func:`deterministic_classifier` entirely (design.md:
            "reads bypass the classifier entirely").
        classifier: The classifier callable to wire in. Defaults to
            :func:`deterministic_classifier` -- the single autonomy
            authority for every write-tool call this intervention gates.
            Overriding this is a test seam only; production callers should
            never pass anything other than the default.

    Returns:
        A ``HumanInTheLoop`` instance ready to pass as
        ``Agent(interventions=[build_human_in_the_loop(read_tools=...)])``.
    """
    return HumanInTheLoop(allowed_tools=build_allowed_tools(read_tools), classifier=classifier)
