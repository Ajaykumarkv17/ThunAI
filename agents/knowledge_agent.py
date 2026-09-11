"""``Knowledge_Agent``: answers a resident safety question from the curated
``Knowledge_Base`` with citations only, and refuses to answer ungrounded
(Req 8; design.md §3.1 (Knowledge), §3.4 Design Question 3).

Verification note (mandatory per tasks.md 13.7, "verify first"):
    Installed version confirmed: ``strands-agents==1.55.0`` (``pip show
    strands-agents``, matching ``pyproject.toml``'s pin and every other
    already-implemented agent module's own verification note in this
    codebase -- ``agents/config.py``, ``agents/intake_agent.py``,
    ``agents/dispatch_agent.py``, ``agents/alert_agent.py``,
    ``agents/safety_qa_agent.py``). No new Strands API surface is
    introduced by this module beyond what those modules already verified
    directly against the installed package:

    - ``strands.Agent.__init__``'s ``agent_id``, ``name``, ``system_prompt``,
      ``tools``, ``structured_output_model``, ``hooks``, and (deliberately
      omitted here, see below) ``session_manager`` keywords are unchanged
      from every other already-verified module's own citation of
      ``inspect.signature(Agent.__init__)``.
    - ``strands.models.bedrock.BedrockModel(model_id=..., region_name=...,
      temperature=..., max_tokens=...)`` is unchanged, per
      ``agents/config.py``'s own already-recorded verification.
    - ``agent(prompt, structured_output_model=SomeModel)`` returns the
      validated instance on ``.structured_output``; a validation failure
      raises ``strands.types.exceptions.StructuredOutputException`` --
      unchanged, per ``schemas/structured.py``'s own already-recorded
      verification; this module reuses ``safe_structured()`` unchanged
      rather than re-catching that exception itself.

    Session manager (Req 4.13): ``Knowledge_Agent`` is invoked both as an
    ``Incident_Graph``-adjacent specialist (via ``Intake_Agent``'s Req 5.7
    "route the message to Knowledge_Agent" routing point, design.md's own
    agent-role table) and as a tool wrapped by
    ``Coordinator_Orchestrator`` (design.md §3.1's role table: "The
    Coordinator_Orchestrator SHALL expose Monitor_Agent, Intake_Agent,
    Dispatch_Agent, and Knowledge_Agent as callable tools", Req 4.10).
    Req 4.13's own wording is unconditional either way: "attach
    Memory_Store to the orchestrating component only and ... construct
    every member agent of Incident_Graph with no attached session
    manager" -- ``Knowledge_Agent`` is never itself the orchestrating
    component (``Coordinator_Orchestrator`` is), so it never carries a
    session manager regardless of which caller invokes it. This module's
    ``build_knowledge_agent()`` therefore never passes ``session_manager=``,
    matching ``build_intake_agent()``/``build_dispatch_agent()``/
    ``build_alert_agent()``/``build_safety_qa_agent()``'s identical,
    unconditional precedent.

Grounded-answer / refuse-ungrounded design (Req 8.3, and design.md's own
role-table justification for why ``Knowledge_Agent`` is a distinct agent:
"must refuse to answer ungrounded"):
    The relevance-floor check (Req 8.6: "IF no retrieved passage carries a
    relevance score at or above the configured relevance threshold ...")
    is **not** left to the model's own judgement -- it is a plain,
    deterministic comparison against
    ``tools.knowledge_tools.retrieve_passages``'s own unfiltered ``score``
    list, performed by this module's ``select_grounding_passages()``
    *before* any model call is made. When no passage clears the floor (or
    retrieval itself failed/timed out, per ``retrieve_passages``'s own
    ``ok``/``no_relevant_passages_found`` fields), the model is never
    invoked at all -- there is nothing to compose an answer from, and Req
    8.3's "no statement that is not supported by a cited retrieved
    passage" is trivially unviolable when no answer is produced in the
    first place. This mirrors ``agents/dispatch_agent.py``'s Req 6.11
    shelter-capacity gate precedent ("checked before produce_decision() is
    ever called ... no dispatch decision is meaningful before the shelter
    question is resolved") applied to the identical "the deterministic
    precondition for a safe answer is missing" shape.

    When at least one passage clears the floor, the model IS invoked, but
    only with the cleared passages as context, and ``SYSTEM_PROMPT`` (below)
    instructs it, explicitly and repeatedly, to compose the answer using
    only sentences traceable to a passage's own text and to cite every
    passage it uses -- Req 8.3's "no statement... not supported by a cited
    retrieved passage" property is therefore enforced by construction (the
    model is never shown anything else to draw on: no general knowledge
    instruction, no tool other than the already-filtered passage set
    embedded in the prompt) plus this module's own
    ``citations_are_grounded_in_offered_passages()`` defensive check (a
    non-mutating validator -- see its own docstring -- that flags, for
    Audit_Ledger, any citation the model names that was not actually among
    the passages it was given; it does not itself correct the model's
    output, matching this codebase's established "flag, never silently
    fix" precedent for message-content judgement calls, e.g.
    ``agents/intake_agent.py::validate_category_assignment``).

No-answer / relevance-floor and retrieval-failure routing (Req 8.6, 8.7):
    Both branches escalate through ``policy.escalation_policy.decide()``
    (Design Question 3's single-authority requirement) against two new
    ``CATEGORY_TABLE`` categories added by this task,
    ``"knowledge_no_answer"`` (Req 8.6) and ``"knowledge_unavailable"``
    (Req 8.7) -- see ``policy/escalation_policy.py``'s own updated
    docstring/table entries for the rationale. Both are ``always_ask=True``
    (mirroring the existing ``"data_outage"`` entry's shape exactly:
    fabricating a safety answer with no grounded passage, or with
    retrieval itself broken, is never safe to auto-deliver regardless of
    confidence), so ``decide()`` always returns ``escalate=True`` for
    either category -- this module still routes every escalation *through*
    ``decide()`` rather than hand-rolling the "always ask" outcome, for the
    same "one authority" reason every other agent module in this codebase
    does (``agents/dispatch_agent.py``, ``agents/intake_agent.py``,
    ``agents/alert_agent.py`` all call ``decide()`` even on their own
    unconditionally-escalating branches).

Language handling (Req 8.4, 8.5): ``tools.intake_tools.detect_language`` and
``tools.intake_tools.CONFIGURED_LANGUAGES``/``DEFAULT_LANGUAGE`` (already
implemented, task 12.3) are reused directly rather than reimplementing
language detection a second time in this module -- ``Community_Language_
Configuration`` is a platform-wide concept (design.md's own glossary entry),
not something specific to Intake_Agent, and ``tools/intake_tools.py``'s own
docstring already declares it as the module of record for that constant.
When the detected language is outside ``CONFIGURED_LANGUAGES`` (or detection
returns ``"unknown"``), the answer is composed in ``DEFAULT_LANGUAGE`` and
``SYSTEM_PROMPT`` instructs the model to state, in that default language,
that the question's language was not recognised (Req 8.5's own wording) --
this is left to the model (it is composing the answer's prose either way)
rather than deterministically prepended, since the exact phrasing needs to
read naturally alongside the rest of the answer in whatever language is
being composed.

Advisory disclaimer (Req 8.8, 18.6): ``ADVISORY_CATEGORIES`` and
``ADVISORY_DISCLAIMER`` are declared in this module (no prior task declared
either as a Python constant anywhere in this codebase -- confirmed by
`grep`: no ``ADVISORY_DISCLAIMER``/``ADVISORY_CATEGOR`` symbol exists outside
markdown/requirements text before this task), following this codebase's
established "declare the missing constant where it is first needed, with an
env-override, mirroring the sibling convention already established
elsewhere" pattern (`tools/intake_tools.py`'s own "Design note --
Community_Language_Configuration not yet declared as code" precedent is the
direct model for this same judgement call). The disclaimer text itself
matches design.md's own repeatedly-used framing verbatim ("ThunAI is a
community coordination aid ... not an official emergency service", design.md
line 16 and Req 18.6/13's own "official emergency instructions take
precedence" wording) plus Req 18.6's explicit second half ("a referral to a
qualified authority", "exclude any directive instructing the resident to
perform a medical procedure or a structural repair" -- the latter half is a
model-prompt instruction, not something a fixed disclaimer string alone can
guarantee, so ``SYSTEM_PROMPT`` states it explicitly too). Whether an answer
concerns an advisory category is a message-content judgement call this
module leaves to the model's own ``requires_advisory_disclaimer: bool`` field
on the local LLM-call schema (mirroring ``agents/alert_agent.py``'s own
"structured-output schema used for the LLM call is smaller than the final
decision model" design decision, see that module's docstring decision 1) --
this module then deterministically prepends ``ADVISORY_DISCLAIMER`` (never
leaves disclaiming to the model's own prose) whenever that field is `True`,
so the disclaimer's exact wording is never itself model-generated (Req
8.8's "prefix that answer with the configured advisory disclaimer" --
"configured" implies a fixed string, not a paraphrase).

Citations (Req 8.2, 8.9): every ``Citation`` in a produced ``KnowledgeAnswer``
carries the passage's own ``source_id`` (the citation/identifier field
``retrieve_passages`` already returns per Req 8.2's "passage identifier")
and ``source_version`` (Req 8.2's "knowledge source version"), copied
verbatim from the grounding passages this module selected -- never
re-derived or guessed by the model, matching this module's own "the model
is only shown pre-filtered passages and is never the source of truth for
which passages exist" design. ``process_knowledge_question()``'s return
value and every audit entry it writes carry the full passage id/score list
(Req 8.9: "the retrieved passage identifiers with their relevance scores")
regardless of which branch executed.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Final

from pydantic import BaseModel, Field
from strands import Agent
from strands.models import BedrockModel

from agents.config import get_model_id_for_role
from harness.audit import append_audit_entry
from harness.hooks import HARNESS_HOOKS
from policy import escalation_policy
from schemas.structured import ValidationFailureFallback, safe_structured
from tools import intake_tools
from tools.escalation_tools import create_escalation
from tools.knowledge_tools import retrieve_passages

__all__ = [
    "AGENT_ID",
    "SYSTEM_PROMPT",
    "ADVISORY_CATEGORIES",
    "ADVISORY_DISCLAIMER_ENV_VAR",
    "ADVISORY_DISCLAIMER",
    "RELEVANCE_FLOOR_ENV_VAR",
    "DEFAULT_RELEVANCE_FLOOR",
    "Citation",
    "KnowledgeAnswer",
    "build_knowledge_agent",
    "select_grounding_passages",
    "citations_are_grounded_in_offered_passages",
    "answer_question",
    "process_knowledge_question",
]

# ---------------------------------------------------------------------------
# Agent identity (Req 4.12: unique agent_id and a system prompt distinct
# from every other agent role's).
# ---------------------------------------------------------------------------

AGENT_ID: Final[str] = "knowledge_agent"

SYSTEM_PROMPT: Final[str] = (
    "You are Knowledge_Agent for the Kollidam Ward-7 Neighbourhood Flood Committee. Your only "
    "job is to answer ONE resident safety question using ONLY the passages given to you below "
    "-- never your own general knowledge.\n\n"
    "Every sentence of your answer must be directly supported by at least one of the given "
    "passages. If a passage does not say something, do not say it either, even if you believe "
    "it to be true. Cite every passage you actually used by its source_id, exactly as given.\n\n"
    "Compose your answer in the requested answer_language. If question_language_recognised is "
    "false, say plainly, in answer_language, that the question's language was not recognised, "
    "before answering in answer_language anyway using the given passages.\n\n"
    "Set requires_advisory_disclaimer to true if the question or your answer concerns medical "
    "treatment, structural safety, or a legal obligation -- in that case, never instruct the "
    "resident to perform a medical procedure or a structural repair themselves; point them to a "
    "qualified authority instead. A fixed disclaimer will be added separately; do not write your "
    "own disclaimer text.\n\n"
    "Set confidence to reflect how well the given passages actually answer the question, not how "
    "fluent your answer sounds."
)
"""Distinct from every other agent role's system prompt (Req 4.12)."""

