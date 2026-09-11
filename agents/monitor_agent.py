"""``Monitor_Agent``: interprets hazard readings and produces a typed
``HazardAssessment``, then applies Req 3.3-3.10's deterministic incident
create/update/downgrade/escalate rules over the result (Req 3, design.md §3.1).

Verification note (mandatory per tasks.md 13.2, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (`pip show
    strands-agents`, matching `agents/config.py`'s and `agents/rule_engine.py`'s
    own already-recorded verification notes for this same pin). Independently
    re-confirmed via ``inspect.signature`` against the installed package
    (rather than trusting design.md's pseudocode or an earlier module's
    verification note alone) that ``strands.Agent.__init__`` still exposes,
    as keyword-only parameters, every constructor argument this module
    relies on: ``system_prompt``, ``tools``, ``agent_id``,
    ``structured_output_model``, ``hooks``, ``interventions``,
    ``session_manager`` (all still present, unchanged in shape from
    `agents/config.py`'s own description of the `BedrockModel` constructor
    it neighbours). This module passes no ``session_manager`` argument at
    all (its default is ``None``), which is the correct way to satisfy Req
    4.13/"no attached session manager" for an `Incident_Graph` member agent
    — there is no sentinel or explicit "disable session manager" value to
    pass; simply never supplying one is the contract. ``interventions`` is
    also left unset here: per this task's own instructions, the
    ask-human-vs-execute decision is NOT made by this agent at all (see
    "Design decision" below), so there is nothing for a
    `HumanInTheLoop`/interventions handler to gate on this agent's own tool
    calls — the deterministic gate this module implements
    (`policy.escalation_policy.decide()` + `tools.escalation_tools
    .create_escalation`) sits entirely in this module's own Python code,
    downstream of the agent's structured-output call, not inside the
    agent's own tool-call loop. `agents/incident_graph.py` (task 15, not
    yet implemented) is the module design.md expects to wire a
    `HumanInTheLoop` classifier/interventions handler onto write-tool calls
    generally (design.md's Design Question 3) — this module does not
    pre-empt that wiring since it deliberately gives this agent zero write
    tools in the first place (see below).

    Also re-confirmed ``strands.models.BedrockModel.__init__``'s signature
    (`inspect.signature`): accepts ``region_name`` and arbitrary
    ``**model_config`` keys including ``model_id``, ``temperature``, and
    ``max_tokens`` — matching `agents/config.py`'s own already-verified
    description of this constructor exactly. No deviation from design.md
    or from `agents/config.py`'s prior verification note was found; this
    module's own model construction (`_build_bedrock_model`) is a thin,
    directly-parallel use of that same constructor shape, reading the
    model id via `agents.config.get_model_id_for_role("monitor_agent")`
    (never hardcoding a model id, per that module's own stated contract)
    and the region via `AWS_REGION` (matching `.env.example`'s documented
    "agents/config.py, all boto3 clients" comment for that variable, reused
    here for the one other module in the tree that constructs a
    `BedrockModel` directly).

Design decision — what the model decides vs. what deterministic code
decides (per this task's explicit instruction: "do NOT let it write
incident state itself via free model judgement... deterministic
post-processing / tools handle writes", and "this agent must NOT itself
decide ask_human vs execute"):

    The `Agent` this module builds (`build_monitor_agent()`) is given
    **read-only** tools only — `tools.sensor_tools.get_river_level` /
    `get_rainfall_rate` / `get_dam_release` and
    `tools.incident_tools.get_prior_sweep_readings`. It is never given
    `tools.incident_tools.create_or_update_incident` or
    `tools.escalation_tools.create_escalation` as a tool at all, mirroring
    design.md §3.1's own `ask_monitor` read-only specialist pattern
    exactly (`tools=[get_current_readings, get_open_incidents]  # no
    create_incident/update_incident`). This is a structural guarantee, not
    a prompt instruction: the model has no way to write incident state or
    raise an escalation from inside its own tool-call loop, because no such
    tool exists in its tool list.

    `run_monitor_sweep()` (the function that actually orchestrates one
    hazard sweep for one river reach, called by the eventual
    `Incident_Graph` node wrapper, task 15) is 100% deterministic Python
    aside from exactly one call into the agent (`_assess()`, via
    `schemas.structured.safe_structured()`) to obtain the model's
    genuinely-judgement-based fields: `confidence`, `action`, `rationale`,
    and `is_irreversible_action`. Every other field of the returned
    `HazardAssessment` — `severity_band`, `rate_of_change`,
    `anomaly_indicator`, `unavailable_readings` — is **computed by plain
    arithmetic in this module and force-overwritten onto the model's
    output** via `HazardAssessment.model_copy(update={...})` immediately
    after the call, regardless of what the model itself echoed for those
    fields. This mirrors `agents/rule_engine.py`'s own precedent of never
    trusting a downstream component to recompute a value this codebase can
    compute exactly and deterministically itself (there, the severity band
    is fixed threshold arithmetic; here, rate-of-change and anomaly-ratio
    are fixed arithmetic over tool-returned values already in hand before
    the model is ever called). The prompt built by `_build_prompt()`
    explicitly tells the model these four fields are already computed and
    instructs it to echo them back unchanged — the override is defence in
    depth against the model disregarding that instruction, not the primary
    mechanism.

    The **ask-human-vs-execute** decision is likewise never read from the
    model's own `action` field in isolation — `run_monitor_sweep()` always
    passes the assessment's `confidence`/`action`/`is_irreversible_action`
    fields, plus the deterministically-computed anomaly ratio, into
    `policy.escalation_policy.decide()` (the single autonomy authority,
    per that module's own docstring) and acts on `decide()`'s own
    `"escalate"` boolean, never on `assessment.action` directly. This is
    the literal meaning of "this agent must NOT itself decide ask_human vs
    execute": the model's stated `action` opinion is one *input* among
    several to the one deterministic authority that actually decides,
    exactly as `policy/escalation_policy.py`'s own docstring describes
    every other typed decision model being consumed.

Incident create/update/downgrade/escalate logic (Req 3.3-3.10), literal
mapping from acceptance criteria to code branches in `run_monitor_sweep()`:

    - **Req 3.6 (total sensor outage)**: checked first, before any model
      call. If every one of the three configured reading types comes back
      `available: False` from this module's own direct calls to
      `get_river_level`/`get_rainfall_rate`/`get_dam_release` (Req 3.1's
      retry/timeout behaviour already lives inside those tool functions —
      see `tools/sensor_tools.py`'s own docstring — so this module performs
      no additional retry of its own), the agent is never invoked at all:
      a `data_outage` record is written to Audit_Ledger, a `data_outage`
      escalation is raised via `policy.escalation_policy.decide()` +
      `tools.escalation_tools.create_escalation` (that category is
      `always_ask=True` in `CATEGORY_TABLE`, so `decide()` always
      escalates it), and the function returns immediately — no incident
      lookup, no incident write, satisfying "leave every existing incident
      and every stored severity band unchanged" exactly by never reaching
      the incident-touching code path at all.
    - **Req 3.8 (partial outage)**: when at least one but not all reading
      types are available, the model is still invoked (using only the
      available readings' tool results in the prompt); the deterministic
      override sets `HazardAssessment.unavailable_readings` to the exact
      list of unavailable types, and every audit entry written downstream
      includes that list explicitly.
    - **Req 3.9 (no prior sweep / no baseline for a reading type)**:
      `_compute_rate_of_change()`/`_compute_anomaly_indicator()` each
      return `None` for a reading type lacking, respectively, a prior-sweep
      value or a baseline average — never a fabricated number — and every
      audit entry records `prior_sweep_available` explicitly.
    - **Req 3.5 / Req 3.3 (NORMAL or WATCH+, no open incident)**: with no
      open incident for the reach, `policy.escalation_policy.decide()` is
      consulted once, uniformly, for both bands (`category=
      "hazard_monitoring"`, `anomaly_ratio=` the maximum available anomaly
      indicator). When `decide()` does not escalate, a NORMAL band with a
      sub-threshold anomaly and adequate confidence/action reduces exactly
      to Req 3.5's literal no-op ("complete the run without creating an
      incident, without escalating a decision, and without sending a
      notification" — this module never calls
      `tools.alert_tools.deliver_alert` or any notification tool at all,
      so "without sending a notification" holds structurally); a WATCH+
      band with adequate confidence/action/anomaly reduces exactly to Req
      3.3's literal creation gate (confidence at/above the floor, `action`
      not `"ask_human"`) via `tools.incident_tools.create_or_update_incident`.
      When `decide()` does escalate for either band — including the case a
      NORMAL-band anomaly ratio exceeds the threshold, a gap Req 3.5 itself
      does not explicitly name but that `decide()`'s own anomaly-ratio
      check already covers — a `hazard_monitoring` escalation is raised
      instead of a silent no-op or a silently-withheld incident creation
      (documented explicitly here as a reasoned completion of the literal
      requirements, not an invented feature: every path either satisfies a
      named acceptance criterion exactly, or escalates rather than guessing).
    - **Req 3.4 / Req 3.10 (WATCH+ or NORMAL, open incident already
      exists)**: update happens **unconditionally** — `decide()` is not
      consulted at all on this path — exactly matching both requirements'
      literal "SHALL update"/"SHALL keep the incident open" wording, which
      carries no confidence/action gate the way Req 3.3's creation gate
      does. `tools.incident_tools.higher_severity_band()` (already
      idempotent-update-tested by task 12.8's Property 11) guarantees the
      stored band is never lowered by the NORMAL-band downgrade path (Req
      3.10's "without closing the incident").
    - **Req 3.7 (every created/updated incident recorded in Audit_Ledger)**:
      `_audit_inputs()` builds the one shared payload shape (current
      readings, prior readings, rate of change, anomaly indicator,
      severity band, triggered rule ids, unavailable reading types, prior-
      sweep availability) attached to every `harness.audit
      .append_audit_entry()` call this module makes, on every branch —
      not only the two "created"/"updated" branches Req 3.7 names by name,
      since the escalation and no-op branches need the identical facts
      recorded for the same auditability reason.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable

from strands import Agent
from strands.models import BedrockModel

from agents.config import get_model_id_for_role
from harness.audit import append_audit_entry
from harness.hooks import HARNESS_HOOKS
from memory import state_store
from policy import escalation_policy
from schemas.decisions import HazardAssessment
from schemas.structured import ValidationFailureFallback, safe_structured
from tools import incident_tools
from tools.escalation_tools import create_escalation
from tools.sensor_tools import get_dam_release, get_rainfall_rate, get_river_level

__all__ = [
    "MONITOR_SYSTEM_PROMPT",
    "build_monitor_agent",
    "run_monitor_sweep",
]

# ---------------------------------------------------------------------------
# System prompt (Req 3.2; design.md §3.1's "interpret sensor time series"
# distinct-prompt-responsibility framing). States the sole job and the
# structural constraint (no write tool exists) explicitly, so the prompt
# itself never claims a capability the tool list contradicts.
# ---------------------------------------------------------------------------

MONITOR_SYSTEM_PROMPT: str = (
    "You are Monitor_Agent for ThunAI, a neighbourhood flood-response assistant. "
    "Your sole job is to interpret hazard sensor readings and time series for one "
    "monitored river reach, in the context of the most recently completed sweep's "
    "readings and the reach's 30-day baseline, and decide whether the current "
    "reading pattern represents a meaningful change worth a human's attention. "
    "You hold no tool that creates, updates, or deletes an incident, and no tool "
    "that sends a notification or raises an escalation -- those actions are taken "
    "by deterministic code outside your control, never by you. "
    "The severity band, rate of change per reading type, anomaly ratio per reading "
    "type, and the list of unavailable reading types are already computed and given "
    "to you in the prompt; echo those four values back exactly as given -- do not "
    "recompute or alter them. Your job is only to state: your confidence (0.0-1.0) "
    "that this pattern is a meaningful change, whether you propose to 'execute' or "
    "to 'ask_human', and one rationale sentence of at most 200 characters written "
    "for a non-technical reader. Never invent a reading value, never assume a "
    "reading you were not given, and never claim certainty a genuinely ambiguous "
    "reading pattern does not support."
)

# ---------------------------------------------------------------------------
# Model / Agent construction (Req 19.9, 20.6; design.md's Cost Model)
# ---------------------------------------------------------------------------

_DEFAULT_TEMPERATURE: float = 0.2
"""Low temperature for a decision-adjacent role (hackathon guidelines §1.5:
"Low temperature for decisions. 0.0-0.3 for anything that extracts,
classifies, routes, or decides")."""

_DEFAULT_MAX_TOKENS: int = 4096


def _build_bedrock_model() -> BedrockModel:
    """Construct the `BedrockModel` Monitor_Agent uses, per `agents.config`'s
    role routing (`monitor_agent` -> `HIGH_CAPABILITY_MODEL_ID`, since this
    role performs trade-off reasoning about whether a reading pattern is
    meaningful, per design.md's Cost Model table).
    """
    return BedrockModel(
        model_id=get_model_id_for_role("monitor_agent"),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        temperature=_DEFAULT_TEMPERATURE,
        max_tokens=_DEFAULT_MAX_TOKENS,
    )


def build_monitor_agent(*, model: Any | None = None) -> Agent:
    """Build a fresh `Monitor_Agent` instance (Req 4.12, 4.13).

    A distinct `agent_id` ("monitor_agent") and a distinct system prompt
    from every other agent role in this codebase (Req 4.12). No
    `session_manager` is passed (Req 4.13: `Incident_Graph` member agents
    are constructed with none). Registers the shared `harness.hooks
    .HARNESS_HOOKS` list, the same instances every other agent construction
    site in this codebase shares, so per-run caps/audit/approval-gate
    accounting accumulate correctly across every agent role sharing one
    `run_id` (see `harness/hooks.py::HARNESS_HOOKS`'s own docstring).

    Args:
        model: An optional pre-built model to use instead of constructing a
            fresh `BedrockModel` via `_build_bedrock_model()` — primarily
            for tests that want to avoid a live Bedrock call while still
            exercising `Agent` construction; `run_monitor_sweep()`'s own
            `agent` parameter is the more direct test seam for avoiding a
            model call entirely (it accepts a pre-built `Agent` or a
            plain callable double).

    Returns:
        A new `Agent`, built fresh (never reused/cached across sweeps) —
        matching the hackathon guidelines' "build a fresh Agent per
        request" rule.
    """
    return Agent(
        name="monitor_agent",
        agent_id="monitor_agent",
        system_prompt=MONITOR_SYSTEM_PROMPT,
        tools=[get_river_level, get_rainfall_rate, get_dam_release, incident_tools.get_prior_sweep_readings],
        model=model if model is not None else _build_bedrock_model(),
        hooks=list(HARNESS_HOOKS),
    )


# ---------------------------------------------------------------------------
# Deterministic arithmetic (Req 3.2, 3.9): never asked of the model, never
# guessed -- either a precise ratio/rate, or None when the required prior
# value genuinely does not exist.
# ---------------------------------------------------------------------------


def _compute_rate_of_change(
    reading_type: str,
    current_reading: dict[str, Any],
    prior_readings: dict[str, Any],
) -> float | None:
    """Rate of change for one reading type, in that type's unit per hour
    (Req 3.2). `None` when the current reading is itself unavailable (Req
    3.8), when no prior-sweep value exists for this reading type (Req 3.9),
    or when the two timestamps cannot be parsed/ordered.
    """
    if not current_reading.get("available"):
        return None
    prior = prior_readings.get(reading_type)
    if not prior:
        return None
    try:
        current_ts = datetime.fromisoformat(current_reading["source_timestamp"])
        prior_ts = datetime.fromisoformat(prior["ts"])
        hours = (current_ts - prior_ts).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return (current_reading["value"] - prior["value"]) / hours


def _compute_anomaly_indicator(current_reading: dict[str, Any]) -> float | None:
    """Ratio of the current reading to its 30-day baseline average (Req
    3.2). `None` when the current reading is unavailable (Req 3.8) or no
    30-day baseline average exists for this reading type (Req 3.9).
    """
    if not current_reading.get("available"):
        return None
    baseline = current_reading.get("baseline")
    if not baseline or not baseline.get("available"):
        return None
    average = baseline.get("average")
    if not average:
        return None
    return current_reading["value"] / average


def _audit_inputs(
    *,
    current_readings: dict[str, Any],
    prior_readings: dict[str, Any],
    rate_of_change: dict[str, float | None],
    anomaly_indicator: dict[str, float | None],
    severity_band: str,
    triggered_rule_ids: list[str],
    unavailable_reading_types: list[str],
    prior_sweep_available: bool,
) -> dict[str, Any]:
    """The one shared audit-entry payload shape used on every branch of
    `run_monitor_sweep()` (Req 3.7's named fields, extended per this
    module's docstring to every branch, not only created/updated)."""
    return {
        "current_readings": current_readings,
        "prior_readings": prior_readings,
        "rate_of_change": rate_of_change,
        "anomaly_indicator": anomaly_indicator,
        "severity_band": severity_band,
        "triggered_rule_ids": list(triggered_rule_ids),
        "unavailable_reading_types": list(unavailable_reading_types),
        "prior_sweep_available": prior_sweep_available,
    }


_READING_TYPES: tuple[str, ...] = ("river_level", "rainfall_rate", "dam_release")

_READING_TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "river_level": get_river_level,
    "rainfall_rate": get_rainfall_rate,
    "dam_release": get_dam_release,
}


