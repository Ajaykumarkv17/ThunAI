"""`thunai-state` DynamoDB access layer: entities, idempotency, atomic
conditional updates, run-progress, and processed-trigger-event dedup
(design.md §4.4; Req 15.1, 15.2, 15.3, 15.4, 15.5, 15.8, 15.9, 15.10, 15.11,
6.4, 6.7, 6.10).

Verification note (mandatory per tasks.md 6.1, "verify first"):
    Checked against `boto3==1.43.90` (`pip show boto3`, matching the pin in
    `requirements.txt`/`pyproject.toml`) via the current AWS documentation
    MCP server:

    - `ConditionExpression` on `update_item`/`put_item` is still the
      current, documented mechanism for a conditional write; the legacy
      `Expected`/`ConditionalOperator` parameters are explicitly superseded
      by it and are not used here.
    - A failed `ConditionExpression` raises `botocore.exceptions.ClientError`
      with `err.response["Error"]["Code"] == "ConditionalCheckFailedException"`
      — there is **no** dedicated `ConditionalCheckFailedException` class
      exported by `botocore.exceptions` (confirmed: `botocore.exceptions`
      has no attribute containing `"Condition"`). Every conditional-write
      call site below therefore catches `botocore.exceptions.ClientError`
      and inspects `.response["Error"]["Code"]`, never a class named
      `ConditionalCheckFailedException`.
    - DynamoDB TTL does **not** delete an expired item instantly: AWS's own
      documentation states expired items "might be deleted by the system at
      any time, typically within a few days after their expiration" (and
      historically documented as "usually within 48 hours"). This matters
      for the 72-hour idempotency/dedup windows below: this module's own
      *read* path (`_get_idempotency_record`, `has_trigger_event_been_processed`)
      never trusts TTL-based deletion to enforce the window — every record
      also carries an explicit `expires_at` epoch-seconds attribute that is
      checked at read time (treating an item as absent once
      `expires_at <= now`, even if DynamoDB has not yet physically deleted
      it). TTL is configured purely as storage-cost hygiene (eventual
      cleanup), never as the correctness mechanism for the 72-hour window
      Req 15.2/15.3/15.9 require.
    - This module uses the boto3 **resource** interface
      (`boto3.resource("dynamodb").Table(...)`), not the low-level client,
      because it accepts and returns plain Python values (str/int/float/
      Decimal/dict/list) rather than the client's `{"S": ...}`/`{"N": ...}`
      typed-attribute-value wire format — matching the plain-value style
      design.md's own §4.4 pseudocode uses in its `ExpressionAttributeValues`
      examples (e.g. `{":amt": amount}`, not `{":amt": {"N": str(amount)}}`).

Deviation from design.md §4.4 recorded here (docs/requirements win per the
"verify first" instruction):

1. **Synchronous, not `async def`.** design.md's §4.4 pseudocode (`async def
   idempotent_write(...)`, `await ddb.update_item(...)`) is illustrative;
   `boto3` (pinned `1.43.90`) has no native async DynamoDB client, and
   `aioboto3`/an async DynamoDB client is not a project dependency. Every
   function in this module is a plain synchronous function. Callers already
   running inside an async context (e.g. a future async tool wrapper) are
   expected to run these via a thread-pool executor; that is that caller's
   concern, not this module's.
2. **One overloaded GSI, not four.** design.md's §4.4 GSI list names four
   GSIs ("GSI1".."GSI4") but its own attribute-name column reuses the exact
   same pair `gsi1pk`/`gsi1sk` for every one of them (e.g. GSI2's row reads
   `gsi1pk = "REQUEST_BY_STATE#{state}"`, not `gsi2pk = ...`). Read literally
   that is a single physical index shared across four entity-scoped access
   patterns — a standard single-table-design "overloaded index" idiom, since
   no query in design.md ever needs to combine two of these access patterns
   in one index scan (each query does an exact-match on a distinct literal
   `gsi1pk` prefix per entity type). This module therefore declares and
   queries exactly **one** GSI, named `GSI1`, with hash key `gsi1pk` (S) and
   range key `gsi1sk` (S), populated differently per entity type as
   documented on each write helper below. This is the reasoned, documented
   choice the task instructions invite when the design's exact GSI schema is
   ambiguous; it also means the (not-yet-implemented) CDK Data stack only
   needs to declare one GSI on `thunai-state`, not four.
3. **`EVENT#{trigger_source_id}` uses a fixed sort key, not
   `RECEIVED#{iso_ts}`.** design.md's key-schema table shows
   `sk = "RECEIVED#{iso_ts}"` for the processed-trigger-event row. Read
   literally, a `ConditionExpression="attribute_not_exists(pk)"` write guard
   against a composite `(pk, sk)` key can never detect a duplicate under
   that scheme, because every write embeds a *new* timestamp into `sk`, so
   the exact `(pk, sk)` pair is different every time and the "already
   exists" condition can never fire — defeating the very duplicate-detection
   guarantee Req 15.3/15.4 require ("record the trigger event identifier...
   before performing any downstream write" / "IF a received trigger event
   identifier is already recorded... skip all processing"). This module
   instead uses a **fixed** sort key (`sk = "RECEIVED"`) for this row, so the
   atomic `attribute_not_exists(pk)` guard genuinely fires on the second
   write for the same `trigger_source_id`; the first-received timestamp is
   stored as a plain attribute (`received_at`) rather than embedded in the
   sort key.

Task-scoping note on Req 1.9 vs. Req 15.3/15.4: this task's assigned
requirement list is 15.1, 15.2, 15.3, 15.4, 15.5, 15.8, 15.9, 15.10, 15.11,
6.4, 6.7, 6.10 — Req 1.9 is not in that list. Req 1.9's own text describes a
distinct 24-hour duplicate-suppression check performed **against
Audit_Ledger** ("a payload... already recorded in Audit_Ledger within the
preceding 24 hours"), which belongs to `memory/audit_ledger.py` /
`triggers/ingestion_lambda.py` (tasks 7.1/20.2), not to this module. Req 15.3
is explicit that *this* module's trigger-event record must be retained "for
at least 72 hours". `record_trigger_event` / `has_trigger_event_been_processed`
below therefore use a 72-hour window to satisfy the requirement this task is
actually scoped to (15.3/15.4), not the 24-hour figure that belongs to the
separate Audit_Ledger-based check in Req 1.9.

Retry/failure-escalation semantics (Req 15.4, 15.5): "IF a write to
State_Store fails on 3 successive attempts within 30 seconds, THEN...
escalate a state-write failure... within 60 seconds of the final failed
attempt." This module owns the *detection* half only: `_write_with_retry`
performs up to 3 attempts of a single logical write with short backoff
between attempts (well under the 30-second window for the common case of a
transient `ClientError`/`BotoCoreError`), and raises `StateWriteFailureError`
— a distinct exception type, never silently swallowed — after the third
failure. It is the caller's responsibility (eventually
`harness/hooks.py::AuditHook`, task 8.2) to catch `StateWriteFailureError`
and drive the actual `Escalation_Record` creation within the 60-second
window; that escalation call is out of this module's scope (this module has
no dependency on `surface/escalation_service.py`). Note that a
`ConditionalCheckFailedException` is **never** treated as a transient
failure and is never retried by `_write_with_retry` — it is an expected,
immediate business-logic outcome (e.g. "responder no longer available") that
must propagate to the caller straight away so it can react (e.g. try the
next ranked alternative), not be mistaken for infrastructure flakiness.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Final, TypeVar

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from schemas.entities import EscalationRecord, Incident, Responder, Shelter

__all__ = [
    "MissingIdempotencyKeyError",
    "ResponderNoLongerAvailableError",
    "ShelterCapacityExceededError",
    "ShelterCapacityOverReleaseError",
    "StateWriteFailureError",
    "idempotent_write",
    "assign_responder_atomic",
    "decrement_shelter_capacity",
    "increment_shelter_capacity",
    "put_incident",
    "get_incident",
    "update_incident",
    "put_responder",
    "get_responder",
    "update_responder",
    "put_shelter",
    "get_shelter",
    "update_shelter",
    "put_escalation_record",
    "get_escalation_record",
    "update_escalation_record",
    "query_incidents_by_severity",
    "query_available_responders",
    "query_open_escalations",
    "query_requests_by_state",
    "query_shelters",
    "record_trigger_event",
    "has_trigger_event_been_processed",
    "get_trigger_event",
    "persist_run_progress",
    "get_run_progress",
    "put_run_record",
    "get_run_record",
    "update_run_record",
    "query_recent_runs",
    "query_in_progress_runs",
    "put_request",
    "get_request",
    "update_request",
]

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TABLE_NAME_ENV_VAR: Final[str] = "THUNAI_STATE_TABLE"
DEFAULT_TABLE_NAME: Final[str] = "thunai-state"

IDEMPOTENCY_TTL_HOURS: Final[int] = 72
"""Req 15.2/15.9: idempotency records survive replay for at least 72 hours."""

TRIGGER_EVENT_TTL_HOURS: Final[int] = 72
"""Req 15.3: processed-trigger-event records are retained for at least 72
hours (see module docstring's "Task-scoping note" for why this is 72, not
the 24 hours mentioned in the unrelated Req 1.9 Audit_Ledger-based check)."""

# Severity-band ascending order, reused from policy.escalation_policy rather
# than redeclared, so GSI1's severity rank can never drift out of sync with
# the one place the band ordering is authoritative.
from policy.escalation_policy import SEVERITY_BANDS_ASCENDING  # noqa: E402

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MissingIdempotencyKeyError(ValueError):
    """Req 15.10: a write was submitted to State_Store without an
    Idempotency_Key."""

    def __init__(self) -> None:
        super().__init__(
            "idempotent_write() requires a non-empty idempotency_key; "
            "State_Store rejects writes submitted without one (Req 15.10)."
        )


class ResponderNoLongerAvailableError(RuntimeError):
    """Req 6.10: the selected responder's availability_status was no longer
    AVAILABLE at the moment the assignment write was applied."""

    def __init__(self, responder_id: str) -> None:
        self.responder_id = responder_id
        super().__init__(
            f"Responder {responder_id!r} is no longer AVAILABLE; "
            "assignment rejected, responder and request left unchanged."
        )


class ShelterCapacityExceededError(RuntimeError):
    """Req 6.7/15.8: a decrement would take available_capacity below 0."""

    def __init__(self, shelter_id: str, amount: int) -> None:
        self.shelter_id = shelter_id
        self.amount = amount
        super().__init__(
            f"Shelter {shelter_id!r} does not hold {amount} units of "
            "available capacity; decrement rejected, capacity unchanged."
        )


class ShelterCapacityOverReleaseError(RuntimeError):
    """Inverse of ShelterCapacityExceededError: an increment/release would
    take available_capacity above total_capacity."""

    def __init__(self, shelter_id: str, amount: int) -> None:
        self.shelter_id = shelter_id
        self.amount = amount
        super().__init__(
            f"Shelter {shelter_id!r} cannot release {amount} units without "
            "exceeding total_capacity; increment rejected, capacity unchanged."
        )


class StateWriteFailureError(RuntimeError):
    """Req 15.4/15.5: a write to State_Store failed on 3 successive attempts
    within the retry window. Distinct exception type so a caller (eventually
    harness/hooks.py::AuditHook) can catch it and drive an escalation, rather
    than the failure being silently swallowed."""

    def __init__(self, *, attempts: int, elapsed_seconds: float, last_error: BaseException | None) -> None:
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.last_error = last_error
        super().__init__(
            f"State_Store write failed on {attempts} successive attempts "
            f"within {elapsed_seconds:.2f}s; last error: {last_error!r}"
        )


# ---------------------------------------------------------------------------
# boto3 resource/table access (lazily constructed, cache-resettable for tests)
# ---------------------------------------------------------------------------

_table_cache: Any = None


def _table_name() -> str:
    return os.environ.get(TABLE_NAME_ENV_VAR, DEFAULT_TABLE_NAME)


def _table() -> Any:
    """Return the cached `boto3.resource("dynamodb").Table` for `thunai-state`.

    Cached at module scope so repeated calls within one process (and one
    moto `mock_aws` context in tests) reuse the same resource/table object.
    Call `reset_table_cache()` between tests that enter/exit separate
    `moto.mock_aws()` contexts, since a table object bound to a mocked
    session must not leak into a later, differently-mocked (or real) session.
    """
    global _table_cache
    if _table_cache is None:
        resource = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        _table_cache = resource.Table(_table_name())
    return _table_cache


def reset_table_cache() -> None:
    """Drop the cached Table resource (test-only helper; see `_table()`)."""
    global _table_cache
    _table_cache = None


# ---------------------------------------------------------------------------
# Value conversion: boto3's DynamoDB resource requires Decimal (never native
# float) for numeric attributes, and Pydantic models must be dict-ified.
# ---------------------------------------------------------------------------


def _to_dynamo_safe(value: Any) -> Any:
    """Recursively convert a value into something the boto3 DynamoDB
    resource will accept: Pydantic models -> dict, float -> Decimal, and the
    same recursively for list/dict contents."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, datetime):
        # DynamoDB has no native datetime type; persist as an ISO-8601 string
        # (the same representation every other timestamp field in this module
        # uses). Handled here, in the one recursive conversion helper, so a
        # datetime nested anywhere in a payload (e.g. a reading's ``ts`` inside
        # a paused run's persisted inputs) is always serialisable, not only a
        # top-level one.
        return value.isoformat()
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo_safe(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Recursively convert DynamoDB's returned Decimal values back into
    plain int/float so they round-trip through Pydantic models cleanly."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if as_int == value else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_from_dynamo(v) for v in value]
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_seconds(delta_hours: float) -> int:
    return int((datetime.now(timezone.utc) + timedelta(hours=delta_hours)).timestamp())


# ---------------------------------------------------------------------------
# Retry-with-backoff for transient write failures (Req 15.4, 15.5)
# ---------------------------------------------------------------------------


def _is_conditional_check_failure(exc: BaseException) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _write_with_retry(
    perform: Callable[[], T],
    *,
    max_attempts: int = 3,
    backoff_seconds: tuple[float, ...] = (0.05, 0.15),
) -> T:
    """Attempt `perform()` up to `max_attempts` times with short backoff
    between attempts, retrying only on transient boto3 errors.

    A `ConditionalCheckFailedException` (an expected business-logic
    rejection, e.g. "responder no longer available") is never retried and
    is re-raised immediately on its first occurrence.

    Raises:
        StateWriteFailureError: after `max_attempts` consecutive transient
            failures (Req 15.4/15.5), naming the attempt count, elapsed
            time, and the last underlying error.
    """
    start = time.monotonic()
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return perform()
        except ClientError as exc:
            if _is_conditional_check_failure(exc):
                raise
            last_error = exc
        except BotoCoreError as exc:
            last_error = exc
        if attempt < max_attempts:
            time.sleep(backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)])
    raise StateWriteFailureError(
        attempts=max_attempts,
        elapsed_seconds=time.monotonic() - start,
        last_error=last_error,
    )


