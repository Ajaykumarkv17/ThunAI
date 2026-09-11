"""``thunai-audit`` DynamoDB access — the append-only Audit_Ledger (Req 15.6, 16.8).

Implements the ``thunai-audit`` key schema and write path described in
design.md §4.4 ("DynamoDB single-table design"):

    ``thunai-audit`` key schema: ``pk = "RUN#{run_id}"``,
    ``sk = "{iso_timestamp}#{entry_id}"`` — supports Req 12.7's "entries for a
    selected incident ordered oldest to newest" via a GSI
    (``gsi1pk = "INCIDENT#{incident_id}"``, ``gsi1sk = "{iso_timestamp}"``)
    populated whenever an entry carries an ``incident_id``. Every write is
    ``PutItem`` with ``ConditionExpression="attribute_not_exists(pk)"`` —
    Req 15.6's append-only guarantee is a table-level condition on every
    write, not a convention; an ``UpdateItem``/``DeleteItem`` call against
    this table is never issued anywhere in the codebase, and the
    audit-writer IAM policy grants ``PutItem``/``Query``/``GetItem`` only.

Task-prompt note on the partition key: the task description for 7.1 suggests
using ``AuditEntry.entry_id`` directly as the DynamoDB partition key. This
module instead follows design.md §4.4's already-specified, more detailed key
schema (``pk = "RUN#{run_id}"``, ``sk = "{iso_timestamp}#{entry_id}"``) rather
than the simpler task-prompt suggestion, because design.md's schema is a
concrete decision already justified by a named GSI and a named requirement
(Req 12.7's oldest-to-newest per-incident read), not an area where the docs
and the design disagree (the "docs win" rule in tasks.md's verify-first
banner applies to *API surface* disputes, not to re-deriving a key schema
design.md has already made explicit). The **append-only guarantee itself**
is unaffected by which attribute is named ``pk`` vs ``sk``:
``ConditionExpression="attribute_not_exists(pk)"`` is evaluated by DynamoDB
against the single item identified by the *complete* primary key (pk AND sk
together, confirmed against the current AWS documentation — see
"Verification note" below), so a composite ``(pk, sk)`` scheme still gives
exactly the same "this exact item may never be overwritten" guarantee that a
single ``entry_id`` partition key would, while additionally supporting the
per-run and per-incident query patterns Req 12.7 and the Coordinator_Console
audit view need. ``entry_id`` remains globally unique (used inside ``sk`` and
stored as its own attribute) so a caller can still address one entry
precisely.

Verification note (mandatory per tasks.md 7.1, "verify first"):
    Consulted the AWS documentation MCP server against the installed
    ``boto3==1.43.90`` (per ``requirements.txt``) before writing this module:

    - ``Expressions.ConditionExpressions.html`` ("Conditional put"): a
      ``PutItem`` with ``ConditionExpression="attribute_not_exists(<key
      attribute>)"`` proceeds only when no item with that *complete* primary
      key (partition key + sort key, for a composite-key table) already
      exists; DynamoDB evaluates the condition against the single item
      identified by the request's key, so ``attribute_not_exists(pk)`` on a
      composite-key table is true only when no item with that exact
      ``(pk, sk)`` pair exists yet — confirming the "append-only, no
      overwrite" pattern is exactly what design.md §4.4 states, and that it
      still holds with the ``pk``/``sk`` composite scheme used here (not
      only with a single-attribute partition key as the task prompt's
      simplified phrasing might suggest).
    - A condition-expression failure raises the boto3-modelled
      ``ConditionalCheckFailedException`` (a subclass surfaced through
      ``botocore.exceptions.ClientError`` with
      ``error.response["Error"]["Code"] == "ConditionalCheckFailedException"``)
      and leaves the existing item unchanged — confirmed against
      "Protect a write with a condition expression" / the ``PutItem`` API
      reference's "Errors" section. This module maps that specific error
      code to :class:`AuditAppendDuplicateError` (rather than the more
      generic :class:`AuditAppendError`) so callers can distinguish "someone
      already wrote this exact entry" from an arbitrary write failure, per
      Req 15.6's requirement that a rejected update/delete attempt be
      reported "with an error indicating that the ledger is append-only."
    - Read path: ``ScanIndexForward`` (``boto3.resource("dynamodb").Table
      .query(..., ScanIndexForward=True)``) — confirmed against the
      ``Query`` API reference and ``Query.KeyConditionExpressions.html``:
      ``ScanIndexForward=True`` (the default) returns items in ascending
      sort-key order, which for this table's ``sk = "{iso_timestamp}#..."``
      shape is oldest-first — exactly the "Oldest→newest" ordering
      task 25.3's per-incident audit-trail view (not yet implemented) needs,
      confirmed rather than assumed.
    - There is genuinely **no** ``update_item``/``delete_item`` code path
      anywhere in this module. Every function below either performs a
      ``put_item`` (write path) or a ``query``/``get_item`` (read path); no
      function calls ``Table.update_item`` or ``Table.delete_item``, and no
      such method is imported, referenced, or wrapped. This is the intended,
      structural expression of Req 15.6/16.8's append-only invariant — there
      is no update/delete capability in this module for a bug to route
      through, not merely a convention.

    No boto3 API deviated from design.md's assumed shape; nothing here
    required a docs-vs-design reconciliation.

Testing note: no live ``thunai-audit`` table exists yet (the CDK data stack,
tasks 21.x, is not yet implemented). Unit tests in
``tests/unit/test_audit_ledger.py`` use ``moto`` (``@mock_aws``) to provide
an in-process fake DynamoDB backend and create the table's schema themselves
before exercising this module — the same "fake/mocked backend, self-declared
schema" approach as ``memory/state_store.py`` (task 6.1) is expected to use,
so both modules' tests share one mocking convention. ``moto`` was already
present in the environment (version 5.1.21) at the time this task was
implemented; it has now been added as an explicit, exactly-pinned test-only
dependency in ``pyproject.toml``'s ``[project.optional-dependencies].test``
list and in ``requirements.txt`` (pinned to the already-installed
``moto==5.1.21`` rather than the latest PyPI release, so the pin matches
what was actually verified against in this session).
"""

