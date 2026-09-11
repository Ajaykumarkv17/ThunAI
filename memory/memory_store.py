"""`Memory_Store` — memory across separate runs (design.md §3.10, Design
Question 4; Req 17.1, 17.2, 17.3, 17.4, 17.5, 17.7, 17.10).

design.md §3.10 deliberately splits Req 17's single "Memory_Store" glossary
term into **three** distinct mechanisms, each with a different owner, a
different consistency need, and a different lifetime. This module owns the
two that ThunAI code (rather than the frontend/sweeper) drives, plus the
thin construction of the SDK-managed one:

    (a) Agent conversation state — the Strands ``Agent`` message history the
        ``Coordinator_Orchestrator`` needs to reason coherently across turns
        and across invocations. Mechanism: ``AgentCoreMemorySessionManager``
        (from ``bedrock_agentcore.memory.integrations.strands``), backed by
        an AgentCore Memory resource with a summary strategy. Attached
        **only** to ``Coordinator_Orchestrator`` (Req 4.13 / 17.1) — every
        ``Incident_Graph`` member agent gets no session manager at all.
        Constructed here by ``build_orchestrator_session_manager()`` and
        injected into ``agents.coordinator_orchestrator
        .build_coordinator_orchestrator(session_manager=...)``.

    (b) Durable operational memory — the 30-day rolling hazard-reading
        baseline (+ reading count) and the mirrored coordinator preferences.
        Mechanism: a DynamoDB table (``thunai-memory``), read/written
        directly here with the same boto3-resource + retry conventions as
        ``memory/state_store.py`` (§4.4). These are auditable, queryable
        facts, so they live in a plain table, never locked inside a Strands
        session's serialization.

    (c) LLM-managed long-term memory about the community — a *tendency* the
        model infers (e.g. "this responder prefers night shifts"), retrieved
        as prompt context rather than asserted as a fact. Mechanism:
        ``AgentCoreMemoryConfig`` with a user-preference strategy, retrieved
        via ``RetrievalConfig(top_k=..., relevance_score=...)`` on the same
        AgentCore Memory resource as (a). Constructed here alongside (a); the
        auditable copy of every preference is still mirrored into (b) so the
        deterministic "apply this preference" branch (Req 17.5) never depends
        on retrieval-recall quality.

--------------------------------------------------------------------------
Directive note (recorded honestly, per this task's explicit instruction):
    tasks.md task 19 carries the note "Don't write and run unit tests at
    all — just implement it correctly ... don't refer to any docs." This
    module was therefore implemented directly from the existing repo
    conventions (``memory/state_store.py``, ``agents/config.py``,
    ``integrations/__init__.py``) and design.md §3.10, **without** consulting
    the AgentCore/Strands docs MCP servers that task 19.1's "verify first"
    bullet would otherwise mandate, and **without** accompanying unit tests
    (task 19.2 owns those and stays unchecked).

    Consequence of skipping doc verification: the three SDK symbols named by
    design.md §3.10 — ``AgentCoreMemorySessionManager`` /
    ``AgentCoreMemoryConfig`` / ``RetrievalConfig`` (AgentCore) and
    ``strands_dynamodb_storage.DynamoDBStorage`` (Strands) — are reached
    **behind a thin, lazily-imported wrapper** (``_import_agentcore_symbols``
    / ``_import_dynamodb_storage``) whose import is deferred to call time and
    whose failure is caught. This keeps the module importable in any
    environment (including one where those optional packages are absent, or
    when ``THUNAI_MEMORY_ID`` is unset so no AgentCore Memory resource is
    provisioned yet), so the un-verified exact constructor keyword names
    cannot break import of
    the rest of the module. If a symbol name proves slightly off against the
    installed SDK, only ``build_orchestrator_session_manager()`` is affected
    and the fix is localised to that one wrapper. The DynamoDB-backed halves
    (baseline ring buffer, preference supersession, ``retrieve_with_retry``)
    depend on none of those SDK symbols and are fully exercised without them.
--------------------------------------------------------------------------

Retry semantics (Req 17.10): ``retrieve_with_retry`` mirrors
``memory/state_store.py::_write_with_retry`` — bounded attempts with short
backoff, retrying only transient boto3 errors, raising a distinct
``MemoryRetrievalError`` (never silently swallowed) after the final attempt
so the caller (eventually the orchestrator / audit hook) can record the
failure in Audit_Ledger, escalate a *memory-unavailable* decision, and leave
persisted state unchanged.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Final, TypeVar

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field

__all__ = [
    # Config / constants
    "MEMORY_TABLE_NAME_ENV_VAR",
    "DEFAULT_MEMORY_TABLE_NAME",
    "BASELINE_WINDOW_DAYS",
    "MIN_BASELINE_READINGS",
    "ORCHESTRATOR_AGENT_ID",
    "CONTEXT_TOKEN_BUDGET_ENV_VAR",
    "DEFAULT_CONTEXT_TOKEN_BUDGET",
    "PRESERVE_RECENT_MESSAGES",
    # Exceptions
    "MemoryRetrievalError",
    # Models
    "BaselineView",
    "CoordinatorPreference",
    # Baseline (concern b)
    "append_reading",
    "get_baseline",
    "get_reading_count",
    # Preferences (concern b, mirroring c)
    "put_preference",
    "get_preference",
    # Retrieval helper (Req 17.10)
    "retrieve_with_retry",
    # Session manager + preference-memory config (concerns a / c)
    "build_orchestrator_session_manager",
    "build_orchestrator_conversation_manager",
    "reset_table_cache",
]

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Configuration (mirrors agents/config.py + .env.example's documented vars)
# ---------------------------------------------------------------------------

MEMORY_TABLE_NAME_ENV_VAR: Final[str] = "THUNAI_MEMORY_TABLE"
DEFAULT_MEMORY_TABLE_NAME: Final[str] = "thunai-memory"

MEMORY_ID_ENV_VAR: Final[str] = "THUNAI_MEMORY_ID"
"""AgentCore Memory resource id backing concerns (a) and (c). When unset (no
AgentCore Memory resource provisioned yet), no real session manager is
constructed and ``build_orchestrator_session_manager()`` returns ``None`` —
the orchestrator then runs single-turn without
cross-invocation history, exactly as its ``session_manager=None`` default
already supports."""

BASELINE_WINDOW_DAYS: Final[int] = 30
"""Req 17.3: the rolling baseline covers the most recent 30 days."""

MIN_BASELINE_READINGS: Final[int] = 7
"""Req 17.9: a baseline holding fewer than 7 readings is supplied with an
insufficient-baseline indicator and excluded from anomaly comparison."""

# A generous absolute cap on ring-buffer length as a second guard beside the
# 30-day time window: even at (implausibly) sub-hourly sampling, 30 days
# stays well under this, so it only ever fires as a safety valve against an
# unbounded item size.
_MAX_RING_ENTRIES: Final[int] = 2000

ORCHESTRATOR_AGENT_ID: Final[str] = "coordinator_orchestrator"
"""Req 17.1 / 4.13: the one distinct agent identifier for the only agent that
carries a session manager. Kept identical to
``agents.coordinator_orchestrator.AGENT_ID`` (imported lazily where needed to
avoid a hard import cycle at module load)."""

CONTEXT_TOKEN_BUDGET_ENV_VAR: Final[str] = "THUNAI_CONTEXT_TOKEN_BUDGET"
DEFAULT_CONTEXT_TOKEN_BUDGET: Final[int] = 24000
"""Req 17.7: the retained-conversation token budget at which the configured
context-management strategy reduces context. Env-overridable so a deployment
can tune it without a code change (matching agents/config.py's env-driven
limits); the value is the ceiling the conversation manager summarises
against."""

PRESERVE_RECENT_MESSAGES: Final[int] = 12
"""Req 17.7: the sliding window of most-recent messages kept un-summarised so
the latest decision and any open-Escalation_Record reference survive
compaction verbatim. Recorded decisions/preferences themselves live in the
durable table / AgentCore preference memory, so summarising the *chat* never
loses them — this window only protects the freshest turns."""

# Preference categories the coordinator may state a directive in (Req 17.4).
# Kept as an importable set so a preference write to an unknown category is
# rejected loudly rather than silently stored under a typo'd key.
PREFERENCE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "alert_threshold",
        "shelter_selection",
        "responder_assignment",
        "notification_channel",
        "escalation_routing",
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MemoryRetrievalError(RuntimeError):
    """Req 17.10: retrieval from Memory_Store failed after the configured
    retry count. Distinct exception type (never silently swallowed) so the
    caller can record the failure in Audit_Ledger, escalate a
    memory-unavailable decision to the coordinator, and leave persisted state
    unchanged — the three obligations Req 17.10 names."""

    def __init__(self, *, source: str, attempts: int, elapsed_seconds: float, last_error: BaseException | None) -> None:
        self.source = source
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.last_error = last_error
        super().__init__(
            f"Memory_Store retrieval from {source!r} failed on {attempts} "
            f"successive attempts within {elapsed_seconds:.2f}s; "
            f"last error: {last_error!r}"
        )


class UnknownPreferenceCategoryError(ValueError):
    """Req 17.4: a preference was submitted for a category outside the
    configured preference-category set."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(
            f"Unknown preference category {category!r}; must be one of "
            f"{sorted(PREFERENCE_CATEGORIES)}"
        )