# ---------------------------------------------------------------------------
# Idempotent write (Req 15.2, 15.9, 15.10)
# ---------------------------------------------------------------------------


def _idempotency_key_to_pk(idempotency_key: str) -> dict[str, str]:
    return {"pk": f"IDEMPOTENCY#{idempotency_key}", "sk": "META"}


def _get_idempotency_record(idempotency_key: str) -> dict[str, Any] | None:
    resp = _write_with_retry(lambda: _table().get_item(Key=_idempotency_key_to_pk(idempotency_key)))
    item = resp.get("Item")
    if item is None:
        return None
    expires_at = item.get("expires_at")
    if expires_at is not None and int(expires_at) <= int(datetime.now(timezone.utc).timestamp()):
        # Past the 72h window (Req 15.9) — treat as absent even if DynamoDB
        # has not yet physically deleted the item (TTL deletion is not
        # instant; see module docstring's verification note).
        return None
    return _from_dynamo(item)


def _put_idempotency_record(idempotency_key: str, recorded_outcome: Any) -> None:
    item = {
        **_idempotency_key_to_pk(idempotency_key),
        "idempotency_key": idempotency_key,
        "recorded_outcome": _to_dynamo_safe(recorded_outcome),
        "recorded_at": _now_iso(),
        "expires_at": _epoch_seconds(IDEMPOTENCY_TTL_HOURS),
        "ttl": _epoch_seconds(IDEMPOTENCY_TTL_HOURS),
    }
    _write_with_retry(
        lambda: _table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    )


