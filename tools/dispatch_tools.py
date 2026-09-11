"""`Dispatch_Agent`'s `@tool` functions: candidate responder search and
atomic assignment (Req 6.1, 6.8; design.md §3.1 (Dispatch), §4.4).

Verification note (mandatory per tasks.md 12.4/12, "verify first"):
    Checked against the current Strands Agents docs (`strands-agents`
    installed at `1.55.0` per `pyproject.toml`;
    `user-guide/concepts/tools/custom-tools.mdx`, "Python Basic Tool
    Creation" / "Custom Tool Return Type in Python" / "Python Tool Result
    Handling with @tool decorator"):

    - The `@tool` decorator (imported as `from strands import tool`, the
      same import design.md's own `dispatch_agent.py` pseudocode and every
      already-implemented `@tool` example in this repo uses) infers the
      tool's name, description, and input schema from the decorated
      function's **signature and docstring** — the first docstring
      paragraph becomes the tool description, and an `Args:` section
      supplies each parameter's description. No decorator arguments are
      required, matching every existing tool-shaped convention already
      established in this codebase (`harness/hooks.py`'s `WRITE_TOOLS`
      allowlist names `find_candidate_responders`/`assign_responder` as
      exactly these two flat function names, with no class/namespace
      wrapper).
    - A tool may return a plain dict; the docs confirm three accepted
      return shapes: (1) a simple value, auto-wrapped as `{"text":
      str(result)}`; (2) a dict already in the `ToolResult` shape
      (`{"status": "success"|"error", "content": [...]}`); (3) letting an
      uncaught exception convert to an automatic error response. This
      module follows the **typed-plain-dict** convention design.md's own
      tool docstrings and this repo's error-handling philosophy
      (`memory/state_store.py`'s "never raise for expected failures"
      pattern used throughout the harness/policy layers) both point to:
      every return value here is a plain, JSON-serialisable dict carrying
      an explicit `ok: bool` field and a typed failure `reason` on the
      `ok=False` path, deliberately **not** raising for the expected,
      recoverable business outcome "responder no longer available"
      (Req 6.10 requires the caller — `Dispatch_Agent` — to fall back to
      the next ranked alternative, which is far easier to do against a
      returned `{"ok": False, "reason": "responder_no_longer_available"}`
      than against a caught exception threaded back through the model's
      tool-call loop). `assign_responder` therefore catches
      `state_store.ResponderNoLongerAvailableError` and
      `state_store.MissingIdempotencyKeyError` itself and converts each to
      a typed `ok=False` result rather than letting either propagate as an
      uncaught exception (which the docs confirm the SDK would otherwise
      auto-wrap as a generic `{"status": "error", ...}` — a shape that
      loses the specific `reason` discriminator `Dispatch_Agent`'s prompt
      needs to decide whether to retry with an alternative).
    - Every docstring below follows the "one verb per tool" / "return
      structured data, not prose" / "every argument with its units and
      format" rules already modelled by every other verified tool
      docstring convention referenced in this repo's own steering
      guidance, so `Dispatch_Agent`'s model has an unambiguous contract for
      when to call each tool and how to read what comes back.

Configuration judgment call (design docs do not name env-var identifiers for
these two dispatch-search parameters — Req 6.1 only says "the configured
dispatch search radius in kilometres" and "the configured maximum candidate
count" without naming the override variable): this module follows the exact
pattern already used by `policy/escalation_policy.py` for every other
env-overridable numeric threshold in this codebase (`_read_float_env`/
`_read_int_env` helpers, a `Final` module constant, a documented in-code
default, and a `.env.example`-style override name), introducing
`THUNAI_DISPATCH_SEARCH_RADIUS_KM` (default `5.0`, matching design.md's
14-responder/one-ward scale and `idea.md`'s example fixture distances of
1-5 km) and `THUNAI_DISPATCH_MAX_CANDIDATE_COUNT` (default `5`, matching
`DispatchDecision.alternative_responder_ids`'s own `max_length=3` — leaving
headroom above the 1 selected + 3 alternatives a decision can name). These
are read via the same `os.environ.get(...)` pattern the rest of this
codebase uses for env-driven configuration, not hardcoded, so a deployer can
retune them the same way every other threshold in `.env.example` is retuned,
without a code change.
"""

