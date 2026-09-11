"""``Safety_QA_Agent``: adversarial safety/quality review before release (Req 9,
design.md §3.1 (Safety_QA_Agent), §3.3/§3.4's ``safety_policy``/``escalation_policy``
integration, Design Question 3).

``Safety_QA_Agent`` reviews a proposed outbound communication (an
``AlertDraft`` variant's rendered content) or a proposed ``Irreversible_Action``
against the active ``policy/safety_policy.py`` safety policy **before**
release/execution, and produces a typed ``schemas.decisions.SafetyReview``
(Req 9.1, 9.6). It is deliberately its own agent, with its own ``agent_id``
and its own system prompt, never folded into ``Alert_Agent`` or
``Dispatch_Agent`` — design.md §3.1's own table states the reason plainly:
"Must be structurally independent of the producing agent (Alert_Agent,
Dispatch_Agent) — a self-review by the same agent that wrote the content is
not a credible safety gate." This module never reviews content it itself
produced; the caller (``Alert_Agent``/``Dispatch_Agent`` via
``Incident_Graph``'s ``alert``/``dispatch`` -> ``safety_qa`` edges, design.md
§3.2's worked ``build_incident_graph()``) always submits *someone else's*
proposed content to this agent for review.

Verification note (mandatory per tasks.md 13.6, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (``pip show
    strands-agents``, matching every other already-implemented agent/tool
    module's own verification note in this codebase — ``agents/config.py``,
    ``agents/rule_engine.py``, ``harness/hooks.py``). Checked directly
    against the installed package (mirroring those modules' own precedent
    of reading installed source when a signature needs exact confirmation
    rather than trusting design.md's illustrative pseudocode verbatim):

    - ``strands.Agent.__init__``'s current keyword-only parameters include
      ``agent_id: str | None``, ``structured_output_model: type[BaseModel]
      | None``, ``hooks: list[HookProvider | HookCallback] | None``,
      ``interventions: list[InterventionHandler] | None``, and
      ``session_manager: SessionManager | None`` — all present and typed
      exactly as this module uses them (confirmed via
      ``inspect.signature(Agent.__init__)``). No member of
      ``Incident_Graph`` (this agent included) is constructed with a
      ``session_manager`` (design.md §3.1's own table: "every member agent
      of Incident_Graph [is] constructed with no attached session
      manager"), matching Req 4.13 exactly — this module's
      ``build_safety_qa_agent()`` therefore never passes
      ``session_manager=``.
    - ``strands.Agent.__call__``'s current keyword-only ``structured_output_model``
      parameter (confirmed via ``inspect.signature(Agent.__call__)``)
      overrides the constructor-level default per invocation — this module
      constructs the agent with ``structured_output_model=SafetyReview`` at
      build time (matching every other specialist-agent convention this
      codebase's already-implemented modules assume, e.g.
      ``schemas/decisions.py``'s and ``schemas/structured.py``'s own
      verification notes) rather than passing it again at call time, since
      ``Safety_QA_Agent`` only ever produces one output type.
    - ``strands.models.bedrock.BedrockModel.__init__`` accepts
      ``model_id`` (via ``**model_config: Unpack[BedrockConfig]``),
      ``region_name``, ``temperature``, and ``max_tokens`` as documented,
      current constructor parameters (confirmed via
      ``inspect.signature(BedrockModel.__init__)`` and
      ``BedrockModel.BedrockConfig.__annotations__``) — no deviation from
      every other already-implemented module's assumed
      ``BedrockModel(model_id=..., region_name=..., temperature=...,
      max_tokens=...)`` shape (``agents/config.py``'s own verification
      note already confirmed this exact shape; this module reuses it
      unchanged). ``BedrockConfig`` also declares ``guardrail_id`` /
      ``guardrail_version`` fields (confirmed present on the installed
      SDK), the mechanism the hackathon guidelines' §3.4 and design.md's
      Req 9.4 ("apply the configured content guardrail to every model
      invocation that processes resident-supplied text") point at; no
      Bedrock Guardrail resource has been provisioned anywhere in this
      repository yet (no CDK stack, no env var naming a guardrail id exists
      as of this task — ``grep`` for ``guardrail``/``Guardrail`` across the
      entire tree, this module's own new code excepted, returns zero
      matches), so ``build_safety_qa_agent()`` reads the not-yet-provisioned
      ``THUNAI_GUARDRAIL_ID``/``THUNAI_GUARDRAIL_VERSION`` env vars and
      passes them through to ``BedrockModel`` only when both are set
      (leaving Bedrock's own guardrail parameters unset, i.e. no guardrail
      applied, when they are absent) — this wires the mechanism correctly
      for the day a guardrail resource exists (a later CDK/observability
      task, out of this task's scope) without inventing a fake identifier
      today.
    - ``strands.vended_interventions.hitl.HumanInTheLoop.__init__`` accepts
      keyword-only ``allowed_tools: list[str] | None`` and
      ``classifier: bool | LLMClassifierConfig | HumanInTheLoopClassifier |
      None`` (confirmed via ``inspect.signature``), and
      ``HumanInTheLoopClassifier`` (confirmed via
      ``inspect.getsource(HumanInTheLoopClassifier)``) is a
      ``@runtime_checkable`` ``Protocol`` requiring exactly
      ``def __call__(self, event: BeforeToolCallEvent, **kwargs) ->
      ClassifierResult | Awaitable[ClassifierResult]``, and
      ``ClassifierResult`` (confirmed via
      ``inspect.getsource(ClassifierResult)``) is
      ``@dataclass class ClassifierResult: requires_human_in_the_loop: bool;
      reason: str | None = None`` — matching design.md's Design Question 3
      worked example (``ClassifierResult(requires_human_in_the_loop=...,
      reason=...)``) exactly; no deviation. This module builds
      ``Safety_QA_Agent`` with **no write tools at all** (see the "No write
      tools" section below), so it registers **no** ``HumanInTheLoop``
      intervention of its own — the deterministic escalation gate this
      module's outputs feed into (``policy.escalation_policy.decide()``,
      via Req 9.2/9.6/9.7's "withhold... escalate... through
      Escalation_Service") lives entirely in the *caller*
      (``Alert_Agent``/``Dispatch_Agent``/``Incident_Graph``'s own
      driver), exactly mirroring how ``tools/escalation_tools.py
      ::create_escalation`` is itself "called only by the deterministic
      escalation gate... never by a model's tool-call loop" (that module's
      own docstring) rather than being a tool any agent (including this
      one) holds.

Deviation from design.md §3.4's Design Question 3 worked example, recorded
here per the "docs win" instruction: design.md's
``deterministic_classifier(tool_use, agent_output)`` sketch takes two
positional arguments, but the installed ``HumanInTheLoopClassifier``
protocol's actual, confirmed signature is
``__call__(self, event: BeforeToolCallEvent, **kwargs)`` — a single
``BeforeToolCallEvent`` object, not a separate ``tool_use``/``agent_output``
pair. This has **no effect on this module**, since ``Safety_QA_Agent`` holds
no write tools and therefore needs no classifier of its own; it is recorded
here only because this module is the natural place to note it for whichever
future task (``agents/dispatch_agent.py``/``agents/alert_agent.py``'s own
write-tool-gating classifier, already implemented per those modules'
"verify first" notes referenced above) implements the actual
``deterministic_classifier`` callable design.md sketches — a future reader
of *this* module should not copy design.md's two-argument shape verbatim.

No-write-tools design decision, stated explicitly (Req 9's own acceptance
criteria never name a tool ``Safety_QA_Agent`` must call; design.md §3.1's
table describes its job purely as "review... against policy," never as
"deliver" or "execute"): ``Safety_QA_Agent`` is built with **zero** tools.
It receives the proposed content/action as plain text in its prompt (the
content to review, plus enough context — e.g. the resident record fields
Req 9.3's name/address check needs to cross-reference against, when the
caller has them available) and returns a ``SafetyReview`` purely from that
context and this module's own deterministic PII/secret pre-scan (see below)
— it never fetches anything itself, and it never releases/executes anything
itself (Req 9.2/9.6/9.7's "withhold... execution" is enforced entirely by
the caller reading ``pass_indicator``/the escalation gate, never by this
agent taking an action). This mirrors ``Knowledge_Agent``'s own "always
cite or refuse" posture design.md contrasts against every operational
agent's write-tool-holding posture, applied here to "always ground the
verdict in a real, checkable policy match, never invent one."

Deterministic pre-scan + model review, division of labour (Req 9.3, 9.6):
``policy.safety_policy.detect_pii_or_secrets()`` (already implemented,
task 3.3) is the **authoritative, deterministic** source for the four
regex-detectable violation categories it covers
(``resident_contact_leak``, ``resident_id_leak``, ``secret_leak`` — see
that module's own "Detection scope, stated honestly" docstring section:
it explicitly does NOT attempt ``resident_name_leak``/
``resident_address_leak`` detection, since neither has a reliable regex
shape). This module therefore:

1. Always runs ``detect_pii_or_secrets()`` over the reviewed content
   itself, deterministically, with **zero model involvement** for this
   half of the check — Req 9.3's contact-number check in particular is
   *fully* covered by this deterministic pass (an Indian mobile number or
   an email address is exactly what ``detect_pii_or_secrets()`` already
   detects), so a model hallucinating "no violation" can never override a
   real regex match: any policy id this pass finds is unconditionally
   included in the returned ``SafetyReview.violated_policy_ids``,
   regardless of what the model itself concludes.
2. Additionally cross-references the reviewed content against a supplied
   resident record's own ``resident_name``/``resident_address`` field
   values (an exact-substring, case-insensitive check — deterministic, not
   model-based either) for the two categories
   ``policy.safety_policy.py``'s own docstring explicitly defers to "task
   13.6's own implementation, which has the resident record available and
   this module does not." This closes Req 9.3's full three-category
   requirement (name, contact, address) deterministically wherever the
   caller supplies the resident record; when no resident record is
   supplied (content with no single, identifiable resident behind it, e.g.
   a mass alert with no specific occupant named), only the
   name/address *presence-of-a-name-like-token* check cannot run — the
   contact/id/secret regex pass still runs unconditionally either way, and
   this is documented explicitly on ``review_content``'s own docstring
   rather than left as a silent gap.
3. Delegates only the qualitative safety/quality judgement genuinely
   requiring a model — is the tone appropriate, is the recommended action
   safe/sensible, are there policy violations *design.md itself*
   anticipates a model needs to catch beyond the four regex-detectable
   categories (design.md's own Cost Model table names this exact framing
   for ``safety_qa_agent``: "policy-check classification against a fixed
   rubric") — to the underlying ``Agent`` invocation, whose
   ``structured_output_model=SafetyReview`` result is then **merged** with
   the deterministic pass's findings (union of violated policy ids; a
   deterministically-found violation always forces ``pass_indicator=False``
   regardless of what the model itself set that field to).

This two-layer design is what makes ``pass_indicator`` in the returned
``SafetyReview`` a genuinely trustworthy "false pass" signal for Req 9.2's
"IF a review result... carries a false pass indicator" withholding
requirement: the deterministic layer cannot be talked out of flagging a
real regex match by anything the model says, closing the most obvious way
a "safety reviewer" agent could itself be unreliable.
"""