def idempotent_write(idempotency_key: str, perform: Callable[[], T]) -> T:
    """Apply `perform()` exactly once per `idempotency_key`, replaying the
    first recorded outcome on every subsequent call within a 72-hour window
    (Req 15.2, 15.9), and rejecting a write submitted without a key
    (Req 15.10).

    Args:
        idempotency_key: The caller-supplied idempotency key. Must be a
            non-empty string.
        perform: A zero-argument callable performing the actual write (e.g.
            a single conditional `update_item`/`put_item` call) and
            returning a JSON/DynamoDB-serialisable result (a `dict`, a
            Pydantic model, or a value composed of primitives).

    Returns:
        `perform()`'s return value on the first call for a given key; the
        exact recorded outcome of that first call on every subsequent call
        within the 72-hour window (Property 2).

    Raises:
        MissingIdempotencyKeyError: If `idempotency_key` is falsy.
        StateWriteFailureError: If the underlying write fails on 3
            successive attempts (Req 15.4/15.5), propagated from
            `_write_with_retry`.
        Exception: Any exception `perform()` itself raises for reasons other
            than a transient boto3 error (e.g. `ResponderNoLongerAvailableError`)
            propagates unchanged — it is not recorded as a successful outcome.
    """
    if not idempotency_key:
        raise MissingIdempotencyKeyError()

    existing = _get_idempotency_record(idempotency_key)
    if existing is not None:
        return existing["recorded_outcome"]  # type: ignore[return-value]

    result = perform()

    try:
        _put_idempotency_record(idempotency_key, result)
    except ClientError as exc:
        if _is_conditional_check_failure(exc):
            # Lost the race to record the outcome first; the winner's
            # recorded outcome is authoritative (Property 2 requires every
            # call after the first to return the *same* outcome).
            winner = _get_idempotency_record(idempotency_key)
            if winner is not None:
                return winner["recorded_outcome"]  # type: ignore[return-value]
        raise
    return result


# ---------------------------------------------------------------------------
# Atomic conditional updates (Req 6.4, 6.7, 6.10, 15.8)
# ---------------------------------------------------------------------------


def _conditional_assign(responder_id: str, request_id: str) -> dict[str, Any]:
    try:
        resp = _write_with_retry(
            lambda: _table().update_item(
                Key={"pk": f"RESPONDER#{responder_id}", "sk": "META"},
                UpdateExpression=(
                    "SET availability_status = :assigned, "
                    "active_assignment_id = :rid, "
                    "active_assignment_count = if_not_exists(active_assignment_count, :zero) + :one "
                    "REMOVE gsi1pk, gsi1sk"
                ),
                ConditionExpression="availability_status = :available",
                ExpressionAttributeValues={
                    ":assigned": "ASSIGNED",
                    ":available": "AVAILABLE",
                    ":rid": request_id,
                    ":one": 1,
                    ":zero": 0,
                },
                ReturnValues="ALL_NEW",
            )
        )
    except ClientError as exc:
        if _is_conditional_check_failure(exc):
            raise ResponderNoLongerAvailableError(responder_id) from exc
        raise
    return _from_dynamo(dict(resp["Attributes"]))


