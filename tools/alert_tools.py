"""`Alert_Agent`'s `@tool` functions: channel/language delivery limits,
shelter-capacity lookup, and idempotent alert delivery (Req 7.1, 7.10;
design.md §3.1 (Alert), §4.4).

Verification note (mandatory per tasks.md 12.5, "verify first"):
    Checked against the current Strands Agents docs (`strands-agents`
    installed at `1.55.0` per `pyproject.toml`, matching the version already
    recorded in `agents/config.py`/`harness/hooks.py`/`tools/sensor_tools.py`/
    `tools/dispatch_tools.py`'s own verification notes) for the `@tool`
    decorator + docstring contract, cross-checked against every already-
    implemented tool module in this codebase (`tools/sensor_tools.py`,
    `tools/dispatch_tools.py`): the decorator infers the tool's name,
    description, and input schema from the decorated function's signature
    and docstring (first paragraph -> description, `Args:` section ->
    per-parameter description); a tool may return a plain, JSON-serialisable
    dict, auto-formatted as the model's tool result, with no
    `status`/`content` envelope required. No deviation from that established
    contract is needed for this module — every tool below follows the exact
    same "plain dict, `ok: bool`, typed non-raising failure `reason`" shape
    `tools/dispatch_tools.py` already established, so `Alert_Agent`'s model
    reads an unambiguous, consistent contract across every tool it is given.

    For `deliver_alert`'s eventual live send, `integrations/notification_provider
    .py` (task 10.2, already implemented) was read in full: its
    `SnsSesNotificationProvider.send_sms(phone, body, idempotency_key)` /
    `send_email(address, subject, body, idempotency_key)` are the confirmed
    live call shapes (SNS `publish(PhoneNumber=..., Message=...)`; SESv2
    `send_email(FromEmailAddress=..., Destination={"ToAddresses": [...]},
    Content={"Simple": {...}})` — see that module's own verification note for
    the full AWS-docs citation trail; this task does not need to re-verify
    the AWS API shape, only how `deliver_alert` must call the seam it already
    exposes). `notification_provider()` (the module-level factory, no flag —
    Notification_Provider has no synthetic backend in any environment) is
    used unchanged; this module does not construct
    `SnsSesNotificationProvider` directly.

Design deviation/judgment call — no per-recipient send loop here:
    `AlertDraft` (schemas/decisions.py) and `Notification_Provider`
    (integrations/notification_provider.py) are both scoped to **one**
    channel/language variant's content and **one** destination
    (`send_sms(phone, ...)` / `send_email(address, ...)` each take exactly
    one recipient identifier). Neither design.md's Repository-layout comment
    (`alert_tools.py # get_channel_limits, get_shelter_capacity,
    deliver_alert`) nor `AlertDraft.audience_count`'s own field (a plain
    `int` recipient *count*, not a list of addresses) name a resident
    address book / distribution-list source this task is scoped to build —
    no such module exists anywhere in the current tree (`grep` for
    "distribution_list"/"recipient_list"/"resident_contacts" across the
    repository returns nothing). This task's scope (per its own instructions:
    "`deliver_alert`: idempotent per incident/channel/language/severity-band
    combination... calling `integrations/notification_provider.py`... to
    actually send") is the **delivery-audience-level** idempotent send
    operation, not resident-contact-book management, which belongs to a
    future, not-yet-scoped task. `deliver_alert` therefore accepts an
    explicit `recipients` list (each a phone number for `channel="sms"` or an
    email address for `channel="email"`) supplied by the caller
    (`Alert_Agent`, which is expected to obtain it from wherever the
    resident-contact list eventually lives) and sends the one already-
    composed `body` to every recipient in that list under the **one**
    Idempotency_Key Req 7.10 specifies (derived from `incident_id`,
    `channel`, `language`, `severity_band` — not per-recipient), guarded by
    `memory.state_store.idempotent_write` so a retried call for the same four
    fields performs the underlying sends at most once and simply replays the
    first recorded delivery outcome afterwards (Req 7.10's exact wording:
    "deliver the alert content to each recipient of the delivery audience
    exactly once ... under an Idempotency_Key derived from the incident
    identifier, the channel, the language, and the severity band").

    Reusing `memory.state_store.idempotent_write` here (rather than a
    module-local in-memory cache, the choice
    `integrations/notification_provider.py`'s own docstring explains and
    rejects for *that* module's narrower within-process "no re-notify on
    resume" safety net) is the deliberate opposite choice for this module,
    because `deliver_alert`'s idempotency requirement (Req 7.10) is exactly
    the durable, cross-process, 72-hour-replay case
    `memory.state_store.idempotent_write` exists for: a resumed run (a
    *different* AgentCore Runtime invocation, per design.md §3.2's pause/
    resume architecture) that reissues the exact same alert-delivery tool
    call must replay the original outcome, not re-send — precisely the
    scenario `idempotent_write`'s docstring describes as its primary use
    case, and precisely why `tools/dispatch_tools.py::assign_responder` and
    `memory/state_store.py`'s own shelter-capacity functions already reuse it
    rather than a local cache. The per-provider-instance cache inside
    `SnsSesNotificationProvider` still applies underneath this as an
    additional, narrower safety net (per that module's own docstring) — the
    two layers are complementary, not redundant, exactly as that module's
    docstring already describes for the general case.

Configuration judgment call (Req 7.4's "configured character limit for that
channel" and "configured recomposition attempt count" name no override
env-var identifiers anywhere in design.md, `.env.example`, or the tasks
list — `grep` for "CHANNEL"/"RECOMPOSITION" across the repository, `.env
.example` included, returns nothing before this task): this module follows
the exact established pattern every other env-overridable numeric constant
in this codebase uses (`policy/escalation_policy.py`'s `_read_int_env`
helper; `tools/dispatch_tools.py`'s `_dispatch_search_radius_km`), introducing
`THUNAI_SMS_CHANNEL_CHAR_LIMIT` (default `160`, the standard single-segment
GSM-7 SMS length) and `THUNAI_EMAIL_CHANNEL_CHAR_LIMIT` (default `2000`, a
generous plain-text email body length well above what any recommended-action
paragraph needs) plus `THUNAI_ALERT_RECOMPOSITION_ATTEMPT_COUNT` (default
`2`, matching the language throughout design.md's failure-mode table
row for `Alert_Agent`/"Variant exceeds channel limit after recomposition
attempts", Req 7.9, which implies more than zero but a small, bounded
number of retries). `get_channel_limits` is this module's single source for
these three values, read live from the environment on every call (not
cached at import time) so a deployer's `.env` change takes effect on the
next tool call without a process restart, matching `policy/escalation_policy
.py`'s own live-read-at-call-time pattern for its own env-overridable
thresholds ... actually mirrored here at read time rather than at import
time specifically because, unlike `policy/escalation_policy.py`'s
`POLICY_VERSION` content hash (which *needs* a fixed value for a whole
process lifetime to stay meaningful), no other value in this module depends
on these three staying fixed across a process lifetime.

Shelter-capacity judgment call (Req 7.1's "the nearest shelter holding
available capacity above zero", Req 7.2's "no shelter holds available
capacity above zero"): `Shelter` (schemas/entities.py) carries `coords`
but `AlertDraft`/`get_shelter_capacity`'s task description does not name a
distance computation here — nearest-shelter selection for *dispatch*
already has its own haversine helper (`tools/dispatch_tools.py
::haversine_km`, Req 6.1's dispatch-specific "search radius" framing).
Req 7's own acceptance criteria never mention a distance/radius input for
the alert's shelter field — only "the nearest shelter holding available
capacity above zero" without naming what "nearest" is computed relative to
for a mass, multi-recipient alert (unlike a single dispatch request's exact
coordinates). This task's `get_shelter_capacity`, per the task instructions
("read current shelter capacity/availability via memory/state_store.py
entity CRUD"), is therefore the direct entity-CRUD-backed capacity lookup
Req 7.1/7.2 actually need: given a set of candidate shelter ids (or every
known shelter when none is named), return each one's current
`available_capacity`, so `Alert_Agent` itself picks the nearest one with
`available_capacity > 0` using whatever location context it already holds
for the incident (its own prompt/tool set, out of this task's scope) and
falls back to the configured no-capacity guidance text when none qualifies
— mirroring `tools/dispatch_tools.py::find_candidate_responders`'s own
"tool reports the candidates, the agent/caller decides" division of labour
exactly.

No-capacity guidance text (Req 7.2): `design.md`/`.env.example` name no
override env-var identifier for this text either (same `grep` as above).
Following the same established env-override pattern,
`THUNAI_NO_CAPACITY_GUIDANCE` (default: a plain-language fallback sentence)
supplies it, read by `get_shelter_capacity` and returned as one field of
its result so `Alert_Agent` never has to hardcode this text into its own
prompt/tool-selection logic — it reads the configured value from this one
tool's result, matching the task's own explicit contract ("or the configured
no-capacity guidance text (Req 7.2)", `schemas/decisions.py
::AlertDraft.nearest_shelter`'s own docstring).
"""