def _build_prompt(
    river_reach_id: str,
    as_of: str,
    severity_band: str,
    triggered_rule_ids: list[str],
    current_readings: dict[str, Any],
    prior_readings: dict[str, Any],
    prior_sweep_available: bool,
    rate_of_change: dict[str, float | None],
    anomaly_indicator: dict[str, float | None],
    unavailable_reading_types: list[str],
) -> str:
    """Build the one prompt Monitor_Agent's single model call receives.

    States every deterministically-computed fact plainly and instructs the
    model to echo the four already-computed fields back unchanged (see the
    module docstring's "Design decision" section for why this module then
    still force-overwrites them regardless).
    """
    return (
        f"Hazard sweep for river reach {river_reach_id!r} as of {as_of}.\n"
        f"Rule_Engine severity band (already computed, echo back exactly): {severity_band}\n"
        f"Triggered rule ids: {triggered_rule_ids}\n"
        f"Current readings: {current_readings}\n"
        f"Prior sweep readings ({'available' if prior_sweep_available else 'no prior sweep recorded'}): "
        f"{prior_readings}\n"
        f"Rate of change per reading type, already computed, echo back exactly: {rate_of_change}\n"
        f"Anomaly ratio (current / 30-day baseline) per reading type, already computed, echo back "
        f"exactly: {anomaly_indicator}\n"
        f"Unavailable reading types this sweep, already computed, echo back exactly: "
        f"{unavailable_reading_types}\n\n"
        "State your confidence (0.0-1.0) that this reading pattern is a meaningful change worth a "
        "human's attention, whether you propose 'execute' or 'ask_human', and a one-sentence "
        "rationale (at most 200 characters) for a non-technical reader."
    )