def assign_responder_atomic(responder_id: str, request_id: str, idempotency_key: str) -> dict[str, Any]:
    """Atomically assign `responder_id` to `request_id` via a single
    conditional `update_item` (Req 6.4, 6.10): the update only succeeds if
    the responder's current `availability_status` is `AVAILABLE`, and in the
    same write flips it to `ASSIGNED`, sets `active_assignment_id`, and
    increments `active_assignment_count` — never a read-then-write pattern,
    so it is safe under concurrent/racing calls (the primitive Property 12
    later verifies).

    Also removes the sparse GSI1 attributes (`gsi1pk`/`gsi1sk`) in the same
    write, since `query_available_responders()` relies on a responder no
    longer appearing in the `RESPONDER_BY_AVAILABILITY#AVAILABLE` index the
    instant it is no longer AVAILABLE.

    Args:
        responder_id: The responder to assign.
        request_id: The request being assigned to that responder.
        idempotency_key: Idempotency key for this specific assignment
            (Req 6.4's "using an Idempotency_Key derived from the request
            identifier" — typically `f"assign:{request_id}"`).

    Returns:
        The updated responder's attributes as a plain dict.

    Raises:
        MissingIdempotencyKeyError: If `idempotency_key` is falsy.
        ResponderNoLongerAvailableError: If the responder's
            `availability_status` was not `AVAILABLE` at write time.
        StateWriteFailureError: On 3 successive transient write failures.
    """
    return idempotent_write(idempotency_key, lambda: _conditional_assign(responder_id, request_id))


def _conditional_decrement(shelter_id: str, amount: int) -> dict[str, Any]:
    try:
        resp = _write_with_retry(
            lambda: _table().update_item(
                Key={"pk": f"SHELTER#{shelter_id}", "sk": "META"},
                UpdateExpression="SET available_capacity = available_capacity - :amt",
                ConditionExpression="available_capacity >= :amt",
                ExpressionAttributeValues={":amt": amount},
                ReturnValues="ALL_NEW",
            )
        )
    except ClientError as exc:
        if _is_conditional_check_failure(exc):
            raise ShelterCapacityExceededError(shelter_id, amount) from exc
        raise
    return _from_dynamo(dict(resp["Attributes"]))


def decrement_shelter_capacity(shelter_id: str, occupant_count: int, idempotency_key: str) -> dict[str, Any]:
    """Atomically decrement a shelter's `available_capacity` by
    `occupant_count` via a single conditional `update_item` (Req 6.7, 15.8),
    enforcing the invariant `available_capacity >= 0` at write time — the
    condition rejects a decrement that would take capacity below zero,
    matching `schemas/entities.py::Shelter.available_capacity`'s
    `Field(ge=0)` constraint.

    Args:
        shelter_id: The shelter to decrement.
        occupant_count: The number of occupants being placed (must be a
            non-negative integer; DynamoDB's `ConditionExpression` enforces
            the floor regardless).
        idempotency_key: Idempotency key for this specific placement.

    Returns:
        The updated shelter's attributes as a plain dict.

    Raises:
        MissingIdempotencyKeyError: If `idempotency_key` is falsy.
        ShelterCapacityExceededError: If the decrement would take
            `available_capacity` below 0.
        StateWriteFailureError: On 3 successive transient write failures.
    """
    return idempotent_write(idempotency_key, lambda: _conditional_decrement(shelter_id, occupant_count))


def _conditional_increment(shelter_id: str, amount: int) -> dict[str, Any]:
    """Note on `ConditionExpression` limits: DynamoDB's `ConditionExpression`
    grammar supports comparisons between an attribute path and a value (or
    another attribute path) but does **not** support arithmetic operators
    (`+`/`-`) inside the condition itself — arithmetic is only valid inside
    `UpdateExpression`'s `SET` clause (confirmed against the current AWS
    docs' condition-expression grammar; a naive
    `ConditionExpression="available_capacity + :amt <= total_capacity"`
    fails to parse). `total_capacity` is effectively immutable once a
    shelter is seeded (no code path in this module ever changes it), so this
    reads `total_capacity` once, computes the allowed ceiling in Python, and
    guards the *actually concurrent* attribute (`available_capacity`) with a
    plain value comparison — the single-item `update_item` call remains the
    sole write and the sole point where a race on `available_capacity` is
    resolved atomically.
    """
    shelter = get_shelter(shelter_id)
    if shelter is None:
        raise KeyError(f"No Shelter found for shelter_id={shelter_id!r}")
    max_available_after = shelter.total_capacity - amount
    try:
        resp = _write_with_retry(
            lambda: _table().update_item(
                Key={"pk": f"SHELTER#{shelter_id}", "sk": "META"},
                UpdateExpression="SET available_capacity = available_capacity + :amt",
                ConditionExpression="available_capacity <= :max_before",
                ExpressionAttributeValues={":amt": amount, ":max_before": max_available_after},
                ReturnValues="ALL_NEW",
            )
        )
    except ClientError as exc:
        if _is_conditional_check_failure(exc):
            raise ShelterCapacityOverReleaseError(shelter_id, amount) from exc
        raise
    return _from_dynamo(dict(resp["Attributes"]))


def increment_shelter_capacity(shelter_id: str, amount: int, idempotency_key: str) -> dict[str, Any]:
    """Inverse of `decrement_shelter_capacity` (Req 6.7's capacity-management
    lifecycle: placements are eventually released). Atomically increments
    `available_capacity` by `amount` via a single conditional `update_item`,
    guarding the upper bound `available_capacity <= total_capacity` so a
    release can never overshoot the shelter's declared total.

    Raises:
        MissingIdempotencyKeyError: If `idempotency_key` is falsy.
        ShelterCapacityOverReleaseError: If the increment would take
            `available_capacity` above `total_capacity`.
        StateWriteFailureError: On 3 successive transient write failures.
    """
    return idempotent_write(idempotency_key, lambda: _conditional_increment(shelter_id, amount))


# ---------------------------------------------------------------------------
# Entity CRUD: Incident (design.md §4.4 key schema: INCIDENT#{id} / META)
# ---------------------------------------------------------------------------


