"""``Intake_Agent``: converts one unstructured resident message (text or
uploaded hazard image) into a typed ``EmergencyRequest``, then applies
deterministic post-processing before any State_Store write (Req 5.1-5.10;
design.md §3.1 (Intake), §4.1).

Verification note (mandatory per tasks.md 13.3, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (``pip show
    strands-agents``, matching ``pyproject.toml``'s pin and every other
    already-verified module in this codebase — ``agents/config.py``,
    ``agents/rule_engine.py``, ``agents/dispatch_agent.py``,
    ``harness/hooks.py``). The following surfaces were re-confirmed against
    the *installed* package directly (``inspect.signature``/
    ``inspect.getsource`` on the live classes), not assumed from design.md's
    pseudocode alone, per the "verify first" instruction that docs/
    installed-code win when the two disagree:

    1. ``strands.Agent.__call__``'s ``prompt`` parameter accepts
       ``str | list[ContentBlock] | list[InterruptResponseContent] |
       list[Message] | None`` (confirmed via
       ``inspect.signature(Agent.__call__)``) — i.e. an image-carrying
       invocation is made by passing a plain ``list[ContentBlock]`` as the
       *prompt itself*, not through a separate ``image_reader`` `@tool`.
       ``strands.types.content.ContentBlock`` is a ``TypedDict`` (confirmed
       via ``inspect.getsource``) with an ``image: ImageContent`` key,
       alongside the already-familiar ``text: str`` key
       (``agents/rule_engine.py``'s own verification note already recorded
       ``ContentBlock``'s ``TypedDict``-not-class-constructor nature for
       its own, unrelated `text`-only usage; this module is the first to
       populate the ``image`` key).
    2. Cross-checked against the Strands docs multimodal example (Graph's
       own "Multi-Modal Input Support" page,
       ``docs/user-guide/concepts/multi-agent/graph.md``, fetched via the
       Ref documentation-search tool since no live Strands docs MCP server
       is configured in this environment): the documented image
       ``ContentBlock`` shape is
       ``{"image": {"format": "png", "source": {"bytes": image_bytes}}}``
       — a plain nested dict literal, matching ``ContentBlock``'s
       ``TypedDict`` nature exactly. This module builds exactly that shape
       in ``_build_intake_prompt()`` below.

    **Deviation from design.md's task-description hint** ("Verify `Agent` +
    `image_reader`/image content handling for uploaded hazard images"):
    design.md's own tasks.md wording suggested `image_reader` (a
    `strands-agents-tools` `@tool`) as one option, but that tool reads an
    image from a **local filesystem path** the model itself supplies as a
    tool argument (confirmed by ``tools/knowledge_tools.py``'s own
    already-recorded citation of that same multimodal example: "the
    [`image_reader`] tool provides the capability to analyze ... image
    content" from a **path**, e.g. ``output/whale.png``). Req 1.4's actual
    trigger shape is "an uploaded hazard image of 10 megabytes or less ...
    THE ThunAI_Platform SHALL start an intake run that includes image
    interpretation" — i.e. the image arrives as raw bytes on the inbound
    ingestion payload (an HTTP/API-Gateway upload, per design.md's
    Ingestion_Endpoint), not as a path on a shared filesystem the deployed
    AgentCore Runtime container could read from an out-of-band tool call.
    Passing the image directly as a ``ContentBlock`` in the invocation
    ``prompt`` (verified path 1/2 above) needs no filesystem intermediary
    at all and is the natural fit for "bytes already in hand from an HTTP
    upload" — this module therefore does **not** register
    ``strands_tools.image_reader`` as a tool on ``Intake_Agent``; the image
    is interpreted by the same single model call that also extracts every
    other ``EmergencyRequest`` field, exactly as Req 5.1 describes ("an
    inbound resident message arrives as text or as an uploaded hazard
    image, THE Intake_Agent SHALL produce exactly one typed emergency
    request" — one production path, one call, regardless of modality).

Conservative-extraction safety property (Req 5, glossary; design.md §3.1's
own justification for why `Intake_Agent` is a distinct role: "its prompt
must actively resist inferring facts not stated"): `SYSTEM_PROMPT` below
states this instruction directly and explicitly ("do not infer or guess any
fact the message does not state; use the literal 'unknown' value for
`occupant_count`/`mobility_assistance`/`medical_need` whenever the message
does not say"), and every deterministic post-processing rule in this module
(`postprocess_emergency_request`) is written to never *invent* a fact the
model did not already assert on the produced `EmergencyRequest` — every
override either (a) forces a stricter, safer outcome from information the
caller/model already supplied (IMMEDIATE banding, escalation), or (b)
withholds dispatch eligibility, never grants it based on a guess. No rule
below fabricates a location, occupant count, or category the model left as
`"unknown"`/absent.

Category assignment rules (Req 5.2) are intentionally left to the model's
own `structured_output_model=EmergencyRequest` production, not re-derived
deterministically here: "assign INFORMATION only where the message requests
no physical assistance" and "assign OTHER only where the message matches no
other category" are natural-language judgement calls about the message's
own content that this codebase's established division of labour (see
`agents/dispatch_agent.py`'s own precedent: ranking/candidate-search logic
that is genuinely mechanical is factored into a pure function, but message
*understanding* stays with the model) assigns to the model, constrained by
`SYSTEM_PROMPT`'s explicit statement of both rules verbatim. What this
module *does* deterministically re-check, per this task's own explicit
instruction ("validate/co-enforce deterministically after model output
where feasible"), is `validate_category_assignment()`: a defensive,
non-mutating check (see its own docstring) that flags — without silently
"fixing" — an `INFORMATION` category paired with a request the caller
states involves physical assistance (`mobility_assistance is True` or
`medical_need is True`), since that combination is a genuine self-
contradiction in the model's own output, not a judgement call a
deterministic rule could resolve better than the model itself for the
harder cases (RESCUE vs. OTHER, SUPPLIES vs. SHELTER, etc. are inherently
message-content judgement calls this module does not attempt to
second-guess).

Idempotency (Req 5.9): implemented via `tools.intake_tools.dedupe_check`
(already built, task 12.3) checked *before* the model is ever invoked
(`process_inbound_message()`'s first step) plus
`memory.state_store.idempotent_write` wrapping the actual persisted-outcome
write (mirroring `tools/incident_tools.py`'s / `tools/escalation_tools.py`'s
own established "check first, wrap the write" pattern) — a second call for
the same `message_id` short-circuits before any model call, any
`EmergencyRequest` creation, any escalation, and any Knowledge_Agent
routing, replaying the first call's recorded outcome exactly (Property 2's
generator already includes intake's own idempotency key prefix, per
`tools/intake_tools.py`'s own docstring).

Free-text storage-separation and PII exclusion (Req 5.10): implemented by
`process_inbound_message()` never placing the resident's raw `text` (or a
`sender_reference`/contact/exact-address value) into the typed
`EmergencyRequest`-derived dict that is exposed to `Resident_Status_Page` —
the raw text is instead passed to `memory.state_store.put_request` under a
distinct top-level key (`resident_free_text`) that this module's own
`RESIDENT_FACING_REQUEST_FIELDS` allowlist (mirroring
`schemas.entities.EscalationRecord.template_fields()`'s own "strict allow-
list, never a blocklist" precedent) never includes when building a
resident-facing projection via `resident_facing_fields()`.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Final

from strands import Agent
from strands.models import BedrockModel
from strands.types.content import ContentBlock

from agents.config import get_model_id_for_role
from harness.hooks import HARNESS_HOOKS
from memory import state_store
from policy import escalation_policy
from schemas.decisions import EmergencyRequest
from schemas.structured import ValidationFailureFallback, safe_structured
from tools import intake_tools
from tools.escalation_tools import create_escalation

__all__ = [
    "AGENT_ID",
    "SYSTEM_PROMPT",
    "RESIDENT_FACING_REQUEST_FIELDS",
    "build_intake_agent",
    "validate_category_assignment",
    "postprocess_emergency_request",
    "resident_facing_fields",
    "process_inbound_message",
]

AGENT_ID: Final[str] = "intake_agent"
"""Unique agent identifier for this role (Req 4.12)."""

SYSTEM_PROMPT: Final[str] = (
    "You are Intake_Agent for the Kollidam Ward-7 Neighbourhood Flood Committee. Your only "
    "job is to turn ONE resident message (text, or a photo the resident sent) into a typed "
    "EmergencyRequest.\n\n"
    "Do NOT infer or guess any fact the message does not state. If the message does not say "
    "how many people are affected, set occupant_count to the literal 'unknown', never a guessed "
    "number. If the message does not say whether someone needs help moving, set "
    "mobility_assistance to 'unknown', not false. If the message does not mention an injury or "
    "illness, set medical_need to 'unknown', not false. Guessing a fact the resident did not "
    "state is a safety failure, not a helpful shortcut.\n\n"
    "Call resolve_location once with the resident's own location wording to check whether it "
    "resolves to exactly one ward area or shelter. Call detect_language once on the message text. "
    "Use both tool results to inform source_language and location_reference, but write "
    "location_reference in the resident's own words when resolve_location returns anything other "
    "than exactly one candidate — do not silently pick one candidate for them.\n\n"
    "Assign exactly one request_category from RESCUE, MEDICAL, SHELTER, SUPPLIES, INFORMATION, "
    "OTHER. Assign INFORMATION only when the message requests no physical assistance at all (a "
    "question, not a plea for help). Assign OTHER only when the message matches none of the "
    "other five categories (e.g. it is off-topic for this flood-response service).\n\n"
    "Assign urgency_band from IMMEDIATE, URGENT, ROUTINE. Set IMMEDIATE whenever "
    "mobility_assistance or medical_need is true.\n\n"
    "Write rationale as one plain sentence, at most 200 characters, a non-technical coordinator "
    "can read. Set action to 'ask_human' when you are not confident in your own extraction; "
    "otherwise set action to 'execute' and set confidence to reflect how certain you actually are "
    "given what the message states."
)
"""Distinct from every other agent role's system prompt (Req 4.12)."""