from __future__ import annotations

import os
from typing import Any, Final

from strands import Agent
from strands.models.bedrock import BedrockModel

from agents.config import get_model_id_for_role
from harness.hooks import HARNESS_HOOKS
from policy.safety_policy import SAFETY_POLICY_VERSION, detect_pii_or_secrets
from schemas.decisions import SafetyReview
from schemas.structured import ValidationFailureFallback, safe_structured

__all__ = [
    "AGENT_ID",
    "SYSTEM_PROMPT",
    "build_safety_qa_agent",
    "cross_reference_resident_record",
    "review_content",
]

# ---------------------------------------------------------------------------
# Agent identity (Req 4.12: unique agent_id and a system prompt distinct
# from every other agent role's).
# ---------------------------------------------------------------------------

AGENT_ID: Final[str] = "safety_qa_agent"

SYSTEM_PROMPT: Final[str] = (
    "You are Safety_QA_Agent for ThunAI, a flood-response coordination system for the "
    "Kollidam Ward-7 Neighbourhood Flood Committee. Your ONLY job is to adversarially "
    "review a proposed outbound communication or a proposed irreversible action against "
    "the published safety policy, before it is released or executed. You did not write "
    "the content you are reviewing and you must never assume it is safe just because it "
    "reads fluently or persuasively — a reviewer that trusts the author is not a safety "
    "gate.\n\n"
    "You will be given: (1) the content or action proposed for review, (2) the applied "
    "safety policy version, (3) a list of policy violations already found by a "
    "deterministic pre-scan (you must treat every one of these as certain and MUST include "
    "them in your own violated_policy_ids — never contradict or omit a pre-scan finding), "
    "and (4) optionally, additional context such as the resident record the content "
    "concerns.\n\n"
    "Check specifically for: resident names, resident contact numbers, and resident "
    "dwelling addresses appearing in the content (policy ids resident_name_leak, "
    "resident_contact_leak, resident_address_leak); content that could cause harm if "
    "acted on literally (e.g. directing someone into a flooded area, contradicting the "
    "stated severity band, omitting a stated safety-critical instruction); and tone or "
    "wording inappropriate for a public safety notice to a distressed resident.\n\n"
    "Set pass_indicator to true ONLY when you find no violation of any kind, deterministic "
    "or your own. If you are at all unsure, set pass_indicator to false and name the "
    "specific concern — a cautious reviewer that occasionally over-flags is the correct "
    "failure mode; a reviewer that lets something through is not. For every violation you "
    "find, name the specific violated policy id and suggest a concrete revision that would "
    "resolve it. Never propose delivering or executing anything yourself — you only report "
    "the review result."
)