def _severity_rank(severity_band: str) -> str:
    return f"{SEVERITY_BANDS_ASCENDING.index(severity_band):02d}"


def _incident_item(incident: Incident) -> dict[str, Any]:
    item = _to_dynamo_safe(incident.model_dump())
    item["pk"] = f"INCIDENT#{incident.incident_id}"
    item["sk"] = "META"
    # GSI1: overloaded index, "INCIDENT_BY_SEVERITY" use case (Req 12.4:
    # open incidents ordered by severity desc, then recency).
    item["gsi1pk"] = "INCIDENT_BY_SEVERITY"
    item["gsi1sk"] = f"{_severity_rank(incident.severity_band)}#{incident.updated_at}"
    return item


def put_incident(incident: Incident) -> Incident:
    """Write (create or overwrite) an Incident, last-write-wins (Req 15.1)."""
    _write_with_retry(lambda: _table().put_item(Item=_incident_item(incident)))
    return incident


def get_incident(incident_id: str) -> Incident | None:
    """Read the most recently committed version of an Incident (Req 15.1)."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"INCIDENT#{incident_id}", "sk": "META"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    return Incident(**_from_dynamo(dict(item)))


def update_incident(incident_id: str, **field_updates: Any) -> Incident:
    """Read-merge-write update of an Incident's fields, last-write-wins.

    Not a conditional/atomic primitive — for entity-level field edits with
    no concurrency invariant to enforce (unlike `assign_responder_atomic`/
    `decrement_shelter_capacity`, which must be single-item conditional
    updates precisely because they *do* enforce an invariant under
    concurrency).
    """
    current = get_incident(incident_id)
    if current is None:
        raise KeyError(f"No Incident found for incident_id={incident_id!r}")
    updated = current.model_copy(update=field_updates)
    return put_incident(updated)


# ---------------------------------------------------------------------------
# Entity CRUD: Responder (design.md §4.4 key schema: RESPONDER#{id} / META)
# ---------------------------------------------------------------------------


def _responder_item(responder: Responder) -> dict[str, Any]:
    item = _to_dynamo_safe(responder.model_dump())
    item["pk"] = f"RESPONDER#{responder.responder_id}"
    item["sk"] = "META"
    # GSI1: overloaded index, "RESPONDER_BY_AVAILABILITY#AVAILABLE" use case
    # (Req 6.1: Dispatch_Agent's candidate search). Sparse: only AVAILABLE
    # responders get gsi1pk/gsi1sk set, so an ASSIGNED responder simply does
    # not appear in that index.
    if responder.availability_status == "AVAILABLE":
        item["gsi1pk"] = "RESPONDER_BY_AVAILABILITY#AVAILABLE"
        item["gsi1sk"] = responder.responder_id
    return item


def put_responder(responder: Responder) -> Responder:
    """Write (create or overwrite) a Responder, last-write-wins (Req 15.1)."""
    item = _responder_item(responder)
    # A plain put_item must clear any stale gsi1pk/gsi1sk left over from a
    # prior AVAILABLE write when the new status is not AVAILABLE — put_item
    # fully replaces the item, so simply omitting the keys here is enough;
    # no separate REMOVE is needed as it would be for update_item.
    _write_with_retry(lambda: _table().put_item(Item=item))
    return responder


def get_responder(responder_id: str) -> Responder | None:
    """Read the most recently committed version of a Responder (Req 15.1)."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"RESPONDER#{responder_id}", "sk": "META"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    item = dict(item)
    item.pop("gsi1pk", None)
    item.pop("gsi1sk", None)
    return Responder(**_from_dynamo(item))


def update_responder(responder_id: str, **field_updates: Any) -> Responder:
    """Read-merge-write update of a Responder's fields, last-write-wins.
    See `update_incident`'s docstring for why this is not a conditional
    primitive."""
    current = get_responder(responder_id)
    if current is None:
        raise KeyError(f"No Responder found for responder_id={responder_id!r}")
    updated = current.model_copy(update=field_updates)
    return put_responder(updated)


# ---------------------------------------------------------------------------
# Entity CRUD: Shelter (design.md §4.4 key schema: SHELTER#{id} / META)
# ---------------------------------------------------------------------------


def _shelter_item(shelter: Shelter) -> dict[str, Any]:
    item = _to_dynamo_safe(shelter.model_dump())
    item["pk"] = f"SHELTER#{shelter.shelter_id}"
    item["sk"] = "META"
    # GSI1: overloaded index, "SHELTER_ALL" use case — Req 12.4 requires the
    # Coordinator_Console incident list to show "the shelter availability
    # expressed as unoccupied places and total capacity per shelter", which
    # needs every shelter, and a Query on a sparse GSI partition is the
    # scan-free way to get it (added by task 17.3, alongside the existing
    # INCIDENT_BY_SEVERITY / REQUEST_BY_STATE / ESCALATION_BY_STATUS /
    # RESPONDER_BY_AVAILABILITY use cases on the same index).
    item["gsi1pk"] = "SHELTER_ALL"
    item["gsi1sk"] = shelter.shelter_id
    return item


def put_shelter(shelter: Shelter) -> Shelter:
    """Write (create or overwrite) a Shelter, last-write-wins (Req 15.1)."""
    _write_with_retry(lambda: _table().put_item(Item=_shelter_item(shelter)))
    return shelter


def get_shelter(shelter_id: str) -> Shelter | None:
    """Read the most recently committed version of a Shelter (Req 15.1)."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"SHELTER#{shelter_id}", "sk": "META"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    return Shelter(**_from_dynamo(dict(item)))


def update_shelter(shelter_id: str, **field_updates: Any) -> Shelter:
    """Read-merge-write update of a Shelter's fields, last-write-wins. See
    `update_incident`'s docstring for why this is not a conditional
    primitive (use `decrement_shelter_capacity`/`increment_shelter_capacity`
    for capacity changes, which must remain atomic under concurrency)."""
    current = get_shelter(shelter_id)
    if current is None:
        raise KeyError(f"No Shelter found for shelter_id={shelter_id!r}")
    updated = current.model_copy(update=field_updates)
    return put_shelter(updated)


# ---------------------------------------------------------------------------
# Entity CRUD: EscalationRecord (design.md §4.4 key schema:
# ESCALATION#{id} / META)
# ---------------------------------------------------------------------------


def _escalation_item(record: EscalationRecord) -> dict[str, Any]:
    item = _to_dynamo_safe(record.model_dump())
    item["pk"] = f"ESCALATION#{record.escalation_id}"
    item["sk"] = "META"
    # GSI1: overloaded index, "ESCALATION_BY_STATUS#OPEN" use case (Req 12.1:
    # Decision_Inbox ordered by soonest deadline). Sparse: only OPEN
    # escalations get gsi1pk/gsi1sk set.
    if record.status == "OPEN":
        item["gsi1pk"] = "ESCALATION_BY_STATUS#OPEN"
        item["gsi1sk"] = record.response_deadline
    return item


def put_escalation_record(record: EscalationRecord) -> EscalationRecord:
    """Write (create or overwrite) an EscalationRecord, last-write-wins
    (Req 15.1)."""
    _write_with_retry(lambda: _table().put_item(Item=_escalation_item(record)))
    return record


def get_escalation_record(escalation_id: str) -> EscalationRecord | None:
    """Read the most recently committed version of an EscalationRecord
    (Req 15.1)."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"ESCALATION#{escalation_id}", "sk": "META"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    item = dict(item)
    item.pop("gsi1pk", None)
    item.pop("gsi1sk", None)
    return EscalationRecord(**_from_dynamo(item))