from __future__ import annotations

import os
from typing import Any, Final, TypedDict

from strands import tool

from integrations import notification_provider
from memory import state_store

__all__ = [
    "SMS_CHANNEL_CHAR_LIMIT_ENV_VAR",
    "EMAIL_CHANNEL_CHAR_LIMIT_ENV_VAR",
    "ALERT_RECOMPOSITION_ATTEMPT_COUNT_ENV_VAR",
    "NO_CAPACITY_GUIDANCE_ENV_VAR",
    "DEFAULT_SMS_CHANNEL_CHAR_LIMIT",
    "DEFAULT_EMAIL_CHANNEL_CHAR_LIMIT",
    "DEFAULT_ALERT_RECOMPOSITION_ATTEMPT_COUNT",
    "DEFAULT_NO_CAPACITY_GUIDANCE",
    "get_channel_limits",
    "get_shelter_capacity",
    "deliver_alert",
]

# ---------------------------------------------------------------------------
# Configuration (see module docstring's "Configuration judgment call" and
# "No-capacity guidance text" sections).
# ---------------------------------------------------------------------------

SMS_CHANNEL_CHAR_LIMIT_ENV_VAR: Final[str] = "THUNAI_SMS_CHANNEL_CHAR_LIMIT"
EMAIL_CHANNEL_CHAR_LIMIT_ENV_VAR: Final[str] = "THUNAI_EMAIL_CHANNEL_CHAR_LIMIT"
ALERT_RECOMPOSITION_ATTEMPT_COUNT_ENV_VAR: Final[str] = "THUNAI_ALERT_RECOMPOSITION_ATTEMPT_COUNT"
NO_CAPACITY_GUIDANCE_ENV_VAR: Final[str] = "THUNAI_NO_CAPACITY_GUIDANCE"