# ---------------------------------------------------------------------------
# Bedrock Guardrail (Req 9.4) — provisioned only when both env vars are set;
# see this module's "verify first" note. Absent by default in this task's
# scope (no CDK guardrail resource exists yet).
# ---------------------------------------------------------------------------

GUARDRAIL_ID_ENV_VAR: Final[str] = "THUNAI_GUARDRAIL_ID"
GUARDRAIL_VERSION_ENV_VAR: Final[str] = "THUNAI_GUARDRAIL_VERSION"


def _guardrail_kwargs() -> dict[str, str]:
    guardrail_id = os.environ.get(GUARDRAIL_ID_ENV_VAR)
    guardrail_version = os.environ.get(GUARDRAIL_VERSION_ENV_VAR)
    if guardrail_id and guardrail_version:
        return {"guardrail_id": guardrail_id, "guardrail_version": guardrail_version}
    return {}


# ---------------------------------------------------------------------------
# Deterministic resident-record cross-reference (Req 9.3's name/address
# half; see module docstring's "Deterministic pre-scan + model review"
# section, item 2).
# ---------------------------------------------------------------------------


def cross_reference_resident_record(content: str, resident_record: dict[str, Any] | None) -> list[str]:
    """Deterministically check `content` for a literal occurrence of the
    supplied resident record's own name/address values.

    Exact-substring, case-insensitive matching only (no fuzzy/NLP matching)
    — deliberately conservative, since a false negative here is closed by
    the model's own qualitative review (which also receives the resident
    record in its prompt context) but a false positive would incorrectly
    block a legitimate release; exact-substring matching against the
    resident's own recorded values is the narrowest check that still
    reliably catches "the drafted text literally contains this resident's
    stored name/address."

    Args:
        content: The proposed outbound content to check.
        resident_record: A dict with optional `"resident_name"` and/or
            `"resident_address"` string keys — the resident's own stored
            values, supplied by the caller when the content concerns one
            specific, identifiable resident. `None` (or an empty dict) when
            no single resident record applies (e.g. a mass alert with no
            named occupant), in which case this function returns `[]`
            unconditionally — it has nothing to cross-reference against.

    Returns:
        A sorted list of violated policy ids from
        `{"resident_name_leak", "resident_address_leak"}`, one per matched
        field. Empty when no resident record is supplied, or when neither
        stored value appears in `content`.
    """
    if not resident_record:
        return []

    violations: set[str] = set()
    content_lower = content.lower()

    name = resident_record.get("resident_name")
    if name and str(name).lower() in content_lower:
        violations.add("resident_name_leak")

    address = resident_record.get("resident_address")
    if address and str(address).lower() in content_lower:
        violations.add("resident_address_leak")

    return sorted(violations)


