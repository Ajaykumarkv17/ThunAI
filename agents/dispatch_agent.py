"""``Dispatch_Agent``: matches an emergency request to a responder under
capacity/equipment constraints and produces a typed dispatch decision
(Req 6.1, 6.2, 6.3, 6.5, 6.6, 6.8, 6.10, 6.11; design.md §3.1 (Dispatch)).

Verification note (mandatory per tasks.md 13.4/13, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (``pip show
    strands-agents``, matching ``pyproject.toml``'s pin and every other
    already-verified module in this codebase — ``agents/config.py``,
    ``agents/rule_engine.py``, ``harness/hooks.py``). The following API
    surfaces were re-confirmed against the *installed* package directly
    (``inspect.signature`` on the live classes), not assumed from
    design.md's pseudocode alone, per the "verify first" instruction that
    docs/installed-code win over design.md when the two disagree:

    1. ``strands.Agent.__init__`` accepts ``agent_id: str | None``,
       ``name: str | None``, ``system_prompt``, ``tools``,
       ``structured_output_model: type[BaseModel] | None``,
       ``hooks: list[HookProvider | HookCallback] | None``,
       ``interventions: list[InterventionHandler] | None``, and
       ``session_manager: SessionManager | None`` — every constructor
       keyword design.md's §3.1 pseudocode and this task's own instructions
       assume. No deviation here: this module constructs the agent with
       ``agent_id=AGENT_ID``, ``name=AGENT_ID`` (Req 4.12: a unique
       ``agent_id`` per role), a role-specific ``system_prompt`` (Req 4.12:
       distinct from every other role's prompt), ``structured_output_model=
       DispatchDecision``, ``hooks=list(HARNESS_HOOKS)``, and no
       ``session_manager`` at all (Req 4.13: no ``Incident_Graph`` member
       agent carries one).
    2. ``strands.Agent.__call__`` accepts ``structured_output_model=`` at
       call time too (confirmed via ``inspect.signature``), and returns an
       ``AgentResult`` whose ``.structured_output`` attribute holds the
       validated Pydantic instance on success — matching
       ``schemas/structured.py::safe_structured()``'s already-verified
       assumption (that module's own docstring records the same finding)
       exactly, so this module reuses ``safe_structured()`` unchanged
       rather than re-implementing its own try/except around
       ``StructuredOutputException``.
    3. ``strands.vended_interventions.hitl.hitl.HumanInTheLoop`` and
       ``strands.vended_interventions.hitl.classifier.ClassifierResult``
       were read directly (docs MCP server ``fetch_doc`` calls against
       ``strands.vended_interventions.hitl.hitl``/``...hitl.classifier``):
       ``HumanInTheLoop.__init__``'s ``classifier`` parameter accepts a
       plain callable of shape
       ``(event: BeforeToolCallEvent, **kwargs) -> ClassifierResult``, and
       ``ClassifierResult`` is a two-field dataclass
       (``requires_human_in_the_loop: bool``, ``reason: str``) — matching
       design.md's Design Question 3 assumption exactly.

    **Deviation from design.md §3.1's inline pseudocode, recorded here**
    (docs/installed-code win, per the "verify first" instruction): the
    pseudocode's ``dispatch_agent = Agent(tools=[find_candidate_responders,
    assign_responder, ...], interventions=[HumanInTheLoop(allowed_tools=
    ["find_candidate_responders"], classifier=deterministic_classifier)])``
    gives the model both tools and lets the model's own tool-call loop
    invoke ``assign_responder`` (gated by ``HumanInTheLoop``'s classifier).
    That classifier is described (Design Question 3) as calling
    ``policy.escalation_policy.decide()`` against
    ``agent_output.structured_output`` — but no such ``agent_output``
    parameter exists on the actually-installed
    ``HumanInTheLoopClassifier.__call__(event: BeforeToolCallEvent, ...)``
    signature (confirmed above); a classifier only ever sees the *pending
    tool call's own arguments* (``event.tool_use["input"]``), exactly the
    same constraint ``harness/hooks.py::ApprovalGateHook`` (already
    implemented, task 8.2) already documents and works around by reading
    ``tool_use["input"].get("category")``. ``tools/dispatch_tools.py
    ::assign_responder`` (task 12.4, already implemented and property-
    tested — Property 12, task 6.3) has a fixed, already-committed
    signature of exactly ``(responder_id: str, request_id: str)``; it
    carries no ``category``/``confidence``/``action``/
    ``is_irreversible_action`` argument for any classifier to read, and
    changing that signature is out of this task's scope (it would silently
    invalidate the already-passing Property 12 test suite's exact call
    shape). Separately, ``agents/hitl.py`` — the module design.md's
    Design Question 3 and task 16.2 assign the *shared*
    ``deterministic_classifier`` to — does not exist anywhere in the tree
    yet (confirmed by a repository-wide search).

    Given both facts, this module does **not** give the model
    ``assign_responder`` as a tool at all, and does **not** wire a
    ``HumanInTheLoop`` intervention on this agent (there is no write tool
    on it for one to gate, and ``find_candidate_responders`` is a read tool
    that design.md's own pseudocode already says "bypasses the classifier
    entirely"). Instead, the model reasons over
    ``find_candidate_responders``'s results and produces a typed
    ``DispatchDecision`` (via ``structured_output_model=``); a plain
    deterministic Python function, ``run_dispatch_decision()`` below, is
    the one place that calls ``policy.escalation_policy.decide()`` directly
    on that decision's own fields and then calls
    ``tools.dispatch_tools.assign_responder()`` as an ordinary function
    call — never through the model's own tool-selection loop. This is the
    exact same "one authority, deterministic code, never the model" outcome
    Design Question 3 requires (Req 10.8: "nothing else in the source tree
    may hold a threshold value used in an autonomy decision" — satisfied
    identically either way, since ``escalation_policy.decide()`` is still
    the sole decision-maker), reached through a call shape that is fully
    unit-testable without a live Bedrock invocation (this task's own
    instructions ask for exactly that: "successful assignment, no-capacity
    escalation, mobility/medical-flagged escalation ..., responder-no-
    longer-available re-plan, idempotent replay"). When ``agents/hitl.py``
    (task 16.2) lands and, if a future revision extends
    ``assign_responder``'s schema to carry the decision's own fields, this
    module's ``build_dispatch_agent()`` can be revisited to give the model
    ``assign_responder`` directly and wire the shared classifier — noted
    here as the explicit extension point, not silently worked around.

Mobility/medical always-ask override (Req 6.5) is enforced **deterministically
in this module**, not left to the model's own ``category``/``action`` fields:
``run_dispatch_decision`` forces ``category="dispatch_non_ambulatory"`` and
``is_irreversible_action=True`` whenever the caller-supplied
``mobility_assistance``/``medical_need`` indicators are ``True``, regardless
of what the produced ``DispatchDecision`` itself claims — matching this
task's explicit instruction ("mobility/medical-flagged requests escalate ...
route through policy/escalation_policy.py::decide()") and Req 6.5's own
wording ("WHERE a request carries a mobility-assistance indicator or a
medical-need indicator, THE Dispatch_Agent SHALL escalate ... and SHALL
withhold the assignment"), which is unconditional on the request's
indicators alone, not conditional on the model's own judgement.

Shelter-capacity escalation (Req 6.11) scoping note: this task's assigned
requirement list is 6.1, 6.2, 6.3, 6.5, 6.6, 6.8, 6.10, 6.11 — **Req 6.7**
("decrement the available capacity of the selected shelter... capacity
floor") is explicitly **not** in that list (it is covered separately, since
the actual atomic decrement primitive,
``memory.state_store.decrement_shelter_capacity``, already exists and is
already tested at the state-store layer, task 6.1). This module therefore
implements Req 6.11's escalation branch (checking whether any shelter holds
capacity at or above the reported occupant count, and escalating a
``shelter_capacity`` decision with no capacity decrement when it does not)
via ``evaluate_shelter_capacity()``, but deliberately performs **no** actual
capacity decrement anywhere in this module — that remains the responsibility
of whichever future task wires Req 6.7's decrement call (most naturally
alongside this module's own execute path, once that task is scoped), so as
not to duplicate or pre-empt a not-yet-assigned design decision about exactly
when in the flow the decrement should occur relative to responder
assignment.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Final, Iterable

from strands import Agent
from strands.models import BedrockModel

from agents.config import get_model_id_for_role
from harness.hooks import HARNESS_HOOKS
from memory import state_store
from policy import escalation_policy
from schemas.decisions import DispatchDecision
from schemas.structured import ValidationFailureFallback, safe_structured
from tools import dispatch_tools
from tools.escalation_tools import create_escalation

__all__ = [
    "AGENT_ID",
    "SYSTEM_PROMPT",
    "build_dispatch_agent",
    "rank_candidates",
    "evaluate_shelter_capacity",
    "run_dispatch_decision",
    "dispatch_request",
]

AGENT_ID: Final[str] = "dispatch_agent"
"""Unique agent identifier for this role (Req 4.12)."""

SYSTEM_PROMPT: Final[str] = (
    "You are Dispatch_Agent for the Kollidam Ward-7 Neighbourhood Flood Committee. "
    "Your only job is to match one dispatch-eligible emergency request to the best "
    "available responder, or to say plainly that none is available.\n\n"
    "Call find_candidate_responders with the request's location to see every "
    "AVAILABLE responder within the search radius, each with its distance in "
    "kilometres, equipment list, and current active_assignment_count. Rank "
    "candidates by: (1) whether their equipment covers what this request needs, "
    "(2) lowest current active_assignment_count (spread load evenly), (3) shortest "
    "distance. Never propose a responder whose availability_status is not "
    "AVAILABLE.\n\n"
    "Produce a DispatchDecision naming your top choice as selected_responder_id and "
    "up to 3 ranked alternatives. Set category to 'no_capacity' with action "
    "'ask_human' when no candidate is returned or none meets the equipment "
    "requirement; otherwise set category to 'routine_dispatch_ambulatory' with "
    "action 'execute' when you are confident, or action 'ask_human' when you are "
    "not. You never assign a responder yourself and you never decide whether a "
    "mobility- or medical-flagged request may proceed without a human — that is "
    "handled deterministically outside your control. Write selection_rationale as "
    "one plain sentence a non-technical coordinator can read."
)
"""Distinct from every other agent role's system prompt (Req 4.12)."""