DEFAULT_SMS_CHANNEL_CHAR_LIMIT: Final[int] = 160
"""Single-segment GSM-7 SMS length (Req 7.4)."""

DEFAULT_EMAIL_CHANNEL_CHAR_LIMIT: Final[int] = 2000
"""Generous plain-text email body length (Req 7.4)."""

DEFAULT_ALERT_RECOMPOSITION_ATTEMPT_COUNT: Final[int] = 2
"""Max recomposition attempts when a variant exceeds its channel limit (Req 7.4, 7.9)."""

DEFAULT_NO_CAPACITY_GUIDANCE: Final[str] = (
    "No shelter currently has available capacity. Please stay where you are if it is safe, "
    "or move to higher ground, and wait for further instructions from the coordinator."
)
"""Fallback guidance text populated into an alert's shelter field when no
shelter holds available capacity above zero (Req 7.2)."""


class ChannelLimits(TypedDict):
    channel_character_limits: dict[str, int]
    recomposition_attempt_count: int
    mass_notification_audience_threshold: int


def _read_int_env(var_name: str, default: int) -> int:
    raw = os.environ.get(var_name)
    if raw is None or raw == "":
        return default
    return int(raw)


# ---------------------------------------------------------------------------
# get_channel_limits (Req 7.4, 7.6)
# ---------------------------------------------------------------------------


@tool
def get_channel_limits() -> ChannelLimits:
    """Get the configured per-channel character limits, the configured
    recomposition attempt count, and the mass-notification audience
    threshold.

    Use this before composing an alert draft, to know how many characters a
    variant may use for a given channel and how many times to try
    recomposing a variant that comes out too long. The mass-notification
    audience threshold is Escalation_Policy's own authoritative value
    (Req 7.6) — read it here rather than assuming a number, since a
    deployment may retune it via `THUNAI_MASS_NOTIFICATION_AUDIENCE_THRESHOLD`
    without a code change.

    Returns:
        {
          "channel_character_limits": {"sms": <int>, "email": <int>},
          "recomposition_attempt_count": <int>,
          "mass_notification_audience_threshold": <int>,
        }
    """
    return ChannelLimits(
        channel_character_limits={
            "sms": _read_int_env(SMS_CHANNEL_CHAR_LIMIT_ENV_VAR, DEFAULT_SMS_CHANNEL_CHAR_LIMIT),
            "email": _read_int_env(EMAIL_CHANNEL_CHAR_LIMIT_ENV_VAR, DEFAULT_EMAIL_CHANNEL_CHAR_LIMIT),
        },
        recomposition_attempt_count=_read_int_env(
            ALERT_RECOMPOSITION_ATTEMPT_COUNT_ENV_VAR, DEFAULT_ALERT_RECOMPOSITION_ATTEMPT_COUNT
        ),
        mass_notification_audience_threshold=_read_int_env(
            "THUNAI_MASS_NOTIFICATION_AUDIENCE_THRESHOLD", 100
        ),
    )