# ---------------------------------------------------------------------------
# build_safety_qa_agent() (Req 4.12, 4.13; design.md §3.1, §3.2's
# build_incident_graph()).
# ---------------------------------------------------------------------------


def build_safety_qa_agent() -> Agent:
    """Construct a fresh `Safety_QA_Agent` instance.

    Called once per graph-node invocation (design.md §3.1's table: "every
    member agent of Incident_Graph [is] constructed with no attached
    session manager" — a fresh `Agent` per invocation is cheap, per that
    same table's note elsewhere in this codebase, e.g.
    `agents/rule_engine.py`'s own precedent). Holds no tools at all (see
    module docstring's "No-write-tools design decision") and therefore no
    `interventions=` of its own — the escalation decision for a failed/
    uncertain review is made by the *caller*, via
    `policy.escalation_policy.decide()`, never by this agent.

    Returns:
        A new `strands.Agent` configured with `agent_id=AGENT_ID`,
        `SYSTEM_PROMPT`, `structured_output_model=SafetyReview`, the shared
        `harness.hooks.HARNESS_HOOKS`, and the low-cost model id resolved
        via `agents.config.get_model_id_for_role("safety_qa_agent")` (Req
        19.9, 20.6 — "policy-check classification against a fixed rubric").
    """
    model = BedrockModel(
        model_id=get_model_id_for_role(AGENT_ID),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        temperature=0.0,
        max_tokens=4096,
        **_guardrail_kwargs(),
    )
    return Agent(
        agent_id=AGENT_ID,
        name=AGENT_ID,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        structured_output_model=SafetyReview,
        hooks=list(HARNESS_HOOKS),
        callback_handler=None,
    )