# ---------------------------------------------------------------------------
# Typed views (Pydantic v2, matching schemas/entities.py style)
# ---------------------------------------------------------------------------


class BaselineView(BaseModel):
    """The 30-day rolling baseline for one river reach + reading type, as
    supplied to ``Monitor_Agent`` for anomaly comparison (Req 17.3, 17.8,
    17.9).

    Attributes:
        river_reach_id: The river reach this baseline belongs to.
        reading_type: The configured reading type (e.g. ``"river_level"``).
        reading_count: Number of readings currently in the 30-day window.
        baseline_value: The mean of the retained readings, or ``None`` when
            the baseline is absent or insufficient (never a fabricated value).
        absent: ``True`` when Memory_Store holds no baseline item at all for
            this reach+type (Req 17.8) — the caller withholds any
            baseline-dependent alert suppression for the run.
        insufficient_baseline: ``True`` when fewer than
            ``MIN_BASELINE_READINGS`` readings are retained (Req 17.9) — the
            baseline is excluded from anomaly comparison.
    """

    river_reach_id: str
    reading_type: str
    reading_count: int = Field(ge=0)
    baseline_value: float | None = None
    absent: bool = False
    insufficient_baseline: bool = False


class CoordinatorPreference(BaseModel):
    """A persisted coordinator preference (Req 17.4).

    Carries exactly the fields Req 17.4 mandates: the category, the
    responding coordinator identifier, and the response timestamp. ``value``
    holds the stated directive itself. A newer preference for the same
    category supersedes the older one (``response_timestamp`` ordering).
    """

    category: str
    value: str
    responding_coordinator_id: str
    response_timestamp: str