RESIDENT_FACING_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "request_id",
        "request_category",
        "urgency_band",
        "state",
        "created_at",
        "updated_at",
    }
)
"""Strict allowlist of persisted-request fields safe to expose to
Resident_Status_Page (Req 5.10): excludes the resident name, contact
number, exact street address, `location_reference` (may itself contain a
street-level address a resident wrote), `occupant_count`,
`mobility_assistance`, `medical_need`, `resident_free_text`,
`location_candidates`, `confidence`, `rationale`, and every other field —
mirroring `schemas.entities.EscalationRecord.template_fields()`'s own
"a strict allowlist, never a blocklist" precedent (see that model's
docstring) so a newly added persisted-request field is excluded by default
until someone deliberately adds it here."""


def _region_name() -> str | None:
    return os.environ.get("AWS_REGION")


def build_intake_agent() -> Agent:
    """Construct a fresh `Intake_Agent` (Req 4.12, 4.13).

    Built fresh per invocation (matching `agents/dispatch_agent.py::
    build_dispatch_agent()`'s established convention for `Incident_Graph`
    member agents) with no `session_manager` (Req 4.13) and the shared
    `HARNESS_HOOKS` (Req 16) registered. `resolve_location` and
    `detect_language` are the only tools given to the model —
    `dedupe_check` is deliberately **not** one of them: idempotency
    (Req 5.9) must hold *before* the model is ever invoked (a model
    deciding whether to bother checking duplication is not a guarantee),
    so `process_inbound_message()` below calls `intake_tools.dedupe_check`
    itself as a plain Python call, the same "deterministic gate, not a
    model-callable tool" precedent `tools/escalation_tools.py::
    create_escalation` already documents for its own function.

    Returns:
        A `strands.Agent` configured with `agent_id`/`name` set to
        `AGENT_ID`, `system_prompt=SYSTEM_PROMPT`,
        `tools=[intake_tools.resolve_location, intake_tools.detect_language]`,
        `structured_output_model=EmergencyRequest`, and
        `hooks=list(HARNESS_HOOKS)`.
    """
    model = BedrockModel(
        model_id=get_model_id_for_role("intake_agent"),
        region_name=_region_name(),
        temperature=0.2,
        max_tokens=4096,
    )
    return Agent(
        model=model,
        name=AGENT_ID,
        agent_id=AGENT_ID,
        system_prompt=SYSTEM_PROMPT,
        tools=[intake_tools.resolve_location, intake_tools.detect_language],
        structured_output_model=EmergencyRequest,
        hooks=list(HARNESS_HOOKS),
    )