# ---------------------------------------------------------------------------
# review_content(): the callable Incident_Graph's driver / Alert_Agent /
# Dispatch_Agent actually invoke (Req 9.1, 9.5, 9.6).
# ---------------------------------------------------------------------------


def _build_review_prompt(
    reviewed_content_id: str,
    content: str,
    deterministic_violations: list[str],
    resident_record: dict[str, Any] | None,
    is_irreversible_action: bool,
    action_description: str | None,
) -> str:
    lines = [
        f"Reviewed content id: {reviewed_content_id}",
        f"Applied safety policy version: {SAFETY_POLICY_VERSION}",
        "",
        "Content to review:",
        content,
        "",
    ]
    if is_irreversible_action:
        lines.append(
            "This proposal is an Irreversible_Action"
            + (f" ({action_description})" if action_description else "")
            + " and must be reviewed before execution."
        )
        lines.append("")
    if deterministic_violations:
        lines.append(
            "Deterministic pre-scan already found the following violated policy id(s), "
            "which you MUST include unchanged in violated_policy_ids: "
            + ", ".join(deterministic_violations)
        )
    else:
        lines.append("Deterministic pre-scan found no violation.")
    lines.append("")
    if resident_record:
        lines.append(f"Resident record context: {resident_record}")
    else:
        lines.append("No single resident record applies to this content.")
    return "\n".join(lines)