def update_escalation_record(escalation_id: str, **field_updates: Any) -> EscalationRecord:
    """Read-merge-write update of an EscalationRecord's fields, last-write-wins.
    See `update_incident`'s docstring for why this is not a conditional
    primitive."""
    current = get_escalation_record(escalation_id)
    if current is None:
        raise KeyError(f"No EscalationRecord found for escalation_id={escalation_id!r}")
    updated = current.model_copy(update=field_updates)
    return put_escalation_record(updated)


# ---------------------------------------------------------------------------
# Entity CRUD: persisted EmergencyRequest (design.md §4.4 key schema:
# REQUEST#{request_id} / META; request_id = inbound message id, Req 5.4).
#
# Added by task 13.3 (agents/intake_agent.py): design.md §4.4's key-schema
# table already names this exact key ("Emergency Request |
# REQUEST#{request_id} | META | request_id = inbound message id
# (Idempotency_Key, Req 5.4)") but no prior task (6.1, 12.x) implemented the
# read/write helpers for it — only `query_requests_by_state`'s GSI1 read
# path existed. This module stores the persisted request as a plain dict
# (matching `query_requests_by_state`'s own already-established "no
# dedicated persisted-request Pydantic model yet" precedent, documented on
# that function's docstring above) rather than introducing a new
# `schemas.entities` model in this state-layer module, since defining that
# model is squarely `schemas/entities.py`'s job and out of this task's
# scope (agents/intake_agent.py, not schemas/entities.py).
# ---------------------------------------------------------------------------


def _request_item(request_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    item = _to_dynamo_safe(dict(fields))
    item["pk"] = f"REQUEST#{request_id}"
    item["sk"] = "META"
    item["request_id"] = request_id
    # GSI1: overloaded index, "REQUEST_BY_STATE#{state}" use case (Req 6.1),
    # matching query_requests_by_state's own already-established key.
    state = fields.get("state")
    if state:
        item["gsi1pk"] = f"REQUEST_BY_STATE#{state}"
        item["gsi1sk"] = str(fields.get("created_at", ""))
    return item


def put_request(request_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Write (create or overwrite) a persisted emergency request, last-write-wins
    (Req 15.1). `fields` is stored verbatim (plus `request_id`/`pk`/`sk` and, when
    `fields["state"]` is set, the `REQUEST_BY_STATE#{state}` GSI1 attributes)."""
    item = _request_item(request_id, fields)
    _write_with_retry(lambda: _table().put_item(Item=item))
    return _from_dynamo({k: v for k, v in item.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")})


def get_request(request_id: str) -> dict[str, Any] | None:
    """Read the most recently committed version of a persisted emergency
    request (Req 15.1), or `None` if no such request exists."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"REQUEST#{request_id}", "sk": "META"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    item = dict(item)
    item.pop("gsi1pk", None)
    item.pop("gsi1sk", None)
    item.pop("pk", None)
    item.pop("sk", None)
    return _from_dynamo(item)


def update_request(request_id: str, **field_updates: Any) -> dict[str, Any]:
    """Read-merge-write update of a persisted emergency request's fields,
    last-write-wins. See `update_incident`'s docstring for why this is not
    a conditional/atomic primitive (no concurrency invariant to enforce
    here)."""
    current = get_request(request_id)
    if current is None:
        raise KeyError(f"No request found for request_id={request_id!r}")
    merged = {**current, **field_updates}
    return put_request(request_id, merged)


# ---------------------------------------------------------------------------
# GSI query helpers (Req 15.10, 15.11) — see module docstring's deviation
# note #2: one overloaded GSI ("GSI1", attributes gsi1pk/gsi1sk), queried
# with a distinct literal gsi1pk value per entity-scoped access pattern.
# ---------------------------------------------------------------------------


def query_incidents_by_severity() -> list[Incident]:
    """Open incidents ordered by severity descending, then recency
    descending (Req 12.4), via GSI1's `INCIDENT_BY_SEVERITY` use case."""
    resp = _write_with_retry(
        lambda: _table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq("INCIDENT_BY_SEVERITY"),
            ScanIndexForward=False,
        )
    )
    return [Incident(**_from_dynamo(dict(item))) for item in resp.get("Items", [])]


def query_available_responders() -> list[Responder]:
    """Every responder currently AVAILABLE (Req 6.1's candidate search),
    via GSI1's `RESPONDER_BY_AVAILABILITY#AVAILABLE` use case (sparse: an
    ASSIGNED responder simply does not appear here)."""
    resp = _write_with_retry(
        lambda: _table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq("RESPONDER_BY_AVAILABILITY#AVAILABLE"),
        )
    )
    results = []
    for item in resp.get("Items", []):
        item = dict(item)
        item.pop("gsi1pk", None)
        item.pop("gsi1sk", None)
        results.append(Responder(**_from_dynamo(item)))
    return results


def query_open_escalations() -> list[EscalationRecord]:
    """Every OPEN escalation ordered by soonest response deadline first
    (Req 12.1's Decision_Inbox ordering), via GSI1's
    `ESCALATION_BY_STATUS#OPEN` use case (sparse)."""
    resp = _write_with_retry(
        lambda: _table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq("ESCALATION_BY_STATUS#OPEN"),
            ScanIndexForward=True,
        )
    )
    results = []
    for item in resp.get("Items", []):
        item = dict(item)
        item.pop("gsi1pk", None)
        item.pop("gsi1sk", None)
        results.append(EscalationRecord(**_from_dynamo(item)))
    return results


