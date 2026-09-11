"""`Knowledge_Agent`'s `@tool` function: grounded passage retrieval over
Knowledge_Base (Req 8.1, 8.1a; design.md §3.1 (Knowledge), §3.7, resolved
Open Question 6).

Verification note (mandatory per tasks.md 12.6, "verify first"):
    Checked against the current Strands Agents docs (`strands-agents`
    installed at `1.55.0` per `pyproject.toml`, matching the version already
    recorded in `agents/config.py`/`harness/hooks.py`/`tools/sensor_tools.py`/
    `tools/dispatch_tools.py`/`tools/alert_tools.py`'s own verification
    notes), cross-checked against every already-implemented tool module in
    this codebase: the `@tool` decorator (`from strands import tool`) infers
    the tool's name, description, and input schema from the decorated
    function's signature and docstring — the first docstring paragraph
    becomes the tool description, an `Args:` section supplies each
    parameter's description, and a plain, JSON-serialisable dict return
    value is auto-formatted as the tool result with no `status`/`content`
    envelope required. This module follows the exact same "plain dict,
    `ok: bool`, typed non-raising failure `reason`" shape every other tool
    module here already established (`tools/sensor_tools.py`,
    `tools/dispatch_tools.py`, `tools/alert_tools.py`), so `Knowledge_Agent`'s
    model reads an unambiguous, consistent contract.

    Confirmed the live retrieval call shape by reading
    `integrations/knowledge_provider.py` (task 10.3, already implemented) in
    full rather than re-deriving it: `KnowledgeProvider.retrieve(query,
    *, max_results=5) -> list[Passage]` is the one interface this module
    calls, via `integrations.get_knowledge_provider()` (the module-level
    factory — `Knowledge_Provider` has no synthetic backend, so there is no
    flag to branch on here, exactly like `notification_provider()`). That
    module's own docstring already documents, with full citation trail, that
    it calls `boto3.client("bedrock-agent-runtime").retrieve(...)` directly
    (never the deprecated `strands_tools.retrieve` tool) and that
    `retrievalConfiguration` is `{"vectorSearchConfiguration":
    {"numberOfResults": ...}}` for an S3 Vectors-backed KB. This module does
    not re-implement or re-verify any of that — it is a thin wrapper around
    the already-verified provider, exactly as this task's instructions
    describe ("this tool module should simply wrap that provider").

Grounding/refusal shape (Req 8.1, 8.1a, and the "must refuse to answer
ungrounded" posture Req 8.3 and design.md's agent-role table assign to
`Knowledge_Agent`, not to this tool):
    `retrieve_passages` itself performs no relevance-threshold filtering and
    makes no refuse/answer decision — `Knowledge_Agent` (task 13.7, not yet
    implemented) owns that decision, per design.md's own division of labour
    ("tool reports the candidates, the agent/caller decides", the identical
    pattern `tools/dispatch_tools.py::find_candidate_responders` and
    `tools/alert_tools.py::get_shelter_capacity` already establish for their
    own read tools). What this tool *does* guarantee, so the caller can make
    that refuse/answer decision without re-deriving anything itself, is:

    - Every passage in the returned `passages` list carries its `score`
      (Req 8.1's "each retrieved passage carrying a relevance score")
      un-altered from `Passage.score`, in the same descending-relevance
      order `KnowledgeProvider.retrieve()` returns them in — no re-sorting,
      no filtering, so `Knowledge_Agent` (or a human reading raw tool
      output) can apply Req 8.6's configured relevance threshold itself
      without this tool having silently dropped a below-threshold passage
      it might still want to inspect.
    - `passage_count` and an explicit `no_relevant_passages_found` boolean
      are surfaced directly on the result (`no_relevant_passages_found` is
      `True` exactly when `passages == []`) so a caller can check "did
      retrieval come back empty" as a single boolean rather than re-deriving
      it from `len(passages) == 0` — matching this task's explicit
      instruction to "clearly signal when no relevant passages were found."
      A genuinely *empty* result is deliberately distinguished, in the
      returned shape, from a *low-relevance* result (every passage present,
      but all below whatever threshold `Knowledge_Agent` applies) — this
      tool cannot know `Knowledge_Agent`'s configured threshold (that is
      Escalation_Policy/Req 8.6's concern, not this integration seam's), so
      it reports the full, unfiltered score list and lets the threshold
      check happen exactly once, in the one place (`Knowledge_Agent`) that
      is responsible for the refuse-vs-answer decision (Req 8.3, 8.6).
    - Every passage carries `source_version` verbatim from `Passage
      .source_version` (`None` when the underlying retrieval result's
      metadata carried no `source_version` key — see
      `integrations/knowledge_provider.py`'s own verification note 3 on why
      that can legitimately happen before the ingestion sidecar file lands),
      so `Knowledge_Agent` has what it needs for Req 8.2's "each citation
      carrying the passage identifier and the knowledge source version"
      without a second lookup.
    - A provider-construction failure (`KnowledgeProviderConfigurationError`,
      raised by `get_knowledge_provider()` itself when
      `THUNAI_KNOWLEDGE_BASE_ID` is unset/empty — see that module's
      docstring) and a live-call failure or timeout (Req 8.7: "retrieval ...
      fails or does not complete within the configured retrieval timeout")
      are both caught here and reported as a typed, non-raising
      `{"ok": False, "reason": "knowledge_base_unavailable", ...}` result,
      never a raised exception reaching the agent loop — matching every
      other tool module's "never raise for an expected, recoverable
      outcome" convention, and giving `Knowledge_Agent` the exact signal
      Req 8.7 needs to route the question to the coordinator via
      Escalation_Service rather than attempting to answer from a failed
      retrieval.

Configuration judgment call (Req 8.1's "retrieve ... at most the configured
maximum passage count" and Req 8.7's "configured retrieval timeout" name no
override env-var identifier anywhere in design.md/.env.example/the tasks
list — `grep` for "MAX_PASSAGE"/"RETRIEVAL_TIMEOUT"/"RELEVANCE" across the
repository, `.env.example` included, returns nothing before this task):
this module follows the exact established pattern every other
env-overridable numeric constant in this codebase uses
(`policy/escalation_policy.py`'s `_read_int_env`/`_read_float_env` helpers;
`tools/dispatch_tools.py`'s `_dispatch_search_radius_km`;
`tools/alert_tools.py`'s `_read_int_env`), introducing
`THUNAI_KNOWLEDGE_MAX_PASSAGE_COUNT` (default `5`, matching
`BedrockKBKnowledgeProvider.retrieve`'s own `max_results=5` default exactly,
so a caller that never overrides either value gets one consistent number in
both places) and `THUNAI_KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS` (default
`8.0`, chosen to leave headroom under Req 8.1's own "shall return a response
within 10 seconds of receipt" end-to-end budget for `Knowledge_Agent`, which
must still run a model call to compose the cited answer after this tool
returns). Read live from the environment on every call, not cached at
import time, matching `tools/alert_tools.py::get_channel_limits`'s
established live-read-at-call-time rationale.
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import Any, Final, TypedDict

from strands import tool

from integrations import get_knowledge_provider
from integrations.knowledge_provider import KnowledgeProviderConfigurationError

__all__ = [
    "KNOWLEDGE_MAX_PASSAGE_COUNT_ENV_VAR",
    "KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS_ENV_VAR",
    "DEFAULT_KNOWLEDGE_MAX_PASSAGE_COUNT",
    "DEFAULT_KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS",
    "retrieve_passages",
]

# ---------------------------------------------------------------------------
# Configuration (see module docstring's "Configuration judgment call")
# ---------------------------------------------------------------------------

KNOWLEDGE_MAX_PASSAGE_COUNT_ENV_VAR: Final[str] = "THUNAI_KNOWLEDGE_MAX_PASSAGE_COUNT"
KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS_ENV_VAR: Final[str] = "THUNAI_KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS"

DEFAULT_KNOWLEDGE_MAX_PASSAGE_COUNT: Final[int] = 5
"""Matches `BedrockKBKnowledgeProvider.retrieve`'s own `max_results=5`
default (Req 8.1's "at most the configured maximum passage count")."""

DEFAULT_KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS: Final[float] = 8.0
"""Leaves headroom under Req 8.1's 10-second end-to-end response budget for
`Knowledge_Agent`'s own model call after retrieval returns (Req 8.7's
"configured retrieval timeout")."""


def _read_int_env(var_name: str, default: int) -> int:
    raw = os.environ.get(var_name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _read_float_env(var_name: str, default: float) -> float:
    raw = os.environ.get(var_name)
    if raw is None or raw == "":
        return default
    return float(raw)


class RetrievedPassage(TypedDict):
    """One passage in `retrieve_passages`'s result, per Req 8.1 ("each
    retrieved passage carrying a relevance score") and Req 8.2 ("each
    citation carrying the passage identifier and the knowledge source
    version")."""

    text: str
    score: float
    source_id: str
    source_version: str | None
    document_id: str | None


class RetrievePassagesResult(TypedDict):
    ok: bool
    query: str
    passages: list[RetrievedPassage]
    passage_count: int
    no_relevant_passages_found: bool
    reason: str | None


# ---------------------------------------------------------------------------
# retrieve_passages (Req 8.1, 8.1a, 8.2, 8.7)
# ---------------------------------------------------------------------------


@tool
def retrieve_passages(query: str, max_results: int | None = None) -> RetrievePassagesResult:
    """Retrieve relevant passages for a resident safety question from Knowledge_Base.

    Use this to ground an answer to a practical community safety question
    (water safety, shelter guidance, evacuation kit contents, evacuation
    basics, or returning home after a flood) in the curated knowledge base.
    Do NOT use this for hazard sensor readings, incident status, dispatch,
    or alert delivery — those are Monitor/Intake/Dispatch/Alert tools, not
    this one. Every passage in the result carries its own relevance
    `score`, un-filtered and un-re-sorted, so the caller applies whatever
    relevance threshold and citation logic it needs; this tool makes no
    answer/refuse decision itself. Never raises for an expected retrieval
    failure — check the returned `ok` field instead of assuming success.

    Args:
        query: The resident's safety question, or any other free-text
            query to search the knowledge base with, e.g. "Is it safe to
            cross the bridge right now?".
        max_results: The maximum number of passages to return. Defaults to
            the configured maximum passage count
            (`THUNAI_KNOWLEDGE_MAX_PASSAGE_COUNT`, default 5) when omitted
            or `None` (Req 8.1).

    Returns:
        On success:
        {
          "ok": true,
          "query": "<the query passed in>",
          "passages": [
            {
              "text": str,
              "score": float,
              "source_id": str,               # citation identifier, e.g. an S3 URI
              "source_version": str | null,    # the knowledge source version (Req 8.2)
              "document_id": str | null,
            }, ...
          ],  # ordered by descending relevance score, as returned by Knowledge_Base
          "passage_count": <int, len(passages)>,
          "no_relevant_passages_found": <true iff passages == []>,
          "reason": null
        }

        On failure (retrieval unavailable, misconfigured, or timed out —
        Req 8.7 — never raised):
        {
          "ok": false,
          "query": "<the query passed in>",
          "passages": [],
          "passage_count": 0,
          "no_relevant_passages_found": true,
          "reason": "knowledge_base_unavailable"
        }
        The caller (`Knowledge_Agent`) is expected to route the question to
        the coordinator through Escalation_Service on this outcome (Req
        8.7), exactly as it must on an `ok: true` result whose
        `no_relevant_passages_found` is `true` (Req 8.6).
    """
    effective_max_results = (
        max_results if max_results is not None else _read_int_env(
            KNOWLEDGE_MAX_PASSAGE_COUNT_ENV_VAR, DEFAULT_KNOWLEDGE_MAX_PASSAGE_COUNT
        )
    )
    timeout_seconds = _read_float_env(
        KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS_ENV_VAR, DEFAULT_KNOWLEDGE_RETRIEVAL_TIMEOUT_SECONDS
    )

    try:
        provider = get_knowledge_provider()
    except KnowledgeProviderConfigurationError:
        return _unavailable_result(query)

    def _do_retrieve() -> list[Any]:
        return provider.retrieve(query, max_results=effective_max_results)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_do_retrieve)
            passages = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        return _unavailable_result(query)
    except Exception:  # noqa: BLE001 - any provider failure is reported, never raised (Req 8.7)
        return _unavailable_result(query)

    retrieved: list[RetrievedPassage] = [
        RetrievedPassage(
            text=passage.text,
            score=float(passage.score),
            source_id=passage.source_id,
            source_version=passage.source_version,
            document_id=passage.document_id,
        )
        for passage in passages
    ]

    return RetrievePassagesResult(
        ok=True,
        query=query,
        passages=retrieved,
        passage_count=len(retrieved),
        no_relevant_passages_found=len(retrieved) == 0,
        reason=None,
    )


def _unavailable_result(query: str) -> RetrievePassagesResult:
    return RetrievePassagesResult(
        ok=False,
        query=query,
        passages=[],
        passage_count=0,
        no_relevant_passages_found=True,
        reason="knowledge_base_unavailable",
    )