def _assess(
    agent: Any,
    prompt: str,
) -> HazardAssessment | ValidationFailureFallback:
    """The one model call this module makes, guarded by
    `schemas.structured.safe_structured()` so a structured-output
    validation failure never propagates as an exception (Req 10.11)."""
    return safe_structured(
        lambda: agent(prompt, structured_output_model=HazardAssessment).structured_output,
        HazardAssessment,
    )


def _decide_hazard_monitoring(
    assessment: HazardAssessment,
    severity_band: str,
    anomaly_indicator: dict[str, float | None],
) -> dict[str, Any]:
    """One uniform call into the single autonomy authority (Req 10.4-10.7),
    used identically by the NORMAL-no-incident (Req 3.5) and WATCH+-no-
    incident (Req 3.3) branches -- see the module docstring's mapping
    section for why both branches share this one call.
    """
    available_ratios = [v for v in anomaly_indicator.values() if v is not None]
    max_anomaly_ratio = max(available_ratios) if available_ratios else None
    return escalation_policy.decide(
        category="hazard_monitoring",
        confidence=assessment.confidence,
        action=assessment.action,
        is_irreversible_action=assessment.is_irreversible_action,
        anomaly_ratio=max_anomaly_ratio,
        severity_band=severity_band,
    )


def _raise_hazard_monitoring_escalation(
    *,
    river_reach_id: str,
    run_id: str,
    severity_band: str,
    assessment: HazardAssessment,
    policy_decision: dict[str, Any],
) -> dict[str, Any]:
    return create_escalation(
        idempotency_key=f"hazard_monitoring:{run_id}:{river_reach_id}:{severity_band}",
        run_id=run_id,
        escalation_type="hazard_monitoring",
        decision_summary=assessment.rationale,
        reason=policy_decision["reason"],
        stakes=(
            f"River reach {river_reach_id} is at severity band {severity_band}; "
            f"Monitor_Agent's confidence is {assessment.confidence:.2f}."
        ),
        options=[
            {"option_id": "approve", "label": "Approve autonomous action"},
            {"option_id": "hold", "label": "Hold and investigate manually"},
        ],
        default_action=policy_decision["default_action"] or "hold",
        response_deadline_minutes=policy_decision["response_deadline_minutes"],
        incident_id=None,
    )


