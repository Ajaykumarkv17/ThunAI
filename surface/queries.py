"""Read-side list/read helpers for the human surfaces (Req 1.7, 12.1, 12.4, 12.7, 12.8).

Task 17.3. This is the one module the Coordinator_Console read paths (via the
console's API handler) call for the four surfaced list types design.md
§ Correctness Properties / Property 20 names:

===================  ==========================================  ===========
List type            Declared sort key                           Truncation
===================  ==========================================  ===========
recent runs          ``started_at`` descending                   20 (Req 1.7)
decision inbox       ``response_deadline`` ascending             none (Req 12.1)
open incidents       severity descending, then ``updated_at``    none (Req 12.4)
                     descending
incident audit       entry ``timestamp`` ascending               none (Req 12.7)
===================  ==========================================  ===========

Every list is fetched through a **GSI query, never a table scan** (design.md
§4.4's overloaded GSI1 on ``thunai-state``, and ``thunai-audit``'s ``gsi1``):

- recent runs      -> ``GSI1``, ``gsi1pk = "RUN_BY_START"``,
                      ``ScanIndexForward=False``, ``Limit=20``
                      (``memory.state_store.query_recent_runs``)
- decision inbox   -> ``GSI1``, ``gsi1pk = "ESCALATION_BY_STATUS#OPEN"``,
                      ``ScanIndexForward=True``
                      (``memory.state_store.query_open_escalations``)
- open incidents   -> ``GSI1``, ``gsi1pk = "INCIDENT_BY_SEVERITY"``,
                      ``ScanIndexForward=False``
                      (``memory.state_store.query_incidents_by_severity``)
- incident audit   -> ``thunai-audit`` ``gsi1``,
                      ``gsi1pk = "INCIDENT#{incident_id}"``,
                      ``ScanIndexForward=True``
                      (``memory.audit_ledger.get_entries_for_incident``)

**Why the ordering is also re-applied in Python.** The GSI sort key already
returns rows in the declared order, so the ``order_*`` functions below are not
compensating for an unordered query. They exist because (a) the same ordering
must hold for a list assembled client-side from real-time AppSync merge-on-
update events (design.md §3.8) and from a partially-consistent GSI read — a
GSI is eventually consistent, so a just-committed item can arrive out of
position — and (b) Property 20 is stated over "any generated collection of
0-40 items ... with randomised out-of-order input ordering", which is only
testable against a pure function. Each ``order_*`` function is therefore pure,
total, and stable, and each ``list_*`` function is exactly "query the GSI, then
apply the matching ``order_*``".

Empty states (Req 1.7's "explicit empty state when no runs exist", extended to
all four lists by Property 20's "presents the declared explicit empty state
when the collection is empty") are returned as data on :class:`ListView`, not
rendered here: this module is backend-side, and the wording a user reads is the
React component's (task 25.x). ``ListView.empty_state`` carries the declared
message so both surfaces agree on one string per list type.

Personal-identifier handling (Req 12.7 "the tool inputs with direct personal
identifiers excluded"): audit entries are redacted **before** they are
persisted (``harness/audit.py::append_audit_entry`` calls
``harness.redaction.redact_fields`` on the way in), so this module performs no
redaction of its own — re-redacting a stored entry would imply the ledger might
hold unredacted values, which it never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Sequence

from memory import audit_ledger, state_store
from policy.escalation_policy import SEVERITY_BANDS_ASCENDING
from schemas.entities import AuditEntry, EscalationRecord, Incident, Shelter
from schemas.lifecycle import RequestState

__all__ = [
    "MAX_RECENT_RUNS",
    "SORT_KEYS",
    "EMPTY_STATES",
    "ListView",
    "RunSummary",
    "IncidentSummary",
    "ShelterAvailability",
    "InboxEntry",
    "AuditView",
    "order_runs",
    "order_decision_inbox",
    "order_open_incidents",
    "order_incident_audit",
    "list_recent_runs",
    "list_decision_inbox",
    "list_open_incidents",
    "list_incident_audit",
]


# ---------------------------------------------------------------------------
# Declared sort keys, truncation limits, and empty states (Property 20).
# ---------------------------------------------------------------------------

MAX_RECENT_RUNS: Final[int] = 20
"""Req 1.7/12.8: "up to the 20 most recent runs". The only truncated list."""

SORT_KEYS: Final[dict[str, str]] = {
    "runs": "started_at descending",
    "decision_inbox": "response_deadline ascending",
    "open_incidents": "severity_band descending, then updated_at descending",
    "incident_audit": "timestamp ascending",
}
"""The declared sort key per list type, surfaced so a caller (and Property 20)
reads the ordering contract from the code rather than restating it."""

EMPTY_STATES: Final[dict[str, str]] = {
    "runs": "No runs have been recorded yet.",
    "decision_inbox": "No decisions are waiting on you.",
    "open_incidents": "No incidents are open.",
    "incident_audit": "No audit entries have been recorded for this incident yet.",
}
"""The explicit empty-state message per list type (Req 1.7, Property 20)."""


@dataclass(frozen=True)
class ListView:
    """One surfaced list, with its ordering/truncation contract attached.

    Attributes:
        list_type: One of the keys of :data:`SORT_KEYS`.
        items: The ordered (and, for runs, truncated) items.
        sort_key: The declared sort key applied, from :data:`SORT_KEYS`.
        max_items: The declared truncation limit, or ``None`` for an
            untruncated list type.
        total_count: How many items existed before truncation.
        truncated: Whether truncation dropped any item.
        empty: Whether the collection is empty.
        empty_state: The declared empty-state message (always populated, so a
            surface never has to invent one; meaningful only when ``empty``).
    """

    list_type: str
    items: list[Any] = field(default_factory=list)
    sort_key: str = ""
    max_items: int | None = None
    total_count: int = 0
    truncated: bool = False
    empty: bool = True
    empty_state: str = ""


def _view(list_type: str, items: list[Any], *, total_count: int, max_items: int | None) -> ListView:
    return ListView(
        list_type=list_type,
        items=items,
        sort_key=SORT_KEYS[list_type],
        max_items=max_items,
        total_count=total_count,
        truncated=total_count > len(items),
        empty=not items,
        empty_state=EMPTY_STATES[list_type],
    )


# ---------------------------------------------------------------------------
# Row shapes. Each carries exactly the fields its requirement names — no more,
# so a surface cannot accidentally display something the requirement did not
# authorise (notably Req 12.10's "disclose no ... content" posture).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """One recent run (Req 1.7 + Req 12.8's cost/latency columns)."""

    run_id: str
    trigger_type: str | None
    trigger_source_id: str | None
    started_at: str
    terminal_status: str | None
    # Req 12.8 columns.
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    model_id: str | None = None
    estimated_cost: float | None = None
    currency: str | None = None


@dataclass(frozen=True)
class ShelterAvailability:
    """One shelter's availability (Req 12.4: unoccupied places and total capacity)."""

    shelter_id: str
    name: str
    unoccupied_places: int
    total_capacity: int


@dataclass(frozen=True)
class IncidentSummary:
    """One open incident row (Req 12.4)."""

    incident_id: str
    severity_band: str
    affected_areas: list[str]
    open_request_count: int
    assigned_responder_count: int
    shelters: list[ShelterAvailability]
    updated_at: str


@dataclass(frozen=True)
class InboxEntry:
    """One open escalation as Decision_Inbox presents it (Req 12.1)."""

    escalation_id: str
    decision_summary: str
    reason: str
    stakes: str
    default_action: str
    response_deadline: str
    remaining_minutes: int
    remaining_seconds: int
    expired: bool
    options: list[dict[str, str]]
    notification_delivery_failed: bool


@dataclass(frozen=True)
class AuditView:
    """One audit entry as the per-incident audit trail presents it (Req 12.7)."""

    timestamp: str
    tool_name: str
    inputs: dict[str, Any]
    outcome: str
    approving_human_id: str | None


# ---------------------------------------------------------------------------
# Pure ordering + truncation (Property 20). Each function is total: it never
# raises on a missing or malformed sort-key value, it substitutes a value that
# sorts last, so one bad row can never hide the rest of the list.
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def order_runs(runs: Sequence[Any], *, limit: int = MAX_RECENT_RUNS) -> list[Any]:
    """Order runs by start timestamp descending, truncated to ``limit`` (Req 1.7).

    Args:
        runs: Run records (``dict`` or :class:`RunSummary`), in any order.
        limit: The truncation limit; defaults to :data:`MAX_RECENT_RUNS`.

    Returns:
        The ``limit`` most recently started runs, newest first. Ties on
        ``started_at`` are broken by ``run_id`` descending, so the order is
        total and repeatable rather than dependent on input order.
    """
    def key(run: Any) -> tuple[str, str]:
        return (_text(_get(run, "started_at")), _text(_get(run, "run_id")))

    return sorted(runs, key=key, reverse=True)[: max(0, int(limit))]


def order_decision_inbox(escalations: Sequence[Any]) -> list[Any]:
    """Order open escalations by response deadline, soonest first (Req 12.1).

    A record with no ``response_deadline`` sorts last (an absent deadline
    cannot be "soonest") rather than raising.
    """
    def key(record: Any) -> tuple[int, str, str]:
        deadline = _text(_get(record, "response_deadline"))
        return (1 if not deadline else 0, deadline, _text(_get(record, "escalation_id")))

    return sorted(escalations, key=key)


def order_open_incidents(incidents: Sequence[Any]) -> list[Any]:
    """Order incidents by severity band descending, then most recent update
    first (Req 12.4).

    Severity rank comes from ``policy.escalation_policy
    .SEVERITY_BANDS_ASCENDING`` — the one authoritative band ordering — so this
    ordering can never drift from the ordering the GSI1 sort key encodes
    (``memory.state_store._severity_rank`` reads the same tuple). An
    unrecognised band ranks below every declared band rather than raising.
    """
    def key(incident: Any) -> tuple[int, str, str]:
        band = _text(_get(incident, "severity_band"))
        try:
            rank = SEVERITY_BANDS_ASCENDING.index(band)
        except ValueError:
            rank = -1
        return (rank, _text(_get(incident, "updated_at")), _text(_get(incident, "incident_id")))

    return sorted(incidents, key=key, reverse=True)


def order_incident_audit(entries: Sequence[Any]) -> list[Any]:
    """Order audit entries by entry timestamp, oldest first (Req 12.7).

    Ties on ``timestamp`` are broken by ``entry_id`` ascending — the same
    ``sk = "{iso_timestamp}#{entry_id}"`` composition ``thunai-audit`` uses, so
    the in-memory order matches the persisted order exactly.
    """
    def key(entry: Any) -> tuple[str, str]:
        return (_text(_get(entry, "timestamp")), _text(_get(entry, "entry_id")))

    return sorted(entries, key=key)


def _get(row: Any, name: str) -> Any:
    """Read ``name`` off a dict, dataclass, or Pydantic model row.

    The ``order_*`` functions are used both on raw DynamoDB dicts (from
    ``state_store``) and on typed rows (``Incident``, ``EscalationRecord``,
    :class:`RunSummary`), and Property 20 generates whichever is convenient, so
    field access is uniform rather than shape-specific.
    """
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


# ---------------------------------------------------------------------------
# Recent runs (Req 1.7, 12.8).
# ---------------------------------------------------------------------------


def _to_run_summary(run: dict[str, Any]) -> RunSummary:
    input_tokens = run.get("input_tokens")
    output_tokens = run.get("output_tokens")
    total = None
    if input_tokens is not None or output_tokens is not None:
        total = int(input_tokens or 0) + int(output_tokens or 0)
    latency = run.get("latency_ms")
    cost = run.get("estimated_cost_minor_units")
    return RunSummary(
        run_id=str(run.get("run_id", "")),
        trigger_type=run.get("trigger_type"),
        trigger_source_id=run.get("trigger_source_id"),
        started_at=str(run.get("started_at", "")),
        # Req 1.7 asks for the *terminal* status; a run still in progress has
        # none yet, and its live `status` is surfaced separately rather than
        # being passed off as terminal.
        terminal_status=run.get("terminal_status"),
        input_tokens=None if input_tokens is None else int(input_tokens),
        output_tokens=None if output_tokens is None else int(output_tokens),
        total_tokens=total,
        latency_ms=None if latency is None else int(latency),
        model_id=run.get("model_id"),
        estimated_cost=None if cost is None else float(cost),
        currency=run.get("currency"),
    )


def list_recent_runs(limit: int = MAX_RECENT_RUNS) -> ListView:
    """The most recent runs, newest start first, truncated to ``limit`` (Req 1.7, 12.8).

    Args:
        limit: Maximum runs to return; defaults to the Req 1.7 maximum of 20.

    Returns:
        A :class:`ListView` of :class:`RunSummary` rows.
    """
    limit = max(0, int(limit))
    # One extra row is requested so `truncated` can be reported honestly: the
    # DynamoDB `Limit` alone cannot distinguish "exactly 20 runs exist" from
    # "more than 20 exist".
    fetched = state_store.query_recent_runs(limit=limit + 1) if limit else []
    ordered = order_runs(fetched, limit=limit)
    return _view(
        "runs",
        [_to_run_summary(run) for run in ordered],
        total_count=len(fetched),
        max_items=limit,
    )


# ---------------------------------------------------------------------------
# Decision_Inbox (Req 12.1).
# ---------------------------------------------------------------------------


def _remaining(deadline: str, now: datetime) -> tuple[int, int, bool]:
    """Return ``(whole_minutes, seconds, expired)`` remaining until ``deadline``.

    Req 12.1: "the remaining time before the response deadline expressed in
    whole minutes and seconds". A past (or unparseable) deadline yields
    ``(0, 0, True)`` — the deadline has effectively expired and the timeout
    sweeper's default action applies — rather than a negative countdown.
    """
    try:
        parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return (0, 0, True)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    remaining = int((parsed - now).total_seconds())
    if remaining <= 0:
        return (0, 0, True)
    return (remaining // 60, remaining % 60, False)


def _to_inbox_entry(record: EscalationRecord, now: datetime) -> InboxEntry:
    minutes, seconds, expired = _remaining(record.response_deadline, now)
    return InboxEntry(
        escalation_id=record.escalation_id,
        decision_summary=record.decision_summary,
        reason=record.reason,
        stakes=record.stakes,
        default_action=record.default_action,
        response_deadline=record.response_deadline,
        remaining_minutes=minutes,
        remaining_seconds=seconds,
        expired=expired,
        options=[{"option_id": o.option_id, "label": o.label} for o in record.options],
        # Req 11.11: the inbox shows a delivery-failure state on a record whose
        # out-of-band notification never got through, so the coordinator can
        # still resolve it from the console.
        notification_delivery_failed=record.notification_delivery_failed,
    )


def list_decision_inbox(*, now: datetime | None = None) -> ListView:
    """Every open escalation, soonest response deadline first (Req 12.1).

    Args:
        now: The instant to compute remaining time against; defaults to the
            current UTC time. Injectable so a caller (or a test) gets a
            deterministic countdown.

    Returns:
        A :class:`ListView` of :class:`InboxEntry` rows. Never truncated —
        every decision waiting on the coordinator must be visible.
    """
    now = now or datetime.now(timezone.utc)
    records = state_store.query_open_escalations()
    ordered = order_decision_inbox(records)
    return _view(
        "decision_inbox",
        [_to_inbox_entry(record, now) for record in ordered],
        total_count=len(records),
        max_items=None,
    )


# ---------------------------------------------------------------------------
# Open incidents (Req 12.4).
# ---------------------------------------------------------------------------

#: Request states that count as "open" for Req 12.4's open-request count: every
#: declared state except the terminal RESOLVED one.
_OPEN_REQUEST_STATES: Final[tuple[str, ...]] = tuple(
    state.value for state in RequestState if state is not RequestState.RESOLVED
)

#: Request states that imply a responder is currently assigned to the request
#: (Req 12.4's "count of assigned responders"). Derived from the request
#: lifecycle rather than from a responder-table scan: a responder is assigned
#: to exactly one request at a time (`Responder.active_assignment_id`), so
#: counting the incident's requests in these states counts its assigned
#: responders without reading every responder row.
_ASSIGNED_REQUEST_STATES: Final[frozenset[str]] = frozenset(
    {
        RequestState.ASSIGNED.value,
        RequestState.EN_ROUTE.value,
        RequestState.ON_SCENE.value,
        RequestState.VERIFICATION.value,
    }
)


def _request_counts_by_incident() -> tuple[dict[str, int], dict[str, int]]:
    """Return ``(open_counts, assigned_counts)`` keyed by ``incident_id``.

    One GSI1 query per open request state (``REQUEST_BY_STATE#{state}``), never
    a scan. Requests carrying no ``incident_id`` are counted under the empty
    string key and therefore attributed to no incident.
    """
    open_counts: dict[str, int] = {}
    assigned_counts: dict[str, int] = {}
    for state in _OPEN_REQUEST_STATES:
        for request in state_store.query_requests_by_state(state):
            incident_id = str(request.get("incident_id") or "")
            open_counts[incident_id] = open_counts.get(incident_id, 0) + 1
            if state in _ASSIGNED_REQUEST_STATES:
                assigned_counts[incident_id] = assigned_counts.get(incident_id, 0) + 1
    return open_counts, assigned_counts


def _to_shelter_availability(shelter: Shelter) -> ShelterAvailability:
    return ShelterAvailability(
        shelter_id=shelter.shelter_id,
        name=shelter.name,
        unoccupied_places=shelter.available_capacity,
        total_capacity=shelter.total_capacity,
    )


def list_open_incidents() -> ListView:
    """Every open incident, severity descending then most recently updated
    first (Req 12.4).

    Each row carries the severity band, the affected areas, the count of open
    requests, the count of assigned responders, and per-shelter availability
    (unoccupied places and total capacity).

    Returns:
        A :class:`ListView` of :class:`IncidentSummary` rows. Never truncated.
    """
    incidents = [
        incident
        for incident in state_store.query_incidents_by_severity()
        if incident.status == "OPEN"
    ]
    open_counts, assigned_counts = _request_counts_by_incident()
    shelters = [_to_shelter_availability(s) for s in state_store.query_shelters()]

    ordered: list[Incident] = order_open_incidents(incidents)
    rows = [
        IncidentSummary(
            incident_id=incident.incident_id,
            severity_band=incident.severity_band,
            affected_areas=list(incident.affected_areas),
            open_request_count=open_counts.get(incident.incident_id, 0),
            assigned_responder_count=assigned_counts.get(incident.incident_id, 0),
            # Shelter availability is ward-wide, not per-incident: Req 12.4
            # lists it "per shelter" on the incident view, and shelters are not
            # partitioned by incident in the data model (§4.2).
            shelters=shelters,
            updated_at=incident.updated_at,
        )
        for incident in ordered
    ]
    return _view("open_incidents", rows, total_count=len(incidents), max_items=None)


# ---------------------------------------------------------------------------
# Per-incident audit trail (Req 12.7).
# ---------------------------------------------------------------------------


def _to_audit_view(entry: AuditEntry) -> AuditView:
    return AuditView(
        timestamp=entry.timestamp,
        tool_name=entry.tool_name,
        # Already redacted at write time (see module docstring).
        inputs=dict(entry.inputs),
        outcome=entry.outcome,
        approving_human_id=entry.approving_human_id,
    )


def list_incident_audit(incident_id: str) -> ListView:
    """Every audit entry for one incident, oldest first (Req 12.7).

    Args:
        incident_id: The incident whose audit trail to read.

    Returns:
        A :class:`ListView` of :class:`AuditView` rows. Never truncated — an
        audit trail with entries omitted is not an audit trail.
    """
    entries = audit_ledger.get_entries_for_incident(incident_id)
    ordered = order_incident_audit(entries)
    return _view(
        "incident_audit",
        [_to_audit_view(entry) for entry in ordered],
        total_count=len(entries),
        max_items=None,
    )