from __future__ import annotations

import os
from typing import Any, Final
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from memory.state_store import _from_dynamo, _to_dynamo_safe
from schemas.entities import AuditEntry

__all__ = [
    "AuditAppendError",
    "AuditAppendDuplicateError",
    "new_entry_id",
    "append_entry",
    "get_entries_for_run",
    "get_entries_for_incident",
]

#: Env var naming the live ``thunai-audit`` table (``.env.example``,
#: ``THUNAI_AUDIT_TABLE=thunai-audit``). Read at call time (not at import
#: time) so tests can point at a per-test moto table via
#: ``monkeypatch.setenv`` without needing to reload this module.
_AUDIT_TABLE_ENV_VAR: Final[str] = "THUNAI_AUDIT_TABLE"
_DEFAULT_AUDIT_TABLE_NAME: Final[str] = "thunai-audit"

#: Name of the GSI serving "entries for a selected incident, oldest to
#: newest" (Req 12.7), per design.md §4.4.
_INCIDENT_GSI_NAME: Final[str] = "gsi1"


class AuditAppendError(Exception):
    """Raised when an ``Audit_Ledger`` append fails for any reason other than
    the entry already existing (see :class:`AuditAppendDuplicateError` for
    that specific case).

    Per Req 16.9 / design.md's error-handling table, a caller (the
    eventual ``AuditHook.after()``, task 8.2) must let this propagate so the
    triggering write-tool call can be blocked rather than allowed to execute
    without a successful audit append.
    """


class AuditAppendDuplicateError(AuditAppendError):
    """Raised when an append is attempted for an ``(run_id, timestamp,
    entry_id)`` key already present in ``Audit_Ledger``.

    Req 15.6: "THE Audit_Ledger SHALL accept appends only and SHALL reject
    any update or deletion of an existing entry with an error indicating
    that the ledger is append-only, leaving the existing entry unchanged."
    Raised from the boto3-modelled ``ConditionalCheckFailedException`` that
    a ``PutItem`` with ``ConditionExpression="attribute_not_exists(pk)"``
    raises when the addressed item already exists (verified above).
    """


def new_entry_id() -> str:
    """Return a fresh, globally-unique ``AuditEntry.entry_id`` value.

    A thin, named helper (rather than inlining ``uuid4().hex`` at every call
    site) so ``harness/audit.py::append_audit_entry`` and any future caller
    generate ids the same way.
    """
    return uuid4().hex


def _table() -> Any:
    """Return the ``boto3`` DynamoDB ``Table`` resource for ``thunai-audit``.

    Reads ``THUNAI_AUDIT_TABLE`` from the environment on every call (not
    cached at import time) so tests can retarget a per-test moto table
    without reloading this module. Falls back to the design default
    ``"thunai-audit"`` (matching ``.env.example``) when the env var is unset,
    consistent with ``policy``/``agents`` modules' pattern of a documented
    in-code default plus an env override.
    """
    table_name = os.environ.get(_AUDIT_TABLE_ENV_VAR, _DEFAULT_AUDIT_TABLE_NAME)
    return boto3.resource("dynamodb").Table(table_name)


def _entry_to_item(entry: AuditEntry, *, sk: str, incident_id: str | None) -> dict[str, Any]:
    """Build the raw DynamoDB item dict for one ``AuditEntry`` (design.md §4.4).

    ``pk``/``sk`` are the table's composite primary key; ``gsi1pk``/``gsi1sk``
    are populated only when ``incident_id`` is supplied, matching design.md's
    "populated whenever an entry carries an incident_id" — the GSI item
    attributes are simply omitted (not written as ``None``) otherwise, since
    a DynamoDB GSI naturally excludes items missing its key attributes.
    """
    item: dict[str, Any] = {
        "pk": f"RUN#{entry.run_id}",
        "sk": sk,
        **_to_dynamo_safe(entry.model_dump(exclude_none=True)),
    }
    if incident_id is not None:
        item["gsi1pk"] = f"INCIDENT#{incident_id}"
        item["gsi1sk"] = entry.timestamp
        item["incident_id"] = incident_id
    return item