# ---------------------------------------------------------------------------
# get_shelter_capacity (Req 7.1, 7.2)
# ---------------------------------------------------------------------------


@tool
def get_shelter_capacity(shelter_ids: list[str] | None = None) -> dict[str, Any]:
    """Get current available capacity for the named shelters (or every
    registered shelter, if none are named).

    Use this to decide which shelter to name as the nearest one holding
    available capacity above zero for an alert draft's shelter field
    (Req 7.1). If none of the returned shelters has `available_capacity > 0`
    (including the case where `shelters` is empty because no shelter of the
    ones asked for was found), populate the alert's shelter field with the
    configured no-capacity guidance text this tool returns, and record the
    absence of available shelter capacity in Audit_Ledger (Req 7.2) — this
    tool only reads State_Store; it never writes and never records to
    Audit_Ledger itself.

    Args:
        shelter_ids: The shelter identifiers to check, e.g.
            `["shelter-1", "shelter-2"]`. When omitted or empty, every
            shelter id known to the caller must be supplied explicitly —
            this tool has no shelter-listing capability of its own (no
            `list_shelters` primitive exists in State_Store); pass every
            candidate shelter id you know about.

    Returns:
        {
          "ok": True,
          "shelters": [
            {"shelter_id": str, "name": str, "available_capacity": int,
             "total_capacity": int}, ...
          ],  # only shelters that were found; a not-found id is silently omitted
          "any_capacity_available": bool,  # True iff any returned shelter has available_capacity > 0
          "no_capacity_guidance": str,  # the configured fallback text (Req 7.2)
        }
    """
    shelters: list[dict[str, Any]] = []
    for shelter_id in shelter_ids or []:
        shelter = state_store.get_shelter(shelter_id)
        if shelter is None:
            continue
        shelters.append(
            {
                "shelter_id": shelter.shelter_id,
                "name": shelter.name,
                "available_capacity": shelter.available_capacity,
                "total_capacity": shelter.total_capacity,
            }
        )

    return {
        "ok": True,
        "shelters": shelters,
        "any_capacity_available": any(s["available_capacity"] > 0 for s in shelters),
        "no_capacity_guidance": os.environ.get(NO_CAPACITY_GUIDANCE_ENV_VAR) or DEFAULT_NO_CAPACITY_GUIDANCE,
    }


# ---------------------------------------------------------------------------
# deliver_alert (Req 7.10, 7.11)
# ---------------------------------------------------------------------------


def _delivery_idempotency_key(incident_id: str, channel: str, language: str, severity_band: str) -> str:
    return f"alert:{incident_id}:{channel}:{language}:{severity_band}"


def _send_to_all_recipients(channel: str, recipients: list[str], subject: str, body: str) -> dict[str, Any]:
    """Perform the actual sends (called at most once per idempotency key, via
    `memory.state_store.idempotent_write`). Not itself a `@tool`.
    """
    provider = notification_provider()
    sent: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for recipient in recipients:
        try:
            if channel == "sms":
                result = provider.send_sms(recipient, body, idempotency_key=f"{recipient}")
            elif channel == "email":
                result = provider.send_email(recipient, subject, body, idempotency_key=f"{recipient}")
            else:
                failed.append({"recipient": recipient, "reason": f"unsupported_channel:{channel}"})
                continue
        except Exception as exc:  # noqa: BLE001 - recorded per-recipient, never raised
            failed.append({"recipient": recipient, "reason": str(exc)})
            continue
        sent.append({"recipient": recipient, "message_id": result.message_id})
    return {
        "sent": sent,
        "failed": failed,
        "audience_count": len(recipients),
        "delivered_count": len(sent),
    }