# ---------------------------------------------------------------------------
# Prompt construction (text and/or image), per this module's own
# verification note on ContentBlock's image key shape.
# ---------------------------------------------------------------------------


def build_intake_prompt(
    text: str | None,
    *,
    image_bytes: bytes | None = None,
    image_format: str = "jpeg",
) -> str | list[ContentBlock]:
    """Build the invocation prompt for one inbound resident message.

    Args:
        text: The resident's free text, or `None`/empty when the message
            is an image with no accompanying caption.
        image_bytes: Raw bytes of an uploaded hazard image (Req 1.4),
            or `None` for a text-only message.
        image_format: The image's format, one of the Bedrock-supported
            image formats (e.g. `"jpeg"`, `"png"`, `"gif"`, `"webp"`).
            Ignored when `image_bytes` is `None`.

    Returns:
        A plain `str` when `image_bytes` is `None` (text-only message,
        the common case); otherwise a `list[ContentBlock]` carrying a
        `text` block (when `text` is non-empty) followed by an `image`
        block built as `{"image": {"format": image_format, "source":
        {"bytes": image_bytes}}}` — the verified `ContentBlock` shape
        (see this module's docstring, verification note items 1-2).
    """
    if image_bytes is None:
        return text or ""

    blocks: list[ContentBlock] = []
    if text:
        blocks.append(ContentBlock(text=text))
    blocks.append(ContentBlock(image={"format": image_format, "source": {"bytes": image_bytes}}))
    return blocks