def query_shelters() -> list[Shelter]:
    """Every registered shelter, ordered by `shelter_id` (Req 12.4's per-shelter
    availability display), via GSI1's `SHELTER_ALL` use case."""
    resp = _write_with_retry(
        lambda: _table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq("SHELTER_ALL"),
            ScanIndexForward=True,
        )
    )
    results = []
    for item in resp.get("Items", []):
        item = dict(item)
        item.pop("gsi1pk", None)
        item.pop("gsi1sk", None)
        results.append(Shelter(**_from_dynamo(item)))
    return results


def query_requests_by_state(state: str) -> list[dict[str, Any]]:
    """Emergency requests currently in `state` (Req 6.1's dispatch-eligible
    lookup), via GSI1's `REQUEST_BY_STATE#{state}` use case.

    Returns raw dicts rather than a Pydantic model: `schemas/entities.py`
    does not (yet) declare a persisted-request entity model (only
    `schemas/decisions.py::EmergencyRequest`, which is a typed *decision*
    output, not necessarily the exact persisted State_Store record shape).
    A future task that defines that persisted model can layer typed
    deserialisation on top of this helper without changing its query shape.
    """
    resp = _write_with_retry(
        lambda: _table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq(f"REQUEST_BY_STATE#{state}"),
        )
    )
    results = []
    for item in resp.get("Items", []):
        item = dict(item)
        item.pop("gsi1pk", None)
        item.pop("gsi1sk", None)
        results.append(_from_dynamo(item))
    return results


# ---------------------------------------------------------------------------
# Processed-trigger-event records (Req 15.3, 15.4)
# ---------------------------------------------------------------------------


