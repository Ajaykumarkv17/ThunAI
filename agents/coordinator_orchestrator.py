"""``Coordinator_Orchestrator``: the one front door for the ward
coordinator's *ad-hoc* questions ("what's the river level at reach-7", "is
shelter B full", "who's available near the market"), answered by routing to
read-only specialist agents exposed as ``@tool``-decorated functions
(Req 4.10-4.14; design.md §3.1 (Coordinator_Orchestrator), and the
coordinator_orchestrator code sketch that follows the §3.1 role table).

This is the **only** agent in the system that carries a session manager
(Req 4.13), because it is the only long-lived conversational surface -- a
coordinator's chat session spans many turns and many tool calls and needs
coherent message history across invocations of the same
``runtimeSessionId`` (design.md §3.7, concern (a): "the message history a
Strands ``Agent`` needs to keep reasoning coherently across tool calls
within and across invocations of ``Coordinator_Orchestrator``'s long-lived
chat session"). Every ``Incident_Graph`` member agent, by contrast, is
built fresh per node invocation with no session manager -- see
``agents/monitor_agent.py``/``agents/dispatch_agent.py``/etc.'s own
identical, unconditional "no ``session_manager``" precedent.

Verification note (mandatory per tasks.md's "READ BEFORE STARTING ANY TASK
-- VERIFY THE LIVE API SURFACE FIRST" rule, tailored to what this task
touches -- the "agents as tools" pattern and session-manager attachment):

    Installed versions confirmed live (not assumed from design.md's pin):
    ``strands-agents==1.55.0`` and ``strands-agents-tools==0.8.8``
    (``pip show``). **Deviation from design.md's pin, recorded here per the
    "docs/installed win" rule:** design.md pins
    ``strands-agents==1.54.0`` / ``strands-agents-tools==0.8.7`` (dated
    2026-08-27); the installed tree is one minor/patch ahead on both
    (1.55.0 / 0.8.8), matching every other already-implemented agent
    module's own recorded 1.55.0 finding (``agents/config.py``,
    ``agents/intake_agent.py``, ``agents/dispatch_agent.py``,
    ``agents/knowledge_agent.py``). No behavioural API difference relevant
    to this module was found across that gap -- every constructor keyword
    and pattern this module relies on is present and unchanged (verified
    below), so the deviation is version-string only.

    The following were re-confirmed against the *installed* package and the
    live Strands docs (via the ``strands`` power's docs MCP server), not
    assumed from design.md's pseudocode alone:

    1. **"Agents as tools" pattern (functional wrapping).** Strands docs
       ("Lesson 10: Multi-Agent Patterns: Agents as Tools", fetched live)
       document the exact pattern design.md §3.1 sketches: wrap a
       sub-``Agent`` inside a ``@tool``-decorated function; instantiate the
       agent *inside* the function so it "starts completely fresh on every
       call ... a clean context window every time the tool is invoked";
       set the sub-agent's ``callback_handler`` to ``None`` so it "run[s]
       silently while the orchestrator programmatically captures and
       processes the final result string". The interface between
       orchestrator and specialist "is a string" and the specialist "has
       absolutely no access to the orchestrator's conversation history"
       (strict context isolation). This module implements exactly that:
       each ``ask_*`` tool builds a fresh read-only specialist ``Agent``
       with ``callback_handler=None`` and returns ``str(specialist(query))``.

    2. **``strands.Agent.__init__`` keyword surface** (``inspect.signature``
       on the installed class): ``model``, ``tools``, ``system_prompt``,
       ``callback_handler``, ``agent_id``, ``name``, ``hooks``,
       ``interventions``, and ``session_manager`` are all present. In
       particular ``session_manager``'s annotation is
       ``strands.session.session_manager.SessionManager | None = None``
       (confirmed directly) -- i.e. a session manager is attached by
       passing a ``SessionManager`` instance to that one keyword, and *not*
       attaching one is simply never supplying it (its default is
       ``None``). This module passes ``session_manager=`` **only** for the
       orchestrator agent, and never for any of the four wrapped
       specialists (Req 4.13).

    3. **Member/specialist agents carry no session manager.** design.md
       §3.1 states the Strands rule directly ("every ``Incident_Graph``
       member agent is built fresh per node invocation with no session
       manager, consistent with the Strands rule that member agents in a
       Graph/Swarm must not carry one") and Req 4.13 makes it a hard
       requirement ("attach Memory_Store to the orchestrating component
       only"). The four ``ask_*`` specialists this module builds are
       structurally sub-agents-as-tools of the orchestrator, so they too
       carry none -- only ``build_coordinator_orchestrator()`` ever passes
       ``session_manager=``.

    Session-manager class deviation / deferral (recorded per the "docs
    win, note the deviation" rule): design.md §3.7 names the concrete
    session-manager implementation as ``AgentCoreMemorySessionManager``
    (from ``bedrock_agentcore.memory.integrations.strands``), the
    ``Memory_Store`` mechanism. That class is constructed by the memory
    epic (a later task), not here -- this module must not hard-depend on an
    AgentCore Memory resource merely to be importable and unit-testable
    without AWS. It therefore accepts the session manager via **dependency
    injection**: ``build_coordinator_orchestrator(session_manager=...)``
    takes any ``strands.session.session_manager.SessionManager`` (the
    verified base type of the constructor keyword) and attaches it
    verbatim. The eventual memory-epic wiring passes a real
    ``AgentCoreMemorySessionManager`` here; tests pass a lightweight double
    or ``None``. This keeps design.md §3.7's "the ONLY agent with one"
    contract exactly (this is the only ``build_*`` in ``agents/`` that has
    a ``session_manager`` parameter at all) while not front-running the
    memory epic's own construction of the concrete class.

Read-only guarantee (Req 4.10, and design.md §3.1's "Explicitly forbidden
by Req 4.10 from holding any write tool"):
    This is a **structural** guarantee, not a prompt instruction. The
    orchestrator agent's own ``tools`` list contains only the four
    ``ask_*`` wrappers -- it holds no incident/dispatch/alert tool
    directly. Each ``ask_*`` wrapper, in turn, builds its specialist with a
    hand-picked subset of that specialist's tools that contains **only the
    read/query tools** -- never ``tools.incident_tools
    .create_or_update_incident``, never ``tools.dispatch_tools
    .assign_responder``, never ``tools.alert_tools.deliver_alert``, never
    ``tools.escalation_tools.create_escalation``. The write tools are not
    imported into this module at all (see the imports below -- only
    read/query tool symbols are imported), so there is no code path,
    prompt-driven or otherwise, by which this agent or any specialist it
    wraps can create, modify, or delete incident, dispatch, or alert data.
    This mirrors design.md §3.1's own ``ask_monitor`` sketch
    (``tools=[get_current_readings, get_open_incidents]  # no
    create_incident/update_incident``) exactly.

    ``READONLY_TOOLS`` (below) is the single, explicit manifest of every
    tool wired into any specialist here; ``assert_no_write_tools()``
    re-checks it at import time against a name-based deny-list of every
    known write tool in this codebase, so a future edit that accidentally
    adds a write tool to a specialist fails loudly at import rather than
    silently violating Req 4.10.

Unroutable-request behaviour (Req 4.11; design.md §3.1's own note: "Req
4.11's 'unroutable request' behaviour is the prompt's explicit instruction
... this is deliberately a prompt-level behaviour (not hook-enforced)
because 'can this be routed' is a judgement call about fit, not a safety
gate"):
    ``SYSTEM_PROMPT`` instructs the orchestrator, explicitly, that when no
    specialist tool fits a request it must return an unroutable-request
    indication that names the request and change nothing. Per design.md
    this is intentionally a prompt-level behaviour verified by an eval
    scenario (§7), not a deterministic gate -- the *actual* safety property
    ("this agent never writes") is the structural read-only guarantee
    above, which holds regardless of what the model does. The prompt also
    carries the machine-recognisable marker string
    ``UNROUTABLE_REQUEST_MARKER`` so the eval scenario (and any caller) can
    assert the unroutable indication deterministically.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.session_manager import SessionManager

from agents.config import get_model_id_for_role
from harness.hooks import HARNESS_HOOKS

# Read/query tools ONLY. No write tool
# (create_or_update_incident / assign_responder / deliver_alert /
# create_escalation) is imported into this module at all (Req 4.10) -- see
# the module docstring's "Read-only guarantee" section.
from tools.alert_tools import get_shelter_capacity
from tools.dispatch_tools import find_candidate_responders
from tools.incident_tools import get_prior_sweep_readings
from tools.intake_tools import detect_language, resolve_location
from tools.knowledge_tools import retrieve_passages
from tools.sensor_tools import get_dam_release, get_rainfall_rate, get_river_level

__all__ = [
    "AGENT_ID",
    "SYSTEM_PROMPT",
    "UNROUTABLE_REQUEST_MARKER",
    "MONITOR_READONLY_PROMPT",
    "INTAKE_READONLY_PROMPT",
    "DISPATCH_READONLY_PROMPT",
    "KNOWLEDGE_READONLY_PROMPT",
    "READONLY_TOOLS",
    "assert_no_write_tools",
    "ask_monitor",
    "ask_intake",
    "ask_dispatch",
    "ask_knowledge",
    "build_coordinator_orchestrator",
]

# ---------------------------------------------------------------------------
# Agent identity (Req 4.12: unique agent_id and a system prompt distinct
# from every other agent role's).
# ---------------------------------------------------------------------------

AGENT_ID: Final[str] = "coordinator_orchestrator"
"""Unique agent identifier for this role (Req 4.12), distinct from
``monitor_agent``/``intake_agent``/``dispatch_agent``/``alert_agent``/
``safety_qa_agent``/``knowledge_agent``."""

UNROUTABLE_REQUEST_MARKER: Final[str] = "UNROUTABLE_REQUEST"
"""A stable, machine-recognisable marker the orchestrator is instructed to
include verbatim in any unroutable-request indication (Req 4.11), so an
eval scenario (design.md §7) and any caller can assert the indication
deterministically without parsing free prose."""

SYSTEM_PROMPT: Final[str] = (
    "You are Coordinator_Orchestrator for the Kollidam Ward-7 Neighbourhood Flood Committee. "
    "You answer the ward coordinator's ad-hoc questions by routing each one to the right "
    "read-only specialist tool, then relaying that tool's answer back plainly.\n\n"
    "You have exactly four tools, each a read-only specialist:\n"
    "- ask_monitor: current hazard readings (river level, rainfall rate, upstream dam release), "
    "the most recent prior sweep's readings, and whether readings are rising or unusual for a "
    "river reach. Use for 'what's the river level', 'is it rising', 'how does this compare'.\n"
    "- ask_intake: how a resident's free-text location wording resolves to ward areas or "
    "shelters, and what language a message is in. Use for 'where is this resident', 'what "
    "language is this message'.\n"
    "- ask_dispatch: which responders are currently available near a location, with their "
    "distance, equipment, and current load; and current available shelter capacity. Use for "
    "'who is available near X', 'is shelter B full', 'how much capacity is left'.\n"
    "- ask_knowledge: grounded answers to resident safety questions from the curated knowledge "
    "base, with citations. Use for 'what should residents do about Y', 'is Z safe'.\n\n"
    "You hold NO tool that creates, modifies, or deletes any incident, dispatch, alert, "
    "escalation, or responder assignment. You cannot take any action that changes stored data, "
    "and you must never claim to have taken such an action. If the coordinator asks you to "
    "assign a responder, create or update an incident, send an alert, or change any record, "
    "explain that you are a read-only assistant and that they must use the console controls for "
    "that -- then, if useful, use a read-only tool to give them the information they need to do "
    "it themselves.\n\n"
    f"If no specialist tool fits the request at all, reply with the exact token "
    f"'{UNROUTABLE_REQUEST_MARKER}' followed by a plain restatement of the request you could not "
    "route, and change nothing. Do not guess at an answer you have no tool to ground."
)
"""Distinct from every other agent role's system prompt (Req 4.12). Encodes
both the read-only stance (Req 4.10) and the unroutable-request behaviour
(Req 4.11) as explicit instructions -- while the *actual* read-only
guarantee is structural (this agent is given no write tool), per the module
docstring."""

# ---------------------------------------------------------------------------
# Per-specialist read-only system prompts (Req 4.12: each distinct; and the
# read-only stance re-stated so the wrapped specialist never claims a write
# it structurally cannot perform). These are deliberately different strings
# from the specialists' own operational SYSTEM_PROMPTs (e.g.
# agents/monitor_agent.py::MONITOR_SYSTEM_PROMPT), because these wrappers
# expose only the read subset and must not carry the operational prompt's
# incident-create/update framing.
# ---------------------------------------------------------------------------

MONITOR_READONLY_PROMPT: Final[str] = (
    "You are a read-only hazard-reading assistant for a ward flood committee. Answer the "
    "question using only your reading tools (river level, rainfall rate, upstream dam release, "
    "and the most recent prior sweep's readings). State current values, whether they are rising "
    "or falling versus the prior sweep, and whether they look unusual. You cannot create, "
    "update, or close an incident and must never claim to; report only what the readings say."
)

INTAKE_READONLY_PROMPT: Final[str] = (
    "You are a read-only location-and-language assistant for a ward flood committee. Given a "
    "resident's wording, use resolve_location to report which ward areas or shelters it could "
    "mean (and how many candidates), and detect_language to report the message's language. You "
    "cannot create or change any request and must never claim to; report only what the tools "
    "return."
)

DISPATCH_READONLY_PROMPT: Final[str] = (
    "You are a read-only availability assistant for a ward flood committee. Use "
    "find_candidate_responders to report which responders are currently AVAILABLE near a "
    "location, with their distance, equipment, and current active assignment count, and "
    "get_shelter_capacity to report current available shelter capacity. You cannot assign a "
    "responder or change any assignment and must never claim to; report only availability and "
    "capacity as the tools return them."
)

KNOWLEDGE_READONLY_PROMPT: Final[str] = (
    "You are a read-only knowledge assistant for a ward flood committee. Use retrieve_passages "
    "to answer a resident safety question using only the retrieved passages, and cite the "
    "passages you use. If nothing relevant is retrieved, say so plainly rather than guessing. "
    "You take no action and change no data."
)

# ---------------------------------------------------------------------------
# Model construction. Coordinator_Orchestrator routes to the high-capability
# model per agents/config.py's own MODEL_FOR_ROLE mapping (design.md's Cost
# Model: "coordinator_orchestrator route[s] to HIGH_CAPABILITY_MODEL_ID ...
# ad-hoc human-facing routing"). Read once per specialist build; the wrapped
# specialists share the same role's model id (they are the orchestrator's
# own read-only reasoning, not separate cost-model roles of their own).
# ---------------------------------------------------------------------------

_DEFAULT_TEMPERATURE: float = 0.2
"""Low temperature for a routing/decision-adjacent role (hackathon
guidelines: 0.0-0.3 for anything that routes or decides)."""

_DEFAULT_MAX_TOKENS: int = 4096


def _build_model() -> BedrockModel:
    """Build the ``BedrockModel`` used by the orchestrator and its wrapped
    read-only specialists, resolved via ``agents.config`` (never a hardcoded
    model id, per that module's contract)."""
    import os

    return BedrockModel(
        model_id=get_model_id_for_role(AGENT_ID),
        region_name=os.environ.get("AWS_REGION"),
        temperature=_DEFAULT_TEMPERATURE,
        max_tokens=_DEFAULT_MAX_TOKENS,
    )


def _build_readonly_specialist(
    *,
    name: str,
    agent_id: str,
    system_prompt: str,
    tools: list[Any],
    model: Any | None = None,
) -> Agent:
    """Build one fresh, read-only specialist ``Agent`` for an ``ask_*``
    wrapper (agents-as-tools functional-wrapping pattern, verified live --
    see module docstring item 1).

    Constructed with ``callback_handler=None`` (so it runs silently and the
    orchestrator captures its final string, per the verified pattern),
    ``hooks=list(HARNESS_HOOKS)`` (so its read-tool calls are still capped
    and audited like every other agent in this codebase, Req 16), and --
    critically -- **no** ``session_manager`` (Req 4.13: only the
    orchestrator itself carries one).
    """
    return Agent(
        name=name,
        agent_id=agent_id,
        system_prompt=system_prompt,
        tools=tools,
        model=model if model is not None else _build_model(),
        hooks=list(HARNESS_HOOKS),
        callback_handler=None,
    )


# ---------------------------------------------------------------------------
# The four read-only specialists, wrapped as @tool functions (Req 4.10).
# Each fresh-builds its specialist on every call (clean context window per
# invocation, per the verified pattern) with only that specialist's read
# tools. The `_model` keyword on each is a test seam (pass a model double to
# avoid a live Bedrock call while still exercising the wrapper); the model
# never sees it -- @tool derives the tool schema from the annotated
# parameters, and a keyword-only argument with a default is not part of the
# model-facing signature the orchestrator calls with.
# ---------------------------------------------------------------------------


@tool
def ask_monitor(query: str) -> str:
    """Ask about current hazard readings, severity trend, or recent change for a river reach.

    Use for questions like "what is the river level at reach-7", "is the rainfall rising",
    "how does the current dam release compare to the last sweep". This tool is READ-ONLY: it
    reports readings only and cannot create, update, or close an incident.

    Args:
        query: The coordinator's question about hazard readings, in plain language, including
            the river reach it concerns.

    Returns:
        The read-only monitor specialist's plain-language answer.
    """
    specialist = _build_readonly_specialist(
        name="monitor_agent_ro",
        agent_id="monitor_ro",
        system_prompt=MONITOR_READONLY_PROMPT,
        tools=[get_river_level, get_rainfall_rate, get_dam_release, get_prior_sweep_readings],
        model=_ask_monitor_model,
    )
    return str(specialist(query))


@tool
def ask_intake(query: str) -> str:
    """Ask how a resident's location wording resolves, or what language a message is in.

    Use for questions like "where is the resident who said 'near the old temple'", "how many
    ward areas could 'the market' mean", "what language is this message". This tool is
    READ-ONLY: it reports location candidates and language only and cannot create or change any
    request.

    Args:
        query: The coordinator's question about a resident's location wording or a message's
            language, in plain language.

    Returns:
        The read-only intake specialist's plain-language answer.
    """
    specialist = _build_readonly_specialist(
        name="intake_agent_ro",
        agent_id="intake_ro",
        system_prompt=INTAKE_READONLY_PROMPT,
        tools=[resolve_location, detect_language],
        model=_ask_intake_model,
    )
    return str(specialist(query))


@tool
def ask_dispatch(query: str) -> str:
    """Ask which responders are available near a location, or how much shelter capacity is left.

    Use for questions like "who is available near the market", "which responders have a boat
    within 3km of reach-7", "is shelter B full", "how much capacity is left at the shelters".
    This tool is READ-ONLY: it reports responder availability and shelter capacity only and
    cannot assign a responder or change any assignment.

    Args:
        query: The coordinator's question about responder availability or shelter capacity, in
            plain language, including the location it concerns.

    Returns:
        The read-only dispatch specialist's plain-language answer.
    """
    specialist = _build_readonly_specialist(
        name="dispatch_agent_ro",
        agent_id="dispatch_ro",
        system_prompt=DISPATCH_READONLY_PROMPT,
        tools=[find_candidate_responders, get_shelter_capacity],
        model=_ask_dispatch_model,
    )
    return str(specialist(query))


@tool
def ask_knowledge(query: str) -> str:
    """Answer a resident safety question from the curated knowledge base, with citations.

    Use for questions like "what should residents do about contaminated flood water", "is it
    safe to drive through the underpass", "how do residents purify water". This tool is
    READ-ONLY: it answers only from retrieved knowledge-base passages and takes no action.

    Args:
        query: The resident safety question, in plain language.

    Returns:
        The read-only knowledge specialist's grounded, cited answer, or a plain statement that
        nothing relevant was found.
    """
    specialist = _build_readonly_specialist(
        name="knowledge_agent_ro",
        agent_id="knowledge_ro",
        system_prompt=KNOWLEDGE_READONLY_PROMPT,
        tools=[retrieve_passages],
        model=_ask_knowledge_model,
    )
    return str(specialist(query))


# ---------------------------------------------------------------------------
# Per-wrapper model injection seams (test-only). Default None -> each
# wrapper builds a real BedrockModel via _build_model(). A test may set
# these (e.g. via monkeypatch) to a model double to exercise a wrapper
# without a live Bedrock call. Kept as module-level names rather than tool
# parameters so the @tool-derived, model-facing signature stays exactly
# `(query: str)` (the orchestrator must not be shown a `_model` argument).
# ---------------------------------------------------------------------------

_ask_monitor_model: Any | None = None
_ask_intake_model: Any | None = None
_ask_dispatch_model: Any | None = None
_ask_knowledge_model: Any | None = None


# ---------------------------------------------------------------------------
# Read-only tool manifest + import-time write-tool guard (Req 4.10).
# ---------------------------------------------------------------------------

READONLY_TOOLS: Final[tuple[Callable[..., Any], ...]] = (
    get_river_level,
    get_rainfall_rate,
    get_dam_release,
    get_prior_sweep_readings,
    resolve_location,
    detect_language,
    find_candidate_responders,
    get_shelter_capacity,
    retrieve_passages,
)
"""The complete, explicit manifest of every tool wired into any specialist
this module builds -- read/query tools only (Req 4.10). Every ``ask_*``
wrapper's ``tools=[...]`` list is a subset of this tuple."""

_KNOWN_WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "create_or_update_incident",
        "assign_responder",
        "deliver_alert",
        "create_escalation",
    }
)
"""Every known incident/dispatch/alert/escalation *write* tool name in this
codebase (``tools/incident_tools.py``, ``tools/dispatch_tools.py``,
``tools/alert_tools.py``, ``tools/escalation_tools.py``). Used by
``assert_no_write_tools()`` as a name-based deny-list so a future edit that
accidentally wires a write tool into a specialist here fails loudly at
import time rather than silently violating Req 4.10."""