_ESCALATION_OPTIONS: Final[dict[str, list[dict[str, str]]]] = {
    "dispatch_non_ambulatory": [
        {"option_id": "approve", "label": "Approve dispatch"},
        {"option_id": "hold", "label": "Hold the assignment"},
    ],
    "no_capacity": [
        {"option_id": "expand_search", "label": "Expand the search radius"},
        {"option_id": "hold", "label": "Hold and keep searching"},
    ],
    "shelter_capacity": [
        {"option_id": "reassign_shelter", "label": "Reassign to another shelter"},
        {"option_id": "hold", "label": "Hold"},
    ],
    "validation_failure": [
        {"option_id": "retry", "label": "Retry the dispatch decision"},
        {"option_id": "hold", "label": "Hold"},
    ],
}
"""Between 2 and 5 options per escalation category (Req 11.1), keyed by the
same category vocabulary `policy.escalation_policy.CATEGORY_TABLE` and
`schemas.decisions.DispatchDecision.category` already use."""


def _region_name() -> str | None:
    return os.environ.get("AWS_REGION")


def build_dispatch_agent() -> Agent:
    """Construct a fresh `Dispatch_Agent` (Req 4.12, 4.13).

    Built fresh per invocation (per this codebase's established convention
    for `Incident_Graph` member agents — cheap, no model call at construction
    time) with no `session_manager` (Req 4.13: only `Coordinator_Orchestrator`
    carries one) and the shared `HARNESS_HOOKS` (Req 16) registered so every
    tool call this agent makes (`find_candidate_responders` only — see the
    module docstring's deviation note for why `assign_responder` is
    deliberately not a tool on this agent) is capped and audited exactly like
    every other agent in this codebase.

    Returns:
        A `strands.Agent` configured with `agent_id`/`name` set to
        `AGENT_ID`, `system_prompt=SYSTEM_PROMPT`,
        `tools=[dispatch_tools.find_candidate_responders]`,
        `structured_output_model=DispatchDecision`, and
        `hooks=list(HARNESS_HOOKS)`.
    """
    model = BedrockModel(
        model_id=get_model_id_for_role("dispatch_agent"),
        region_name=_region_name(),
        temperature=0.2,
        max_tokens=4096,
    )
    return Agent(
        model=model,
        name=AGENT_ID,
        agent_id=AGENT_ID,
        system_prompt=SYSTEM_PROMPT,
        tools=[dispatch_tools.find_candidate_responders],
        structured_output_model=DispatchDecision,
        hooks=list(HARNESS_HOOKS),
    )