def _item_to_entry(item: dict[str, Any]) -> AuditEntry:
    """Reconstruct an :class:`AuditEntry` from a raw DynamoDB item, dropping
    the table-specific bookkeeping attributes (``pk``, ``sk``, ``gsi1pk``,
    ``gsi1sk``, ``incident_id``) that are not part of the ``AuditEntry``
    schema.
    """
    fields = {k: v for k, v in item.items() if k not in {"pk", "sk", "gsi1pk", "gsi1sk", "incident_id"}}
    return AuditEntry.model_validate(_from_dynamo(fields))


def append_entry(entry: AuditEntry, *, incident_id: str | None = None) -> None:
    """Append one entry to ``Audit_Ledger`` (Req 15.6, 16.8).

    Performs a single ``PutItem`` with
    ``ConditionExpression="attribute_not_exists(pk)"`` against the
    ``(pk, sk)`` pair derived from ``entry``, so a second append attempt for
    the same ``(run_id, timestamp, entry_id)`` combination raises rather
    than silently overwriting the first entry — the append-only invariant
    is a table-level condition on every write, never a convention (design.md
    §4.4). There is no function in this module that performs an
    ``update_item`` or a ``delete_item`` against this table.

    Args:
        entry: The entry to append. ``entry.timestamp`` and ``entry.entry_id``
            together form the item's sort key (``sk``); use
            :func:`new_entry_id` and an ISO-8601 UTC timestamp when
            constructing ``entry`` if the caller has not already chosen
            these.
        incident_id: When supplied, populates the ``gsi1pk``/``gsi1sk``
            attributes that back Req 12.7's "entries for a selected incident,
            oldest to newest" query (see :func:`get_entries_for_incident`).
            ``AuditEntry`` itself carries no ``incident_id`` field (per
            ``schemas/entities.py``), so this is supplied by the caller
            (ultimately ``harness/audit.py::append_audit_entry``) out of
            band rather than read off ``entry``.

    Raises:
        AuditAppendDuplicateError: If an item with the same ``(pk, sk)``
            already exists (the append-only guarantee rejected the write).
        AuditAppendError: If the append fails for any other reason (e.g. a
            transient DynamoDB error). Per Req 16.9, callers upstream of
            this function (ultimately ``AuditHook``) must let this
            exception propagate rather than swallow it, so the triggering
            write-tool call is blocked instead of proceeding without a
            successful audit append.
    """
    sk = f"{entry.timestamp}#{entry.entry_id}"
    item = _entry_to_item(entry, sk=sk, incident_id=incident_id)
    try:
        _table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "ConditionalCheckFailedException":
            raise AuditAppendDuplicateError(
                f"Audit_Ledger entry already exists for run_id={entry.run_id!r}, "
                f"entry_id={entry.entry_id!r} (ledger is append-only)"
            ) from exc
        raise AuditAppendError(
            f"Audit_Ledger append failed for run_id={entry.run_id!r}, "
            f"entry_id={entry.entry_id!r}: {exc}"
        ) from exc


def get_entries_for_run(run_id: str) -> list[AuditEntry]:
    """Return every ``Audit_Ledger`` entry for one run, oldest first.

    Queries the table's primary key (``pk = "RUN#{run_id}"``) with
    ``ScanIndexForward=True`` (ascending sort-key order — confirmed against
    the current ``Query`` API reference), which for this table's
    ``sk = "{iso_timestamp}#{entry_id}"`` shape is chronological
    oldest-to-newest order.

    Args:
        run_id: The run identifier to fetch entries for.

    Returns:
        The run's entries as :class:`AuditEntry` instances, oldest first.
        Empty when the run has no recorded entries.
    """
    response = _table().query(
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": f"RUN#{run_id}"},
        ScanIndexForward=True,
    )
    return [_item_to_entry(item) for item in response.get("Items", [])]


def get_entries_for_incident(incident_id: str) -> list[AuditEntry]:
    """Return every ``Audit_Ledger`` entry recorded against one incident,
    oldest first (Req 12.7: "entries for a selected incident ordered oldest
    to newest").

    Queries the ``gsi1`` GSI (``gsi1pk = "INCIDENT#{incident_id}"``) with
    ``ScanIndexForward=True``, so only entries appended with a matching
    ``incident_id`` (see :func:`append_entry`'s ``incident_id`` parameter)
    are returned.

    Args:
        incident_id: The incident identifier to fetch entries for.

    Returns:
        The incident's entries as :class:`AuditEntry` instances, oldest
        first. Empty when the incident has no entries carrying an
        ``incident_id`` reference.
    """
    response = _table().query(
        IndexName=_INCIDENT_GSI_NAME,
        KeyConditionExpression="gsi1pk = :gsi1pk",
        ExpressionAttributeValues={":gsi1pk": f"INCIDENT#{incident_id}"},
        ScanIndexForward=True,
    )
    return [_item_to_entry(item) for item in response.get("Items", [])]