def review_content(
    agent: Agent,
    *,
    reviewed_content_id: str,
    content: str,
    resident_record: dict[str, Any] | None = None,
    is_irreversible_action: bool = False,
    action_description: str | None = None,
) -> SafetyReview | ValidationFailureFallback:
    """Review one proposed outbound communication or proposed action (Req 9.1, 9.6).

    Called by `Alert_Agent`/`Dispatch_Agent` (via `Incident_Graph`'s
    `alert -> safety_qa` / `dispatch -> safety_qa` edges, design.md §3.2)
    for content or an action `Safety_QA_Agent` itself did not produce
    (module docstring's structural-independence requirement) — never called
    by `Safety_QA_Agent` reviewing its own output, since it produces no
    content of its own to review.

    Runs the deterministic PII/secret pre-scan
    (`policy.safety_policy.detect_pii_or_secrets`) and the deterministic
    resident-record cross-reference (`cross_reference_resident_record`)
    first, then invokes `agent` (typically one built by
    `build_safety_qa_agent()`) with a prompt naming both sets of findings
    so the model cannot contradict them, and finally merges every
    deterministic finding into the returned review's `violated_policy_ids`
    and forces `pass_indicator=False` whenever any deterministic violation
    was found, regardless of what the model itself concluded (module
    docstring's "Deterministic pre-scan + model review" section).

    Args:
        agent: A `Safety_QA_Agent` instance, typically from
            `build_safety_qa_agent()`.
        reviewed_content_id: Identifier of the content or proposed action
            being reviewed (Req 9.1) — e.g. an `AlertDraft` variant's own
            composite key, or a dispatch decision's request id.
        content: The proposed outbound communication text, or a plain-text
            description of the proposed action, to review.
        resident_record: Optional dict with `"resident_name"`/
            `"resident_address"` keys for the one resident this content
            concerns, when applicable (Req 9.3). `None` when no single
            resident record applies.
        is_irreversible_action: Whether this review concerns a proposed
            `Irreversible_Action` rather than an outbound communication
            (Req 9.6). Included in the review prompt so the model applies
            the correct posture; does not itself change which policy ids
            are checked.
        action_description: A short, human-readable description of the
            proposed action, used only when `is_irreversible_action=True`,
            to give the model enough context to judge it. Ignored
            otherwise.

    Returns:
        On success: a `SafetyReview` with `reviewed_content_id`,
        `pass_indicator`, `violated_policy_ids` (deterministic findings
        unioned with the model's own), `suggested_revisions`,
        `policy_version=SAFETY_POLICY_VERSION`, and the model's own
        `confidence`/`action`/`category`/`is_irreversible_action` fields
        (each defaulted per `schemas.decisions.SafetyReview`'s own field
        defaults when the model omits them).

        On a structured-output validation failure (Req 10.11): a
        `schemas.structured.ValidationFailureFallback`
        (`action="ask_human"`, `confidence=0.0`,
        `category="validation_failure"`) — this is itself Req 9.7's
        "returns a review result without a pass indicator" case, since a
        `ValidationFailureFallback` carries no `pass_indicator` field at
        all; the caller must treat that the same way it treats an explicit
        `pass_indicator=False` (withhold and escalate).
    """
    deterministic_violations = sorted(
        set(detect_pii_or_secrets(content))
        | set(cross_reference_resident_record(content, resident_record))
    )

    prompt = _build_review_prompt(
        reviewed_content_id,
        content,
        deterministic_violations,
        resident_record,
        is_irreversible_action,
        action_description,
    )

    def _invoke() -> SafetyReview:
        result = agent(prompt, structured_output_model=SafetyReview)
        review = result.structured_output
        if review is None:
            # Defensive: structured_output_model was supplied but the SDK
            # returned no structured_output (should not happen given the
            # confirmed contract, but never silently fabricate a pass here).
            raise ValueError("Safety_QA_Agent invocation returned no structured_output")
        return review

    reviewed = safe_structured(_invoke, SafetyReview)

    if isinstance(reviewed, ValidationFailureFallback):
        return reviewed

    if deterministic_violations:
        merged_ids = sorted(set(reviewed.violated_policy_ids) | set(deterministic_violations))
        reviewed = reviewed.model_copy(
            update={
                "reviewed_content_id": reviewed_content_id,
                "violated_policy_ids": merged_ids,
                "pass_indicator": False,
                "policy_version": SAFETY_POLICY_VERSION,
            }
        )
    else:
        reviewed = reviewed.model_copy(
            update={
                "reviewed_content_id": reviewed_content_id,
                "policy_version": SAFETY_POLICY_VERSION,
            }
        )

    return reviewed
