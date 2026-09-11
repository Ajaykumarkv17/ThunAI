"""Typed decision models for ThunAI (design.md §4.1).

Every model below carries `confidence: float` constrained to [0.0, 1.0] and a
closed `action: Literal[...]` field that includes an explicit `"ask_human"`
value (Req 10.3), plus a `category` field and `is_irreversible_action: bool`
so that `policy.escalation_policy.decide()` (design §3.4) can gate any of
these five models uniformly regardless of which agent produced them.

Verification note (mandatory per tasks.md 2.1):
    - Confirmed against the pinned `pydantic==2.13.5` (see requirements.txt)
      and the current Pydantic v2 docs that `Field(ge=..., le=...)` numeric
      constraints, `typing.Literal` closed value sets, and
      `BaseModel.model_validate` / `BaseModel.model_dump` are the stable v2
      API surface used throughout this module — no deviation from design.md
      on that front.
    - Confirmed against the current Strands Agents docs
      (structured-output.md) that `structured_output_model=` is the
      supported invocation-time parameter, that a validated instance is
      returned on `AgentResult.structured_output`, and that a parsing/
      validation failure is raised as `strands.types.exceptions.
      StructuredOutputException` (not a bare `pydantic.ValidationError`
      surfacing directly to caller code). This is a **deviation from
      design.md §4.1**, which describes `safe_structured()` (implemented in
      task 2.4, `schemas/structured.py`) as catching Pydantic
      `ValidationError`. The guard implemented in task 2.4 must therefore
      catch `StructuredOutputException` (and, defensively, `ValidationError`
      for direct `model_validate()` call sites that do not go through an
      `Agent` invocation) — this note is recorded here for that task's
      author. It has no effect on the model definitions in this module.
"""

from typing import Literal

from pydantic import BaseModel, Field