from __future__ import annotations

import math
import os
from typing import Any, Final

from strands import tool

from memory import state_store

__all__ = [
    "DISPATCH_SEARCH_RADIUS_KM_ENV_VAR",
    "DISPATCH_MAX_CANDIDATE_COUNT_ENV_VAR",
    "DEFAULT_DISPATCH_SEARCH_RADIUS_KM",
    "DEFAULT_DISPATCH_MAX_CANDIDATE_COUNT",
    "haversine_km",
    "find_candidate_responders",
    "assign_responder",
]

# ---------------------------------------------------------------------------
# Configuration (see module docstring's "Configuration judgment call")
# ---------------------------------------------------------------------------

DISPATCH_SEARCH_RADIUS_KM_ENV_VAR: Final[str] = "THUNAI_DISPATCH_SEARCH_RADIUS_KM"
DISPATCH_MAX_CANDIDATE_COUNT_ENV_VAR: Final[str] = "THUNAI_DISPATCH_MAX_CANDIDATE_COUNT"

DEFAULT_DISPATCH_SEARCH_RADIUS_KM: Final[float] = 5.0
"""Kilometres; default matches idea.md's example candidate distances (1-5 km)
for the single monitored Ward-7 river reach (Req 6.1)."""

DEFAULT_DISPATCH_MAX_CANDIDATE_COUNT: Final[int] = 5
"""Default cap on the number of candidates returned by
`find_candidate_responders`, per Req 6.1's "up to the configured maximum
candidate count." """

_EARTH_RADIUS_KM: Final[float] = 6371.0088
"""Mean Earth radius in kilometres (IUGG value), used by `haversine_km`."""


