"""`Monitor_Agent`'s incident-tracking `@tool` functions: idempotent
incident create/update (higher-band wins) and a prior-sweep-readings read,
over the `State_Store` seam (Req 3.1, 3.3, 3.4, 3.7; design.md §3.2, §4.4).

Verification note (mandatory per tasks.md 12.2/12, "verify first"):
    Checked against the current Strands Agents docs (`strands-agents`
    installed at `1.55.0` per `pyproject.toml`/`requirements.txt`;
    `user-guide/concepts/tools/custom-tools.mdx`'s "Python Basic Tool
    Creation" / "Custom Tool Return Type in Python" / "Python Tool Result
    Handling with @tool decorator" sections, cross-checked via the
    Context7-served mirror of that same docs site since no live Strands
    docs MCP server is configured in this environment):

    - The `@tool` decorator infers a tool's name, description, and input
      schema from the decorated function's **signature and docstring**: the
      first docstring paragraph becomes the tool description and an
      `Args:` section supplies each parameter's description — no decorator
      arguments are required. Every tool below follows that contract,
      matching the convention already established by
      `tools/sensor_tools.py` (task 12.1) and `tools/dispatch_tools.py`
      (task 12.4) in this same codebase.
    - Three accepted return shapes are documented: (1) a simple value,
      auto-wrapped as `{"text": str(result)}`; (2) a dict already in the
      `ToolResult` shape (`{"status": ..., "content": [...]}`); (3) an
      uncaught exception, auto-converted to a generic error response. This
      module follows the same **typed-plain-dict** convention already used
      by `tools/sensor_tools.py`/`tools/dispatch_tools.py` and by
      `memory/state_store.py`'s "never raise for an expected, recoverable
      outcome" pattern: every tool below returns a plain, JSON-serialisable
      dict carrying an explicit `ok: bool` field, never raising for an
      expected business outcome (an invalid severity band, an absent prior
      sweep). This lets `Monitor_Agent`'s prompt react to a typed `reason`
      discriminator rather than a caught exception threaded back through
      the model's tool-call loop.
    - No new external API surface beyond what `tools/dispatch_tools.py`
      (task 12.4, already verified in this codebase) already confirmed for
      `@tool`; no further deviation from design.md is introduced by the
      decorator/docstring contract itself.

Idempotent create/update design (Req 3.3, 3.4; design.md §3.2's
`create_or_update_incident`, §4.4's key schema note "`incident_id` derived
from `river_reach_id` (Idempotency_Key basis, Req 3.3)"):

    `incident_id_for_reach(river_reach_id)` deterministically derives
    `incident_id` from `river_reach_id` alone (`f"incident-{river_reach_id}"`),
    matching design.md §4.4's key-schema note verbatim. This means "no open
    incident covers the same river reach" (Req 3.3's creation gate) reduces
    to one `get_incident(incident_id)` lookup rather than a table scan or a
    dedicated by-reach GSI query — `memory/state_store.py` (task 6.1) has no
    "incidents by river reach" query helper, and none is needed given this
    one-incident-id-per-reach derivation.

    - **Create path** (no OPEN incident yet for this reach, and the current
      severity band is not NORMAL): uses
      `memory.state_store.idempotent_write` with
      `idempotency_key = f"create_incident:{river_reach_id}:{severity_band}"`
      — this is Req 3.3's literal wording ("an Idempotency_Key derived from
      the river reach identifier and the severity band"). Two racing calls
      with the same reach and band create exactly one incident; the second
      call replays the first call's recorded outcome rather than creating a
      second incident.
    - **Update path** (an OPEN incident already exists for this reach):
      the applied severity band is `higher_severity_band(existing.severity_band,
      current_severity_band)` — the higher of the two, **never** the lower
      one, per this task's explicit instruction and Req 3.4's wording ("set
      the incident severity band to the higher of the stored severity band
      and the current severity band"). The update is itself wrapped in
      `idempotent_write` keyed by a content fingerprint of
      `(river_reach_id, applied_band, readings)`
      (`f"update_incident:{incident_id}:{fingerprint}"`), so that Req 3.4's
      "leave the incident unchanged where the current readings and severity
      band are identical to those of the last recorded update" holds
      exactly: a second call carrying the identical `(river_reach_id,
      severity_band, readings)` tuple replays the first call's outcome
      (including its `updated_at` timestamp) rather than performing a
      second, redundant write — this is Property 11's idempotent-update
      generator ("repeated identical `(river_reach_id, readings,
      severity_band)` tuples applied 2-5 times").

    Req 3.10 (severity NORMAL, open incident exists: update readings, keep
    the incident open, record the downgrade in Audit_Ledger, without
    closing the incident) is satisfied by the same update path above:
    calling this tool with `severity_band="NORMAL"` against an open,
    higher-banded incident computes
    `higher_severity_band(existing.severity_band, "NORMAL") ==
    existing.severity_band` (NORMAL is the lowest band in
    `policy.escalation_policy.SEVERITY_BANDS_ASCENDING`, so it never wins
    the max), so the *stored* severity band never decreases — matching this
    task's explicit "never lower it" instruction and the Rule_Engine's own
    documented monotonicity spirit (Req 2.8) applied to incidents. The
    "record the severity downgrade" half of Req 3.10 is `Monitor_Agent`'s
    own Audit_Ledger write (task 13.2, not part of this tool), describing
    the *current sweep's computed band* falling below the incident's
    *stored* band — an informational audit fact, not a mutation this tool
    performs. `readings`/`triggered_rule_ids`/`rule_set_version` are still
    refreshed to the current sweep's values on every update call regardless
    of which band wins, so the incident's "current readings" half of Req
    3.4/3.10 is always kept current.

Baseline / prior-sweep read (Req 3.1's "retrieve, from Memory_Store, the
readings of the most recently completed sweep for the same river reach"):

    `get_prior_sweep_readings` answers exactly that half of Req 3.1 by
    reading `schemas.entities.Incident.last_readings` /
    `.severity_band` / `.updated_at` off the same deterministic
    `incident_id_for_reach(river_reach_id)` item `create_or_update_incident`
    itself writes — no new storage schema is introduced, and this
    genuinely is "the most recently completed sweep for the same river
    reach" (whichever call last wrote `last_readings` onto that incident
    item). When no incident item exists yet for the reach (first-ever sweep,
    or every prior sweep was NORMAL with no open incident created), this
    tool reports `available: False` with
    `unavailable_reason="no_prior_sweep_recorded"` — the shape Req 3.9
    requires `Monitor_Agent` to react to ("Memory_Store holds no prior
    sweep readings... mark the rate of change... as unavailable... produce
    the assessment from the current readings alone... record the absence
    ... in Audit_Ledger").

    **Explicit dependency/deviation note on the OTHER half of Req 3.1/17.3**
    ("the stored 30-day baseline for each configured reading type"): design.md
    §"Memory ownership" states the 30-day rolling baseline + reading count is
    stored as a dedicated `BASELINE#{reach_id}#{reading_type}` item family in
    `thunai-state`, owned by `memory/memory_store.py` (task 19.1 — **not
    implemented as of this task**; `memory/state_store.py`, task 6.1, defines
    no `BASELINE#`-prefixed key, no ring-buffer write helper, and no
    `reading_count` accessor). Per this task's explicit instruction ("stub
    the interface clearly and note the dependency on 19.1" if that schema
    is not yet available), this module deliberately does **not** invent a
    competing `BASELINE#` read/write path against `memory/state_store.py` —
    doing so here would pre-empt task 19.1's own design of that exact key
    family and risk a schema collision once 19.1 lands. Instead:

    1. `tools/sensor_tools.py` (task 12.1, already implemented) already
       satisfies `Monitor_Agent`'s 30-day-baseline *anomaly-ratio* need
       today via each `get_river_level`/`get_rainfall_rate`/`get_dam_release`
       tool's `include_baseline=True` parameter, which computes the
       trailing-30-day average directly from `Sensor_Provider`'s own replay
       series (`SyntheticSensorProvider`, the one synthetic backend, per
       design.md's own seed-data note: "`seed/hazard_readings/`... so
       `Memory_Store`'s 30-day baseline requirement... is satisfiable
       without a live sensor"). `Monitor_Agent` (task 13.2) should call
       those tools with `include_baseline=True` for the anomaly-indicator
       half of `HazardAssessment` rather than expecting a second,
       not-yet-existing baseline tool from this module.
    2. This module's own `get_prior_sweep_readings` covers the *other* half
       of Req 3.1 (prior sweep readings, not the 30-day statistical
       baseline) using data already available in `memory/state_store.py`
       today, with no dependency on task 19.1.
    3. **When task 19.1 lands** and defines the `BASELINE#{reach_id}#
       {reading_type}` ring-buffer schema in `memory/memory_store.py`
       (or extends `memory/state_store.py` with it), a future task should
       add a dedicated `get_reading_baseline` tool in this module (or
       `memory_store`'s own tool surface) that reads that schema directly,
       and `Monitor_Agent`'s prompt should be updated to prefer it over
       `tools/sensor_tools.py`'s provider-replay-derived baseline if the two
       ever need to diverge (e.g. once `LiveSensorProvider` replaces the
       synthetic backend, at which point deriving "baseline" from a live
       provider's *own* replay history no longer makes sense, and a
       persisted `Memory_Store` ring buffer becomes the only correct
       source). This module intentionally leaves that extension point
       named and documented here rather than guessing at 19.1's schema.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Final

from strands import tool

from memory import state_store
from policy.escalation_policy import SEVERITY_BANDS_ASCENDING
from schemas.entities import Incident

__all__ = [
    "incident_id_for_reach",
    "higher_severity_band",
    "create_or_update_incident",
    "get_prior_sweep_readings",
]

_INCIDENT_ID_PREFIX: Final[str] = "incident-"


# ---------------------------------------------------------------------------
# Deterministic incident_id derivation (design.md §4.4 key-schema note)
# ---------------------------------------------------------------------------


def incident_id_for_reach(river_reach_id: str) -> str:
    """Deterministically derive the one `incident_id` used for `river_reach_id`.

    Matches design.md §4.4's key-schema note ("`incident_id` derived from
    `river_reach_id`"): every call for the same `river_reach_id` yields the
    same `incident_id`, so "no open incident covers the same river reach"
    (Req 3.3's creation gate) reduces to a single `get_incident` lookup.

    Args:
        river_reach_id: The river reach identifier, e.g. `"reach-7"`.

    Returns:
        The stable incident identifier for that reach, e.g. `"incident-reach-7"`.
    """
    return f"{_INCIDENT_ID_PREFIX}{river_reach_id}"


# ---------------------------------------------------------------------------
# Higher-band-wins comparison (Req 3.4; never lower the stored band)
# ---------------------------------------------------------------------------


def higher_severity_band(a: str, b: str) -> str:
    """Return whichever of `a`/`b` is the higher severity band.

    Ordering is `policy.escalation_policy.SEVERITY_BANDS_ASCENDING`
    (`"NORMAL" < "WATCH" < "WARNING" < "EVACUATE"`) — the single place this
    codebase declares that ordering, per that module's own docstring. This
    function never returns a band lower than either input, matching Req
    3.4's "the higher of the stored severity band and the current severity
    band."

    Args:
        a: One severity band, one of `SEVERITY_BANDS_ASCENDING`.
        b: The other severity band, one of `SEVERITY_BANDS_ASCENDING`.

    Returns:
        The higher of `a` and `b`.
    """
    return max(a, b, key=SEVERITY_BANDS_ASCENDING.index)


def _readings_fingerprint(river_reach_id: str, severity_band: str, readings: dict[str, Any]) -> str:
    """Deterministic content fingerprint of the update-path idempotency
    inputs, so `idempotent_write` replays the first outcome for repeated
    identical `(river_reach_id, severity_band, readings)` tuples (Property 11
    generator) without needing a second, redundant `State_Store` write.

    Mirrors `policy/rule_engine_rules.py::compute_rule_set_version`'s own
    canonical-JSON + `hashlib.sha256` content-hash pattern (that module's
    own docstring already verified `hashlib`'s current API), so this
    module introduces no new hashing approach into the codebase.
    """
    canonical = json.dumps(
        {"river_reach_id": river_reach_id, "severity_band": severity_band, "readings": readings},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# create_or_update_incident (Req 3.3, 3.4, 3.7, 3.10)
# ---------------------------------------------------------------------------


@tool
def create_or_update_incident(
    river_reach_id: str,
    severity_band: str,
    readings: dict[str, Any],
    triggered_rule_ids: list[str],
    rule_set_version: str,
    affected_areas: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update the tracked incident for a river reach.

    When no OPEN incident exists yet for `river_reach_id` and `severity_band`
    is not `"NORMAL"`, creates exactly one new incident (Req 3.3), using an
    Idempotency_Key derived from `river_reach_id` and `severity_band` so a
    retried call with the same reach and band never creates a second
    incident.

    When an OPEN incident already exists for `river_reach_id`, updates that
    incident with the current `readings`/`triggered_rule_ids`/
    `rule_set_version`, and sets its severity band to whichever of the
    stored band and `severity_band` is higher — the stored band is **never
    lowered** by this tool (Req 3.4). A repeated call carrying an identical
    `(river_reach_id, severity_band, readings)` tuple leaves the incident
    exactly as the prior call left it (Req 3.4's idempotent-update
    correctness property) rather than performing a second write.

    Call this once per hazard sweep per river reach, after `Rule_Engine` has
    computed the current `severity_band`. Do not call this when the current
    severity band is `"NORMAL"` and no OPEN incident exists — Req 3.5's
    "complete the run without creating an incident" case is the caller's
    (`Monitor_Agent`'s) responsibility to recognise before calling this
    tool; this tool tolerates that case gracefully (returns `created=False,
    updated=False`) rather than raising, but never escalates or logs a
    decision on the caller's behalf.

    Args:
        river_reach_id: The monitored river reach identifier, e.g.
            `"reach-7"`.
        severity_band: The current sweep's computed severity band, one of
            `"NORMAL"`, `"WATCH"`, `"WARNING"`, `"EVACUATE"` (Rule_Engine's
            output).
        readings: The current sweep's readings, keyed by reading type (e.g.
            `{"river_level": {"value": 2.4, "unit": "m", "ts": "..."},
            ...}`). Stored verbatim as the incident's `last_readings`.
        triggered_rule_ids: The Rule_Engine rule identifiers that justified
            `severity_band` this sweep.
        rule_set_version: The active `Rule_Set_Version` this sweep was
            evaluated against.
        affected_areas: Ward/area names affected by this incident. Defaults
            to an empty list on creation, or is left unchanged on an update
            when omitted (`None`).

    Returns:
        On creation: `{"ok": True, "created": True, "updated": False,
        "incident_id": str, "severity_band": str}`.

        On update: `{"ok": True, "created": False, "updated": True,
        "incident_id": str, "severity_band": str}` — `severity_band` is the
        *applied* (higher-of-the-two) band, which may equal the incident's
        prior stored band when `severity_band` did not exceed it.

        When no OPEN incident exists and `severity_band` is `"NORMAL"`:
        `{"ok": True, "created": False, "updated": False, "incident_id":
        str, "severity_band": "NORMAL", "reason":
        "no_open_incident_and_normal_band"}` — no write is performed.

        On an invalid `severity_band` (not one of the four declared bands):
        `{"ok": False, "reason": "invalid_severity_band", "severity_band":
        str}` — no write is performed.
    """
    if severity_band not in SEVERITY_BANDS_ASCENDING:
        return {"ok": False, "reason": "invalid_severity_band", "severity_band": severity_band}

    incident_id = incident_id_for_reach(river_reach_id)
    existing = state_store.get_incident(incident_id)
    existing_open = existing is not None and existing.status == "OPEN"

    if not existing_open:
        if severity_band == "NORMAL":
            # Req 3.5: no open incident, NORMAL band -- nothing to create.
            return {
                "ok": True,
                "created": False,
                "updated": False,
                "incident_id": incident_id,
                "severity_band": "NORMAL",
                "reason": "no_open_incident_and_normal_band",
            }

        # Req 3.3: create exactly one incident, keyed by reach + band.
        idempotency_key = f"create_incident:{river_reach_id}:{severity_band}"

        def _create() -> dict[str, Any]:
            now = datetime.now(timezone.utc).isoformat()
            incident = Incident(
                incident_id=incident_id,
                river_reach_id=river_reach_id,
                severity_band=severity_band,
                status="OPEN",
                affected_areas=list(affected_areas or []),
                created_at=now,
                updated_at=now,
                last_readings=dict(readings),
                triggered_rule_ids=list(triggered_rule_ids),
                rule_set_version=rule_set_version,
            )
            state_store.put_incident(incident)
            return {
                "ok": True,
                "created": True,
                "updated": False,
                "incident_id": incident.incident_id,
                "severity_band": incident.severity_band,
            }

        return state_store.idempotent_write(idempotency_key, _create)

    # An OPEN incident already exists for this reach -- update path
    # (Req 3.4, 3.10). Never lower the stored severity band.
    applied_band = higher_severity_band(existing.severity_band, severity_band)
    fingerprint = _readings_fingerprint(river_reach_id, applied_band, readings)
    idempotency_key = f"update_incident:{incident_id}:{fingerprint}"

    def _update() -> dict[str, Any]:
        updated = state_store.update_incident(
            incident_id,
            severity_band=applied_band,
            affected_areas=(list(affected_areas) if affected_areas is not None else existing.affected_areas),
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_readings=dict(readings),
            triggered_rule_ids=list(triggered_rule_ids),
            rule_set_version=rule_set_version,
        )
        return {
            "ok": True,
            "created": False,
            "updated": True,
            "incident_id": updated.incident_id,
            "severity_band": updated.severity_band,
        }

    return state_store.idempotent_write(idempotency_key, _update)


# ---------------------------------------------------------------------------
# get_prior_sweep_readings (Req 3.1, 3.9)
# ---------------------------------------------------------------------------


@tool
def get_prior_sweep_readings(river_reach_id: str) -> dict[str, Any]:
    """Retrieve the readings and severity band recorded during the most
    recently completed sweep for a river reach.

    Reads the readings that `create_or_update_incident` most recently
    persisted onto that reach's tracked incident (`last_readings`,
    `severity_band`, `updated_at`) — the "most recently completed sweep for
    the same river reach" Req 3.1 requires `Monitor_Agent` to retrieve
    before computing a fresh hazard assessment's rate-of-change. See this
    module's docstring for why the *30-day statistical baseline* half of
    Req 3.1/17.3 is answered by `tools/sensor_tools.py`'s
    `include_baseline` parameter instead, and for the explicit dependency
    this module records on `memory/memory_store.py` (task 19.1, not yet
    implemented) for a dedicated `BASELINE#` ring-buffer schema.

    Use this once per hazard sweep per river reach, before calling
    `create_or_update_incident`, so `Monitor_Agent` can compute each
    reading type's rate of change against the previous sweep's values. This
    tool only reads State_Store; it never writes and never needs human
    approval.

    Args:
        river_reach_id: The monitored river reach identifier, e.g.
            `"reach-7"`.

    Returns:
        When a prior sweep was recorded (an incident item exists for this
        reach, open or closed):
        `{"ok": True, "available": True, "river_reach_id": str,
        "readings": dict, "severity_band": str, "updated_at": str,
        "unavailable_reason": None}`.

        When no prior sweep has ever been recorded for this reach (Req
        3.9): `{"ok": True, "available": False, "river_reach_id": str,
        "readings": {}, "severity_band": None, "updated_at": None,
        "unavailable_reason": "no_prior_sweep_recorded"}` — `Monitor_Agent`
        should mark the rate-of-change for every reading type as
        unavailable and record the absence in Audit_Ledger per Req 3.9,
        rather than treating this as an error.
    """
    incident_id = incident_id_for_reach(river_reach_id)
    incident = state_store.get_incident(incident_id)

    if incident is None:
        return {
            "ok": True,
            "available": False,
            "river_reach_id": river_reach_id,
            "readings": {},
            "severity_band": None,
            "updated_at": None,
            "unavailable_reason": "no_prior_sweep_recorded",
        }

    return {
        "ok": True,
        "available": True,
        "river_reach_id": river_reach_id,
        "readings": dict(incident.last_readings),
        "severity_band": incident.severity_band,
        "updated_at": incident.updated_at,
        "unavailable_reason": None,
    }
