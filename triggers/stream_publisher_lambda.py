"""DynamoDB Streams -> AppSync Events publisher (Req 12.5, 14.3, 11.2; design.md §3.8).

This Lambda is subscribed to the DynamoDB Stream on ``thunai-state``. It
consumes each stream record, maps the changed item to its namespaced AppSync
Events channel (design.md §3.8's channel conventions), and publishes the change
so the three live surfaces update in near-real-time:

- ``/incidents/{incident_id}`` — the Coordinator_Console open-incident list
  (≤5s update, Req 12.5) and the public Resident_Status_Page (≤15s update,
  Req 14.3) both subscribe here (the resident page reads the same incident
  channel via a wildcard subscription — design.md §3.8's ``events.connect(
  '/incidents/*')``).
- ``/escalations/{escalation_id}`` — the Coordinator_Console Decision_Inbox
  (≤10s publish, Req 11.2), so a newly-opened escalation appears (and a
  resolved one drops out of the open list) without a poll.
- ``/shelters/{shelter_id}`` — shelter availability, shown on both the
  Coordinator_Console (capacity per shelter, Req 12.4) and the
  Resident_Status_Page (Req 14.3).
- ``/responders/{responder_id}`` — responder availability, for the
  Responder_Interface and the console's dispatch view.

design.md §3.8 turns "a DynamoDB commit into the ≤5s Coordinator_Console
update / ≤10s Decision_Inbox publish / ≤15s Resident_Status_Page update"
through this one publish path — the differing latency budgets are all met by
the same path; this module implements that path.

Channel derivation
-------------------
``memory/state_store.py`` keys every entity ``pk = "<TYPE>#<id>"``,
``sk = "META"`` (e.g. ``INCIDENT#inc-1`` / ``META``). :func:`_channel_for`
maps that ``pk`` prefix to its channel namespace. Non-entity items in the same
table (idempotency records ``IDEMPOTENCY#...``, processed-trigger events
``EVENT#...``, run-progress/run-index ``RUN#...``) carry no coordinator- or
resident-facing state and are intentionally **not** published — they are
internal bookkeeping, not surface state, so publishing them would be noise on
the channels and needless public fan-out.

Publishing to AppSync Events
----------------------------
AppSync Events exposes an HTTP publish endpoint (``THUNAI_EVENT_API_HTTP_ENDPOINT``,
a stack output). A backend service publishes with a SigV4-signed ``POST`` of
``{"channel": <name>, "events": ["<json-string>", ...]}`` using the Lambda's
IAM role (AWS_IAM channel authorization — design.md §3.8: "AppSync Events
channel authorization ... re-check the same Cognito group claim" for *client*
subscribers, while a backend publisher authenticates with IAM). Publishing is
best-effort per record: a failed publish for one record is recorded and skipped
rather than failing the whole batch, so one bad channel cannot wedge the stream
(a wedged stream would stop every surface updating, the opposite of Req 12.5's
intent). Delivering the same change more than once (a DynamoDB Streams retry)
is harmless — the surfaces render the latest item state idempotently.

Verification note: docs/MCP verification was explicitly skipped per the task
note. The DynamoDB Streams record shape (``eventName``, ``dynamodb.Keys`` /
``NewImage`` / ``OldImage`` in DynamoDB attribute-value wire format) and the
SigV4 signing approach (``botocore.auth.SigV4Auth`` + ``AWSRequest``, the
service being ``appsync``) are reused from patterns standard to this codebase's
boto3 usage; the SigV4 signer is used rather than a bare ``requests`` call so
no extra dependency is added.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

__all__ = ["handler", "channel_for", "CHANNEL_PREFIXES"]

_EVENT_API_HTTP_ENDPOINT_ENV_VAR = "THUNAI_EVENT_API_HTTP_ENDPOINT"

#: Maps a ``memory/state_store.py`` entity ``pk`` prefix to its AppSync Events
#: channel namespace (design.md §3.8). A prefix not present here is an internal
#: bookkeeping item that is never published (see module docstring).
CHANNEL_PREFIXES: dict[str, str] = {
    "INCIDENT": "incidents",
    "ESCALATION": "escalations",
    "SHELTER": "shelters",
    "RESPONDER": "responders",
}


# ---------------------------------------------------------------------------
# DynamoDB attribute-value decoding (Streams deliver the low-level wire shape).
# ---------------------------------------------------------------------------


def _deserialize(value: dict[str, Any]) -> Any:
    """Convert one DynamoDB attribute-value (``{"S": ...}`` / ``{"N": ...}`` /
    ``{"M": ...}`` / ...) into a plain Python value.

    Streams deliver ``Keys``/``NewImage``/``OldImage`` in the low-level
    attribute-value format regardless of how the item was written; this walks
    it recursively so the published event body is plain JSON, not wire types.
    """
    if not isinstance(value, dict) or not value:
        return value
    (type_tag, inner), = value.items()
    if type_tag == "S":
        return inner
    if type_tag == "N":
        # Preserve integers as ints, everything else as float — the surfaces
        # render numbers, they do not need Decimal precision.
        text = str(inner)
        try:
            as_int = int(text)
            return as_int if str(as_int) == text else float(text)
        except ValueError:
            return float(text)
    if type_tag == "BOOL":
        return bool(inner)
    if type_tag == "NULL":
        return None
    if type_tag == "M":
        return {k: _deserialize(v) for k, v in inner.items()}
    if type_tag == "L":
        return [_deserialize(v) for v in inner]
    if type_tag in ("SS", "NS", "BS"):
        return list(inner)
    if type_tag == "B":
        return inner
    return inner


def _deserialize_image(image: dict[str, Any] | None) -> dict[str, Any]:
    """Deserialise a full ``NewImage``/``OldImage`` map, or ``{}`` if absent."""
    if not image:
        return {}
    return {key: _deserialize(av) for key, av in image.items()}


# ---------------------------------------------------------------------------
# Channel derivation (design.md §3.8).
# ---------------------------------------------------------------------------


def channel_for(pk: str, sk: str | None) -> str | None:
    """Return the AppSync Events channel for a ``(pk, sk)`` entity key, or ``None``.

    Only the entity ``META`` row of a publishable entity type (see
    :data:`CHANNEL_PREFIXES`) maps to a channel; every other row (a non-``META``
    sort key, or an internal ``IDEMPOTENCY#``/``EVENT#``/``RUN#`` partition) is
    not surface state and returns ``None`` (the caller skips it).

    Args:
        pk: The item's partition key, e.g. ``"INCIDENT#inc-1"``.
        sk: The item's sort key, e.g. ``"META"``. ``None`` is treated as a
            non-``META`` row and not published.

    Returns:
        The channel name, e.g. ``"/incidents/inc-1"``, or ``None`` when the
        item is not publishable surface state.
    """
    if not pk or "#" not in pk:
        return None
    if sk != "META":
        return None
    prefix, _, entity_id = pk.partition("#")
    namespace = CHANNEL_PREFIXES.get(prefix)
    if namespace is None or not entity_id:
        return None
    return f"/{namespace}/{entity_id}"


# Backwards-friendly private alias used internally.
_channel_for = channel_for


def _event_body(record: dict[str, Any], entity_id: str, namespace: str) -> dict[str, Any]:
    """Build the JSON body published for one stream record.

    Carries the change type (``INSERT``/``MODIFY``/``REMOVE``, from
    ``record["eventName"]``), the entity type and id, and the current item
    state (the deserialised ``NewImage``, or the ``OldImage`` on a delete so a
    subscriber knows which entity was removed). The surfaces render the latest
    item state, so a subscriber never needs to diff — the full current image is
    included.
    """
    ddb = record.get("dynamodb", {})
    event_name = record.get("eventName", "MODIFY")
    new_image = _deserialize_image(ddb.get("NewImage"))
    body: dict[str, Any] = {
        "type": event_name,
        "entity": namespace,
        "id": entity_id,
    }
    if new_image:
        # Drop internal index attributes the surfaces have no use for.
        body["item"] = {k: v for k, v in new_image.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")}
    elif event_name == "REMOVE":
        old_image = _deserialize_image(ddb.get("OldImage"))
        body["removed"] = {k: v for k, v in old_image.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")}
    return body


# ---------------------------------------------------------------------------
# AppSync Events publish (SigV4-signed HTTP POST).
# ---------------------------------------------------------------------------


def _publish(channel: str, body: dict[str, Any]) -> None:
    """Publish one event to ``channel`` on the AppSync Events HTTP endpoint.

    Signs the request with SigV4 using the Lambda's own IAM credentials
    (service ``appsync``), so channel authorization is the backend
    IAM-authenticated publisher path (design.md §3.8). AppSync Events accepts a
    ``{"channel": ..., "events": ["<json-string>"]}`` body where each event is
    itself a JSON string.

    Raises:
        RuntimeError: If ``THUNAI_EVENT_API_HTTP_ENDPOINT`` is not configured.
        Exception: Propagated from the underlying HTTP send on a publish
            failure (the caller catches it per-record so one failure does not
            fail the whole batch).
    """
    endpoint = os.environ.get(_EVENT_API_HTTP_ENDPOINT_ENV_VAR, "")
    if not endpoint:
        raise RuntimeError(f"{_EVENT_API_HTTP_ENDPOINT_ENV_VAR} is not configured")

    region = os.environ.get("AWS_REGION", "us-west-2")
    url = endpoint.rstrip("/") + "/event"
    payload = json.dumps({"channel": channel, "events": [json.dumps(body)]})

    session = boto3.Session()
    credentials = session.get_credentials()
    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, "appsync", region).add_auth(aws_request)

    import urllib.request

    prepared = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        headers=dict(aws_request.headers),
        method="POST",
    )
    with urllib.request.urlopen(prepared, timeout=5) as response:  # noqa: S310 - AWS endpoint, SigV4-signed
        response.read()


# ---------------------------------------------------------------------------
# Handler.
# ---------------------------------------------------------------------------


def _records(event: dict[str, Any] | None) -> Iterable[dict[str, Any]]:
    if not isinstance(event, dict):
        return []
    records = event.get("Records")
    return records if isinstance(records, list) else []


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """DynamoDB Streams entry point (Req 12.5, 14.3, 11.2).

    Args:
        event: The DynamoDB Streams batch event (``{"Records": [...]}``), each
            record carrying ``eventName`` and ``dynamodb.{Keys,NewImage,OldImage}``.
        context: The Lambda context object (unused; accepted for the AWS
            handler signature).

    Returns:
        A summary: ``{"ok": True, "published": <n>, "skipped": <n>, "failed":
        <n>, "failures": [...]}``. Publishing is best-effort per record — a
        record whose key is not publishable surface state is skipped, and a
        record whose publish call fails is counted in ``failed`` (with the
        error captured in ``failures``) but does not abort the batch. Never
        raises.
    """
    published = 0
    skipped = 0
    failed = 0
    failures: list[dict[str, Any]] = []

    for record in _records(event):
        ddb = record.get("dynamodb", {}) if isinstance(record, dict) else {}
        keys = _deserialize_image(ddb.get("Keys"))
        pk = keys.get("pk")
        sk = keys.get("sk")

        channel = channel_for(str(pk) if pk is not None else "", str(sk) if sk is not None else None)
        if channel is None:
            skipped += 1
            continue

        namespace, _, entity_id = channel.lstrip("/").partition("/")
        body = _event_body(record, entity_id, namespace)
        try:
            _publish(channel, body)
            published += 1
        except Exception as exc:  # noqa: BLE001 - best-effort per record; see docstring
            failed += 1
            failures.append({"channel": channel, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "ok": True,
        "published": published,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
    }