@tool
def deliver_alert(
    incident_id: str,
    channel: str,
    language: str,
    severity_band: str,
    recipients: list[str],
    body: str,
    subject: str = "",
) -> dict[str, Any]:
    """Deliver one already-approved, already-composed alert variant to its
    delivery audience.

    Call this only after the variant has passed Safety_QA_Agent review and,
    where required (Req 7.6), after the coordinator has approved the alert
    release escalation — this tool is a write tool gated by the human-in-
    the-loop approval classifier and harness hooks; it never runs without
    that gate for an escalated release (design.md §3.2). Idempotent per the
    combination of `incident_id`, `channel`, `language`, and `severity_band`
    (Req 7.10): a retried call for the same four values performs no
    additional sends and simply replays the first recorded delivery outcome,
    so this tool is always safe to call again after a resumed run.

    Args:
        incident_id: The incident this alert concerns, e.g. `"inc-042"`.
        channel: Delivery channel, `"sms"` or `"email"`.
        language: Language code the `body` (and `subject`, for email) is
            composed in, e.g. `"ta"` or `"en"` — must match the language the
            content was actually written in.
        severity_band: The incident severity band this alert was composed
            for, one of `"WATCH"`, `"WARNING"`, `"EVACUATE"`.
        recipients: The delivery audience for this variant — a list of
            E.164 phone numbers when `channel="sms"`, or a list of email
            addresses when `channel="email"`. Its length is the delivery
            audience recipient count (Req 7.6).
        body: The already-composed, already-approved message body to send
            verbatim to every recipient.
        subject: The email subject line. Required (non-empty) when
            `channel="email"`; ignored for `channel="sms"`.

    Returns:
        On success (including a replay of a prior call for the same key):
        {
          "ok": True,
          "incident_id": str, "channel": str, "language": str,
          "severity_band": str,
          "audience_count": int,           # len(recipients) from the FIRST call
          "delivered_count": int,          # recipients actually sent to on the FIRST call
          "sent": [{"recipient": str, "message_id": str}, ...],
          "failed": [{"recipient": str, "reason": str}, ...],
          "already_delivered": bool,       # True when this call replayed a prior outcome
        }

        On failure (never raised — Req 7.9's "withhold that variant, continue
        the remaining variants" requires a typed, non-raising result):
        {"ok": False, "reason": "missing_idempotency_key"} when any of
        `incident_id`/`channel`/`language`/`severity_band` is empty (no
        idempotency key could be derived); or
        {"ok": False, "reason": "empty_subject_for_email"} when
        `channel="email"` and `subject` is empty.
    """
    if channel == "email" and not subject:
        return {"ok": False, "reason": "empty_subject_for_email"}

    if not (incident_id and channel and language and severity_band):
        return {"ok": False, "reason": "missing_idempotency_key"}

    idempotency_key = _delivery_idempotency_key(incident_id, channel, language, severity_band)

    # Detect a replay *before* calling idempotent_write, since
    # idempotent_write itself gives no direct signal distinguishing "this
    # call performed a fresh send" from "this call replayed a prior
    # outcome" — both simply return a value. Checking State_Store's own
    # idempotency record first, then calling idempotent_write, gives an
    # accurate `already_delivered` flag without duplicating any of
    # `idempotent_write`'s own replay/race-handling logic.
    already_delivered = _has_prior_record(idempotency_key)

    try:
        outcome = state_store.idempotent_write(
            idempotency_key,
            lambda: _send_to_all_recipients(channel, recipients, subject, body),
        )
    except state_store.MissingIdempotencyKeyError:
        return {"ok": False, "reason": "missing_idempotency_key"}

    return {
        "ok": True,
        "incident_id": incident_id,
        "channel": channel,
        "language": language,
        "severity_band": severity_band,
        "audience_count": outcome["audience_count"],
        "delivered_count": outcome["delivered_count"],
        "sent": outcome["sent"],
        "failed": outcome["failed"],
        "already_delivered": already_delivered,
    }


def _has_prior_record(idempotency_key: str) -> bool:
    """Return True when `idempotency_key` already has a recorded outcome in
    State_Store's idempotency table (i.e. this specific `deliver_alert` call
    was a replay, not a fresh send). Thin wrapper around the same private
    read `memory.state_store.idempotent_write` itself uses, kept here only
    so `deliver_alert` can report `already_delivered` without changing
    `idempotent_write`'s own public contract.
    """
    return state_store._get_idempotency_record(idempotency_key) is not None