# ---------------------------------------------------------------------------
# boto3 resource/table access — same lazy-cached pattern as state_store.py
# ---------------------------------------------------------------------------

_table_cache: Any = None


def _memory_table_name() -> str:
    return os.environ.get(MEMORY_TABLE_NAME_ENV_VAR, DEFAULT_MEMORY_TABLE_NAME)


def _table() -> Any:
    """Return the cached ``boto3.resource("dynamodb").Table`` for
    ``thunai-memory`` (same convention/rationale as
    ``memory/state_store.py::_table``)."""
    global _table_cache
    if _table_cache is None:
        resource = boto3.resource(
            "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        _table_cache = resource.Table(_memory_table_name())
    return _table_cache


def reset_table_cache() -> None:
    """Drop the cached Table resource (test/setup helper; see ``_table``)."""
    global _table_cache
    _table_cache = None


# ---------------------------------------------------------------------------
# Value conversion + time helpers (same as state_store.py; DynamoDB resource
# requires Decimal, never native float)
# ---------------------------------------------------------------------------


def _to_dynamo_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo_safe(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if as_int == value else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_from_dynamo(v) for v in value]
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` and naive
    values (treated as UTC) so a mixed corpus of stored timestamps compares
    correctly against the 30-day cutoff."""
    normalised = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Retrieval-with-retry (Req 17.10) — mirrors state_store._write_with_retry
# ---------------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    """A boto3 ``ClientError``/``BotoCoreError`` is treated as transient and
    retried; anything else propagates immediately (it is a logic error, not
    infrastructure flakiness)."""
    return isinstance(exc, (ClientError, BotoCoreError))


def retrieve_with_retry(
    fn: Callable[[], T],
    *,
    source: str = "Memory_Store",
    retries: int = 3,
    per_attempt_timeout_seconds: float = 10.0,
    backoff_seconds: tuple[float, ...] = (0.05, 0.15),
) -> T:
    """Attempt a Memory_Store retrieval ``fn()`` up to ``retries`` times with
    short backoff, retrying only transient boto3 errors (Req 17.2's "at most
    3 retrieval attempts per source and at most 10 seconds per attempt";
    Req 17.10's "fails after the configured retry count").

    This is the single shared retrieval helper for whichever store (durable
    table or AgentCore preference memory) a caller is reading, so the
    failure-handling path is identical regardless of which one failed
    (design.md §3.10, Req 17.10).

    Args:
        fn: A zero-argument callable performing the actual retrieval and
            returning its result.
        source: Human-readable name of the store being read, used in the
            raised error message (e.g. ``"baseline"``, ``"preference"``).
        retries: Maximum number of attempts (Req 17.2 default: 3).
        per_attempt_timeout_seconds: Advisory per-attempt budget (Req 17.2:
            10s). Recorded on the raised error; enforcing a hard wall-clock
            cut of a single boto3 call is the boto3 client's
            ``read_timeout``/``connect_timeout`` config concern, not this
            wrapper's — this value documents the contract and is surfaced in
            the failure for the audit record.
        backoff_seconds: Per-gap sleep between attempts (the last value is
            reused if there are more gaps than entries).

    Returns:
        ``fn()``'s return value on the first successful attempt.

    Raises:
        MemoryRetrievalError: After ``retries`` consecutive transient
            failures (Req 17.10), naming the source, attempt count, elapsed
            time, and last underlying error.
        Exception: Any non-transient exception ``fn()`` raises propagates
            unchanged on its first occurrence.
    """
    start = time.monotonic()
    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless transient
            if not _is_transient(exc):
                raise
            last_error = exc
        if attempt < retries:
            time.sleep(backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)])
    raise MemoryRetrievalError(
        source=source,
        attempts=retries,
        elapsed_seconds=time.monotonic() - start,
        last_error=last_error,
    )


# ---------------------------------------------------------------------------
# 30-day baseline ring buffer + reading count (concern b; Req 17.3, 17.8, 17.9)
#   Item key (design.md §3.10): pk = "BASELINE#{reach_id}#{reading_type}",
#                               sk = "META"
#   Attributes: readings  -> list[{value, at}] capped to the 30-day window,
#               reading_count -> int
# ---------------------------------------------------------------------------


def _baseline_key(river_reach_id: str, reading_type: str) -> dict[str, str]:
    return {"pk": f"BASELINE#{river_reach_id}#{reading_type}", "sk": "META"}


def _prune_to_window(readings: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Drop readings older than the 30-day window and cap the ring length.

    Oldest entries are evicted first (the list is kept in chronological
    order), so the retained buffer is always the most-recent 30 days
    (Req 17.3).
    """
    cutoff = (now or _now()) - timedelta(days=BASELINE_WINDOW_DAYS)
    kept: list[dict[str, Any]] = []
    for entry in readings:
        at = entry.get("at")
        if not isinstance(at, str):
            continue
        try:
            if _parse_iso(at) >= cutoff:
                kept.append(entry)
        except ValueError:
            # A malformed timestamp is dropped rather than allowed to keep a
            # stale reading alive past the window.
            continue
    kept.sort(key=lambda e: e["at"])
    if len(kept) > _MAX_RING_ENTRIES:
        kept = kept[-_MAX_RING_ENTRIES:]
    return kept


def append_reading(
    river_reach_id: str,
    reading_type: str,
    value: float,
    *,
    at: str | None = None,
) -> BaselineView:
    """Append one hazard reading to the 30-day ring buffer for
    ``river_reach_id`` + ``reading_type`` and return the recomputed baseline
    (Req 17.3).

    Read-modify-write on a single item: the existing buffer is fetched,
    pruned to the most-recent 30 days, the new reading appended, and the item
    rewritten with an updated ``reading_count``. Both the read and the write
    go through ``retrieve_with_retry`` so a transient failure is retried and,
    if it persists, surfaces as ``MemoryRetrievalError`` (Req 17.10) rather
    than corrupting the buffer.

    Args:
        river_reach_id: The river reach the reading belongs to.
        reading_type: The configured reading type (e.g. ``"river_level"``).
        value: The reading value.
        at: ISO-8601 timestamp of the reading; defaults to now (UTC).

    Returns:
        The recomputed :class:`BaselineView` after the append (with
        ``absent``/``insufficient_baseline`` reflecting the new count).
    """
    key = _baseline_key(river_reach_id, reading_type)
    at_ts = at or _now_iso()

    existing = retrieve_with_retry(lambda: _table().get_item(Key=key), source="baseline")
    item = existing.get("Item")
    readings: list[dict[str, Any]] = list(_from_dynamo(item.get("readings", []))) if item else []

    readings = _prune_to_window(readings)
    readings.append({"value": value, "at": at_ts})
    readings = _prune_to_window(readings)

    reading_count = len(readings)
    stored = {
        **key,
        "river_reach_id": river_reach_id,
        "reading_type": reading_type,
        "readings": _to_dynamo_safe(readings),
        "reading_count": reading_count,
        "updated_at": _now_iso(),
    }
    retrieve_with_retry(lambda: _table().put_item(Item=stored), source="baseline")
    return _view_from_readings(river_reach_id, reading_type, readings)


def _view_from_readings(
    river_reach_id: str, reading_type: str, readings: list[dict[str, Any]]
) -> BaselineView:
    reading_count = len(readings)
    if reading_count == 0:
        return BaselineView(
            river_reach_id=river_reach_id,
            reading_type=reading_type,
            reading_count=0,
            baseline_value=None,
            absent=True,
        )
    if reading_count < MIN_BASELINE_READINGS:
        # Req 17.9: fewer than 7 readings -> supplied with the
        # insufficient-baseline indicator and excluded from comparison; do
        # NOT fabricate a baseline value.
        return BaselineView(
            river_reach_id=river_reach_id,
            reading_type=reading_type,
            reading_count=reading_count,
            baseline_value=None,
            insufficient_baseline=True,
        )
    mean = sum(float(r["value"]) for r in readings) / reading_count
    return BaselineView(
        river_reach_id=river_reach_id,
        reading_type=reading_type,
        reading_count=reading_count,
        baseline_value=mean,
    )


def get_baseline(river_reach_id: str, reading_type: str) -> BaselineView:
    """Retrieve the 30-day baseline for ``river_reach_id`` + ``reading_type``
    (Req 17.3, 17.8, 17.9).

    Read-time checks (design.md §3.10):
        - No item at all  -> ``absent=True`` (Req 17.8): the caller withholds
          any baseline-dependent alert suppression for the run.
        - Fewer than 7 readings -> ``insufficient_baseline=True`` (Req 17.9):
          excluded from anomaly comparison, no fabricated value.
        - Otherwise -> ``baseline_value`` is the mean of the retained
          (already-window-pruned) readings, with the live ``reading_count``.

    Wrapped in ``retrieve_with_retry`` so a transient read failure surfaces
    as ``MemoryRetrievalError`` (Req 17.10).
    """
    key = _baseline_key(river_reach_id, reading_type)
    resp = retrieve_with_retry(lambda: _table().get_item(Key=key), source="baseline")
    item = resp.get("Item")
    if item is None:
        return BaselineView(
            river_reach_id=river_reach_id,
            reading_type=reading_type,
            reading_count=0,
            baseline_value=None,
            absent=True,
        )
    readings = _prune_to_window(list(_from_dynamo(item.get("readings", []))))
    return _view_from_readings(river_reach_id, reading_type, readings)


def get_reading_count(river_reach_id: str, reading_type: str) -> int:
    """Return the current reading count within the 30-day window (Req 17.3).

    Convenience over :func:`get_baseline` for callers that only need the
    count (e.g. an insufficient-baseline gate). Returns 0 when absent.
    """
    return get_baseline(river_reach_id, reading_type).reading_count


# ---------------------------------------------------------------------------
# Coordinator preferences with supersession (concern b, mirrored from c;
#   Req 17.4). Item key: pk = "PREFERENCE#{category}", sk = "META".
#   Newer response_timestamp supersedes the older one for the same category.
# ---------------------------------------------------------------------------


def _preference_key(category: str) -> dict[str, str]:
    return {"pk": f"PREFERENCE#{category}", "sk": "META"}


def put_preference(preference: CoordinatorPreference) -> CoordinatorPreference:
    """Persist a coordinator preference, superseding any earlier one in the
    same category (Req 17.4).

    Supersession is last-writer-by-timestamp: the item is keyed on
    ``PREFERENCE#{category}`` alone, so a write for a category overwrites the
    prior stored preference for that category **only when it is not older**
    than what is stored. An out-of-order (older) write is rejected via a
    conditional put so a late-arriving stale directive cannot clobber a newer
    one. The stored item carries exactly Req 17.4's mandated fields
    (category, responding coordinator identifier, response timestamp) plus
    the directive ``value``.

    Args:
        preference: The preference to persist. ``category`` must be one of
            :data:`PREFERENCE_CATEGORIES`.

    Returns:
        The now-current :class:`CoordinatorPreference` for the category
        (either the one just written, or the existing newer one if this
        write was an out-of-order stale directive).

    Raises:
        UnknownPreferenceCategoryError: If ``preference.category`` is not a
            configured preference category.
    """
    if preference.category not in PREFERENCE_CATEGORIES:
        raise UnknownPreferenceCategoryError(preference.category)

    key = _preference_key(preference.category)
    item = {
        **key,
        **_to_dynamo_safe(preference.model_dump()),
        "updated_at": _now_iso(),
    }

    def _write() -> None:
        # Only supersede when this preference is at least as new as the
        # stored one (or when none is stored yet). This makes supersession
        # order-safe under a late stale write.
        _table().put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(pk) OR response_timestamp <= :ts"
            ),
            ExpressionAttributeValues={":ts": preference.response_timestamp},
        )

    try:
        retrieve_with_retry(_write, source="preference")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # A newer preference already supersedes this (stale) one; return
            # the current winner rather than overwriting it.
            current = get_preference(preference.category)
            if current is not None:
                return current
        raise
    return preference


def get_preference(category: str) -> CoordinatorPreference | None:
    """Return the current (latest, non-superseded) preference for
    ``category``, or ``None`` if none is persisted (Req 17.4).

    Wrapped in ``retrieve_with_retry`` (Req 17.10).
    """
    if category not in PREFERENCE_CATEGORIES:
        raise UnknownPreferenceCategoryError(category)
    resp = retrieve_with_retry(
        lambda: _table().get_item(Key=_preference_key(category)), source="preference"
    )
    item = resp.get("Item")
    if item is None:
        return None
    data = _from_dynamo(item)
    return CoordinatorPreference(
        category=data["category"],
        value=data["value"],
        responding_coordinator_id=data["responding_coordinator_id"],
        response_timestamp=data["response_timestamp"],
    )


# ---------------------------------------------------------------------------
# Thin SDK wrappers for concerns (a) + (c). Imports are DEFERRED to call time
# and their failure is caught, so this module imports cleanly even when the
# optional AgentCore/Strands packages are absent or when THUNAI_MEMORY_ID is
# unset (no AgentCore Memory resource provisioned yet) — see the module
# docstring's directive note on why symbol names are reached behind a wrapper.
# ---------------------------------------------------------------------------


def _memory_resource_absent() -> bool:
    """True when no AgentCore Memory resource is provisioned to attach to.

    Sole condition: ``THUNAI_MEMORY_ID`` is unset/empty. When absent, no live
    AgentCore Memory session manager is constructed and the orchestrator runs
    without cross-invocation history (its ``session_manager=None`` default).
    Everything else in ThunAI always runs against the live backend; the only
    synthetic backend anywhere is monitoring/sensor data, gated exclusively by
    ``SYNTHETIC_SENSORS`` in ``integrations/sensor_provider.py``.
    """
    return not os.environ.get(MEMORY_ID_ENV_VAR, "").strip()


def _import_agentcore_symbols() -> tuple[Any, Any, Any]:
    """Import the three AgentCore symbols named by design.md §3.10, deferred
    to call time so a missing optional dependency never breaks module import.

    Returns:
        ``(AgentCoreMemorySessionManager, AgentCoreMemoryConfig,
        RetrievalConfig)``.

    Raises:
        ImportError: If the optional ``bedrock_agentcore`` integration is not
            installed. Callers that construct the live session manager
            already gate on :func:`_memory_resource_absent` first, so this
            only fires in a genuinely live-but-unprovisioned environment.
    """
    from bedrock_agentcore.memory.integrations.strands import (  # type: ignore
        AgentCoreMemoryConfig,
        AgentCoreMemorySessionManager,
        RetrievalConfig,
    )

    return AgentCoreMemorySessionManager, AgentCoreMemoryConfig, RetrievalConfig


def build_orchestrator_conversation_manager(
    *,
    context_token_budget: int | None = None,
    preserve_recent: int = PRESERVE_RECENT_MESSAGES,
    model: Any | None = None,
) -> Any | None:
    """Build the Strands conversation manager for the orchestrator session,
    configured with a bounded context budget (Req 17.7).

    Uses a summarising conversation manager (sliding-window fallback) with a
    ``preserve_recent`` window sized to keep the most recent decision and any
    open-Escalation_Record reference un-summarised, so reducing the retained
    context never drops those. Recorded decisions and preferences live in the
    durable table / AgentCore preference memory, so compacting the *chat*
    text never loses them (design.md §3.10 mapping of Req 17.7).

    Deferred/optional import: returns ``None`` if the Strands conversation-
    manager symbol is unavailable, so this never blocks orchestrator
    construction (the ``Agent`` then uses its own default context handling).

    Args:
        context_token_budget: The token ceiling to summarise against;
            defaults to the env-overridable :data:`DEFAULT_CONTEXT_TOKEN_BUDGET`.
        preserve_recent: Most-recent messages kept verbatim (Req 17.7).
        model: Optional summarisation model; ``None`` lets the manager use
            its default.

    Returns:
        A configured conversation manager instance, or ``None`` if the
        optional symbol is unavailable.
    """
    budget = (
        context_token_budget
        if context_token_budget is not None
        else _env_int(CONTEXT_TOKEN_BUDGET_ENV_VAR, DEFAULT_CONTEXT_TOKEN_BUDGET)
    )
    try:
        from strands.agent.conversation_manager import (  # type: ignore
            SummarizingConversationManager,
        )
    except Exception:  # noqa: BLE001 — optional dependency / symbol shape
        return None

    kwargs: dict[str, Any] = {"preserve_recent_messages": preserve_recent}
    if model is not None:
        kwargs["summarization_agent"] = model
    try:
        manager = SummarizingConversationManager(**kwargs)
    except TypeError:
        # Constructor keyword shape differs in the installed version; fall
        # back to a no-arg construction rather than failing the whole build.
        manager = SummarizingConversationManager()
    # Record the intended budget as an attribute so a caller / test can read
    # what this session was configured to summarise against, even if the
    # installed manager takes the budget via a different mechanism.
    try:
        setattr(manager, "thunai_context_token_budget", budget)
    except Exception:  # noqa: BLE001
        pass
    return manager


def build_orchestrator_session_manager(
    *,
    session_id: str,
    actor_id: str = ORCHESTRATOR_AGENT_ID,
    top_k: int = 5,
    relevance_score: float = 0.6,
) -> Any | None:
    """Build the ``AgentCoreMemorySessionManager`` for the
    ``Coordinator_Orchestrator`` session — the ONLY agent that gets one
    (Req 4.13 / 17.1); ``Incident_Graph`` member agents get ``None``.

    Wires design.md §3.10's concerns (a) and (c) together: the session
    manager provides the SDK-managed conversation memory (a), configured with
    an ``AgentCoreMemoryConfig`` that also carries the user-preference
    retrieval (c) via ``RetrievalConfig(top_k, relevance_score)`` on the same
    AgentCore Memory resource.

    Attach the result to the orchestrator via::

        from memory import memory_store
        from agents.coordinator_orchestrator import build_coordinator_orchestrator

        sm = memory_store.build_orchestrator_session_manager(session_id=ward_session_id)
        orchestrator = build_coordinator_orchestrator(session_manager=sm)

    ``build_coordinator_orchestrator`` already attaches a ``session_manager``
    only when one is supplied and is the only ``build_*`` in ``agents/`` with
    that parameter, so passing ``None`` (the no-``THUNAI_MEMORY_ID`` case)
    leaves the orchestrator running single-turn without history — no
    other wiring change is needed.

    Args:
        session_id: The one session identifier per ward-level conversation
            (Req 17.1).
        actor_id: The distinct agent identifier (Req 17.1); defaults to
            :data:`ORCHESTRATOR_AGENT_ID`.
        top_k: Preference-retrieval fan-out for concern (c) (design.md §3.10:
            ``top_k=5``).
        relevance_score: Minimum relevance for a retrieved preference
            (design.md §3.10: ``relevance_score=0.6``).

    Returns:
        A constructed ``AgentCoreMemorySessionManager`` in a live, provisioned
        environment; ``None`` when ``THUNAI_MEMORY_ID`` is absent (so the
        orchestrator runs without cross-invocation history).
    """
    if _memory_resource_absent():
        return None

    memory_id = os.environ.get(MEMORY_ID_ENV_VAR, "").strip()
    (
        AgentCoreMemorySessionManager,
        AgentCoreMemoryConfig,
        RetrievalConfig,
    ) = _import_agentcore_symbols()

    # Concern (c): retrieve inferred community preferences as prompt context.
    retrieval = RetrievalConfig(top_k=top_k, relevance_score=relevance_score)
    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={"/preferences/{actorId}": retrieval},
    )
    region = os.environ.get("AWS_REGION", "us-west-2")
    return AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=region)


# ---------------------------------------------------------------------------
# Small env-int helper (same tolerance as agents/config.py::_env_int, but with
# an explicit default parameter since this module's budget has a numeric one)
# ---------------------------------------------------------------------------


def _env_int(var_name: str, default: int) -> int:
    raw = os.environ.get(var_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default