def _raise_data_outage_escalation(
    *,
    river_reach_id: str,
    run_id: str,
    unavailable_reading_types: list[str],
    stalest_age_minutes: float | None,
    last_known_severity_band: str,
) -> dict[str, Any]:
    policy_decision = escalation_policy.decide(
        category="data_outage",
        confidence=0.0,
        action="ask_human",
        severity_band=last_known_severity_band,
    )
    age_text = (
        f"; the most recent stored reading is about {stalest_age_minutes:.0f} minute(s) old."
        if stalest_age_minutes is not None
        else "; no prior stored reading is available for this reach."
    )
    return create_escalation(
        idempotency_key=f"data_outage:{run_id}:{river_reach_id}",
        run_id=run_id,
        escalation_type="data_outage",
        decision_summary=(f"Sensor data outage for {river_reach_id}: no reading for "
                           f"{', '.join(unavailable_reading_types)}.")[:200],
        reason=policy_decision["reason"],
        stakes=f"Hazard monitoring for {river_reach_id} is blind until sensors recover{age_text}",
        options=[
            {"option_id": "acknowledge", "label": "Acknowledge and continue monitoring"},
            {"option_id": "manual_watch", "label": "Switch to manual river-level checks"},
        ],
        default_action=policy_decision["default_action"] or "hold",
        response_deadline_minutes=policy_decision["response_deadline_minutes"],
        incident_id=None,
    )