# ---------------------------------------------------------------------------
# validate_category_assignment (Req 5.2): defensive, non-mutating check.
# See module docstring's "Category assignment rules" section for why this
# is a flag-only check, not a silent-fix rule.
# ---------------------------------------------------------------------------


def validate_category_assignment(request: EmergencyRequest) -> list[str]:
    """Flag a self-contradictory category assignment on `request` (Req 5.2).

    This function never mutates `request` and never overrides
    `request_category` itself — see the module docstring's "Category
    assignment rules" section for why message-content category judgement
    stays with the model. It only detects one specific, unambiguous
    contradiction the model's own output can carry: `INFORMATION` (which
    Req 5.2 defines as "the message requests no physical assistance")
    paired with an indicator the same request already asserts *does*
    involve physical assistance.

    Args:
        request: The produced `EmergencyRequest` to check.

    Returns:
        A list of human-readable defect descriptions (for Audit_Ledger);
        empty when no contradiction is found.
    """
    defects: list[str] = []
    if request.request_category == "INFORMATION" and (
        request.mobility_assistance is True or request.medical_need is True
    ):
        defects.append(
            "request_category is INFORMATION but mobility_assistance/medical_need indicates "
            "physical assistance is needed (Req 5.2 contradiction)"
        )
    return defects


# ---------------------------------------------------------------------------
# postprocess_emergency_request (Req 5.3, 5.4, 5.5, 5.6, 5.7, 5.8):
# deterministic, applied AFTER the model call, per this task's own explicit
# instruction that these rules are never left to model judgement.
# ---------------------------------------------------------------------------


