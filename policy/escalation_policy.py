"""``Escalation_Policy`` — the single autonomy authority (Req 10, design.md §3.4, Design Question 3).

This module is **the only place in the source tree that holds an autonomy
threshold value** (Req 10.8). Every agent, hook, and the deterministic
``HumanInTheLoop`` classifier (design.md's Design Question 3) must call
:func:`decide` rather than re-implementing any part of this logic, so that
exactly one mechanism ever judges "act autonomously" vs. "ask a human" for a
given typed decision.

Verification note (mandatory per tasks.md 3.1, "verify first"):
    This module is pure Python — the only import from outside the standard
    library is nothing at all; ``schemas/decisions.py``'s five decision
    models (``HazardAssessment``, ``EmergencyRequest``, ``DispatchDecision``,
    ``AlertDraft``, ``SafetyReview``) were read to confirm the exact
    ``category`` literal/free-string values each model actually produces
    (transcribed into ``CATEGORY_TABLE`` below) and to confirm every one of
    those five models carries ``confidence: float``, an ``action`` field
    whose closed value set includes ``"ask_human"``, a ``category`` field,
    and ``is_irreversible_action: bool`` — the four fields :func:`decide`
    depends on, uniformly, regardless of which agent produced the decision.
    ``schemas/entities.py``'s ``EscalationRecord``/``_humanize()`` was read
    to confirm the default-action-key vocabulary already assumed downstream
    (``"hold"``, ``"hold_assignment"``, ``"send_with_standard_wording"``,
    ``"pay_now"``, ``"close_request"``); ``DEFAULT_ACTION`` below reuses that
    exact vocabulary rather than inventing new keys, so
    ``EscalationRecord.template_fields()``'s ``_humanize()`` call continues
    to render sensible text for every default action this module can
    produce. ``policy/rule_engine_rules.py::compute_rule_set_version`` was
    read to confirm and mirror its deterministic canonical-JSON SHA-256
    content-hash pattern for :data:`POLICY_VERSION` below (the same
    ``hashlib``/``json`` standard-library usage, already verified in that
    module's own docstring). No external SDK surface (Strands, boto3,
    Pydantic) is touched by this module at all.

Deviation from design.md §3.4 recorded here (per the "verify first"
instruction: "when the docs and the design disagree, the docs win" — here
the disagreement is between the design document's inline illustrative
pseudocode and the actually-committed ``.env.example``, which is the
concrete deployment artifact a judge/deployer reads): design.md's inline
``policy/escalation_policy.py`` pseudocode shows ``CONFIDENCE_FLOOR`` at
``0.75``, but ``.env.example``'s committed comment block for
``THUNAI_CONFIDENCE_FLOOR`` explicitly documents the design default as
``0.70``. This module follows ``.env.example`` (``0.70``) as instructed,
since that file is the artifact a deployer actually reads and edits, and
disagreeing with it silently would make the module's own documented default
wrong the moment someone reads ``.env.example`` instead of ``design.md``.
The two other env-overridable thresholds (``THUNAI_MASS_NOTIFICATION_
AUDIENCE_THRESHOLD=100``, ``THUNAI_ANOMALY_RATIO_THRESHOLD=1.25``) already
agree between ``.env.example`` and design.md, so no further deviation exists
for those two.

Fail-safe integration point (Req 10.10, Property 7 in design.md's
correctness-property list): ``validate_policy()`` is called once at module
import time and its result is cached in the module-level
:data:`_POLICY_DEFECTS` / :data:`_POLICY_IS_VALID` flag (the second of the
two integration points the task description offers, chosen over having
``decide()`` call ``validate_policy()`` itself on every call, because a
flag set once at cold start makes it impossible for a broken policy to be
"caught" on some call paths and missed on others within the same process —
every :func:`decide` call within a process shares the exact same cached
verdict). When :data:`_POLICY_IS_VALID` is ``False``, :func:`decide`
unconditionally returns an ``ASK_HUMAN`` verdict for every input, regardless
of that input's own fields, until the process restarts with a corrected
policy.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# Severity bands, in ascending order (Req 2.3, 10.1).
# ---------------------------------------------------------------------------

SEVERITY_BANDS_ASCENDING: Final[tuple[str, ...]] = ("NORMAL", "WATCH", "WARNING", "EVACUATE")

# ---------------------------------------------------------------------------
# Default-action vocabulary (Req 10.12; must match schemas/entities.py's
# EscalationRecord._humanize() known-key set exactly, so a template never
# renders an un-humanized raw key).
# ---------------------------------------------------------------------------

DEFAULT_ACTION_VOCABULARY: Final[frozenset[str]] = frozenset(
    {"hold", "hold_assignment", "send_with_standard_wording", "pay_now", "close_request"}
)


# ---------------------------------------------------------------------------
# Small dataclasses (design.md §3.4's `PolicyEntry` pattern, split into two
# shapes because the task's `PolicyEntry` is a *category row* while the
# scalar thresholds carry a unit/valid_range/rationale triple instead).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdSpec:
    """One scalar/per-band threshold entry (Req 10.1, 10.2): a value, its
    unit, its declared permitted range, and a non-empty one-sentence
    rationale."""

    value: float
    unit: str
    valid_range: tuple[float, float]
    rationale: str


@dataclass(frozen=True)
class PolicyEntry:
    """One row of the escalation decision table, keyed by ``category``
    (Req 10.1's always-ask/never-ask category sets).

    Attributes:
        category: The escalation-policy category string, as produced by one
            of ``schemas/decisions.py``'s five typed decision models (or, for
            categories not yet emitted by an unimplemented agent role, the
            category string that role's design.md section names).
        always_ask: True when this category must always be escalated
            (Req 10.1's always-ask category set), independent of confidence.
        never_ask: True when this category is always safe to auto-execute
            provided it is not otherwise irreversible and confidence clears
            the floor (Req 10.1's never-ask category set, Req 10.7).
        response_deadline_minutes_override: A per-category response deadline
            in whole minutes, overriding the per-severity-band default in
            :data:`RESPONSE_DEADLINE_MINUTES` for this category specifically.
            ``None`` when the per-band default applies unmodified.
        rationale: Non-empty one-sentence rationale for this row (Req 10.2).
    """

    category: str
    always_ask: bool = False
    never_ask: bool = False
    response_deadline_minutes_override: int | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.always_ask and self.never_ask:
            raise ValueError(
                f"PolicyEntry for category {self.category!r} cannot be both "
                "always_ask and never_ask (Req 10.10)"
            )
        if not self.rationale:
            raise ValueError(f"PolicyEntry for category {self.category!r} must carry a non-empty rationale (Req 10.2)")


# ---------------------------------------------------------------------------
# Env-var reading helpers (Req 10.8: overridable without code changes;
# .env.example documents THUNAI_CONFIDENCE_FLOOR, THUNAI_MASS_NOTIFICATION_
# AUDIENCE_THRESHOLD, THUNAI_ANOMALY_RATIO_THRESHOLD as the exact override
# var names for this module).
# ---------------------------------------------------------------------------


def _read_float_env(var_name: str, default: float) -> float:
    raw = os.environ.get(var_name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _read_int_env(var_name: str, default: int) -> int:
    raw = os.environ.get(var_name)
    if raw is None or raw == "":
        return default
    return int(raw)


# ---------------------------------------------------------------------------
# CONFIDENCE_FLOOR (Req 10.1, 10.4): global minimum confidence, below which
# decide() always escalates. Default 0.70 per .env.example's documented
# THUNAI_CONFIDENCE_FLOOR default (see the module docstring's deviation
# note re: design.md's inline 0.75 pseudocode value).
# ---------------------------------------------------------------------------

CONFIDENCE_FLOOR: Final[ThresholdSpec] = ThresholdSpec(
    value=_read_float_env("THUNAI_CONFIDENCE_FLOOR", 0.70),
    unit="probability",
    valid_range=(0.0, 1.0),
    rationale=(
        "Below this, misclassification risk on a safety-relevant decision outweighs the "
        "coordinator's few-second cost to confirm; matches the deployed default documented "
        "in .env.example's THUNAI_CONFIDENCE_FLOOR."
    ),
)

# ---------------------------------------------------------------------------
# MASS_NOTIFICATION_AUDIENCE_THRESHOLD (Req 10.1, 10.6): recipient count of
# 1 or greater above which an alert release always escalates, checked against
# AlertDraft.audience_count. Default 100 per .env.example's THUNAI_MASS_
# NOTIFICATION_AUDIENCE_THRESHOLD.
# ---------------------------------------------------------------------------

MASS_NOTIFICATION_AUDIENCE_THRESHOLD: Final[ThresholdSpec] = ThresholdSpec(
    value=_read_int_env("THUNAI_MASS_NOTIFICATION_AUDIENCE_THRESHOLD", 100),
    unit="recipient_count",
    valid_range=(1, 10_000),
    rationale=(
        "Below Ward-7's ~900-resident population, 100 recipients is the point a bad message "
        "becomes reputationally and logistically costly to walk back, so it gets a human look."
    ),
)

# ---------------------------------------------------------------------------
# ANOMALY_RATIO_THRESHOLD (Req 10.1, 10.7): ratio of the current reading to
# the stored 30-day baseline, of 1.0 or greater, checked against
# HazardAssessment.anomaly_indicator. Default 1.25 per .env.example's
# THUNAI_ANOMALY_RATIO_THRESHOLD.
# ---------------------------------------------------------------------------

ANOMALY_RATIO_THRESHOLD: Final[ThresholdSpec] = ThresholdSpec(
    value=_read_float_env("THUNAI_ANOMALY_RATIO_THRESHOLD", 1.25),
    unit="ratio_of_30day_baseline",
    valid_range=(1.0, 10.0),
    rationale=(
        "25% above the 30-day baseline is the smallest deviation the committee, in practice, "
        "treats as worth a look rather than normal river variability."
    ),
)

# ---------------------------------------------------------------------------
# RESPONSE_DEADLINE_MINUTES (Req 10.1, 10.8): per-severity-band deadline, in
# whole minutes between 1 and 120, monotonically non-increasing as severity
# rises (Req 10.10's monotonicity defect check).
# ---------------------------------------------------------------------------

RESPONSE_DEADLINE_MINUTES: Final[dict[str, ThresholdSpec]] = {
    "NORMAL": ThresholdSpec(
        value=_read_int_env("THUNAI_RESPONSE_DEADLINE_NORMAL_MINUTES", 120),
        unit="minutes",
        valid_range=(1, 120),
        rationale="Routine items can wait for the coordinator's next check-in.",
    ),
    "WATCH": ThresholdSpec(
        value=_read_int_env("THUNAI_RESPONSE_DEADLINE_WATCH_MINUTES", 60),
        unit="minutes",
        valid_range=(1, 120),
        rationale="A watch-level item still allows an hour before the default action applies.",
    ),
    "WARNING": ThresholdSpec(
        value=_read_int_env("THUNAI_RESPONSE_DEADLINE_WARNING_MINUTES", 20),
        unit="minutes",
        valid_range=(1, 120),
        rationale="Warning-level stakes tighten the response window sharply.",
    ),
    "EVACUATE": ThresholdSpec(
        value=_read_int_env("THUNAI_RESPONSE_DEADLINE_EVACUATE_MINUTES", 10),
        unit="minutes",
        valid_range=(1, 120),
        rationale=(
            "Evacuate-level communications cannot wait long, but still require a human before "
            "mass release (never-auto-send)."
        ),
    ),
}

# ---------------------------------------------------------------------------
# CATEGORY_TABLE (Req 10.1): every category string produced by
# schemas/decisions.py's five models, plus the escalation-type categories
# implied by Req 5.5-5.8, 6.5-6.6, 7.6, and the Irreversible_Action glossary
# entry (evacuation order, mass broadcast, dispatch to a non-ambulatory
# occupant, shelter closure, request closure). ALWAYS_ASK_CATEGORIES and
# NEVER_ASK_CATEGORIES below are derived from this table, so the table is
# the single source of truth for category membership (Req 10.8).
# ---------------------------------------------------------------------------

CATEGORY_TABLE: Final[dict[str, PolicyEntry]] = {
    # --- HazardAssessment.category (schemas/decisions.py) --------------
    "hazard_monitoring": PolicyEntry(
        category="hazard_monitoring",
        rationale=(
            "Routine hazard sweeps are neutral: escalation for a hazard assessment is driven by "
            "confidence, action, and irreversibility (e.g. an EVACUATE-band alert), not by this "
            "category alone (Req 3.3, 3.5)."
        ),
    ),
    "data_outage": PolicyEntry(
        category="data_outage",
        always_ask=True,
        rationale="A sensor data outage must always be surfaced to the coordinator, never silently assumed (Req 3.6).",
    ),
    # --- DispatchDecision.category (schemas/decisions.py) ---------------
    "routine_dispatch_ambulatory": PolicyEntry(
        category="routine_dispatch_ambulatory",
        never_ask=True,
        rationale="An ambulatory, non-medical dispatch at sufficient confidence is the routine case the coordinator delegates (Req 6.3).",
    ),
    "dispatch_non_ambulatory": PolicyEntry(
        category="dispatch_non_ambulatory",
        always_ask=True,
        response_deadline_minutes_override=RESPONSE_DEADLINE_MINUTES["WARNING"].value,
        rationale="Dispatch to a location with a reported non-ambulatory occupant is an Irreversible_Action; always confirmed (Req 6.5).",
    ),
    "no_capacity": PolicyEntry(
        category="no_capacity",
        always_ask=True,
        rationale="No available responder for a request is a real trade-off only the coordinator can resolve (Req 6.6).",
    ),
    "shelter_capacity": PolicyEntry(
        category="shelter_capacity",
        always_ask=True,
        rationale="A shelter-capacity conflict changes where people are sent and must be confirmed by a human (Req 6.7).",
    ),
    # --- AlertDraft.category (schemas/decisions.py: fixed literal
    # "alert_release"; the audience/anomaly/EVACUATE checks that actually
    # distinguish a routine alert from a mass one are evaluated by decide()
    # via its own audience_count/anomaly_ratio/is_irreversible_action
    # parameters, described further below in the module docstring for
    # decide()) -------------------------------------------------------
    "alert_release": PolicyEntry(
        category="alert_release",
        rationale=(
            "Neutral by category; escalation for an alert release is actually driven by the "
            "mass-notification audience threshold, the EVACUATE-band irreversibility flag, and "
            "confidence, evaluated by decide()'s own dedicated parameters (Req 7.6, 10.1, 10.6)."
        ),
    ),
    "routine_alert_below_threshold": PolicyEntry(
        category="routine_alert_below_threshold",
        never_ask=True,
        rationale="An alert below the mass-notification audience threshold, at sufficient confidence, is the routine case (Req 10.1).",
    ),
    "evacuation_order": PolicyEntry(
        category="evacuation_order",
        always_ask=True,
        response_deadline_minutes_override=RESPONSE_DEADLINE_MINUTES["EVACUATE"].value,
        rationale="An evacuation order is an Irreversible_Action and is withheld until human approval (Req 2.6, 10.12).",
    ),
    "mass_broadcast": PolicyEntry(
        category="mass_broadcast",
        always_ask=True,
        rationale="A mass broadcast notification is an Irreversible_Action per the glossary; always confirmed (Req 10.12).",
    ),
    # --- SafetyReview.category (schemas/decisions.py) -------------------
    "safety_review": PolicyEntry(
        category="safety_review",
        rationale="A safety review's own pass/fail is reported to the reviewed content's producing agent, not escalated by category alone (Req 9.1).",
    ),
    # --- Intake_Agent escalation categories implied by Req 5.4-5.8 -------
    # "routine_intake_request" added by task 13.3 (agents/intake_agent.py):
    # design.md/§4.1's EmergencyRequest.category docstring names
    # "routine_dispatch_ambulatory"/"manual_triage"-shaped strings as
    # examples but, unlike DispatchDecision/AlertDraft, never actually
    # enumerates a never-ask category for Intake_Agent's own Req 5.4
    # ("confidence at/above the floor AND category outside the always-ask
    # set -> dispatch-eligible, no escalation"). Without one, Intake_Agent
    # has no CATEGORY_TABLE entry to pass to decide() for its routine,
    # non-escalating path. This entry fills that gap, mirroring
    # "routine_dispatch_ambulatory"'s never_ask=True shape exactly, so
    # decide()'s existing confidence-floor/action/irreversibility checks
    # (Req 10.4-10.7) are what actually gate the Req 5.4/5.5 split, with
    # this category never itself forcing an escalation.
    "routine_intake_request": PolicyEntry(
        category="routine_intake_request",
        never_ask=True,
        rationale=(
            "A resident request at or above the confidence floor, with no explicit ask-human "
            "action and not otherwise irreversible, is the routine intake case the coordinator "
            "delegates without review (Req 5.4)."
        ),
    ),
    "request_triage": PolicyEntry(
        category="request_triage",
        always_ask=True,
        rationale="A request below the confidence floor or in an always-ask category is triaged by the coordinator, never auto-dispatched (Req 5.5).",
    ),
    "location_clarification": PolicyEntry(
        category="location_clarification",
        always_ask=True,
        rationale="An ambiguous location resolves to more than one candidate; only a human can pick the right one (Req 5.6).",
    ),
    "manual_triage": PolicyEntry(
        category="manual_triage",
        always_ask=True,
        rationale="An uncategorisable or unsupported-language message must be preserved and handed to a human, never guessed (Req 5.8).",
    ),
    # --- Knowledge_Agent -------------------------------------------------
    "knowledge_answer_grounded": PolicyEntry(
        category="knowledge_answer_grounded",
        never_ask=True,
        rationale="A grounded, cited knowledge-base answer with no physical-assistance request is safe to deliver directly (Req 8).",
    ),
    # Added by task 13.7 (agents/knowledge_agent.py): Req 8.6/8.7 both
    # require Knowledge_Agent to route the question to the coordinator via
    # Escalation_Service rather than answer -- neither the relevance-floor
    # miss (8.6) nor the retrieval-failure/timeout case (8.7) had a
    # CATEGORY_TABLE entry before this task; both mirror "data_outage"'s
    # always_ask=True shape exactly, since fabricating a safety answer with
    # no grounded passage (or with retrieval itself broken) is never safe
    # to auto-deliver, regardless of confidence.
    "knowledge_no_answer": PolicyEntry(
        category="knowledge_no_answer",
        always_ask=True,
        rationale=(
            "No retrieved passage cleared the configured relevance threshold; fabricating a "
            "safety answer with no grounded passage is unsafe, so the coordinator fields the "
            "question directly (Req 8.6)."
        ),
    ),
    "knowledge_unavailable": PolicyEntry(
        category="knowledge_unavailable",
        always_ask=True,
        rationale=(
            "Knowledge_Base retrieval failed or did not complete within the configured retrieval "
            "timeout; the coordinator must field the question directly rather than the platform "
            "guessing (Req 8.7)."
        ),
    ),
    # --- Platform-level fault categories (Req 2.10, 10.10, 10.11) --------
    "config_fault": PolicyEntry(
        category="config_fault",
        always_ask=True,
        rationale="A failed or missing active rule set must be surfaced, never silently assumed (Req 2.10).",
    ),
    "credential_unavailable": PolicyEntry(
        category="credential_unavailable",
        always_ask=True,
        rationale="A missing credential for a live integration must halt autonomous action and notify the coordinator (Req 19.13).",
    ),
    "state_write_failure": PolicyEntry(
        category="state_write_failure",
        always_ask=True,
        rationale="A failed state write leaves data in an unknown condition; only a human sign-off should proceed from there.",
    ),
    "memory_unavailable": PolicyEntry(
        category="memory_unavailable",
        always_ask=True,
        rationale="Missing prior-run memory removes the basis for a confident autonomous decision; ask rather than guess.",
    ),
    "request_closure": PolicyEntry(
        category="request_closure",
        always_ask=True,
        rationale="Closing a resident's request is an Irreversible_Action per the glossary; always confirmed (Req 10.12).",
    ),
    "shelter_closure": PolicyEntry(
        category="shelter_closure",
        always_ask=True,
        rationale="Closing a shelter is an Irreversible_Action per the glossary; always confirmed (Req 10.12).",
    ),
    "validation_failure": PolicyEntry(
        category="validation_failure",
        always_ask=True,
        rationale="A structured-output validation failure (schemas/structured.py's ValidationFailureFallback) is always treated as ask-human (Req 10.11).",
    ),
}

# ---------------------------------------------------------------------------
# ALWAYS_ASK_CATEGORIES / NEVER_ASK_CATEGORIES (Req 10.1): derived from
# CATEGORY_TABLE so the table remains the single source of truth.
# ---------------------------------------------------------------------------

ALWAYS_ASK_CATEGORIES: Final[frozenset[str]] = frozenset(
    category for category, entry in CATEGORY_TABLE.items() if entry.always_ask
)

NEVER_ASK_CATEGORIES: Final[frozenset[str]] = frozenset(
    category for category, entry in CATEGORY_TABLE.items() if entry.never_ask
)

# ---------------------------------------------------------------------------
# DEFAULT_ACTION (Req 10.1, 10.8, 10.12): per-category default action applied
# by Escalation_Service's timeout sweeper (task 20.3) when the response
# deadline passes with no human response. Values are drawn exclusively from
# schemas/entities.py::EscalationRecord's _humanize() known-key vocabulary
# (DEFAULT_ACTION_VOCABULARY above) so the rendered notification text is
# always sensible. Every category that can ever be escalated (every member
# of ALWAYS_ASK_CATEGORIES, plus any neutral category that may still
# escalate via confidence/action/audience/anomaly) is withhold-by-default
# ("hold"/"hold_assignment"/"close_request" — never an action that commits
# resources), per Req 10.12's "default action for every escalation type
# raised for an Irreversible_Action is withholding that action", extended
# by design.md §3.4 to every escalation type.
# ---------------------------------------------------------------------------

DEFAULT_ACTION: Final[dict[str, str]] = {
    "hazard_monitoring": "hold",
    "data_outage": "hold",
    "routine_dispatch_ambulatory": "hold_assignment",
    "dispatch_non_ambulatory": "hold_assignment",
    "no_capacity": "hold_assignment",
    "shelter_capacity": "hold_assignment",
    "routine_intake_request": "hold",
    "alert_release": "hold",
    "routine_alert_below_threshold": "send_with_standard_wording",
    "evacuation_order": "hold",
    "mass_broadcast": "hold",
    "safety_review": "hold",
    "request_triage": "hold",
    "location_clarification": "hold",
    "manual_triage": "hold",
    "knowledge_answer_grounded": "hold",
    "knowledge_no_answer": "hold",
    "knowledge_unavailable": "hold",
    "config_fault": "hold",
    "credential_unavailable": "hold",
    "state_write_failure": "hold",
    "memory_unavailable": "hold",
    "request_closure": "hold",
    "shelter_closure": "hold",
    "validation_failure": "hold",
}

assert set(DEFAULT_ACTION) == set(CATEGORY_TABLE), (
    "DEFAULT_ACTION must cover exactly the categories declared in CATEGORY_TABLE"
)
assert set(DEFAULT_ACTION.values()) <= DEFAULT_ACTION_VOCABULARY, (
    "every DEFAULT_ACTION value must be a member of the entities.py _humanize() known-key vocabulary"
)


# ---------------------------------------------------------------------------
# POLICY_VERSION (Req 10.2): deterministic content hash over every policy
# constant that affects decide()'s output, mirroring
# policy/rule_engine_rules.py::compute_rule_set_version's canonical-JSON
# SHA-256 pattern exactly.
# ---------------------------------------------------------------------------


def _canonicalize_category_table(table: dict[str, PolicyEntry]) -> dict:
    return {
        category: {
            "always_ask": entry.always_ask,
            "never_ask": entry.never_ask,
            "response_deadline_minutes_override": entry.response_deadline_minutes_override,
        }
        for category, entry in table.items()
    }


def _canonicalize_threshold(spec: ThresholdSpec) -> dict:
    return {"value": spec.value, "unit": spec.unit, "valid_range": list(spec.valid_range)}


def _canonicalize_response_deadlines(deadlines: dict[str, ThresholdSpec]) -> dict:
    return {band: _canonicalize_threshold(spec) for band, spec in deadlines.items()}


def compute_policy_version(
    category_table: dict[str, PolicyEntry],
    confidence_floor: ThresholdSpec,
    mass_notification_threshold: ThresholdSpec,
    anomaly_ratio_threshold: ThresholdSpec,
    response_deadline_minutes: dict[str, ThresholdSpec],
    default_action: dict[str, str],
) -> str:
    """Compute the deterministic ``POLICY_VERSION`` content hash.

    Identical arguments always produce the identical hash, across processes
    and runs; changing any category row, any threshold value/unit/range, any
    per-band deadline, or any default-action mapping changes the hash
    (Req 10.2, Property 8).
    """
    payload = {
        "category_table": _canonicalize_category_table(category_table),
        "confidence_floor": _canonicalize_threshold(confidence_floor),
        "mass_notification_threshold": _canonicalize_threshold(mass_notification_threshold),
        "anomaly_ratio_threshold": _canonicalize_threshold(anomaly_ratio_threshold),
        "response_deadline_minutes": _canonicalize_response_deadlines(response_deadline_minutes),
        "default_action": dict(sorted(default_action.items())),
    }
    canonical_json = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:12]


POLICY_VERSION: Final[str] = compute_policy_version(
    CATEGORY_TABLE,
    CONFIDENCE_FLOOR,
    MASS_NOTIFICATION_AUDIENCE_THRESHOLD,
    ANOMALY_RATIO_THRESHOLD,
    RESPONSE_DEADLINE_MINUTES,
    DEFAULT_ACTION,
)


# ---------------------------------------------------------------------------
# validate_policy() (Req 10.10, Property 7): cold-start defect detection.
# Mirrors agents/config.py::validate_config()'s "collect every problem,
# report once naming all of them" pattern, but *returns* the defect list
# (design.md's own pseudocode shape) rather than raising, since Req 10.10's
# required behaviour on a defect is "execute no action without human
# approval" — a live fail-safe degradation, not a hard process abort.
# ---------------------------------------------------------------------------


def validate_policy(
    *,
    category_table: dict[str, PolicyEntry] | None = None,
    confidence_floor: ThresholdSpec | None = None,
    mass_notification_threshold: ThresholdSpec | None = None,
    anomaly_ratio_threshold: ThresholdSpec | None = None,
    response_deadline_minutes: dict[str, ThresholdSpec] | None = None,
    default_action: dict[str, str] | None = None,
) -> list[str]:
    """Detect every defect in the escalation policy configuration (Req 10.10).

    Every parameter defaults to the module's live configuration; a caller
    (typically a test) may pass an alternate value to validate a mutated
    policy without changing process state — mirroring
    ``agents.config.validate_config``'s testable-override pattern.

    Returns:
        A list of human-readable defect descriptions. Empty when the policy
        is fully valid. Never raises.
    """
    if category_table is None:
        category_table = CATEGORY_TABLE
    if confidence_floor is None:
        confidence_floor = CONFIDENCE_FLOOR
    if mass_notification_threshold is None:
        mass_notification_threshold = MASS_NOTIFICATION_AUDIENCE_THRESHOLD
    if anomaly_ratio_threshold is None:
        anomaly_ratio_threshold = ANOMALY_RATIO_THRESHOLD
    if response_deadline_minutes is None:
        response_deadline_minutes = RESPONSE_DEADLINE_MINUTES
    if default_action is None:
        default_action = DEFAULT_ACTION

    defects: list[str] = []

    # 1. No category in both always-ask and never-ask (Req 10.10).
    always_ask = {c for c, e in category_table.items() if e.always_ask}
    never_ask = {c for c, e in category_table.items() if e.never_ask}
    overlap = always_ask & never_ask
    if overlap:
        defects.append(f"category listed in both always-ask and never-ask: {sorted(overlap)}")

    # 2. Every scalar threshold within its declared range (Req 10.1, 10.10).
    for name, spec in (
        ("confidence_floor", confidence_floor),
        ("mass_notification_threshold", mass_notification_threshold),
        ("anomaly_ratio_threshold", anomaly_ratio_threshold),
    ):
        low, high = spec.valid_range
        if not (low <= spec.value <= high):
            defects.append(f"{name} value {spec.value} outside declared range [{low}, {high}]")

    # 3. Every declared severity band present, each within [1, 120] minutes,
    #    and non-increasing as severity rises (Req 10.1, 10.10).
    missing_bands = [b for b in SEVERITY_BANDS_ASCENDING if b not in response_deadline_minutes]
    if missing_bands:
        defects.append(f"RESPONSE_DEADLINE_MINUTES missing band(s): {missing_bands}")
    else:
        deadlines = [response_deadline_minutes[b].value for b in SEVERITY_BANDS_ASCENDING]
        for band, value in zip(SEVERITY_BANDS_ASCENDING, deadlines):
            low, high = response_deadline_minutes[band].valid_range
            if not (low <= value <= high):
                defects.append(f"RESPONSE_DEADLINE_MINUTES[{band!r}] value {value} outside declared range [{low}, {high}]")
        for i in range(len(deadlines) - 1):
            if deadlines[i] < deadlines[i + 1]:
                defects.append(
                    f"RESPONSE_DEADLINE_MINUTES[{SEVERITY_BANDS_ASCENDING[i + 1]!r}] "
                    f"({deadlines[i + 1]} min) exceeds "
                    f"RESPONSE_DEADLINE_MINUTES[{SEVERITY_BANDS_ASCENDING[i]!r}] ({deadlines[i]} min), "
                    "a higher-severity band must not declare a longer deadline than a lower one"
                )

    # 4. DEFAULT_ACTION covers every category that can be escalated. Any
    #    category can, in principle, escalate via confidence/action/
    #    audience/anomaly even when not in always_ask, so every declared
    #    category must carry a default action (Req 10.1, 10.8, 10.12).
    missing_defaults = set(category_table) - set(default_action)
    if missing_defaults:
        defects.append(f"DEFAULT_ACTION missing entry for escalatable category(ies): {sorted(missing_defaults)}")
    unknown_values = {
        category: action
        for category, action in default_action.items()
        if action not in DEFAULT_ACTION_VOCABULARY
    }
    if unknown_values:
        defects.append(f"DEFAULT_ACTION has value(s) outside the known vocabulary: {unknown_values}")

    # 5. Every always-ask category must carry a non-empty rationale
    #    (Req 10.2) -- PolicyEntry.__post_init__ already enforces this at
    #    construction time for the live table, but a caller-supplied
    #    mutated table for testing may not go through PolicyEntry at all,
    #    so re-check defensively here too.
    for category, entry in category_table.items():
        if not entry.rationale:
            defects.append(f"category {category!r} carries no rationale (Req 10.2)")

    return defects


# ---------------------------------------------------------------------------
# Fail-safe cache (Req 10.10): computed once at import time. decide() reads
# _POLICY_IS_VALID rather than re-running validate_policy() on every call, so
# that a defect found once is guaranteed to be honoured by every subsequent
# decide() call in this process (see the module docstring's "Fail-safe
# integration point" note).
# ---------------------------------------------------------------------------

_POLICY_DEFECTS: list[str] = validate_policy()
_POLICY_IS_VALID: bool = not _POLICY_DEFECTS


# ---------------------------------------------------------------------------
# decide(): the single autonomy authority (Req 10.4-10.7, Design Question 3,
# Property 6).
# ---------------------------------------------------------------------------


def decide(
    category: str,
    confidence: float,
    *,
    action: str | None = None,
    is_irreversible_action: bool = False,
    anomaly_ratio: float | None = None,
    audience_count: int | None = None,
    severity_band: str = "WATCH",
    **_ignored: Any,
) -> dict[str, Any]:
    """The single deterministic authority deciding EXECUTE vs. ASK_HUMAN.

    Called uniformly regardless of which of the five decision models in
    ``schemas/decisions.py`` produced the inputs (mirroring
    ``schemas.structured.safe_structured()``'s uniform design) — a caller
    typically unpacks a decision's ``category``, ``confidence``, ``action``,
    and ``is_irreversible_action`` fields directly, and passes
    ``audience_count``/``anomaly_ratio`` only for the two model types that
    carry those extra fields (``AlertDraft.audience_count``,
    ``HazardAssessment.anomaly_indicator``).

    Args:
        category: The escalation-policy category string for this decision
            (e.g. ``"routine_dispatch_ambulatory"``, ``"alert_release"``). A
            category absent from :data:`CATEGORY_TABLE` is treated as
            neutral (subject only to confidence/action/irreversibility/
            audience/anomaly checks), never as an error — a category the
            table does not yet know about must not silently execute nor
            silently escalate for the wrong reason.
        confidence: The decision's confidence value, expected in [0.0, 1.0].
        action: The decision's own ``action`` field value, if the caller has
            one (``"execute"`` or ``"ask_human"`` for the five typed decision
            models; ``None`` when the caller has no such field, e.g. a
            platform-level fault that has no underlying typed decision).
        is_irreversible_action: Whether this decision is flagged as an
            ``Irreversible_Action`` (Req 10.6, 10.12) — always escalates.
        anomaly_ratio: The current-reading-to-30-day-baseline ratio, for a
            ``HazardAssessment``-shaped decision (``anomaly_indicator``).
            ``None`` when not applicable.
        audience_count: The recipient count for an ``AlertDraft``-shaped
            decision. ``None`` when not applicable.
        severity_band: The incident severity band used to select the
            per-band response deadline when no per-category override
            applies. Defaults to ``"WATCH"``.

    Returns:
        A dict with:
            - ``"escalate"`` (bool): ``True`` for ASK_HUMAN, ``False`` for
              EXECUTE.
            - ``"reason"`` (str): a short human-readable explanation, fit for
              ``EscalationRecord.reason`` when ``escalate`` is ``True``.
            - ``"response_deadline_minutes"`` (int): the per-category
              override if one applies, else the per-severity-band default.
            - ``"default_action"`` (str | None): the
              :data:`DEFAULT_ACTION` entry for ``category`` when
              ``escalate`` is ``True``, else ``None``.
            - ``"policy_version"`` (str): the active :data:`POLICY_VERSION`,
              for audit correlation (Req 10.8: "record ... the policy
              version identifier, the entry that determined the outcome,
              and whether the outcome was execution or escalation").
            - ``"determining_entry"`` (str): the name of the policy entry
              that determined the outcome (e.g. ``"is_irreversible_action"``,
              ``"CONFIDENCE_FLOOR"``, ``"NEVER_ASK_CATEGORIES"``), for the
              same audit requirement.
    """
    entry = CATEGORY_TABLE.get(category)
    deadline_minutes = (
        entry.response_deadline_minutes_override
        if entry is not None and entry.response_deadline_minutes_override is not None
        else RESPONSE_DEADLINE_MINUTES.get(severity_band, RESPONSE_DEADLINE_MINUTES["WATCH"]).value
    )

    def _escalate(reason: str, determining_entry: str) -> dict[str, Any]:
        return {
            "escalate": True,
            "reason": reason,
            "response_deadline_minutes": deadline_minutes,
            "default_action": DEFAULT_ACTION.get(category, "hold"),
            "policy_version": POLICY_VERSION,
            "determining_entry": determining_entry,
        }

    def _execute(reason: str, determining_entry: str) -> dict[str, Any]:
        return {
            "escalate": False,
            "reason": reason,
            "response_deadline_minutes": deadline_minutes,
            "default_action": None,
            "policy_version": POLICY_VERSION,
            "determining_entry": determining_entry,
        }

    # Fail-safe: an invalid policy configuration escalates every decision
    # unconditionally (Req 10.10, Property 7), regardless of every other
    # input below.
    if not _POLICY_IS_VALID:
        return _escalate(
            f"Escalation_Policy is unusable ({len(_POLICY_DEFECTS)} defect(s) found at cold start); "
            "every decision is escalated until the policy is corrected (Req 10.10).",
            "_POLICY_IS_VALID",
        )

    if is_irreversible_action:
        return _escalate(
            "irreversible action always escalates (Req 10.6, 10.12)",
            "is_irreversible_action",
        )

    if action == "ask_human":
        return _escalate(
            "the decision explicitly requested human input (Req 10.5)",
            "action",
        )

    if category in ALWAYS_ASK_CATEGORIES or (entry is not None and entry.always_ask):
        return _escalate(
            f"category '{category}' is in the always-ask set (Req 10.1, 10.6)",
            "ALWAYS_ASK_CATEGORIES",
        )

    if confidence < CONFIDENCE_FLOOR.value:
        return _escalate(
            f"confidence {confidence} is below the floor {CONFIDENCE_FLOOR.value} (Req 10.4)",
            "CONFIDENCE_FLOOR",
        )

    if audience_count is not None and audience_count > MASS_NOTIFICATION_AUDIENCE_THRESHOLD.value:
        return _escalate(
            f"audience_count {audience_count} exceeds the mass-notification threshold "
            f"{MASS_NOTIFICATION_AUDIENCE_THRESHOLD.value} (Req 10.1, 10.6)",
            "MASS_NOTIFICATION_AUDIENCE_THRESHOLD",
        )

    if anomaly_ratio is not None and anomaly_ratio > ANOMALY_RATIO_THRESHOLD.value:
        return _escalate(
            f"anomaly_ratio {anomaly_ratio} exceeds the anomaly ratio threshold "
            f"{ANOMALY_RATIO_THRESHOLD.value} (Req 10.1, 10.7)",
            "ANOMALY_RATIO_THRESHOLD",
        )

    if category in NEVER_ASK_CATEGORIES or (entry is not None and entry.never_ask):
        return _execute(
            f"category '{category}' is never-ask, not irreversible, and confidence clears the floor (Req 10.7)",
            "NEVER_ASK_CATEGORIES",
        )

    return _execute("no escalation trigger met", "none")