def run_monitor_sweep(
    *,
    river_reach_id: str,
    as_of: str,
    rule_engine_result: dict[str, Any],
    run_id: str,
    agent: Any | None = None,
) -> dict[str, Any]:
    """Run one Monitor_Agent hazard sweep for one river reach (Req 3.1-3.10).

    This is the function an `Incident_Graph` "monitor" node wrapper (task
    15, not yet implemented) calls once per sweep per river reach, after
    `Rule_Engine` has produced its result for the same readings. See the
    module docstring's "Incident create/update/downgrade/escalate logic"
    section for the exact requirement-by-requirement branch mapping.

    Args:
        river_reach_id: The monitored river reach identifier (e.g.
            `"reach-7"`), matching `tools.incident_tools
            .incident_id_for_reach`'s own parameter.
        as_of: ISO-8601 timestamp with timezone offset, the point in time
            this sweep reads sensor data as of (passed straight through to
            `tools.sensor_tools.get_river_level`/etc.).
        rule_engine_result: The dict `agents.rule_engine.evaluate_rule_engine`
            returns for the same reach/readings -- must carry at least
            `"severity_band"`, `"triggered_rules"`, and `"rule_set_version"`.
            Decoupled from a live `RuleEngineNode` call here (this module
            takes the result as a plain argument) exactly as
            `tools/incident_tools.py`'s own docstring defers the analogous
            `Incident_Graph`-wiring decision -- a future `Incident_Graph`
            node wrapper is expected to run `Rule_Engine` first and pass its
            result straight into this parameter.
        run_id: The current run identifier, threaded into every
            `harness.audit.append_audit_entry()` call and into every
            `tools.escalation_tools.create_escalation()` idempotency key.
        agent: An optional pre-built `Agent` (or any callable matching
            `Agent.__call__`'s `(prompt, *, structured_output_model=...)
            -> AgentResult`-shaped contract) to use instead of building a
            fresh one via `build_monitor_agent()`. Skipped entirely on the
            Req 3.6 total-outage path (no model call is made in that case
            regardless of this argument). The primary test seam for
            exercising this function's deterministic logic without a live
            Bedrock call.

    Returns:
        A dict describing the outcome, always carrying at least `"ok":
        True` and an `"outcome"` discriminator, one of:
        `"data_outage"`, `"escalated_hazard_monitoring"`, `"no_incident"`,
        `"incident_created"`, `"incident_updated"`,
        `"escalated_validation_failure"`.
    """
    incident_id = incident_tools.incident_id_for_reach(river_reach_id)

    prior = incident_tools.get_prior_sweep_readings(river_reach_id=river_reach_id)
    prior_available: bool = prior["available"]
    prior_readings: dict[str, Any] = prior["readings"] if prior_available else {}

    current_readings: dict[str, Any] = {
        reading_type: tool_fn(as_of=as_of, include_baseline=True, baseline_days=30)
        for reading_type, tool_fn in _READING_TOOLS.items()
    }
    unavailable_reading_types = [t for t in _READING_TYPES if not current_readings[t]["available"]]
    available_reading_types = [t for t in _READING_TYPES if current_readings[t]["available"]]

    severity_band: str = rule_engine_result["severity_band"]
    triggered_rule_ids: list[str] = list(rule_engine_result.get("triggered_rules", []))

    # -----------------------------------------------------------------
    # Req 3.6: total sensor outage -- no model call, no incident touch.
    # -----------------------------------------------------------------
    if not available_reading_types:
        stalest_age_minutes: float | None = None
        if prior_available and prior_readings:
            try:
                timestamps = [
                    datetime.fromisoformat(reading["ts"])
                    for reading in prior_readings.values()
                    if isinstance(reading, dict) and reading.get("ts")
                ]
                if timestamps:
                    now = datetime.fromisoformat(as_of)
                    stalest_age_minutes = (now - max(timestamps)).total_seconds() / 60.0
            except (KeyError, TypeError, ValueError):
                stalest_age_minutes = None

        append_audit_entry(
            run_id=run_id,
            tool_name="monitor_agent",
            outcome="data_outage",
            inputs=_audit_inputs(
                current_readings=current_readings,
                prior_readings=prior_readings,
                rate_of_change={t: None for t in _READING_TYPES},
                anomaly_indicator={t: None for t in _READING_TYPES},
                severity_band=severity_band,
                triggered_rule_ids=triggered_rule_ids,
                unavailable_reading_types=unavailable_reading_types,
                prior_sweep_available=prior_available,
            ),
            incident_id=incident_id,
        )
        escalation_outcome = _raise_data_outage_escalation(
            river_reach_id=river_reach_id,
            run_id=run_id,
            unavailable_reading_types=unavailable_reading_types,
            stalest_age_minutes=stalest_age_minutes,
            last_known_severity_band=severity_band,
        )
        return {
            "ok": True,
            "outcome": "data_outage",
            "escalated": True,
            "created": False,
            "updated": False,
            "incident_id": incident_id,
            "severity_band": severity_band,
            "escalation": escalation_outcome,
        }

    # -----------------------------------------------------------------
    # Deterministic arithmetic (Req 3.2, 3.9), computed once, used both
    # in the prompt and in the force-overwritten assessment.
    # -----------------------------------------------------------------
    rate_of_change = {
        t: _compute_rate_of_change(t, current_readings[t], prior_readings) for t in _READING_TYPES
    }
    anomaly_indicator = {t: _compute_anomaly_indicator(current_readings[t]) for t in _READING_TYPES}

    resolved_agent = agent if agent is not None else build_monitor_agent()
    prompt = _build_prompt(
        river_reach_id=river_reach_id,
        as_of=as_of,
        severity_band=severity_band,
        triggered_rule_ids=triggered_rule_ids,
        current_readings=current_readings,
        prior_readings=prior_readings,
        prior_sweep_available=prior_available,
        rate_of_change=rate_of_change,
        anomaly_indicator=anomaly_indicator,
        unavailable_reading_types=unavailable_reading_types,
    )
    raw_assessment = _assess(resolved_agent, prompt)

    audit_inputs = _audit_inputs(
        current_readings=current_readings,
        prior_readings=prior_readings,
        rate_of_change=rate_of_change,
        anomaly_indicator=anomaly_indicator,
        severity_band=severity_band,
        triggered_rule_ids=triggered_rule_ids,
        unavailable_reading_types=unavailable_reading_types,
        prior_sweep_available=prior_available,
    )

    # -----------------------------------------------------------------
    # Req 10.11: structured-output validation failure -- always ask-human,
    # never touches incident state.
    # -----------------------------------------------------------------
    if isinstance(raw_assessment, ValidationFailureFallback):
        append_audit_entry(
            run_id=run_id,
            tool_name="monitor_agent",
            outcome="validation_failure",
            inputs={**audit_inputs, "error_detail": raw_assessment.error_detail},
            incident_id=incident_id,
        )
        policy_decision = escalation_policy.decide(
            category="validation_failure", confidence=0.0, action="ask_human", severity_band=severity_band
        )
        escalation_outcome = create_escalation(
            idempotency_key=f"validation_failure:{run_id}:{river_reach_id}",
            run_id=run_id,
            escalation_type="validation_failure",
            decision_summary=f"Monitor_Agent's structured output failed validation for {river_reach_id}."[:200],
            reason=policy_decision["reason"],
            stakes=f"Hazard assessment for {river_reach_id} could not be produced this sweep.",
            options=[
                {"option_id": "acknowledge", "label": "Acknowledge and continue monitoring"},
                {"option_id": "manual_review", "label": "Review readings manually"},
            ],
            default_action=policy_decision["default_action"] or "hold",
            response_deadline_minutes=policy_decision["response_deadline_minutes"],
            incident_id=incident_id,
        )
        return {
            "ok": True,
            "outcome": "escalated_validation_failure",
            "escalated": True,
            "created": False,
            "updated": False,
            "incident_id": incident_id,
            "severity_band": severity_band,
            "escalation": escalation_outcome,
        }

    # Force-overwrite the deterministic fields (module docstring's "Design
    # decision" section) regardless of what the model echoed.
    assessment: HazardAssessment = raw_assessment.model_copy(
        update={
            "severity_band": severity_band,
            "rate_of_change": rate_of_change,
            "anomaly_indicator": anomaly_indicator,
            "unavailable_readings": list(unavailable_reading_types),
        }
    )

    existing = state_store.get_incident(incident_id)
    existing_open = existing is not None and existing.status == "OPEN"

    # -----------------------------------------------------------------
    # Req 3.4 / Req 3.10: an open incident already exists -- update
    # unconditionally, regardless of confidence/action/anomaly.
    # -----------------------------------------------------------------
    if existing_open:
        write_result = incident_tools.create_or_update_incident(
            river_reach_id=river_reach_id,
            severity_band=severity_band,
            readings=current_readings,
            triggered_rule_ids=triggered_rule_ids,
            rule_set_version=rule_engine_result.get("rule_set_version", existing.rule_set_version),
        )
        outcome = "incident_updated" if severity_band != "NORMAL" else "incident_updated_normal_downgrade"
        append_audit_entry(
            run_id=run_id,
            tool_name="monitor_agent",
            outcome=outcome,
            inputs={**audit_inputs, "applied_severity_band": write_result.get("severity_band")},
            incident_id=incident_id,
        )
        return {
            "ok": True,
            "outcome": outcome,
            "escalated": False,
            "created": False,
            "updated": True,
            "incident_id": incident_id,
            "severity_band": write_result.get("severity_band", severity_band),
            "assessment": assessment.model_dump(),
        }

    # -----------------------------------------------------------------
    # Req 3.5 / Req 3.3: no open incident -- consult the single autonomy
    # authority once, uniformly, for both NORMAL and WATCH+ bands.
    # -----------------------------------------------------------------
    policy_decision = _decide_hazard_monitoring(assessment, severity_band, anomaly_indicator)

    if policy_decision["escalate"]:
        append_audit_entry(
            run_id=run_id,
            tool_name="monitor_agent",
            outcome="escalated_hazard_monitoring",
            inputs={**audit_inputs, "determining_entry": policy_decision["determining_entry"]},
            incident_id=incident_id,
        )
        escalation_outcome = _raise_hazard_monitoring_escalation(
            river_reach_id=river_reach_id,
            run_id=run_id,
            severity_band=severity_band,
            assessment=assessment,
            policy_decision=policy_decision,
        )
        return {
            "ok": True,
            "outcome": "escalated_hazard_monitoring",
            "escalated": True,
            "created": False,
            "updated": False,
            "incident_id": incident_id,
            "severity_band": severity_band,
            "assessment": assessment.model_dump(),
            "escalation": escalation_outcome,
        }

    if severity_band == "NORMAL":
        # Req 3.5: no incident, no escalation, no notification.
        append_audit_entry(
            run_id=run_id,
            tool_name="monitor_agent",
            outcome="normal_no_anomaly_no_incident",
            inputs=audit_inputs,
            incident_id=incident_id,
        )
        return {
            "ok": True,
            "outcome": "no_incident",
            "escalated": False,
            "created": False,
            "updated": False,
            "incident_id": incident_id,
            "severity_band": severity_band,
            "assessment": assessment.model_dump(),
        }

    # Req 3.3: WATCH+, no open incident, confidence/action/anomaly all
    # clear -- create exactly one incident.
    write_result = incident_tools.create_or_update_incident(
        river_reach_id=river_reach_id,
        severity_band=severity_band,
        readings=current_readings,
        triggered_rule_ids=triggered_rule_ids,
        rule_set_version=rule_engine_result.get("rule_set_version", ""),
    )
    append_audit_entry(
        run_id=run_id,
        tool_name="monitor_agent",
        outcome="incident_created",
        inputs={**audit_inputs, "created_incident_id": write_result.get("incident_id")},
        incident_id=incident_id,
    )
    return {
        "ok": True,
        "outcome": "incident_created",
        "escalated": False,
        "created": True,
        "updated": False,
        "incident_id": write_result.get("incident_id", incident_id),
        "severity_band": write_result.get("severity_band", severity_band),
        "assessment": assessment.model_dump(),
    }