def postprocess_emergency_request(
    request: EmergencyRequest,
    *,
    location_candidate_count: int,
    location_candidates: list[str],
    is_outside_configured_languages: bool,
) -> EmergencyRequest:
    """Apply every deterministic post-processing rule to a produced
    `EmergencyRequest`, in order (Req 5.3, 5.6).

    Never invents a fact the model did not already assert — see the module
    docstring's "Conservative-extraction safety property" section. Returns
    a new `EmergencyRequest` (via `model_copy(update=...)`); never mutates
    `request` in place.

    Rules applied, in order:
        1. Req 5.3: force `urgency_band="IMMEDIATE"` whenever
           `mobility_assistance is True` or `medical_need is True`,
           regardless of what the model itself set.
        2. Req 5.6: when `location_candidate_count != 1`, record every
           passed candidate (already capped to 5 by
           `tools.intake_tools.resolve_location`, Req 5.6's own limit) onto
           `location_candidates` and force `dispatch_eligible=False` — an
           ambiguous location withholds dispatch eligibility unconditionally
           until a coordinator resolves the resulting escalation.

    Note: this function does **not** itself decide `dispatch_eligible` for
    the confidence-floor/always-ask split (Req 5.4/5.5) or the
    manual-triage split (Req 5.8) — those are `process_inbound_message()`'s
    job, since they need `policy.escalation_policy.decide()`'s own verdict
    (Req 5.4/5.5) and the caller-supplied `is_outside_configured_languages`
    signal (Req 5.8) respectively, both of which are function-call-level
    concerns rather than a pure field transformation on the model's typed
    output alone. This function's `is_outside_configured_languages`
    parameter is accepted and threaded through only so a single call site
    in `process_inbound_message()` can pass every signal through one
    consistent post-processing entry point; it currently affects no field
    mutation here (the manual-triage branch's "preserve the message,
    create no dispatch-eligible request" behaviour is enforced by
    `process_inbound_message()` short-circuiting *before* this function is
    even called on that path — see that function's docstring).

    Args:
        request: The model-produced `EmergencyRequest`.
        location_candidate_count: `resolve_location`'s own
            `candidate_count` for this message's stated location.
        location_candidates: `resolve_location`'s own ranked
            `location_reference` strings (already capped to 5), used
            verbatim as `EmergencyRequest.location_candidates` when
            `location_candidate_count != 1`.
        is_outside_configured_languages: `detect_language`'s own
            `is_outside_configured_languages` signal; see the "Note" above
            for why this parameter causes no mutation in this function.

    Returns:
        The post-processed `EmergencyRequest`.
    """
    updates: dict[str, Any] = {}

    # Rule 1 (Req 5.3).
    if request.mobility_assistance is True or request.medical_need is True:
        updates["urgency_band"] = "IMMEDIATE"

    # Rule 2 (Req 5.6).
    if location_candidate_count != 1:
        updates["location_candidates"] = list(location_candidates)[:5]
        updates["dispatch_eligible"] = False

    if not updates:
        return request
    return request.model_copy(update=updates)


# ---------------------------------------------------------------------------
# resident_facing_fields (Req 5.10)
# ---------------------------------------------------------------------------


def resident_facing_fields(persisted_request: dict[str, Any]) -> dict[str, Any]:
    """Project a persisted request dict down to the strict allowlist safe
    for `Resident_Status_Page` (Req 5.10).

    Args:
        persisted_request: The full persisted-request dict, as returned by
            `memory.state_store.get_request`/`put_request`.

    Returns:
        A new dict containing only the keys in
        `RESIDENT_FACING_REQUEST_FIELDS` that are present in
        `persisted_request` — never the resident name, contact number,
        exact street address, `location_reference`, `resident_free_text`,
        or any other field not on that allowlist.
    """
    return {key: value for key, value in persisted_request.items() if key in RESIDENT_FACING_REQUEST_FIELDS}


# ---------------------------------------------------------------------------
# process_inbound_message: the deterministic driver (Req 5.1, 5.4-5.9).
# ---------------------------------------------------------------------------

_ESCALATION_OPTIONS: Final[dict[str, list[dict[str, str]]]] = {
    "request_triage": [
        {"option_id": "approve", "label": "Approve as dispatch-eligible"},
        {"option_id": "reject", "label": "Reject this request"},
    ],
    "location_clarification": [
        {"option_id": "pick_candidate", "label": "Pick the correct location"},
        {"option_id": "contact_resident", "label": "Contact the resident for clarification"},
    ],
    "manual_triage": [
        {"option_id": "categorize", "label": "Manually categorise this message"},
        {"option_id": "discard", "label": "Discard (not a service request)"},
    ],
    "validation_failure": [
        {"option_id": "retry", "label": "Retry the intake decision"},
        {"option_id": "manual_triage", "label": "Send to manual triage"},
    ],
}
"""Between 2 and 5 options per escalation category (Req 11.1), matching
`agents/dispatch_agent.py::_ESCALATION_OPTIONS`'s own established shape."""