class HazardAssessment(BaseModel):
    """Typed output of Monitor_Agent for one hazard sweep (Req 3.2).

    `rate_of_change` and `anomaly_indicator` are keyed by reading type (e.g.
    "river_level", "rainfall_rate", "dam_release"); values are `None` when
    that reading type is unavailable for this evaluation (Req 3.8, 3.9).
    """

    severity_band: Literal["NORMAL", "WATCH", "WARNING", "EVACUATE"] = Field(
        description="Severity band computed by Rule_Engine for this sweep."
    )
    rate_of_change: dict[str, float | None] = Field(
        description=(
            "Rate of change per reading type, in that reading type's unit "
            "per hour (e.g. metres/hour for river level, mm/h per hour for "
            "rainfall rate, m3/s per hour for dam release). None if the "
            "reading type is unavailable."
        )
    )
    anomaly_indicator: dict[str, float | None] = Field(
        description=(
            "Ratio of the current reading to the stored 30-day baseline, "
            "per reading type. None if unavailable."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in [0.0, 1.0].")
    action: Literal["execute", "ask_human"] = Field(
        description="Whether Monitor_Agent proposes to act or to ask a human."
    )
    category: Literal["hazard_monitoring"] = "hazard_monitoring"
    is_irreversible_action: bool = False
    rationale: str = Field(
        max_length=200,
        description="One sentence, at most 200 characters, written for a non-technical reader.",
    )
    unavailable_readings: list[str] = Field(default_factory=list)


class EmergencyRequest(BaseModel):
    """Typed output of Intake_Agent for one inbound resident message (Req 5.1)."""

    request_category: Literal["RESCUE", "MEDICAL", "SHELTER", "SUPPLIES", "INFORMATION", "OTHER"] = Field(
        description="Exactly one request category per Req 5.2."
    )
    occupant_count: int | Literal["unknown"] = Field(
        default="unknown",
        description="Whole number 0-99, or the literal string 'unknown' where the message states no count.",
    )
    location_reference: str = Field(description="The resolved or best-effort location reference.")
    location_candidates: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Up to 5 ranked candidate locations recorded when resolution is ambiguous (Req 5.6).",
    )
    mobility_assistance: Literal[True, False, "unknown"] = Field(
        description="True/False/unknown mobility-assistance indicator."
    )
    medical_need: Literal[True, False, "unknown"] = Field(
        description="True/False/unknown medical-need indicator."
    )
    source_language: str = Field(description="Detected source language of the inbound message.")
    urgency_band: Literal["IMMEDIATE", "URGENT", "ROUTINE"] = Field(
        description="Exactly one urgency band; IMMEDIATE required when mobility_assistance or medical_need is True (Req 5.3)."
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in [0.0, 1.0].")
    action: Literal["execute", "ask_human"] = Field(
        description="Whether Intake_Agent proposes to act or to ask a human."
    )
    category: str = Field(
        description='Escalation-policy category, e.g. "routine_dispatch_ambulatory", "manual_triage".'
    )
    is_irreversible_action: bool = False
    rationale: str = Field(
        max_length=200,
        description="One sentence, at most 200 characters, written for a human reader.",
    )
    dispatch_eligible: bool = Field(
        description="Whether this request is currently eligible for dispatch (Req 5.4, 5.5)."
    )

    @property
    def is_always_ask(self) -> bool:
        """True when the request must always be escalated (Req 6.5): mobility or medical need."""
        return self.mobility_assistance is True or self.medical_need is True


class DispatchDecision(BaseModel):
    """Typed output of Dispatch_Agent for one emergency request (Req 6.2)."""

    selected_responder_id: str | None = Field(
        description="Identifier of the selected responder, or None when no responder is selected."
    )
    alternative_responder_ids: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Up to 3 alternative responder identifiers ranked by suitability.",
    )
    selection_rationale: str = Field(
        max_length=200,
        description="One sentence, at most 200 characters, explaining the selection.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in [0.0, 1.0].")
    action: Literal["execute", "ask_human"] = Field(
        description="Whether Dispatch_Agent proposes to act or to ask a human."
    )
    category: Literal[
        "routine_dispatch_ambulatory",
        "dispatch_non_ambulatory",
        "no_capacity",
        "shelter_capacity",
    ] = Field(description="Escalation-policy category for this dispatch decision.")
    is_irreversible_action: bool = False


class AlertDraft(BaseModel):
    """Typed output of Alert_Agent for one channel/language variant of an incident alert (Req 7.1)."""

    channel: str = Field(description="Delivery channel this draft is composed for, e.g. 'sms', 'email'.")
    language: str = Field(description="Language this draft is composed in, e.g. 'ta', 'en'.")
    affected_areas: list[str] = Field(description="Affected area list for this alert.")
    recommended_action: str = Field(description="Exactly one recommended action for the recipient.")
    nearest_shelter: str = Field(
        description="Nearest shelter with available capacity above zero, or the configured no-capacity guidance text (Req 7.2)."
    )
    validity_start: str = Field(description="ISO-8601 timestamp with time zone.")
    validity_end: str = Field(description="ISO-8601 timestamp with time zone.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in [0.0, 1.0].")
    action: Literal["execute", "ask_human"] = Field(
        description="Whether Alert_Agent proposes to deliver or to ask a human."
    )
    category: Literal["alert_release"] = "alert_release"
    is_irreversible_action: bool = Field(
        description="True whenever the incident severity band is EVACUATE (set by Alert_Agent)."
    )
    audience_count: int = Field(description="Recipient count for this delivery audience.")


class SafetyReview(BaseModel):
    """Typed output of Safety_QA_Agent reviewing one proposed communication or action (Req 9.1)."""

    reviewed_content_id: str = Field(description="Identifier of the reviewed content or proposed action.")
    pass_indicator: bool = Field(description="True when the reviewed content passes every checked policy.")
    violated_policy_ids: list[str] = Field(default_factory=list)
    suggested_revisions: list[str] = Field(default_factory=list)
    policy_version: str = Field(description="Active safety policy version applied during this review.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        default=1.0,
        description="Confidence in [0.0, 1.0]; this check is deterministic-leaning but still typed uniformly.",
    )
    action: Literal["execute", "ask_human"] = "execute"
    category: Literal["safety_review"] = "safety_review"
    is_irreversible_action: bool = False