# ---------------------------------------------------------------------------
# rank_candidates (Req 6.1, 6.2): deterministic, directly unit-testable
# ranking helper. The model still performs its own reasoning against
# find_candidate_responders's raw result inside build_dispatch_agent()'s real
# invocation; this pure function exists so the "distance/availability/
# equipment/current load" ranking claim is independently verifiable without
# a live model call, mirroring agents/rule_engine.py's own precedent of
# factoring deterministic logic out of the Strands-facing wrapper.
# ---------------------------------------------------------------------------


def rank_candidates(
    candidates: list[dict[str, Any]],
    required_equipment: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministically rank candidate responders (Req 6.1, 6.2).

    `find_candidate_responders` (`tools/dispatch_tools.py`) already restricts
    its result to responders whose `availability_status` is `AVAILABLE` and
    within the configured search radius, sorted by ascending distance; this
    function re-ranks that same list by a stricter ordering that also
    accounts for equipment fit and current load, so ties on distance alone
    do not silently favour an over-loaded or under-equipped responder.

    Args:
        candidates: The `candidates` list from `find_candidate_responders`'s
            result — each a dict with at least `distance_km`,
            `active_assignment_count`, and `equipment` (a list of strings).
        required_equipment: Equipment the request needs (e.g. `["boat"]`).
            A candidate whose `equipment` does not cover every required item
            is ranked below every candidate that does; when omitted or
            empty, equipment plays no role in the ordering.

    Returns:
        A new list, ranked (equipment fit descending, then
        `active_assignment_count` ascending, then `distance_km` ascending).
        Never mutates `candidates`.
    """
    required = set(required_equipment or [])

    def _sort_key(candidate: dict[str, Any]) -> tuple[int, int, float]:
        equipped = set(candidate.get("equipment", []))
        equipment_penalty = 0 if not required or required.issubset(equipped) else 1
        return (
            equipment_penalty,
            candidate.get("active_assignment_count", 0),
            candidate.get("distance_km", 0.0),
        )

    return sorted(candidates, key=_sort_key)


# ---------------------------------------------------------------------------
# evaluate_shelter_capacity (Req 6.11)
# ---------------------------------------------------------------------------


def evaluate_shelter_capacity(shelter_id: str, occupant_count: int) -> dict[str, Any]:
    """Check whether `shelter_id` currently holds capacity at or above
    `occupant_count` (Req 6.11).

    Reads `State_Store` directly (via `memory.state_store.get_shelter`)
    rather than `tools/alert_tools.py::get_shelter_capacity` (which is
    scoped to `Alert_Agent`'s own multi-shelter "nearest with capacity"
    lookup, Req 7.1/7.2, and silently omits a not-found id) — this function
    needs a definite sufficient/insufficient verdict for exactly one named
    shelter, never a silent omission.

    Args:
        shelter_id: The shelter identifier the request names, e.g.
            `"shelter-2"`.
        occupant_count: The reported occupant count of the request.

    Returns:
        `{"ok": True, "sufficient": bool, "available_capacity": int,
        "reason": None}` when the shelter is found and
        `available_capacity >= occupant_count`; otherwise
        `{"ok": True, "sufficient": False, "available_capacity": int,
        "reason": "insufficient_capacity"}`; or, when `shelter_id` does not
        resolve to a known shelter,
        `{"ok": True, "sufficient": False, "available_capacity": 0,
        "reason": "shelter_not_found"}`. Never raises.
    """
    shelter = state_store.get_shelter(shelter_id)
    if shelter is None:
        return {"ok": True, "sufficient": False, "available_capacity": 0, "reason": "shelter_not_found"}

    sufficient = shelter.available_capacity >= occupant_count
    return {
        "ok": True,
        "sufficient": sufficient,
        "available_capacity": shelter.available_capacity,
        "reason": None if sufficient else "insufficient_capacity",
    }


# ---------------------------------------------------------------------------
# run_dispatch_decision (Req 6.1-6.3, 6.5, 6.6, 6.8, 6.10, 6.11): the
# deterministic driver. See the module docstring's deviation note for why
# this function, not a model-driven tool call, is what actually calls
# policy.escalation_policy.decide() and tools.dispatch_tools.assign_responder.
# ---------------------------------------------------------------------------


def _escalate(
    *,
    request_id: str,
    run_id: str,
    category: str,
    verdict: dict[str, Any],
    decision_summary: str,
    reason: str,
    stakes: str,
    incident_id: str | None = None,
) -> dict[str, Any]:
    """Shared escalation-recording helper for every branch below (Req 6.8:
    "record every assignment, rejection, escalation... in Audit_Ledger" —
    `create_escalation` itself persists the record; the Audit_Ledger entry
    for this escalation is written by `harness.hooks.AuditHook` the moment
    the enclosing agent run's tool-call loop next fires, exactly like every
    other write in this codebase; this module has no direct Audit_Ledger
    dependency of its own, matching `tools/escalation_tools.py`'s own
    documented scope)."""
    escalation = create_escalation(
        idempotency_key=f"escalate:{request_id}:{category}",
        run_id=run_id,
        escalation_type=category,
        decision_summary=decision_summary[:200],
        reason=reason,
        stakes=stakes,
        options=_ESCALATION_OPTIONS.get(category, _ESCALATION_OPTIONS["no_capacity"]),
        default_action=verdict.get("default_action") or "hold_assignment",
        response_deadline_minutes=verdict["response_deadline_minutes"],
        incident_id=incident_id,
    )
    return {
        "ok": True,
        "outcome": "escalated",
        "escalation_type": category,
        "escalation": escalation,
        "assigned_responder_id": None,
    }


def run_dispatch_decision(
    *,
    request_id: str,
    run_id: str,
    produce_decision: Callable[[], DispatchDecision | ValidationFailureFallback],
    request_category: str = "RESCUE",
    occupant_count: int = 1,
    location_reference: str = "",
    mobility_assistance: bool | str = False,
    medical_need: bool | str = False,
    shelter_id: str | None = None,
    incident_id: str | None = None,
) -> dict[str, Any]:
    """Drive one dispatch decision for one request, end to end (Req 6.1-6.3,
    6.5, 6.6, 6.8, 6.10, 6.11).

    Never calls a language model itself — `produce_decision` supplies the
    typed `DispatchDecision` (or a `ValidationFailureFallback` on a
    structured-output validation failure), letting this function be
    unit-tested without a live Bedrock invocation. `dispatch_request()`
    below is the production entry point that wires a real
    `Dispatch_Agent` invocation into `produce_decision`.

    Flow:
        1. Req 6.11: if `shelter_id` is given and no shelter holds capacity
           at or above `occupant_count`, escalate a `"shelter_capacity"`
           decision immediately and return — `produce_decision` is never
           called on this path (no dispatch decision is meaningful before
           the shelter question is resolved by a human), and no capacity
           decrement is ever performed by this function (see the module
           docstring's scoping note on Req 6.7).
        2. Otherwise call `produce_decision()` to obtain the `DispatchDecision`.
        3. Req 6.5: if `mobility_assistance` or `medical_need` is `True`,
           deterministically force `category="dispatch_non_ambulatory"` and
           treat the decision as an `Irreversible_Action`, regardless of
           what the decision itself claims.
        4. Call `policy.escalation_policy.decide()` on the (possibly
           overridden) category/confidence/action/irreversibility. If it
           says escalate, escalate and return with no assignment made.
        5. Otherwise attempt `tools.dispatch_tools.assign_responder()` on
           the selected responder, then each ranked alternative in order,
           on a `"responder_no_longer_available"` result (Req 6.10's
           re-plan). If every candidate is exhausted, escalate a
           `"no_capacity"` decision (Req 6.6).

    Args:
        request_id: The emergency request identifier being dispatched.
        run_id: The current `Incident_Graph` run identifier, threaded into
            every `create_escalation` call.
        produce_decision: A zero-argument callable returning the typed
            `DispatchDecision` for this request (or a
            `ValidationFailureFallback` on validation failure). See
            `dispatch_request()` for the real, model-backed implementation.
        request_category: The request's category (Req 5.2's set); used only
            for escalation stakes text.
        occupant_count: The request's reported occupant count; used for the
            shelter-capacity check (step 1) and escalation stakes text.
        location_reference: The request's resolved location, for escalation
            stakes text only.
        mobility_assistance: The request's mobility-assistance indicator
            (Req 6.5).
        medical_need: The request's medical-need indicator (Req 6.5).
        shelter_id: The shelter this request would be placed in, if this
            request involves a shelter placement/capacity check (Req 6.11).
            `None` when the request does not involve shelter placement.
        incident_id: The incident this request belongs to, if any, threaded
            into any resulting `EscalationRecord`.

    Returns:
        On successful assignment:
        `{"ok": True, "outcome": "assigned", "assigned_responder_id": str,
        "attempted": [{"responder_id": str, "result": dict}, ...]}`.

        On escalation (shelter-capacity, mobility/medical, no-capacity, or a
        structured-output validation failure):
        `{"ok": True, "outcome": "escalated", "escalation_type": str,
        "escalation": dict, "assigned_responder_id": None}` — `escalation`
        is `tools.escalation_tools.create_escalation`'s own return value.
    """
    # Step 1 (Req 6.11): shelter-capacity gate, independent of responder
    # dispatch. Checked before produce_decision() is ever called.
    if shelter_id is not None:
        capacity = evaluate_shelter_capacity(shelter_id, occupant_count)
        if not capacity["sufficient"]:
            verdict = escalation_policy.decide(category="shelter_capacity", confidence=1.0)
            shortfall = max(occupant_count - capacity["available_capacity"], 0)
            return _escalate(
                request_id=request_id,
                run_id=run_id,
                category="shelter_capacity",
                verdict=verdict,
                decision_summary=(
                    f"Shelter {shelter_id} cannot hold {occupant_count} people "
                    f"(available: {capacity['available_capacity']})."
                ),
                reason=(
                    "no shelter holds available capacity at or above the reported "
                    "occupant count (Req 6.11)"
                ),
                stakes=f"{occupant_count} occupant(s) need placement; shortfall of {shortfall}",
                incident_id=incident_id,
            )

    # Step 2.
    decision = produce_decision()

    # Structured-output validation failure -> always ask-human (Req 10.11).
    if isinstance(decision, ValidationFailureFallback):
        verdict = escalation_policy.decide(
            category=decision.category,
            confidence=decision.confidence,
            action=decision.action,
            is_irreversible_action=decision.is_irreversible_action,
        )
        return _escalate(
            request_id=request_id,
            run_id=run_id,
            category="validation_failure",
            verdict=verdict,
            decision_summary=f"Dispatch decision for request {request_id} could not be validated.",
            reason=decision.error_detail[:200],
            stakes=f"request {request_id} has no valid dispatch decision to act on",
            incident_id=incident_id,
        )

    # Step 3 (Req 6.5): deterministic override, independent of the model's
    # own category/action.
    always_ask_mobility_medical = mobility_assistance is True or medical_need is True
    category = "dispatch_non_ambulatory" if always_ask_mobility_medical else decision.category
    is_irreversible = decision.is_irreversible_action or always_ask_mobility_medical

    # Step 4.
    verdict = escalation_policy.decide(
        category=category,
        confidence=decision.confidence,
        action=decision.action,
        is_irreversible_action=is_irreversible,
    )

    if verdict["escalate"]:
        return _escalate(
            request_id=request_id,
            run_id=run_id,
            category=category,
            verdict=verdict,
            decision_summary=decision.selection_rationale or f"Dispatch decision for request {request_id}.",
            reason=verdict["reason"],
            stakes=(
                f"request {request_id} ({request_category}) for {occupant_count} "
                f"occupant(s) at {location_reference}"
            ),
            incident_id=incident_id,
        )

    # Step 5 (Req 6.3, 6.8, 6.10): execute, re-planning through alternatives.
    candidate_chain = [
        responder_id
        for responder_id in [decision.selected_responder_id, *decision.alternative_responder_ids]
        if responder_id
    ]

    attempted: list[dict[str, Any]] = []
    for responder_id in candidate_chain:
        result = dispatch_tools.assign_responder(responder_id, request_id)
        attempted.append({"responder_id": responder_id, "result": result})
        if result["ok"]:
            return {
                "ok": True,
                "outcome": "assigned",
                "assigned_responder_id": responder_id,
                "attempted": attempted,
            }
        if result.get("reason") != "responder_no_longer_available":
            # An unexpected, non-recoverable failure (e.g. a missing
            # idempotency key) -- stop re-planning rather than retrying a
            # call that will never succeed for the same reason.
            break

    # Every ranked candidate was unavailable (or none was ever named) --
    # Req 6.6/6.10's no-capacity escalation.
    verdict = escalation_policy.decide(category="no_capacity", confidence=1.0)
    return {
        **_escalate(
            request_id=request_id,
            run_id=run_id,
            category="no_capacity",
            verdict=verdict,
            decision_summary=(
                f"No responder could be assigned to request {request_id}; every ranked "
                "candidate was unavailable."
            ),
            reason=(
                "every ranked candidate responder was no longer AVAILABLE at assignment "
                "time, or no candidate was named (Req 6.6, 6.10)"
            ),
            stakes=(
                f"request {request_id} ({request_category}) for {occupant_count} "
                f"occupant(s) at {location_reference} has no responder assigned"
            ),
            incident_id=incident_id,
        ),
        "attempted": attempted,
    }


# ---------------------------------------------------------------------------
# dispatch_request: the production entry point wiring a real Dispatch_Agent
# invocation into run_dispatch_decision's produce_decision callable.
# ---------------------------------------------------------------------------


def _dispatch_prompt(
    *,
    request_id: str,
    request_category: str,
    occupant_count: int,
    location_reference: str,
    latitude: float,
    longitude: float,
) -> str:
    return (
        f"Emergency request {request_id} (category: {request_category}) at "
        f"{location_reference} (lat={latitude}, lon={longitude}) for {occupant_count} "
        "occupant(s). Find and rank candidate responders, then produce your "
        "DispatchDecision."
    )


def dispatch_request(
    agent: Agent,
    *,
    request_id: str,
    run_id: str,
    latitude: float,
    longitude: float,
    request_category: str = "RESCUE",
    occupant_count: int = 1,
    location_reference: str = "",
    mobility_assistance: bool | str = False,
    medical_need: bool | str = False,
    shelter_id: str | None = None,
    incident_id: str | None = None,
) -> dict[str, Any]:
    """Production entry point: dispatch one request using a real
    `Dispatch_Agent` invocation (Req 6.1-6.3).

    Thin wrapper over `run_dispatch_decision`, supplying a `produce_decision`
    that actually invokes `agent` with `structured_output_model=
    DispatchDecision`, guarded by `schemas.structured.safe_structured` (Req
    10.11). See `run_dispatch_decision`'s own docstring for the full
    escalation/re-plan/idempotence behaviour, which this wrapper does not
    duplicate.

    Args:
        agent: A `Dispatch_Agent` instance, typically from
            `build_dispatch_agent()`.
        request_id: The emergency request identifier being dispatched.
        run_id: The current `Incident_Graph` run identifier.
        latitude: Latitude of the request's location, in decimal degrees.
        longitude: Longitude of the request's location, in decimal degrees.
        request_category: See `run_dispatch_decision`.
        occupant_count: See `run_dispatch_decision`.
        location_reference: See `run_dispatch_decision`.
        mobility_assistance: See `run_dispatch_decision`.
        medical_need: See `run_dispatch_decision`.
        shelter_id: See `run_dispatch_decision`.
        incident_id: See `run_dispatch_decision`.

    Returns:
        Exactly `run_dispatch_decision`'s own return value.
    """
    prompt = _dispatch_prompt(
        request_id=request_id,
        request_category=request_category,
        occupant_count=occupant_count,
        location_reference=location_reference,
        latitude=latitude,
        longitude=longitude,
    )

    def _produce_decision() -> DispatchDecision | ValidationFailureFallback:
        return safe_structured(
            lambda: agent(prompt, structured_output_model=DispatchDecision).structured_output,
            DispatchDecision,
        )

    return run_dispatch_decision(
        request_id=request_id,
        run_id=run_id,
        produce_decision=_produce_decision,
        request_category=request_category,
        occupant_count=occupant_count,
        location_reference=location_reference,
        mobility_assistance=mobility_assistance,
        medical_need=medical_need,
        shelter_id=shelter_id,
        incident_id=incident_id,
    )