# ---------------------------------------------------------------------------
# Relevance floor (Req 8.6). No prior task declared this constant --
# mirrors tools/knowledge_tools.py's own env-override-with-in-code-default
# convention for the sibling max-passage-count/retrieval-timeout constants.
# ---------------------------------------------------------------------------

RELEVANCE_FLOOR_ENV_VAR: Final[str] = "THUNAI_KNOWLEDGE_RELEVANCE_FLOOR"

DEFAULT_RELEVANCE_FLOOR: Final[float] = 0.5
"""A passage below this relevance score is treated as not relevant enough
to ground an answer (Req 8.6). Chosen as the midpoint of a [0, 1] relevance
score range -- a passage that scores below the midpoint is, in relative
terms, a weaker match than a strong one, and the fixture-backed
`SAMPLE_PASSAGES` scores already established in `tests/unit/
test_knowledge_tools.py` (0.87 "relevant", 0.61 "borderline-relevant") sit
comfortably on the "clears the floor" side of this default, matching that
test fixture's own already-committed intent without requiring this module
to introduce a second, disagreeing fixture convention."""


def _relevance_floor() -> float:
    raw = os.environ.get(RELEVANCE_FLOOR_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_RELEVANCE_FLOOR
    return float(raw)


# ---------------------------------------------------------------------------
# Advisory categories + disclaimer (Req 8.8, 18.6). No prior task declared
# either constant as code -- see module docstring's "Advisory disclaimer"
# section.
# ---------------------------------------------------------------------------

ADVISORY_CATEGORIES: Final[tuple[str, ...]] = ("medical_treatment", "structural_safety", "legal_obligation")
"""The three advisory categories Req 8.8/18.6 name by name. Not itself a
model-facing enum -- the model reports `requires_advisory_disclaimer: bool`
(a single boolean covering all three, since Req 8.8's own wording treats
them as one trigger condition, "an answer concerns a configured advisory
category... prefix that answer with the configured advisory disclaimer");
this tuple exists so a future task can extend/inspect the named category
set without redefining it, and so this module's own tests/docstring can
name the three categories Req 8.8 requires by name."""

ADVISORY_DISCLAIMER_ENV_VAR: Final[str] = "THUNAI_ADVISORY_DISCLAIMER"

_DEFAULT_ADVISORY_DISCLAIMER: Final[str] = (
    "ThunAI is a community coordination aid for the Kollidam Ward-7 Neighbourhood Flood "
    "Committee, not an official emergency service, and this is not medical, structural, or "
    "legal advice. For medical treatment, structural safety, or a legal obligation, please "
    "contact a qualified authority; official emergency instructions always take precedence."
)
"""Matches design.md's own repeatedly-used framing ("ThunAI is a community
coordination aid... not an official emergency service") verbatim, plus Req
18.6's explicit "referral to a qualified authority" and "official emergency
instructions take precedence" wording."""

ADVISORY_DISCLAIMER: Final[str] = os.environ.get(ADVISORY_DISCLAIMER_ENV_VAR, "") or _DEFAULT_ADVISORY_DISCLAIMER
"""The configured advisory disclaimer (Req 8.8), overridable via
`THUNAI_ADVISORY_DISCLAIMER` -- read once at import time, matching
`tools/intake_tools.py::DEFAULT_LANGUAGE`'s identical
`os.environ.get(VAR, "") or default` pattern."""


def _region_name() -> str | None:
    return os.environ.get("AWS_REGION")


# ---------------------------------------------------------------------------
# Local structured-output schema for the LLM call (see module docstring's
# "Advisory disclaimer" section: mirrors agents/alert_agent.py's own
# decision 1, "the structured-output schema used for the LLM call is
# smaller than the final decision model").
# ---------------------------------------------------------------------------


class _ComposedAnswer(BaseModel):
    """Local structured-output schema for one grounded-answer compose call."""

    answer_text: str = Field(description="The composed answer, in answer_language.")
    cited_source_ids: list[str] = Field(
        default_factory=list,
        description="source_id of every passage actually used to compose answer_text.",
    )
    requires_advisory_disclaimer: bool = Field(
        description="True when the question/answer concerns medical treatment, structural "
        "safety, or a legal obligation (Req 8.8)."
    )
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Final typed decision model (Req 8.2, 8.3, 8.8; design.md §4.1's own
# "carries confidence + action + category + is_irreversible_action so
# decide() can gate it uniformly" convention, extended here with the
# grounded-answer-specific fields Req 8 needs).
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """One citation on a `KnowledgeAnswer` (Req 8.2: "each citation carrying
    the passage identifier and the knowledge source version")."""

    source_id: str
    source_version: str | None = None


class KnowledgeAnswer(BaseModel):
    """Typed output of `Knowledge_Agent` for one resident safety question
    (Req 8.1-8.9). Not one of `schemas/decisions.py`'s five models -- Req 8's
    own acceptance criteria never assign `Knowledge_Agent` a `category`
    from that module's shared vocabulary the way Dispatch/Alert/Monitor do;
    this module defines its own decision shape here (mirroring
    `schemas/structured.py::ValidationFailureFallback`'s own precedent of a
    small, purpose-built model living outside `schemas/decisions.py`) while
    still carrying `confidence`/`action`/`category`/`is_irreversible_action`
    so `policy.escalation_policy.decide()` can gate it exactly like every
    other decision model (Design Question 3's own uniform-gating
    requirement)."""

    answer_text: str | None = Field(
        default=None, description="The composed, cited answer text, or None on a no-answer/unavailable outcome."
    )
    citations: list[Citation] = Field(default_factory=list)
    answer_language: str = Field(description="The language the answer is composed in.")
    question_language_recognised: bool = Field(
        description="False when the question's language could not be detected or was outside "
        "Community_Language_Configuration (Req 8.5)."
    )
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    action: str = Field(default="execute", description="'execute' or 'ask_human'.")
    category: str = Field(
        default="knowledge_answer_grounded",
        description="'knowledge_answer_grounded', 'knowledge_no_answer', or 'knowledge_unavailable'.",
    )
    is_irreversible_action: bool = False


def build_knowledge_agent() -> Agent:
    """Construct a fresh `Knowledge_Agent` (Req 4.12, 4.13).

    Built fresh per invocation (this codebase's established convention for
    every specialist agent, whether or not it happens to be a formal
    `Incident_Graph` member -- see module docstring's "Session manager"
    section for why `Knowledge_Agent` never carries one regardless of
    caller) with the shared `HARNESS_HOOKS` (Req 16) registered. Holds NO
    tools -- unlike every operational agent in this codebase,
    `Knowledge_Agent`'s model call never calls `retrieve_passages` itself;
    `answer_question()` (below) calls it deterministically *before* the
    model is invoked, so the model only ever sees an already-filtered,
    already-scored passage set embedded directly in the prompt (module
    docstring's "Grounded-answer / refuse-ungrounded design" section) --
    mirroring `agents/safety_qa_agent.py`'s own "zero tools" precedent for
    the identical reason (a model that can independently decide when/how to
    call its own grounding tool is not a stronger grounding guarantee than
    one that is simply never given the choice).

    Returns:
        A `strands.Agent` configured with `agent_id=AGENT_ID`,
        `SYSTEM_PROMPT`, `structured_output_model=_ComposedAnswer`, the
        shared `HARNESS_HOOKS`, and the low-cost model id resolved via
        `agents.config.get_model_id_for_role("knowledge_agent")` (Req 19.9,
        20.6 -- design.md's Cost Model names `knowledge_agent` as a
        "grounded retrieval+summarisation" low-cost role).
    """
    model = BedrockModel(
        model_id=get_model_id_for_role(AGENT_ID),
        region_name=_region_name(),
        temperature=0.2,
        max_tokens=1024,
    )
    return Agent(
        agent_id=AGENT_ID,
        name=AGENT_ID,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        structured_output_model=_ComposedAnswer,
        hooks=list(HARNESS_HOOKS),
        callback_handler=None,
    )


# ---------------------------------------------------------------------------
# select_grounding_passages (Req 8.6): deterministic relevance-floor filter,
# applied BEFORE any model call.
# ---------------------------------------------------------------------------


def select_grounding_passages(
    passages: list[dict[str, Any]],
    *,
    relevance_floor: float | None = None,
) -> list[dict[str, Any]]:
    """Deterministically select the passages that clear the relevance floor
    (Req 8.6).

    Args:
        passages: The `passages` list from `tools.knowledge_tools
            .retrieve_passages`'s result (each a dict with at least
            `score`), un-filtered.
        relevance_floor: The relevance floor to apply. Defaults to
            `_relevance_floor()` (`THUNAI_KNOWLEDGE_RELEVANCE_FLOOR`,
            default `DEFAULT_RELEVANCE_FLOOR`) when omitted.

    Returns:
        A new list containing only the passages whose `score` is at or
        above the floor, in the same order as `passages`. Never mutates
        `passages`. Empty when no passage clears the floor -- Req 8.6's own
        trigger condition for the no-answer outcome.
    """
    floor = relevance_floor if relevance_floor is not None else _relevance_floor()
    return [passage for passage in passages if passage.get("score", 0.0) >= floor]


# ---------------------------------------------------------------------------
# citations_are_grounded_in_offered_passages (Req 8.3): defensive,
# non-mutating check -- see module docstring's "Grounded-answer /
# refuse-ungrounded design" section for why this never silently corrects
# the model's own output.
# ---------------------------------------------------------------------------


def citations_are_grounded_in_offered_passages(
    cited_source_ids: list[str],
    offered_passages: list[dict[str, Any]],
) -> list[str]:
    """Flag any cited `source_id` that was not among `offered_passages`
    (Req 8.3).

    This function never mutates or drops a citation itself -- see the
    module docstring's "Grounded-answer / refuse-ungrounded design" section
    for why message-content grounding judgement calls are flagged, not
    silently fixed, mirroring `agents/intake_agent.py
    ::validate_category_assignment`'s identical precedent.

    Args:
        cited_source_ids: The model's own `cited_source_ids` field.
        offered_passages: The exact passage list the model was shown (i.e.
            `select_grounding_passages()`'s own return value for this
            question) -- each a dict with at least `source_id`.

    Returns:
        A list of human-readable defect descriptions (for Audit_Ledger);
        empty when every cited id was among the offered passages.
    """
    offered_ids = {passage["source_id"] for passage in offered_passages}
    return [
        f"cited source_id {source_id!r} was not among the passages offered to the model (Req 8.3)"
        for source_id in cited_source_ids
        if source_id not in offered_ids
    ]


# ---------------------------------------------------------------------------
# answer_question (Req 8.1-8.5, 8.8): the deterministic driver.
# ---------------------------------------------------------------------------

_ESCALATION_OPTIONS: Final[dict[str, list[dict[str, str]]]] = {
    "knowledge_no_answer": [
        {"option_id": "answer_directly", "label": "Answer the resident directly"},
        {"option_id": "hold", "label": "Hold"},
    ],
    "knowledge_unavailable": [
        {"option_id": "retry", "label": "Retry retrieval"},
        {"option_id": "answer_directly", "label": "Answer the resident directly"},
    ],
    "validation_failure": [
        {"option_id": "retry", "label": "Retry the knowledge answer"},
        {"option_id": "answer_directly", "label": "Answer the resident directly"},
    ],
}
"""Between 2 and 5 options per escalation category (Req 11.1), matching
every other agent module's own established `_ESCALATION_OPTIONS` shape."""


def _escalate(
    *,
    question_id: str,
    run_id: str,
    escalation_type: str,
    decision_summary: str,
    reason: str,
    stakes: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verdict = escalation_policy.decide(category=escalation_type, confidence=0.0)
    escalation = create_escalation(
        idempotency_key=f"escalate:{question_id}:{escalation_type}",
        run_id=run_id,
        escalation_type=escalation_type,
        decision_summary=decision_summary[:200],
        reason=reason,
        stakes=stakes,
        options=_ESCALATION_OPTIONS.get(escalation_type, _ESCALATION_OPTIONS["knowledge_no_answer"]),
        default_action=verdict.get("default_action") or "hold",
        response_deadline_minutes=verdict["response_deadline_minutes"],
        context=context or {},
    )
    return {"ok": True, "outcome": "escalated", "escalation_type": escalation_type, "escalation": escalation}


def answer_question(
    *,
    question_id: str,
    run_id: str,
    question: str,
    produce_answer: Callable[[dict[str, Any]], _ComposedAnswer | ValidationFailureFallback],
) -> dict[str, Any]:
    """Drive one resident safety question through Knowledge_Agent, end to
    end (Req 8.1-8.6, 8.8).

    Never calls a language model itself -- `produce_answer` supplies the
    typed `_ComposedAnswer` (or a `ValidationFailureFallback`), given the
    already-filtered grounding context this function assembles, mirroring
    `agents/dispatch_agent.py::run_dispatch_decision`'s own
    `produce_decision` pattern so this function is fully unit-testable
    without a live Bedrock invocation. `process_knowledge_question()` below
    is the production entry point wiring a real `Knowledge_Agent`
    invocation into `produce_answer`.

    Flow:
        1. Req 8.1: `tools.knowledge_tools.retrieve_passages(question)`.
        2. Req 8.7: a retrieval failure (`ok=False`) escalates
           `"knowledge_unavailable"` immediately; `produce_answer` is never
           called.
        3. Req 8.4/8.5: `tools.intake_tools.detect_language(question)` to
           pick `answer_language` (the detected language when it is within
           `CONFIGURED_LANGUAGES`, else `DEFAULT_LANGUAGE`) and
           `question_language_recognised`.
        4. Req 8.6: `select_grounding_passages()` on the retrieved
           passages. Zero passages clearing the floor escalates
           `"knowledge_no_answer"` immediately; `produce_answer` is never
           called -- no answer is composed, matching Req 8.6's "return a
           no-answer response that contains no safety guidance".
        5. Otherwise call `produce_answer(grounding_context)`, where
           `grounding_context` is a plain dict naming the cleared passages,
           `answer_language`, and `question_language_recognised` -- the
           exact context `process_knowledge_question()`'s real prompt is
           built from.
        6. Req 10.11: a `ValidationFailureFallback` escalates
           `"validation_failure"`.
        7. Req 8.3: `citations_are_grounded_in_offered_passages()` on the
           produced answer's `cited_source_ids`; any defect is recorded on
           the returned outcome's `citation_defects` field (never silently
           dropped, never silently corrected -- see that function's own
           docstring) but does not itself force an escalation, since a
           defect here is a monitoring signal (Req 8.3's own wording
           states a correctness property, not an escalation trigger) for a
           gap this module's own construction (module docstring's
           "Grounded-answer" section) already makes structurally rare.
        8. Req 8.8: the disclaimer is deterministically prepended to
           `answer_text` whenever the produced answer's
           `requires_advisory_disclaimer` is `True`.
        9. Req 8.9: every branch's outcome, including the question, the
           detected/final language, every retrieved passage id with its
           score, and the returned answer or no-answer outcome, is written
           to Audit_Ledger via `harness.audit.append_audit_entry`.

    Args:
        question_id: Identifier of this resident safety question (used for
            the escalation idempotency key and the audit entry).
        run_id: The current run identifier, threaded into every
            `create_escalation` call and the audit entry.
        question: The resident's free-text safety question.
        produce_answer: A callable accepting the grounding context dict
            (see step 5) and returning the typed `_ComposedAnswer` (or a
            `ValidationFailureFallback`). See `process_knowledge_question()`
            for the real, model-backed implementation.

    Returns:
        On a grounded answer (Req 8.1-8.4, 8.8):
        `{"ok": True, "outcome": "answered", "answer": KnowledgeAnswer,
        "citation_defects": list[str]}`.

        On no passage clearing the relevance floor (Req 8.6), a retrieval
        failure (Req 8.7), or a structured-output validation failure (Req
        10.11):
        `{"ok": True, "outcome": "escalated", "escalation_type": str,
        "escalation": dict}`.
    """
    retrieval = retrieve_passages(question)

    language_result = intake_tools.detect_language(question)
    question_language_recognised = not bool(language_result.get("is_outside_configured_languages"))
    detected_language = language_result.get("detected_language", "unknown")
    answer_language = detected_language if question_language_recognised else intake_tools.DEFAULT_LANGUAGE

    retrieved_passages = retrieval.get("passages", [])

    def _audit(outcome: str, **extra: Any) -> None:
        append_audit_entry(
            run_id=run_id,
            tool_name="knowledge_agent",
            outcome=outcome,
            inputs={
                "question_id": question_id,
                "question": question,
                "detected_language": detected_language,
                "retrieved_passages": [
                    {"source_id": p.get("source_id"), "score": p.get("score")} for p in retrieved_passages
                ],
                **extra,
            },
        )

    # Step 2 (Req 8.7).
    if not retrieval.get("ok", False):
        result = _escalate(
            question_id=question_id,
            run_id=run_id,
            escalation_type="knowledge_unavailable",
            decision_summary=f"Knowledge_Base retrieval failed for question {question_id}.",
            reason=(
                f"retrieve_passages returned reason={retrieval.get('reason')!r} (Req 8.7)"
            ),
            stakes=f"resident question {question_id!r} has no grounded answer available",
        )
        _audit("escalated:knowledge_unavailable")
        return result

    # Step 4 (Req 8.6).
    grounding_passages = select_grounding_passages(retrieved_passages)
    if not grounding_passages:
        result = _escalate(
            question_id=question_id,
            run_id=run_id,
            escalation_type="knowledge_no_answer",
            decision_summary=f"No relevant passage found for question {question_id}.",
            reason="no retrieved passage cleared the configured relevance threshold (Req 8.6)",
            stakes=f"resident question {question_id!r} has no grounded guidance to offer",
        )
        _audit("escalated:knowledge_no_answer")
        return result

    # Step 5.
    grounding_context = {
        "question": question,
        "passages": grounding_passages,
        "answer_language": answer_language,
        "question_language_recognised": question_language_recognised,
    }
    composed = produce_answer(grounding_context)

    # Step 6 (Req 10.11).
    if isinstance(composed, ValidationFailureFallback):
        result = _escalate(
            question_id=question_id,
            run_id=run_id,
            escalation_type="validation_failure",
            decision_summary=f"Knowledge answer for question {question_id} could not be validated.",
            reason=composed.error_detail[:200],
            stakes=f"resident question {question_id!r} has no valid answer to deliver",
        )
        _audit("escalated:validation_failure")
        return result

    # Step 7 (Req 8.3).
    citation_defects = citations_are_grounded_in_offered_passages(composed.cited_source_ids, grounding_passages)

    passages_by_id = {p["source_id"]: p for p in grounding_passages}
    citations = [
        Citation(source_id=source_id, source_version=passages_by_id[source_id].get("source_version"))
        for source_id in composed.cited_source_ids
        if source_id in passages_by_id
    ]

    # Step 8 (Req 8.8).
    answer_text = composed.answer_text
    if composed.requires_advisory_disclaimer:
        answer_text = f"{ADVISORY_DISCLAIMER} {answer_text}"

    answer = KnowledgeAnswer(
        answer_text=answer_text,
        citations=citations,
        answer_language=answer_language,
        question_language_recognised=question_language_recognised,
        confidence=composed.confidence,
        action="execute" if composed.confidence >= escalation_policy.CONFIDENCE_FLOOR.value else "ask_human",
        category="knowledge_answer_grounded",
        is_irreversible_action=False,
    )

    _audit(
        "answered",
        answer_language=answer_language,
        cited_source_ids=composed.cited_source_ids,
        citation_defects=citation_defects,
    )

    return {"ok": True, "outcome": "answered", "answer": answer, "citation_defects": citation_defects}


# ---------------------------------------------------------------------------
# process_knowledge_question: the production entry point wiring a real
# Knowledge_Agent invocation into answer_question's produce_answer callable.
# ---------------------------------------------------------------------------


def _compose_prompt(grounding_context: dict[str, Any]) -> str:
    passages_text = "\n\n".join(
        f"[source_id={p['source_id']}] {p['text']}" for p in grounding_context["passages"]
    )
    return (
        f"Resident question: {grounding_context['question']}\n\n"
        f"answer_language: {grounding_context['answer_language']}\n"
        f"question_language_recognised: {grounding_context['question_language_recognised']}\n\n"
        f"Passages you may use (cite by source_id):\n{passages_text}"
    )


def process_knowledge_question(
    *,
    question_id: str,
    run_id: str,
    question: str,
    agent: Agent | None = None,
) -> dict[str, Any]:
    """Production entry point: build a real `Knowledge_Agent` (unless one is
    supplied), invoke it against the deterministically-filtered grounding
    context, and drive the result through `answer_question()` (Req
    8.1-8.9).

    Args:
        question_id: Identifier of this resident safety question.
        run_id: The current run identifier.
        question: The resident's free-text safety question.
        agent: An optional pre-built `Agent` (e.g. from
            `build_knowledge_agent()`). Built fresh via
            `build_knowledge_agent()` when omitted.

    Returns:
        `answer_question()`'s own return shape.
    """
    real_agent = agent if agent is not None else build_knowledge_agent()

    def _produce(grounding_context: dict[str, Any]) -> _ComposedAnswer | ValidationFailureFallback:
        prompt = _compose_prompt(grounding_context)
        return safe_structured(
            lambda: real_agent(prompt, structured_output_model=_ComposedAnswer).structured_output,
            _ComposedAnswer,
        )

    return answer_question(
        question_id=question_id,
        run_id=run_id,
        question=question,
        produce_answer=_produce,
    )