def _escalate(
    *,
    message_id: str,
    run_id: str,
    escalation_type: str,
    decision_summary: str,
    reason: str,
    stakes: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verdict = escalation_policy.decide(category=escalation_type, confidence=0.0)
    escalation = create_escalation(
        idempotency_key=f"escalate:{message_id}:{escalation_type}",
        run_id=run_id,
        escalation_type=escalation_type,
        decision_summary=decision_summary[:200],
        reason=reason,
        stakes=stakes,
        options=_ESCALATION_OPTIONS.get(escalation_type, _ESCALATION_OPTIONS["manual_triage"]),
        default_action=verdict.get("default_action") or "hold",
        response_deadline_minutes=verdict["response_deadline_minutes"],
        context=context or {},
    )
    return {"ok": True, "outcome": "escalated", "escalation_type": escalation_type, "escalation": escalation}


def process_inbound_message(
    *,
    message_id: str,
    run_id: str,
    text: str,
    produce_request: Callable[[], EmergencyRequest | ValidationFailureFallback],
    sender_reference: str | None = None,
) -> dict[str, Any]:
    """Drive one inbound resident message through intake, end to end
    (Req 5.1, 5.4-5.9).

    Never calls a language model itself — `produce_request` supplies the
    typed `EmergencyRequest` (or a `ValidationFailureFallback` on a
    structured-output validation failure), mirroring
    `agents/dispatch_agent.py::run_dispatch_decision`'s own
    `produce_decision` pattern so this function is fully unit-testable
    without a live Bedrock invocation. `intake_request()` below is the
    production entry point wiring a real `Intake_Agent` invocation into
    `produce_request`.

    Flow:
        1. Req 5.9: `intake_tools.dedupe_check(message_id)` first, before
           anything else. If already processed, return the first
           processing's outcome unchanged — no model call, no new
           `EmergencyRequest`, no additional escalation, no additional
           Knowledge_Agent routing.
        2. Call `produce_request()`.
        3. Structured-output validation failure -> escalate
           `"validation_failure"` (Req 10.11), never create a request.
        4. Req 5.7: if the produced category is `INFORMATION`, route to
           Knowledge_Agent (recorded here as a typed outcome; the actual
           Knowledge_Agent invocation is the caller's/orchestrator's
           responsibility per design.md §3.1's own "Coordinator_Orchestrator
           exposes Knowledge_Agent as a tool" — this function's job per
           Req 5.7 ends at "route the message ... and record the routing in
           Audit_Ledger", which `harness.hooks.AuditHook` performs on
           whichever tool call actually performs that routing downstream);
           create no dispatch-eligible request.
        5. Req 5.8: if `request_category` was not assigned at all (should
           not occur given `EmergencyRequest.request_category`'s closed
           `Literal`, kept as a defensive check for a `None`/empty value a
           future schema relaxation could introduce) or the message's
           detected language falls outside
           `intake_tools.CONFIGURED_LANGUAGES`, preserve the message and
           escalate `"manual_triage"`; create no dispatch-eligible request.
        6. Req 5.6: `intake_tools.resolve_location` on the request's own
           stated location wording. If it does not resolve to exactly one
           candidate, apply `postprocess_emergency_request`'s Rule 2 and
           escalate `"location_clarification"` (dispatch eligibility
           already withheld by that rule).
        7. Req 5.3: apply `postprocess_emergency_request`'s Rule 1
           (IMMEDIATE forcing) unconditionally.
        8. Req 5.4/5.5: call `policy.escalation_policy.decide()` on the
           (possibly IMMEDIATE-forced) request's own
           category/confidence/action/is_irreversible_action. Escalate
           `"request_triage"` on an ask-human verdict; otherwise persist
           the request as dispatch-eligible with no escalation.
        9. Every persisted outcome (steps 4-8) is wrapped in
           `memory.state_store.idempotent_write` keyed by
           `f"intake:{message_id}"` — the exact key
           `tools.intake_tools.dedupe_check` itself reads (see that
           module's `INTAKE_IDEMPOTENCY_KEY_PREFIX`) — so step 1's dedupe
           check and this function's own write share one idempotency
           record.

    Args:
        message_id: The inbound message identifier (Idempotency_Key basis,
            Req 5.4).
        run_id: The current run identifier, threaded into every
            `create_escalation` call.
        text: The resident's free text (may be empty for an image-only
            message with no caption). Stored separately from the typed
            request under `resident_free_text` (Req 5.10) — never placed
            in any resident-facing field.
        produce_request: A zero-argument callable returning the typed
            `EmergencyRequest` for this message (or a
            `ValidationFailureFallback` on validation failure). See
            `intake_request()` for the real, model-backed implementation.
        sender_reference: An opaque sender identifier (e.g. a phone number
            or channel-assigned id), stored alongside `resident_free_text`
            for the coordinator's own use — never placed in any
            resident-facing field (Req 5.10).

    Returns:
        On a duplicate message (Req 5.9): the first processing's recorded
        outcome, unchanged (whichever of the shapes below it was).

        On dispatch-eligible creation (Req 5.4):
        `{"ok": True, "outcome": "created", "request_id": str,
        "dispatch_eligible": True}`.

        On non-dispatch-eligible creation with escalation (Req 5.5):
        `{"ok": True, "outcome": "escalated", "escalation_type":
        "request_triage", "escalation": dict, "request_id": str}`.

        On ambiguous location (Req 5.6):
        `{"ok": True, "outcome": "escalated", "escalation_type":
        "location_clarification", "escalation": dict, "request_id": str}`.

        On INFORMATION routing (Req 5.7):
        `{"ok": True, "outcome": "routed_to_knowledge_agent",
        "request_id": None}`.

        On manual triage (Req 5.8, and on a structured-output validation
        failure):
        `{"ok": True, "outcome": "escalated", "escalation_type":
        "manual_triage" | "validation_failure", "escalation": dict,
        "request_id": None}`.
    """
    idempotency_key = f"{intake_tools.INTAKE_IDEMPOTENCY_KEY_PREFIX}:{message_id}"

    # Step 1 (Req 5.9): dedupe before the model is ever invoked.
    dedupe = intake_tools.dedupe_check(message_id)
    if dedupe.get("ok") and dedupe.get("is_duplicate"):
        existing = state_store._get_idempotency_record(idempotency_key)  # noqa: SLF001
        if existing is not None:
            return existing["recorded_outcome"]  # type: ignore[return-value]

    def _perform() -> dict[str, Any]:
        # Step 2.
        request = produce_request()

        # Step 3 (Req 10.11).
        if isinstance(request, ValidationFailureFallback):
            return _escalate(
                message_id=message_id,
                run_id=run_id,
                escalation_type="validation_failure",
                decision_summary=f"Intake decision for message {message_id} could not be validated.",
                reason=request.error_detail[:200],
                stakes=f"message {message_id} has no valid emergency request to act on",
            ) | {"request_id": None}

        # Step 4 (Req 5.7). Checked with validate_category_assignment() FIRST:
        # an INFORMATION category paired with a mobility/medical indicator
        # that already asserts physical assistance is needed is a genuine
        # self-contradiction in the model's own output (Req 5.2), not a
        # message this function should silently route to Knowledge_Agent
        # as if it were a pure information request. Such a contradiction is
        # therefore treated the same as Req 5.8's "no category could be
        # confidently assigned" case -- manual triage, never dispatch,
        # never silent routing.
        category_defects = validate_category_assignment(request)
        if request.request_category == "INFORMATION" and not category_defects:
            return {"ok": True, "outcome": "routed_to_knowledge_agent", "request_id": None}

        # Step 5 (Req 5.8).
        language_result = intake_tools.detect_language(text)
        outside_languages = bool(language_result.get("is_outside_configured_languages"))
        no_category = not request.request_category
        if no_category or outside_languages or category_defects:
            if no_category or category_defects:
                reason = "; ".join(category_defects) or "no request category could be assigned"
            else:
                reason = (
                    "detected language outside Community_Language_Configuration "
                    f"({language_result.get('detected_language')!r})"
                )
            return _escalate(
                message_id=message_id,
                run_id=run_id,
                escalation_type="manual_triage",
                decision_summary=f"Message {message_id} could not be categorised or triaged automatically.",
                reason=reason,
                stakes=f"resident message {message_id} is preserved and awaits manual triage",
                context={"preserved_message": text, "sender_reference": sender_reference},
            ) | {"request_id": None}

        # Step 6 (Req 5.6).
        location_result = intake_tools.resolve_location(request.location_reference)
        candidate_count = location_result.get("candidate_count", 1) if location_result.get("ok") else 1
        candidate_refs = (
            [c["location_reference"] for c in location_result.get("candidates", [])]
            if location_result.get("ok")
            else []
        )

        processed = postprocess_emergency_request(
            request,
            location_candidate_count=candidate_count,
            location_candidates=candidate_refs,
            is_outside_configured_languages=outside_languages,
        )

        request_id = message_id
        persisted = {
            "request_category": processed.request_category,
            "occupant_count": processed.occupant_count,
            "location_reference": processed.location_reference,
            "location_candidates": processed.location_candidates,
            "mobility_assistance": processed.mobility_assistance,
            "medical_need": processed.medical_need,
            "source_language": processed.source_language,
            "urgency_band": processed.urgency_band,
            "resident_free_text": text,
            "sender_reference": sender_reference,
            "created_at": state_store._now_iso(),  # noqa: SLF001
            "updated_at": state_store._now_iso(),  # noqa: SLF001
        }

        if candidate_count != 1:
            persisted["state"] = "NON_DISPATCH_ELIGIBLE"
            state_store.put_request(request_id, persisted)
            return _escalate(
                message_id=message_id,
                run_id=run_id,
                escalation_type="location_clarification",
                decision_summary=f"Location for message {message_id} did not resolve to exactly one place.",
                reason=(
                    f"resolve_location returned {candidate_count} candidate(s) for "
                    f"{processed.location_reference!r} (Req 5.6)"
                ),
                stakes=(
                    f"{processed.request_category} request for "
                    f"{processed.occupant_count} occupant(s) awaits a location match"
                ),
                context={
                    "location_candidates": processed.location_candidates,
                    "resident_wording": processed.location_reference,
                },
            ) | {"request_id": request_id}

        # Step 8 (Req 5.4, 5.5).
        verdict = escalation_policy.decide(
            category="routine_intake_request" if processed.action == "execute" else "request_triage",
            confidence=processed.confidence,
            action=processed.action,
            is_irreversible_action=processed.is_irreversible_action,
        )

        if verdict["escalate"]:
            persisted["state"] = "NON_DISPATCH_ELIGIBLE"
            state_store.put_request(request_id, persisted)
            return _escalate(
                message_id=message_id,
                run_id=run_id,
                escalation_type="request_triage",
                decision_summary=(processed.rationale or f"Intake decision for message {message_id}.")[:200],
                reason=verdict["reason"],
                stakes=(
                    f"{processed.request_category} request for "
                    f"{processed.occupant_count} occupant(s), urgency {processed.urgency_band}"
                ),
            ) | {"request_id": request_id}

        persisted["state"] = "DISPATCH_ELIGIBLE"
        state_store.put_request(request_id, persisted)
        return {
            "ok": True,
            "outcome": "created",
            "request_id": request_id,
            "dispatch_eligible": True,
        }

    return state_store.idempotent_write(idempotency_key, _perform)


def intake_request(
    *,
    message_id: str,
    run_id: str,
    text: str,
    sender_reference: str | None = None,
    image_bytes: bytes | None = None,
    image_format: str = "jpeg",
) -> dict[str, Any]:
    """Production entry point: build a real `Intake_Agent`, invoke it on
    `text`/`image_bytes` (Req 1.4, 5.1), and drive the result through
    `process_inbound_message()`.

    Args:
        message_id: The inbound message identifier (Idempotency_Key basis).
        run_id: The current run identifier.
        text: The resident's free text (may be empty for an image-only
            message).
        sender_reference: An opaque sender identifier, stored alongside
            `resident_free_text` only (Req 5.10).
        image_bytes: Raw bytes of an uploaded hazard image (Req 1.4), or
            `None` for a text-only message.
        image_format: The image's format (ignored when `image_bytes` is
            `None`).

    Returns:
        `process_inbound_message()`'s own return shape.
    """
    agent = build_intake_agent()
    prompt = build_intake_prompt(text, image_bytes=image_bytes, image_format=image_format)

    def _produce() -> EmergencyRequest | ValidationFailureFallback:
        return safe_structured(
            lambda: agent(prompt, structured_output_model=EmergencyRequest).structured_output,
            EmergencyRequest,
        )

    return process_inbound_message(
        message_id=message_id,
        run_id=run_id,
        text=text,
        produce_request=_produce,
        sender_reference=sender_reference,
    )