def _tool_name(candidate: Any) -> str:
    """Best-effort name of a tool object (a ``@tool``-decorated function or
    the underlying function), for the write-tool deny-list check."""
    for attr in ("tool_name", "__name__"):
        value = getattr(candidate, attr, None)
        if isinstance(value, str) and value:
            return value
    inner = getattr(candidate, "__wrapped__", None)
    if inner is not None:
        return _tool_name(inner)
    return type(candidate).__name__


def assert_no_write_tools() -> None:
    """Assert that ``READONLY_TOOLS`` contains no known write tool (Req 4.10).

    Raises:
        AssertionError: naming any write tool found in ``READONLY_TOOLS`` --
            i.e. if a future edit accidentally adds an incident/dispatch/
            alert/escalation write tool to a specialist wired here.
    """
    offending = sorted(
        {
            name
            for name in (_tool_name(tool_obj) for tool_obj in READONLY_TOOLS)
            if name in _KNOWN_WRITE_TOOL_NAMES
        }
    )
    assert not offending, (
        "Coordinator_Orchestrator (Req 4.10) must hold no tool that creates, modifies, or "
        f"deletes incident/dispatch/alert data; found write tool(s): {offending}"
    )


# Fail loudly at import if the read-only invariant is ever broken (Req 4.10).
assert_no_write_tools()


