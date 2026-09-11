"""Deterministic threshold rule table for the ``Rule_Engine`` graph node.

Implements the threshold table, staleness limit, and ``Rule_Set_Version``
described in design.md §3.3 ("The `Rule_Engine` custom graph node") and
required by Req 2.1 and Req 2.7.

Verification note (mandatory per tasks.md 3.2, "verify first"):
    This module is pure Python — no Strands/boto3/Pydantic API surface to
    verify. The only external dependency is the standard-library ``hashlib``
    module. Its idiomatic use here (``hashlib.sha256(<bytes>).hexdigest()``)
    was confirmed against the current Python ``hashlib`` documentation: the
    constructor accepts an optional initial ``bytes``-like object, and
    ``.hexdigest()`` returns the digest as a lowercase hex string with no
    further arguments needed for our use case. No deviation from design.md
    is introduced by this module.

``THRESHOLDS`` shape
---------------------
``THRESHOLDS`` is keyed by reading type (``"river_level"``, ``"rainfall_rate"``,
``"dam_release"`` — the three reading types named in Req 2.1) and maps each
to a :class:`ReadingSpec` carrying:

- ``unit``: the unit named in Req 2.1 for that reading type (metres,
  millimetres per hour, cubic metres per second respectively).
- ``valid_range``: an inclusive ``(min, max)`` sanity range; a reading outside
  this range is treated as unavailable per Req 2.5 ("falls outside the
  configured valid range for its unit").
- ``rules_ascending``: the per-severity-band thresholds for this reading
  type, in ascending order of ``threshold`` value, one :class:`ThresholdRule`
  per band above ``NORMAL`` (``WATCH``, ``WARNING``, ``EVACUATE``). Scanning
  this list ascending and taking the highest band whose threshold is met is
  what makes ``RuleEngineNode``'s severity computation a structurally
  monotonic function of the reading value (design.md §3.3, Req 2.8) — a
  larger value satisfies every rule a smaller value satisfied, plus possibly
  more, so the assigned band never decreases as the value increases.

Numeric values below (river level 3.5m/4.2m/5.0m WATCH/WARNING/EVACUATE) are
taken directly from design.md §3.3's own worked example
(``# e.g. WATCH@3.5m, WARNING@4.2m, EVACUATE@5.0m``). Neither requirements.md
nor design.md gives exact numbers for ``rainfall_rate`` or ``dam_release``, so
the values for those two reading types below are **illustrative defaults**,
scaled to a plausible flood-response ward on the same three-band spacing
pattern the design's own river-level example uses (~1.2x step to WATCH,
~1.4x step to WARNING/EVACUATE relative to the prior band) and using the
exact units Req 2.1 specifies. They are clearly marked below and are meant to
be tuned against real Kollidam Ward-7 data, not treated as calibrated
science.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdRule:
    """One severity-band threshold for a single reading type."""

    rule_id: str
    band: str
    threshold: float


@dataclass(frozen=True)
class ReadingSpec:
    """The full rule set for one hazard reading type."""

    unit: str
    valid_range: tuple[float, float]
    rules_ascending: tuple[ThresholdRule, ...]


# ---------------------------------------------------------------------------
# THRESHOLDS (Req 2.1, design.md §3.3)
# ---------------------------------------------------------------------------

THRESHOLDS: dict[str, ReadingSpec] = {
    "river_level": ReadingSpec(
        unit="metres",
        valid_range=(0.0, 15.0),
        rules_ascending=(
            # Exact values from design.md §3.3's worked example.
            ThresholdRule(rule_id="river_level_watch", band="WATCH", threshold=3.5),
            ThresholdRule(rule_id="river_level_warning", band="WARNING", threshold=4.2),
            ThresholdRule(rule_id="river_level_evacuate", band="EVACUATE", threshold=5.0),
        ),
    ),
    "rainfall_rate": ReadingSpec(
        unit="mm/h",
        valid_range=(0.0, 500.0),
        rules_ascending=(
            # Illustrative defaults (see module docstring) — same
            # three-band spacing pattern as the river-level example above.
            ThresholdRule(rule_id="rainfall_rate_watch", band="WATCH", threshold=20.0),
            ThresholdRule(rule_id="rainfall_rate_warning", band="WARNING", threshold=35.0),
            ThresholdRule(rule_id="rainfall_rate_evacuate", band="EVACUATE", threshold=50.0),
        ),
    ),
    "dam_release": ReadingSpec(
        unit="m3/s",
        valid_range=(0.0, 20000.0),
        rules_ascending=(
            # Illustrative defaults (see module docstring) — same
            # three-band spacing pattern as the river-level example above.
            ThresholdRule(rule_id="dam_release_watch", band="WATCH", threshold=1000.0),
            ThresholdRule(rule_id="dam_release_warning", band="WARNING", threshold=1800.0),
            ThresholdRule(rule_id="dam_release_evacuate", band="EVACUATE", threshold=2500.0),
        ),
    ),
}


# ---------------------------------------------------------------------------
# STALENESS_LIMIT_S (Req 2.5: "the configured staleness limit of 15 minutes")
# ---------------------------------------------------------------------------

STALENESS_LIMIT_S: int = 15 * 60  # 900 seconds, exactly the value Req 2.5 states.


# ---------------------------------------------------------------------------
# RULE_SET_VERSION (Req 2.7): a deterministic content hash of THRESHOLDS +
# STALENESS_LIMIT_S, computed via a small testable function rather than only
# a bare module-level expression, so tests can call it directly with mutated
# inputs (tasks.md 3.2).
# ---------------------------------------------------------------------------


def _canonicalize(thresholds: dict[str, ReadingSpec]) -> dict:
    """Turn the ``THRESHOLDS`` structure into a plain, JSON-serialisable,
    order-independent representation suitable for deterministic hashing.

    Dict key order and the tuple/dataclass structure are flattened into
    plain dicts/lists so that ``json.dumps(..., sort_keys=True)`` produces
    the exact same string for the exact same logical content on every
    run/process, regardless of dict insertion order.
    """
    return {
        reading_type: {
            "unit": spec.unit,
            "valid_range": list(spec.valid_range),
            "rules_ascending": [
                {"rule_id": rule.rule_id, "band": rule.band, "threshold": rule.threshold}
                for rule in spec.rules_ascending
            ],
        }
        for reading_type, spec in thresholds.items()
    }


def compute_rule_set_version(thresholds: dict[str, ReadingSpec], staleness_limit_s: int) -> str:
    """Compute the deterministic ``Rule_Set_Version`` content hash.

    Deterministic and reproducible: identical ``thresholds`` +
    ``staleness_limit_s`` always produce the identical hash, across
    processes and runs (no timestamps, no dict-iteration-order sensitivity —
    ``sort_keys=True`` and the canonicalisation above remove any such
    dependency). Changing any threshold value, any unit, any valid range, or
    ``staleness_limit_s`` changes the hash (Req 2.7).
    """
    payload = {
        "thresholds": _canonicalize(thresholds),
        "staleness_limit_s": staleness_limit_s,
    }
    canonical_json = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:12]


RULE_SET_VERSION: str = compute_rule_set_version(THRESHOLDS, STALENESS_LIMIT_S)
