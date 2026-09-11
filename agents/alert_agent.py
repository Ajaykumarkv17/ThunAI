"""``Alert_Agent``: composes channel- and language-specific alert content for
an incident, submits every draft to ``Safety_QA_Agent`` before delivery, and
withholds delivery (escalating instead) for an EVACUATE-band or
mass-audience release (Req 7; design.md §3.1 (Alert), §3.4 Design Question 3,
§3.5).

Verification note (mandatory per tasks.md 13.5, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (matches every
    other already-implemented module's own verification note in this
    codebase; `pip show strands-agents`). Re-confirmed the same three facts
    `agents/config.py` (task 13.1) and `schemas/structured.py` (task 2.4)
    already verified and documented, so this module does not re-derive them
    independently:
        - ``strands.models.BedrockModel(model_id=..., region_name=...,
          temperature=..., max_tokens=...)`` is still the current
          constructor shape (`BedrockModel.__init__`'s signature is
          ``(*, boto_session=None, boto_client_config=None,
          region_name=None, endpoint_url=None, api_key=None,
          **model_config)`` — ``model_id``/``temperature``/``max_tokens``
          flow through ``**model_config`` as ``BedrockConfig`` keys,
          confirmed via ``inspect.signature`` against the installed
          package).
        - ``Agent(agent_id=..., name=..., system_prompt=..., model=...,
          tools=[...], hooks=[...])`` with no ``session_manager=`` argument
          is the correct construction for an ``Incident_Graph`` member
          agent (Req 4.13: "construct every member agent of Incident_Graph
          with no attached session manager" — this module never passes
          ``session_manager``).
        - ``agent(prompt, structured_output_model=SomeModel)`` returns the
          validated instance on ``.structured_output``, and a validation
          failure raises ``strands.types.exceptions
          .StructuredOutputException`` — already verified by
          ``schemas/structured.py`` (task 2.4); this module re-uses that
          module's ``safe_structured()`` guard rather than catching the
          exception a second time.
    No new Strands API surface is introduced by this module beyond what
    ``agents/config.py``, ``schemas/structured.py``, and
    ``harness/hooks.py`` (all already implemented and verified) already
    cover.

Design/scope decisions this module makes, recorded explicitly:

1. **Structured-output schema used for the LLM call is smaller than
   ``AlertDraft``.** ``schemas.decisions.AlertDraft`` (the record this
   module ultimately produces) carries several fields that are populated
   deterministically in this module's own code, never by a model call:
   ``channel``/``language`` (loop variables), ``affected_areas`` (caller
   input), ``nearest_shelter`` (from ``get_shelter_capacity``),
   ``validity_start``/``validity_end`` (caller input), ``audience_count``
   (caller input), ``category``/``is_irreversible_action`` (derived from
   ``severity_band``). The only field a model genuinely needs to compose
   is ``recommended_action`` — "exactly one recommended action for the
   recipient", worded for a specific language and bounded to fit inside
   the remaining character budget after the rest of the rendered message
   template. Asking the model to also re-produce every deterministic field
   (as a full ``AlertDraft``) would risk it silently drifting one of those
   fields (e.g. mangling ``audience_count``) with no benefit, so this
   module defines a small local schema, ``_ComposedContent`` (below), for
   the LLM call only, and constructs the real ``AlertDraft`` itself from
   deterministic inputs plus the model's ``recommended_action``/
   ``confidence``.
2. **No resident distribution-list ownership** (mirrors
   ``tools/alert_tools.py::deliver_alert``'s own recorded scope decision,
   which this module inherits directly): ``compose_incident_alerts()``
   accepts an explicit ``audience_count: int`` (the recipient count Req
   7.1/7.6 need for the drafts and the mass-notification check) and an
   optional ``recipients_by_channel: dict[str, list[str]]`` used only when
   delivery is not withheld — a future resident-contact-book module is
   expected to supply the latter; this module does not invent one.
3. **Rendered message body.** ``AlertDraft`` has no single "body" text
   field of its own (Req 7.1's fields — affected areas, recommended
   action, nearest shelter, validity window — are separate, structured
   fields). Req 7.4's channel character limit is a limit on the actual
   outbound message, so this module defines ``render_body()``, a
   deterministic template rendering every ``AlertDraft`` field (plus the
   incident's ``severity_band``, tracked alongside the draft rather than
   on it, since ``AlertDraft`` itself carries no ``severity_band`` field)
   into the one string passed to ``tools.alert_tools.deliver_alert``.
   Recomposition (Req 7.4, 7.9) re-asks the model for a shorter
   ``recommended_action`` and re-renders the body, up to the configured
   recomposition attempt count, before withholding that variant.
4. **Safety_QA_Agent integration point.** ``agents/safety_qa_agent.py``
   (task 13.6) does not exist yet in this repository as of this task
   (confirmed: no such file under ``agents/``). Per this task's own
   instruction ("call into agents/safety_qa_agent.py if implemented, or
   leave a clear call site / TODO ... if not yet available"), this module
   defines ``_submit_for_safety_review()``, which tries to import
   ``agents.safety_qa_agent.review_content`` and falls back, only when that
   import fails, to a deterministic interim check built from the one
   safety module that *does* already exist
   (``policy.safety_policy.detect_pii_or_secrets``): a rendered body with
   no detected PII/secret pattern passes; one with a detected pattern
   fails, carrying the matched policy id(s) as ``violated_policy_ids``.
   This is a real, working, testable placeholder — not a broken stub —
   documented here as pending replacement once task 13.6 lands (search for
   ``# TODO(13.6)`` below for the exact line to replace).
5. **Escalation authority.** Per Design Question 3, the *only* function
   that ever decides EXECUTE vs. ASK_HUMAN for the alert-release decision
   is ``policy.escalation_policy.decide()``; this module never re-derives
   the EVACUATE/mass-audience/confidence thresholds itself. A false
   Safety_QA pass indicator (Req 9.2) is escalated unconditionally and
   directly (Req 9.2's own wording has no confidence/threshold gate at
   all — it always escalates), so that one case calls
   ``escalation_tools.create_escalation`` directly rather than through
   ``decide()``.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from pydantic import BaseModel, Field
from strands import Agent

from agents.config import get_model_id_for_role
from harness.audit import append_audit_entry
from harness.hooks import HARNESS_HOOKS
from policy import escalation_policy
from policy.safety_policy import SAFETY_POLICY_VERSION, detect_pii_or_secrets
from schemas.decisions import AlertDraft, SafetyReview
from schemas.structured import ValidationFailureFallback, safe_structured
from tools.alert_tools import deliver_alert, get_channel_limits, get_shelter_capacity
from tools.escalation_tools import create_escalation
from tools.intake_tools import CONFIGURED_LANGUAGES

__all__ = [
    "ALERT_AGENT_ID",
    "ALERT_SYSTEM_PROMPT",
    "build_alert_agent",
    "render_body",
    "pick_nearest_shelter_text",
    "compose_incident_alerts",
]

ALERT_AGENT_ID = "alert_agent"

ALERT_SYSTEM_PROMPT = (
    "You are Alert_Agent for the Kollidam Ward-7 Neighbourhood Flood Committee. "
    "You compose ONE short recommended-action sentence, in the requested language, for a "
    "flood alert of the stated severity band, for the stated channel and character budget. "
    "State exactly one concrete action the recipient should take right now (e.g. move to "
    "higher ground, avoid a named crossing, prepare to evacuate). Never invent a shelter "
    "name, an occupant count, or a deadline not given to you in the prompt — those fields "
    "are supplied separately and are not yours to compose. Keep your sentence well within "
    "the stated character budget; if asked to shorten a previous sentence, produce a strictly "
    "shorter one that keeps the same core instruction."
)


def build_alert_agent(model: Any | None = None) -> Agent:
    """Construct a fresh ``Alert_Agent`` (Req 4.12, 4.13).

    No ``session_manager`` is attached — every ``Incident_Graph`` member
    agent, including this one, is built fresh per invocation (Req 4.13).
    Registers the full ``HARNESS_HOOKS`` set (Req 16) so approval-gate,
    call-cap, spend-cap, notification-cap, and audit enforcement apply to
    this agent's tool calls exactly as they do for every other agent role.

    Args:
        model: An already-constructed Strands model provider to use instead
            of building a fresh ``BedrockModel`` from ``agents.config``.
            Primarily a test seam; production callers omit this.

    Returns:
        A new ``Agent`` instance with ``agent_id="alert_agent"``,
        ``ALERT_SYSTEM_PROMPT``, the two read-only alert tools
        (``get_channel_limits``, ``get_shelter_capacity``), and the shared
        harness hooks.
    """
    if model is None:
        from strands.models import BedrockModel

        model = BedrockModel(
            model_id=get_model_id_for_role("alert_agent"),
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            temperature=0.2,
            max_tokens=1024,
        )
    return Agent(
        agent_id=ALERT_AGENT_ID,
        name=ALERT_AGENT_ID,
        system_prompt=ALERT_SYSTEM_PROMPT,
        model=model,
        tools=[get_channel_limits, get_shelter_capacity],
        hooks=list(HARNESS_HOOKS),
    )


# ---------------------------------------------------------------------------
# LLM call schema and default composer (see module docstring, decision 1)
# ---------------------------------------------------------------------------


class _ComposedContent(BaseModel):
    """Local structured-output schema for one channel/language compose call.

    Deliberately smaller than ``schemas.decisions.AlertDraft`` — see the
    module docstring's decision 1.
    """

    recommended_action: str = Field(max_length=800, description="One sentence recommended action.")
    confidence: float = Field(ge=0.0, le=1.0)


def _default_compose_variant(
    agent: Agent,
    *,
    incident_id: str,
    severity_band: str,
    channel: str,
    language: str,
    affected_areas: list[str],
    shelter_text: str,
    char_limit: int,
    attempt: int,
) -> tuple[str, float]:
    """Ask ``agent`` for one recommended-action sentence, tightening the
    character budget on each recomposition attempt (Req 7.4, 7.9).

    Returns:
        ``(recommended_action, confidence)``. ``recommended_action`` is the
        empty string when structured-output validation failed (never
        raises) — the caller treats an empty string as a failed attempt.
    """
    # Reserve room for the rendered template's fixed text (severity label,
    # affected-area list, shelter line, validity window) so the model's own
    # sentence, once embedded, still fits char_limit. Tightened by 20% per
    # recomposition attempt.
    reserved_for_template = 60 + len(", ".join(affected_areas)) + len(shelter_text)
    budget = max(20, (char_limit - reserved_for_template) - attempt * 20)

    prompt = (
        f"Severity band: {severity_band}. Channel: {channel}. Language: {language}. "
        f"Affected areas: {', '.join(affected_areas) or 'the ward'}. "
        f"Nearest shelter guidance already decided: {shelter_text}. "
        f"Write the recommended-action sentence in at most {budget} characters."
        + (f" This is recomposition attempt {attempt}; make it strictly shorter than before." if attempt else "")
    )

    result = safe_structured(
        lambda: agent(prompt, structured_output_model=_ComposedContent).structured_output,
        _ComposedContent,
    )
    if isinstance(result, ValidationFailureFallback):
        return "", 0.0
    return result.recommended_action, result.confidence


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def pick_nearest_shelter_text(shelter_capacity_result: dict[str, Any]) -> str:
    """Pick the shelter text for an ``AlertDraft.nearest_shelter`` field
    (Req 7.1, 7.2) from ``tools.alert_tools.get_shelter_capacity``'s result.

    Returns the name of whichever returned shelter has the highest
    ``available_capacity`` when at least one has ``available_capacity > 0``;
    otherwise returns the configured no-capacity guidance text (Req 7.2).
    """
    shelters = [s for s in shelter_capacity_result.get("shelters", []) if s.get("available_capacity", 0) > 0]
    if not shelters:
        return shelter_capacity_result.get("no_capacity_guidance", "")
    best = max(shelters, key=lambda s: s["available_capacity"])
    return best["name"]


def render_body(draft: AlertDraft, severity_band: str) -> str:
    """Deterministically render one outbound message body from an
    ``AlertDraft`` (Req 7.4's channel character limit applies to this
    rendered string, not to any single field in isolation — see module
    docstring decision 3).
    """
    areas = ", ".join(draft.affected_areas) if draft.affected_areas else "the ward"
    return (
        f"{severity_band} ALERT for {areas}. {draft.recommended_action} "
        f"Shelter: {draft.nearest_shelter}. Valid {draft.validity_start} to {draft.validity_end}."
    )


def _compose_and_fit_variant(
    agent: Agent,
    *,
    incident_id: str,
    severity_band: str,
    channel: str,
    language: str,
    affected_areas: list[str],
    shelter_text: str,
    validity_start: str,
    validity_end: str,
    audience_count: int,
    char_limit: int,
    recomposition_attempt_count: int,
    compose_variant_fn: Callable[..., tuple[str, float]],
) -> tuple[AlertDraft | None, str | None, str | None]:
    """Compose one channel/language ``AlertDraft`` and fit it under
    ``char_limit``, retrying up to ``recomposition_attempt_count`` times
    (Req 7.4, 7.9).

    Returns:
        ``(draft, body, withheld_reason)``. On success, ``draft``/``body``
        are set and ``withheld_reason`` is ``None``. On failure (composition
        never produced content, or the rendered body never fit under
        ``char_limit`` within the permitted attempts), ``draft``/``body``
        are ``None`` and ``withheld_reason`` names the cause (Req 7.9).
    """
    any_content_produced = False
    for attempt in range(recomposition_attempt_count + 1):
        recommended_action, confidence = compose_variant_fn(
            agent,
            incident_id=incident_id,
            severity_band=severity_band,
            channel=channel,
            language=language,
            affected_areas=affected_areas,
            shelter_text=shelter_text,
            char_limit=char_limit,
            attempt=attempt,
        )
        if not recommended_action:
            continue
        any_content_produced = True

        draft = AlertDraft(
            channel=channel,
            language=language,
            affected_areas=list(affected_areas),
            recommended_action=recommended_action,
            nearest_shelter=shelter_text,
            validity_start=validity_start,
            validity_end=validity_end,
            confidence=confidence,
            action="execute" if confidence >= escalation_policy.CONFIDENCE_FLOOR.value else "ask_human",
            is_irreversible_action=(severity_band == "EVACUATE"),
            audience_count=audience_count,
        )
        body = render_body(draft, severity_band)
        if len(body) <= char_limit:
            return draft, body, None

    if not any_content_produced:
        return None, None, "composition_failed"
    return None, None, "exceeds_channel_limit_after_recomposition_attempts"


def _submit_for_safety_review(reviewed_content_id: str, body: str) -> SafetyReview:
    """Submit one composed body to Safety_QA_Agent before delivery (Req
    7.5). See module docstring decision 4 for the interim fallback used
    while ``agents/safety_qa_agent.py`` (task 13.6) does not yet exist.
    """
    try:
        # TODO(13.6): once agents/safety_qa_agent.py exists, this becomes
        # the real call site: `from agents.safety_qa_agent import
        # review_content` and delegate to it, dropping the deterministic
        # fallback below entirely.
        from agents.safety_qa_agent import review_content  # type: ignore[import-not-found]

        return review_content(reviewed_content_id, body)
    except ImportError:
        violated = detect_pii_or_secrets(body)
        return SafetyReview(
            reviewed_content_id=reviewed_content_id,
            pass_indicator=not violated,
            violated_policy_ids=violated,
            suggested_revisions=(["remove the detected PII/secret pattern before sending"] if violated else []),
            policy_version=SAFETY_POLICY_VERSION,
        )


# ---------------------------------------------------------------------------
# compose_incident_alerts: the main orchestration entry point (Req 7)
# ---------------------------------------------------------------------------


def compose_incident_alerts(
    *,
    incident_id: str,
    severity_band: str,
    affected_areas: list[str],
    shelter_ids: list[str],
    validity_start: str,
    validity_end: str,
    audience_count: int,
    run_id: str,
    recipients_by_channel: dict[str, list[str]] | None = None,
    agent: Agent | None = None,
    compose_variant_fn: Callable[..., tuple[str, float]] | None = None,
) -> dict[str, Any]:
    """Compose, safety-review, and (unless withheld) deliver every
    channel/language ``AlertDraft`` for one incident (Req 7.1-7.11).

    Fan-out: one draft per (channel × language) combination, channels taken
    from ``tools.alert_tools.get_channel_limits()`` and languages from
    ``tools.intake_tools.CONFIGURED_LANGUAGES`` (Community_Language_
    Configuration, Req 7.3). Each draft is fit under its channel's
    character limit via recomposition (Req 7.4, 7.9); a draft that never
    fits, or never composes at all, is withheld and recorded, while every
    other variant continues (Req 7.9).

    Every drafted, fitted variant is submitted to Safety_QA_Agent (Req 7.5)
    before delivery is even considered; a failed review (Req 9.2)
    unconditionally withholds that variant and escalates the proposal.

    Delivery of every variant that passed safety review is withheld, and
    the release is escalated instead, whenever the incident severity band
    is EVACUATE or the delivery audience exceeds the configured
    mass-notification threshold (Req 7.6, 7.7) — decided exclusively by
    ``policy.escalation_policy.decide()`` (Design Question 3), never by
    this module's own judgement. Otherwise, every passing variant is
    delivered via ``tools.alert_tools.deliver_alert`` (idempotent per
    incident/channel/language/severity-band).

    Args:
        incident_id: The incident this alert release concerns.
        severity_band: One of ``"WATCH"``, ``"WARNING"``, ``"EVACUATE"``
            (Rule_Engine's output; Req 7.1 applies at WATCH or higher).
        affected_areas: Affected area names for every drafted variant.
        shelter_ids: Candidate shelter ids to check via
            ``get_shelter_capacity`` for the nearest-shelter field (Req
            7.1, 7.2).
        validity_start: ISO-8601 timestamp with time zone.
        validity_end: ISO-8601 timestamp with time zone.
        audience_count: The delivery audience recipient count for this
            release (Req 7.1, 7.6's mass-notification check).
        run_id: The current Incident_Graph run identifier, for audit
            correlation and harness per-run counters.
        recipients_by_channel: Delivery audience per channel (e.g.
            ``{"sms": ["+91...", ...], "email": [...]}> ``), used only when
            delivery is not withheld. Channels absent from this mapping (or
            omitted entirely) are composed and safety-reviewed but not
            delivered — see module docstring decision 2.
        agent: An already-constructed ``Alert_Agent`` to reuse. Defaults to
            a fresh ``build_alert_agent()`` call.
        compose_variant_fn: Test seam overriding the default LLM-backed
            composer (``_default_compose_variant``). Production callers
            omit this.

    Returns:
        {
          "ok": True,
          "incident_id": str,
          "status": "delivered" | "escalated" | "no_deliverable_drafts",
          "drafts": [{"channel": str, "language": str, "body": str,
                       "confidence": float}, ...],   # passed safety review
          "withheld": [{"channel": str, "language": str, "reason": str}, ...],
          "escalation": {"escalation_id": str, ...} | None,
          "delivery_results": [dict, ...],  # tools.alert_tools.deliver_alert results
        }
    """
    if compose_variant_fn is None:
        compose_variant_fn = _default_compose_variant
    if agent is None:
        agent = build_alert_agent()

    limits = get_channel_limits()
    channel_limits: dict[str, int] = limits["channel_character_limits"]
    recomposition_attempt_count: int = limits["recomposition_attempt_count"]

    shelter_result = get_shelter_capacity(shelter_ids)
    shelter_text = pick_nearest_shelter_text(shelter_result)
    if not shelter_result.get("any_capacity_available", False):
        append_audit_entry(
            run_id=run_id,
            tool_name="alert_agent.compose_incident_alerts",
            outcome="no_shelter_capacity_available",
            incident_id=incident_id,
        )

    withheld: list[dict[str, Any]] = []
    fitted: list[tuple[AlertDraft, str]] = []

    for channel, char_limit in sorted(channel_limits.items()):
        for language in sorted(CONFIGURED_LANGUAGES):
            draft, body, reason = _compose_and_fit_variant(
                agent,
                incident_id=incident_id,
                severity_band=severity_band,
                channel=channel,
                language=language,
                affected_areas=affected_areas,
                shelter_text=shelter_text,
                validity_start=validity_start,
                validity_end=validity_end,
                audience_count=audience_count,
                char_limit=char_limit,
                recomposition_attempt_count=recomposition_attempt_count,
                compose_variant_fn=compose_variant_fn,
            )
            if draft is None:
                withheld.append({"channel": channel, "language": language, "reason": reason})
                append_audit_entry(
                    run_id=run_id,
                    tool_name="alert_agent.compose_incident_alerts",
                    outcome=f"withheld: {reason}",
                    incident_id=incident_id,
                )
                continue
            fitted.append((draft, body))

    # Req 7.5: submit every fitted draft to Safety_QA_Agent before delivery.
    passed: list[tuple[AlertDraft, str]] = []
    for draft, body in fitted:
        content_id = f"{incident_id}:{draft.channel}:{draft.language}:{severity_band}"
        review = _submit_for_safety_review(content_id, body)
        append_audit_entry(
            run_id=run_id,
            tool_name="safety_qa_agent.review_content",
            outcome=f"pass={review.pass_indicator} violated={review.violated_policy_ids}",
            incident_id=incident_id,
        )
        if review.pass_indicator:
            passed.append((draft, body))
            continue

        # Req 9.2: a false pass indicator always withholds and escalates,
        # unconditionally -- never gated by policy.escalation_policy.decide().
        withheld.append({"channel": draft.channel, "language": draft.language, "reason": "failed_safety_review"})
        create_escalation(
            idempotency_key=f"safety_review:{content_id}",
            run_id=run_id,
            escalation_type="alert_release",
            decision_summary=(f"A {draft.language} {draft.channel} alert draft failed safety review.")[:200],
            reason="the proposed alert content violated a published safety policy",
            stakes=f"withheld {draft.channel}/{draft.language} variant for incident {incident_id}",
            options=[
                {"option_id": "revise", "label": "Ask ThunAI to revise and resubmit"},
                {"option_id": "hold", "label": "Hold; do not send this variant"},
            ],
            default_action=escalation_policy.DEFAULT_ACTION["alert_release"],
            response_deadline_minutes=escalation_policy.RESPONSE_DEADLINE_MINUTES.get(
                severity_band, escalation_policy.RESPONSE_DEADLINE_MINUTES["WATCH"]
            ).value,
            incident_id=incident_id,
            context={
                "violated_policy_ids": review.violated_policy_ids,
                "suggested_revisions": review.suggested_revisions,
                "channel": draft.channel,
                "language": draft.language,
            },
        )

    result: dict[str, Any] = {
        "ok": True,
        "incident_id": incident_id,
        "drafts": [
            {"channel": d.channel, "language": d.language, "body": b, "confidence": d.confidence}
            for d, b in passed
        ],
        "withheld": withheld,
        "escalation": None,
        "delivery_results": [],
    }

    if not passed:
        result["status"] = "no_deliverable_drafts"
        return result

    # Req 7.6, 7.7: EVACUATE band or mass audience -> escalate, withhold delivery.
    min_confidence = min(d.confidence for d, _ in passed)
    verdict = escalation_policy.decide(
        category="alert_release",
        confidence=min_confidence,
        is_irreversible_action=(severity_band == "EVACUATE"),
        audience_count=audience_count,
        severity_band=severity_band,
    )

    if verdict["escalate"]:
        escalation = create_escalation(
            idempotency_key=f"alert_release:{incident_id}:{severity_band}",
            run_id=run_id,
            escalation_type="alert_release",
            decision_summary=(
                f"ThunAI drafted a {severity_band} alert for "
                f"{', '.join(affected_areas) or 'the ward'} reaching {audience_count} residents."
            )[:200],
            reason=verdict["reason"],
            stakes=f"{audience_count} residents; severity {severity_band}",
            options=[
                {"option_id": "send", "label": "Send now"},
                {"option_id": "show_draft", "label": "Show me the draft"},
                {"option_id": "hold", "label": "Hold"},
            ],
            default_action=verdict["default_action"] or escalation_policy.DEFAULT_ACTION["alert_release"],
            response_deadline_minutes=verdict["response_deadline_minutes"],
            incident_id=incident_id,
            context={"severity_band": severity_band, "audience_count": audience_count},
        )
        result["status"] = "escalated"
        result["escalation"] = escalation
        return result

    # Not withheld -- deliver every passing variant (Req 7.10, 7.11).
    delivery_results = []
    for draft, body in passed:
        recipients = (recipients_by_channel or {}).get(draft.channel, [])
        delivery_results.append(
            deliver_alert(
                incident_id=incident_id,
                channel=draft.channel,
                language=draft.language,
                severity_band=severity_band,
                recipients=recipients,
                body=body,
                subject=(f"{severity_band} flood alert" if draft.channel == "email" else ""),
            )
        )
    result["status"] = "delivered"
    result["delivery_results"] = delivery_results
    return result