# ---------------------------------------------------------------------------
# The orchestrator itself -- the ONLY agent in the system with a session
# manager (Req 4.13; design.md §3.1's coordinator_orchestrator sketch).
# ---------------------------------------------------------------------------


def build_coordinator_orchestrator(
    *,
    session_manager: SessionManager | None = None,
    model: Any | None = None,
) -> Agent:
    """Build the ``Coordinator_Orchestrator`` agent (Req 4.10-4.13).

    This is the one front door for the coordinator's ad-hoc questions. It is
    given exactly the four read-only ``ask_*`` specialist tools (Req 4.10)
    and holds no write tool of any kind (structurally -- no write tool is
    even imported into this module). Its ``system_prompt`` encodes both the
    read-only stance and the unroutable-request behaviour (Req 4.11), and
    its ``agent_id``/``name`` are the unique ``AGENT_ID`` with a system
    prompt distinct from every other role (Req 4.12).

    It is the **only** agent in this codebase constructed with a
    ``session_manager`` (Req 4.13) -- and even here, only when one is
    supplied. The concrete ``Memory_Store`` session manager
    (``AgentCoreMemorySessionManager``, design.md §3.7) is constructed by
    the memory epic and injected here; this function does not construct it
    itself, so the module stays importable and unit-testable without an
    AgentCore Memory resource (see the module docstring's
    "Session-manager class deviation / deferral" note).

    Args:
        session_manager: The ``Memory_Store`` session manager to attach
            (design.md §3.7 concern (a)). Pass a real
            ``AgentCoreMemorySessionManager`` in production; pass ``None``
            (the default) or a lightweight double in tests. When ``None``,
            no ``session_manager`` is attached at all -- the agent still
            functions for a single-turn ad-hoc question, it simply keeps no
            cross-invocation history.
        model: An optional pre-built model (test seam to avoid a live
            Bedrock call while exercising ``Agent`` construction); defaults
            to a fresh high-capability ``BedrockModel`` via ``_build_model()``.

    Returns:
        The configured ``Coordinator_Orchestrator`` ``strands.Agent``.
    """
    kwargs: dict[str, Any] = {
        "name": AGENT_ID,
        "agent_id": AGENT_ID,
        "system_prompt": SYSTEM_PROMPT,
        "tools": [ask_monitor, ask_intake, ask_dispatch, ask_knowledge],
        "model": model if model is not None else _build_model(),
        "hooks": list(HARNESS_HOOKS),
    }
    # Attach the session manager ONLY when supplied -- and this is the only
    # build_* in agents/ that has this parameter at all (Req 4.13).
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)