def has_trigger_event_been_processed(trigger_source_id: str) -> bool:
    """Check whether `trigger_source_id` has already been recorded
    (Req 15.4's "IF a received trigger event identifier is already recorded
    in State_Store" check)."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"EVENT#{trigger_source_id}", "sk": "RECEIVED"})
    )
    item = resp.get("Item")
    if item is None:
        return False
    expires_at = item.get("expires_at")
    if expires_at is not None and int(expires_at) <= int(datetime.now(timezone.utc).timestamp()):
        return False
    return True


def get_trigger_event(trigger_source_id: str) -> dict[str, Any] | None:
    """Return the persisted processed-trigger-event item, or `None`.

    Added by task 17.2: Req 1.9's duplicate-suppression record must reference
    "the identifier of the run started for the original payload", so the
    caller needs the *original* run id, not merely a boolean "seen before"
    answer (`has_trigger_event_been_processed`). `record_trigger_event` now
    stores that run id on the `EVENT#{trigger_source_id}` item, and this
    helper reads it back. Returns `None` when no record exists or the record
    is past its retention window (same expiry handling as
    `has_trigger_event_been_processed`).
    """
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"EVENT#{trigger_source_id}", "sk": "RECEIVED"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    expires_at = item.get("expires_at")
    if expires_at is not None and int(expires_at) <= int(datetime.now(timezone.utc).timestamp()):
        return None
    return _from_dynamo(dict(item))


def record_trigger_event(trigger_source_id: str, run_id: str | None = None) -> bool:
    """Atomically record `trigger_source_id` as processed (Req 15.3: "record
    the trigger event identifier in State_Store before performing any
    downstream write for that event"), retained for 72 hours.

    Uses a fixed sort key (`sk = "RECEIVED"`, not a timestamp-embedding key
    — see module docstring's deviation note #3) so the
    `attribute_not_exists(pk)` guard genuinely detects a duplicate on the
    second call for the same `trigger_source_id`.

    Args:
        trigger_source_id: The inbound trigger's source identifier.
        run_id: The run started for this trigger, stored on the record so a
            later duplicate can name it (Req 1.9's "referencing the
            identifier of the run started for the original payload"). Added
            by task 17.2; optional so existing callers are unaffected.

    Returns:
        `True` if this call newly recorded the event (first time seen);
        `False` if a record already existed (duplicate — the caller should
        skip all downstream processing per Req 15.4).
    """
    item = {
        "pk": f"EVENT#{trigger_source_id}",
        "sk": "RECEIVED",
        "trigger_source_id": trigger_source_id,
        "run_id": run_id,
        "received_at": _now_iso(),
        "expires_at": _epoch_seconds(TRIGGER_EVENT_TTL_HOURS),
        "ttl": _epoch_seconds(TRIGGER_EVENT_TTL_HOURS),
    }
    try:
        _write_with_retry(
            lambda: _table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        )
        return True
    except ClientError as exc:
        if _is_conditional_check_failure(exc):
            return False
        raise


# ---------------------------------------------------------------------------
# Run-progress persistence (Req 15.5, 15.11)
# ---------------------------------------------------------------------------


def persist_run_progress(run_id: str, node_id: str, status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist one node's run progress (design.md §4.4 key schema:
    `RUN#{run_id}` / `NODE#{node_id}`), so a paused run (awaiting human
    escalation) can resume in a different process (Req 15.5, 15.11) from
    the last persisted step rather than from the run start.

    Args:
        run_id: The Incident_Graph run identifier.
        node_id: The node that just completed/paused.
        status: The node's outcome (e.g. `"succeeded"`, `"paused"`,
            `"failed"`, `"skipped"` — the exact vocabulary is owned by the
            not-yet-implemented `agents/incident_graph.py` driver; this
            module stores whatever string it is given).
        payload: Arbitrary node-specific data needed to resume (e.g. the
            node's inputs, an `interruptId`). Defaults to an empty dict.

    Returns:
        The persisted item as a plain dict.
    """
    item = {
        "pk": f"RUN#{run_id}",
        "sk": f"NODE#{node_id}",
        "run_id": run_id,
        "node_id": node_id,
        "status": status,
        "payload": _to_dynamo_safe(payload or {}),
        "updated_at": _now_iso(),
    }
    _write_with_retry(lambda: _table().put_item(Item=item))
    return _from_dynamo(item)


def get_run_progress(run_id: str) -> list[dict[str, Any]]:
    """Retrieve every persisted node-progress item for `run_id`, so a resume
    driver can determine which steps already completed (Req 15.5, 15.11).

    Returns:
        A list of plain dicts (one per persisted node), in the order
        DynamoDB returns them for the `RUN#{run_id}` partition (insertion
        order is not guaranteed by `sk` alone across arbitrary `node_id`
        values; a caller that needs a specific ordering should sort by
        `updated_at`).
    """
    resp = _write_with_retry(
        lambda: _table().query(
            KeyConditionExpression=Key("pk").eq(f"RUN#{run_id}") & Key("sk").begins_with("NODE#")
        )
    )
    return [_from_dynamo(dict(item)) for item in resp.get("Items", [])]


# ---------------------------------------------------------------------------
# Run records (Req 1.5, 1.7, 1.8, 12.8) — added by task 17.2/17.3.
#
# design.md §4.4 declares the `RUN#{run_id}` partition with `NODE#{node_id}`
# sort keys for *run progress* (Req 15.5) but declares no item for the run
# *record itself*, while Req 1.5/1.7/12.8 require a per-run record that
# Coordinator_Console can list "for up to the 20 most recent runs, ordered by
# start timestamp from most recent to least recent". Req 1.5's authoritative
# copy of that record is the Audit_Ledger entry `surface/entrypoint.py` writes
# at run start (append-only, per Req 1.5's own wording); the item below is the
# *queryable index* over it, because `thunai-audit`'s key schema
# (`pk = "RUN#{run_id}"`) supports "give me one known run" but not "give me the
# 20 newest runs" without a table scan.
#
# The index item reuses the already-declared `RUN#{run_id}` partition with
# `sk = "META"` (exactly the `META` sort-key convention every other entity in
# §4.4 uses) and the already-declared overloaded GSI1, under a new literal
# `gsi1pk = "RUN_BY_START"` use case with `gsi1sk = "{started_at}"`. This adds
# no new table and no new index — one more GSI1 use case alongside
# INCIDENT_BY_SEVERITY / REQUEST_BY_STATE / ESCALATION_BY_STATUS /
# RESPONDER_BY_AVAILABILITY. Stated here rather than inferred silently, since
# design.md §4.4 does not name this use case itself.
# ---------------------------------------------------------------------------

RUN_INDEX_GSI1PK: Final[str] = "RUN_BY_START"
"""GSI1 partition value for the "runs ordered by start timestamp" use case."""

#: Terminal run statuses (Req 1.6's "terminal run record"). A run whose status
#: is not in this set is still in progress — the basis of Req 1.8's sweep skip.
TERMINAL_RUN_STATUSES: Final[frozenset[str]] = frozenset(
    {
        # agents/incident_graph.py's run outcomes, minus `paused` (a paused run
        # is explicitly non-terminal — it is waiting on a coordinator).
        "complete",
        "partial",
        "halted",
        "failed",
        # surface/entrypoint.py's own outcomes.
        "resumed",
        "resume_integrity_failure",
        "skipped",
        "rejected",
        "duplicate_suppressed",
    }
)


def _run_item(run_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    item = _to_dynamo_safe(dict(fields))
    item["pk"] = f"RUN#{run_id}"
    item["sk"] = "META"
    item["run_id"] = run_id
    item["gsi1pk"] = RUN_INDEX_GSI1PK
    item["gsi1sk"] = str(fields.get("started_at") or _now_iso())
    return item


def put_run_record(run_id: str, **fields: Any) -> dict[str, Any]:
    """Write (create or overwrite) the run index item, last-write-wins.

    Args:
        run_id: The run identifier.
        **fields: The run's recorded fields — `trigger_type`,
            `trigger_source_id`, `started_at` (ISO-8601 UTC), `status`,
            plus any of `mode`, `terminal_status`, `finished_at`,
            `latency_ms`, `input_tokens`/`output_tokens`, `model_id`,
            `estimated_cost_minor_units`, `currency`, `config_ids`.

    Returns:
        The persisted item as a plain dict.
    """
    item = _run_item(run_id, fields)
    _write_with_retry(lambda: _table().put_item(Item=item))
    return _from_dynamo(dict(item))


def get_run_record(run_id: str) -> dict[str, Any] | None:
    """Read the run index item for `run_id`, or `None` if absent."""
    resp = _write_with_retry(
        lambda: _table().get_item(Key={"pk": f"RUN#{run_id}", "sk": "META"})
    )
    item = resp.get("Item")
    if item is None:
        return None
    return _from_dynamo(dict(item))


def update_run_record(run_id: str, **field_updates: Any) -> dict[str, Any]:
    """Read-merge-write update of a run index item (e.g. its terminal status,
    latency, token counts and estimated cost once the run finishes).

    Follows `update_incident`'s read-merge-write pattern — there is no
    concurrency invariant on a run record (exactly one process advances a
    given run), so no conditional update is needed.
    """
    existing = get_run_record(run_id) or {"run_id": run_id}
    merged = {**existing, **field_updates}
    merged.pop("pk", None)
    merged.pop("sk", None)
    merged.pop("gsi1pk", None)
    merged.pop("gsi1sk", None)
    merged.pop("run_id", None)
    return put_run_record(run_id, **merged)


def query_recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Return the `limit` most recent run records, newest start first
    (Req 1.7, 12.8).

    Queries GSI1's `RUN_BY_START` use case with `ScanIndexForward=False`
    (descending `gsi1sk` = descending `started_at`) and a DynamoDB `Limit`, so
    the 20-run truncation is applied by the index's declared sort key rather
    than by reading every run and sorting in memory.
    """
    resp = _write_with_retry(
        lambda: _table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("gsi1pk").eq(RUN_INDEX_GSI1PK),
            ScanIndexForward=False,
            Limit=max(1, int(limit)),
        )
    )
    return [_from_dynamo(dict(item)) for item in resp.get("Items", [])]


def query_in_progress_runs(trigger_type: str | None = None, *, scan_limit: int = 50) -> list[dict[str, Any]]:
    """Return recent runs that have not reached a terminal status (Req 1.8).

    Args:
        trigger_type: When given, only runs with this `trigger_type` (e.g.
            `"sweep"`) are returned — the sweep-skip check.
        scan_limit: How many of the most recent runs to inspect. A run older
            than the newest `scan_limit` runs cannot meaningfully still be
            "in progress" for the sweep-skip decision (a sweep runs at most
            once per interval and is bounded by the 900s max run duration).

    Returns:
        The matching non-terminal run records, newest start first.
    """
    candidates = query_recent_runs(limit=scan_limit)
    results: list[dict[str, Any]] = []
    for run in candidates:
        if str(run.get("status", "")) in TERMINAL_RUN_STATUSES:
            continue
        if trigger_type is not None and run.get("trigger_type") != trigger_type:
            continue
        results.append(run)
    return results