def _dispatch_search_radius_km() -> float:
    raw = os.environ.get(DISPATCH_SEARCH_RADIUS_KM_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_DISPATCH_SEARCH_RADIUS_KM
    return float(raw)


def _dispatch_max_candidate_count() -> int:
    raw = os.environ.get(DISPATCH_MAX_CANDIDATE_COUNT_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_DISPATCH_MAX_CANDIDATE_COUNT
    return int(raw)


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------


def haversine_km(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    """Great-circle distance between two `(latitude, longitude)` points in
    decimal degrees, in kilometres, via the haversine formula.

    Args:
        origin: `(latitude, longitude)` in decimal degrees.
        destination: `(latitude, longitude)` in decimal degrees.

    Returns:
        The distance between `origin` and `destination` in kilometres
        (always `>= 0.0`; `0.0` when the two points are identical).
    """
    lat1, lon1 = origin
    lat2, lon2 = destination
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return _EARTH_RADIUS_KM * c


# ---------------------------------------------------------------------------
# find_candidate_responders (Req 6.1)
# ---------------------------------------------------------------------------


@tool
def find_candidate_responders(latitude: float, longitude: float) -> dict[str, Any]:
    """Find candidate responders near an incident/request location.

    Retrieves every responder currently AVAILABLE, computes each one's
    great-circle distance in kilometres from `(latitude, longitude)` via the
    haversine formula, keeps only those within the configured dispatch
    search radius, sorts the result by ascending distance, and truncates it
    to the configured maximum candidate count (Req 6.1).

    Use this before `assign_responder` to decide which responder to select
    and to name ranked alternatives. This tool only reads State_Store; it
    never writes and never needs human approval.

    Args:
        latitude: Latitude of the incident/request location, in decimal
            degrees (e.g. 11.2342).
        longitude: Longitude of the incident/request location, in decimal
            degrees (e.g. 79.8412).

    Returns:
        {
            "ok": True,
            "search_radius_km": float,   # the radius actually applied
            "candidates": [
                {
                    "responder_id": str,
                    "name": str,
                    "distance_km": float,               # rounded to 2 dp
                    "availability_status": "AVAILABLE",  # by construction
                    "equipment": list[str],              # e.g. ["boat", "4x4"]
                    "active_assignment_count": int,
                }, ...
            ],  # ascending distance_km, truncated to the configured max count
        }
        `candidates` is `[]` (with `ok` still `True`) when no AVAILABLE
        responder falls within the search radius — Req 6.6's no-capacity
        escalation decision is composed by the caller (`Dispatch_Agent`)
        from this empty result plus `search_radius_km`, never asserted by
        this tool itself.
    """
    radius_km = _dispatch_search_radius_km()
    max_candidates = _dispatch_max_candidate_count()
    origin = (latitude, longitude)

    scored: list[tuple[float, Any]] = []
    for responder in state_store.query_available_responders():
        distance_km = haversine_km(origin, responder.home_coords)
        if distance_km <= radius_km:
            scored.append((distance_km, responder))

    scored.sort(key=lambda pair: pair[0])
    scored = scored[:max_candidates]

    return {
        "ok": True,
        "search_radius_km": radius_km,
        "candidates": [
            {
                "responder_id": responder.responder_id,
                "name": responder.name,
                "distance_km": round(distance_km, 2),
                "availability_status": responder.availability_status,
                "equipment": list(responder.equipment),
                "active_assignment_count": responder.active_assignment_count,
            }
            for distance_km, responder in scored
        ],
    }


# ---------------------------------------------------------------------------
# assign_responder (Req 6.4, 6.8, 6.10)
# ---------------------------------------------------------------------------


@tool
def assign_responder(responder_id: str, request_id: str) -> dict[str, Any]:
    """Assign a selected responder to a request.

    Atomically transitions the responder's availability status from
    AVAILABLE to ASSIGNED in the same state write as the assignment via
    `memory.state_store.assign_responder_atomic` (Req 6.4: no
    double-assignment), using an Idempotency_Key derived from `request_id`
    (`f"assign:{request_id}"`, Req 6.3) so a retried call for the same
    request never creates a second assignment.

    Call this only after `find_candidate_responders` and only for a
    responder whose `availability_status` it reported as `"AVAILABLE"` —
    this tool is a write tool and is gated by the human-in-the-loop
    approval classifier and harness hooks; it never runs without that gate
    (design.md §3.2).

    Args:
        responder_id: The responder to assign, e.g. `"resp-014"`.
        request_id: The request being assigned to that responder, e.g.
            `"req-2031"`. Used verbatim to derive the idempotency key.

    Returns:
        On success: {"ok": True, "responder_id": str, "request_id": str,
        "availability_status": "ASSIGNED", "active_assignment_count": int}.

        On failure (Req 6.10 — never raised, so the caller can immediately
        fall back to the next ranked alternative candidate):
        {"ok": False, "responder_id": str, "request_id": str,
        "reason": "responder_no_longer_available"} when the responder's
        `availability_status` was not `AVAILABLE` at write time; or
        {"ok": False, "reason": "missing_idempotency_key"} if `request_id`
        is empty (no idempotency key could be derived).
    """
    idempotency_key = f"assign:{request_id}" if request_id else ""

    try:
        updated = state_store.assign_responder_atomic(
            responder_id, request_id, idempotency_key=idempotency_key
        )
    except state_store.MissingIdempotencyKeyError:
        return {
            "ok": False,
            "responder_id": responder_id,
            "request_id": request_id,
            "reason": "missing_idempotency_key",
        }
    except state_store.ResponderNoLongerAvailableError:
        return {
            "ok": False,
            "responder_id": responder_id,
            "request_id": request_id,
            "reason": "responder_no_longer_available",
        }

    return {
        "ok": True,
        "responder_id": responder_id,
        "request_id": request_id,
        "availability_status": updated["availability_status"],
        "active_assignment_count": updated["active_assignment_count"],
    }
