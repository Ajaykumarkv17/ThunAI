"""``Monitor_Agent``'s sensor-reading tools, over the ``Sensor_Provider`` seam.

Implements ``get_river_level`` / ``get_rainfall_rate`` / ``get_dam_release``
as Strands ``@tool``-decorated functions (design.md §2.1, §3.7; Req 3.1).
Each tool wraps :func:`integrations.get_sensor_provider` (the resolver built
in task 10.4, which itself defers to
``integrations.sensor_provider.get_sensor_provider``, the ONLY place
``SYNTHETIC_SENSORS`` is read — task 10.1) so this module never itself
chooses between :class:`~integrations.sensor_provider.SyntheticSensorProvider`
and :class:`~integrations.sensor_provider.LiveSensorProvider`; whichever one
``get_sensor_provider()`` currently returns is used transparently.

Verification note (mandatory per tasks.md 12.1, "verify first"):
    Consulted the current Strands Agents docs (via the Ref documentation
    search tool, since no live Strands docs MCP server is configured in
    this environment) for the ``@tool`` decorator + docstring contract,
    specifically ``docs/user-guide/concepts/tools/python-tools.md``
    ("Python Tool Decorators") cross-checked against
    ``docs/user-guide/concepts/tools/index.md`` ("Custom Tools"):

    1. **Docstring -> tool spec.** "The decorator extracts information from
       your function's docstring to create the tool specification. The
       first paragraph becomes the tool's description, and the 'Args'
       section provides parameter descriptions. These are combined with
       the function's type hints to create a complete tool specification."
       Every tool below therefore carries a first paragraph stating what it
       does, a "when to use" note, an ``Args:`` section giving each
       parameter's unit/format, and a ``Returns:`` section describing the
       structured-dict shape — matching this contract exactly, and matching
       the shape design.md's own §3.1 sketch tools use
       (``get_current_readings``/``get_open_incidents`` docstrings there
       follow the identical one-line-purpose + "Do NOT use to..." pattern).
    2. **Return-value handling.** "By default, your function's return value
       is automatically formatted as a text response. However, if you need
       more control over the response format, you can return a dictionary
       with a specific structure" (``{"status": ..., "content": [...]}``).
       This module's tools intentionally do **not** use that special
       ``status``/``content`` envelope — they return a plain domain dict
       (``{"reading_type": ..., "available": ..., "value": ..., ...}``),
       which the decorator auto-formats as a text response for the model,
       exactly like any other non-``status``/``content`` return value. This
       is the right choice here because these are read tools whose plain
       dict shape is itself the intended "structured data, not prose"
       contract (task instructions; also this design's tools-layer
       convention throughout ``harness/``/``memory/``/``policy/`` of
       returning plain dicts/dataclasses rather than the special envelope).
    3. **No exception on an expected failure.** The docs' "Dictionary
       Return Type" example shows a tool catching its own exception and
       returning ``{"status": "error", ...}`` rather than letting the
       exception propagate — confirming the general Strands convention (and
       this task's explicit instruction) that an *expected* failure (here:
       "no reading available for this reading type/time") should be a
       typed, non-raising return, never a raised exception. Every tool
       below follows that: a total ``Sensor_Provider`` outage or an absent
       reading is reported via ``available: False`` +
       ``unavailable_reason``, never a raised exception reaching the agent
       loop. (Req 3.1 mandates the *retry* behaviour — at most 3 attempts,
       at most 10s per attempt, per source — before that unavailable
       verdict is returned; the retry/timeout wrapper below implements that
       obligation directly rather than delegating it to
       :class:`~integrations.sensor_provider.SensorProvider`, whose two
       backends have no retry logic of their own, per their own module's
       docstrings.)

    Installed-package cross-check: ``strands-agents==1.55.0`` (per
    ``requirements.txt``/``pyproject.toml``'s pin; `agents/config.py` and
    `harness/hooks.py` record the same installed version in their own
    verification notes) — nothing in the ``@tool`` decorator/docstring
    contract above is version-sensitive between 1.54.x and 1.55.0 per the
    docs consulted, so no deviation from design.md's assumed ``@tool``
    surface is required here.

Reading-type <-> unit mapping (Req 3.1, matching
``seed/hazard_readings/README.md``'s "Field" table and
``policy/rule_engine_rules.py::THRESHOLDS``): ``river_level`` in metres
(``"m"``), ``rainfall_rate`` in millimetres per hour (``"mm/h"``),
``dam_release`` in cubic metres per second (``"m3/s"``). Every tool's own
``get_latest_reading``/``get_baseline_series`` result already carries its
``unit`` string verbatim from the fixture/live source (per
``integrations/sensor_provider.py``'s documented reading shape), so this
module does not hardcode the unit into its return value — it passes through
whatever the provider reports, and callers should treat the returned
``unit`` field as authoritative over this docstring's stated mapping should
the two ever disagree.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime
from typing import Literal, TypedDict

from strands import tool

from integrations import get_sensor_provider

# ---------------------------------------------------------------------------
# Retry/timeout wrapper (Req 3.1: "allowing at most 3 retrieval attempts per
# source and at most 10 seconds per attempt"). Applied uniformly to every
# read below, regardless of which SensorProvider backend is in effect.
# ---------------------------------------------------------------------------

_MAX_RETRIEVAL_ATTEMPTS = 3
_PER_ATTEMPT_TIMEOUT_SECONDS = 10.0


class _RetriesExhausted(Exception):
    """Raised internally by :func:`_read_with_retry` once every attempt has
    failed (errored or timed out) — distinct from the provider *successfully*
    returning `None`/`[]` (a legitimate "no reading in range" outcome). Never
    propagated past this module's `@tool` functions; each `@tool` catches it
    and reports the typed `"provider_unavailable_after_retries"` outcome.
    """


def _read_with_retry(read_once):
    """Call `read_once()` up to `_MAX_RETRIEVAL_ATTEMPTS` times, each bounded
    to `_PER_ATTEMPT_TIMEOUT_SECONDS` (Req 3.1), returning the first
    successful result (which may legitimately be `None`/`[]`, per
    `SensorProvider`'s documented "no reading in range" contract).

    Raises:
        _RetriesExhausted: once every attempt has errored or timed out —
            callers catch this and report a typed unavailable outcome
            rather than letting it reach the agent loop.
    """
    last_error: BaseException | None = None
    for _attempt in range(1, _MAX_RETRIEVAL_ATTEMPTS + 1):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(read_once)
            try:
                return future.result(timeout=_PER_ATTEMPT_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError as exc:
                last_error = exc
            except Exception as exc:  # noqa: BLE001 - any provider failure is transient-retriable here
                last_error = exc
    raise _RetriesExhausted("retrieval unavailable after all attempts") from last_error


class BaselineSummary(TypedDict):
    """Summary of a reading type's 30-day baseline series, per
    ``integrations.sensor_provider.SensorProvider.get_baseline_series``.
    """

    available: bool
    average: float | None
    reading_count: int
    window_days: int
    unavailable_reason: Literal["no_baseline_readings"] | None


class SensorReading(TypedDict):
    """The structured-dict return shape common to every tool in this module
    (Req 3.1, 3.8, 3.9). `unavailable_reason` is `None` exactly when
    `available` is `True`.
    """

    reading_type: str
    as_of: str
    available: bool
    value: float | None
    unit: str | None
    source_timestamp: str | None
    unavailable_reason: (
        Literal["no_reading_at_or_before_as_of", "provider_unavailable_after_retries"] | None
    )
    baseline: BaselineSummary | None


def _parse_as_of(as_of: str) -> datetime:
    return datetime.fromisoformat(as_of)


def _read_sensor(reading_type: str, as_of: str, include_baseline: bool, baseline_days: int) -> SensorReading:
    """Shared implementation behind every tool below. Not itself a `@tool` —
    each public tool function keeps its own name-specific docstring (the
    actual tool spec the model sees) and delegates here to avoid duplicating
    the retry/shape logic three times.
    """
    as_of_dt = _parse_as_of(as_of)
    provider = get_sensor_provider()

    provider_unavailable = False
    try:
        latest = _read_with_retry(lambda: provider.get_latest_reading(reading_type, as_of_dt))
    except _RetriesExhausted:
        latest = None
        provider_unavailable = True

    baseline: BaselineSummary | None = None
    if include_baseline:
        try:
            rows = _read_with_retry(lambda: provider.get_baseline_series(reading_type, as_of_dt, days=baseline_days))
        except _RetriesExhausted:
            rows = []
        if rows:
            average = sum(row["value"] for row in rows) / len(rows)
            baseline = BaselineSummary(
                available=True,
                average=average,
                reading_count=len(rows),
                window_days=baseline_days,
                unavailable_reason=None,
            )
        else:
            baseline = BaselineSummary(
                available=False,
                average=None,
                reading_count=0,
                window_days=baseline_days,
                unavailable_reason="no_baseline_readings",
            )

    if latest is None:
        return SensorReading(
            reading_type=reading_type,
            as_of=as_of,
            available=False,
            value=None,
            unit=None,
            source_timestamp=None,
            unavailable_reason=(
                "provider_unavailable_after_retries" if provider_unavailable else "no_reading_at_or_before_as_of"
            ),
            baseline=baseline,
        )

    return SensorReading(
        reading_type=reading_type,
        as_of=as_of,
        available=True,
        value=float(latest["value"]),
        unit=str(latest["unit"]),
        source_timestamp=latest["timestamp"].isoformat(),
        unavailable_reason=None,
        baseline=baseline,
    )


@tool
def get_river_level(as_of: str, include_baseline: bool = False, baseline_days: int = 30) -> SensorReading:
    """Get the latest river-level reading for the monitored river reach, in metres.

    Use this to check the current river level for a hazard sweep or an
    ad-hoc "what's the river level" / "is it rising" question. Retries the
    underlying Sensor_Provider up to 3 times, waiting at most 10 seconds per
    attempt (Req 3.1); does not raise when no reading is available — check
    the returned `available` field instead of assuming success.

    Args:
        as_of: The point in time to read as of, as an ISO-8601 timestamp
            with a timezone offset (e.g. "2026-09-04T23:00:00+05:30"). The
            latest reading at or before this timestamp is returned.
        include_baseline: When True, also compute and return the 30-day
            baseline average (mean river level over the trailing
            `baseline_days` days ending at `as_of`), for anomaly-ratio
            calculations (Req 3.2). Defaults to False.
        baseline_days: Length of the baseline window in days, ending at
            `as_of`. Only used when `include_baseline` is True. Defaults to
            30 (Req 3.1's "stored 30-day baseline").

    Returns:
        A dict shaped like:
        {
          "reading_type": "river_level",
          "as_of": "<the as_of value passed in>",
          "available": true | false,
          "value": <float, metres> | null,
          "unit": "m" | null,
          "source_timestamp": "<ISO-8601 timestamp of the returned reading>" | null,
          "unavailable_reason":
              "no_reading_at_or_before_as_of" | "provider_unavailable_after_retries" | null,
          "baseline": {
            "available": true | false,
            "average": <float, metres> | null,
            "reading_count": <int>,
            "window_days": <int>,
            "unavailable_reason": "no_baseline_readings" | null
          } | null
        }
        `available` is false and `value`/`unit`/`source_timestamp` are null
        whenever no reading exists at or before `as_of`, or the provider
        remained unavailable after every retry — this reflects an
        unavailable reading type, not an error (Req 3.6, 3.8).
    """
    return _read_sensor("river_level", as_of, include_baseline, baseline_days)


@tool
def get_rainfall_rate(as_of: str, include_baseline: bool = False, baseline_days: int = 30) -> SensorReading:
    """Get the latest rainfall-rate reading for the monitored river reach, in millimetres per hour.

    Use this to check current rainfall intensity for a hazard sweep or an
    ad-hoc "how hard is it raining" question. Retries the underlying
    Sensor_Provider up to 3 times, waiting at most 10 seconds per attempt
    (Req 3.1); does not raise when no reading is available — check the
    returned `available` field instead of assuming success.

    Args:
        as_of: The point in time to read as of, as an ISO-8601 timestamp
            with a timezone offset (e.g. "2026-09-04T23:00:00+05:30"). The
            latest reading at or before this timestamp is returned.
        include_baseline: When True, also compute and return the 30-day
            baseline average (mean rainfall rate over the trailing
            `baseline_days` days ending at `as_of`), for anomaly-ratio
            calculations (Req 3.2). Defaults to False.
        baseline_days: Length of the baseline window in days, ending at
            `as_of`. Only used when `include_baseline` is True. Defaults to
            30 (Req 3.1's "stored 30-day baseline").

    Returns:
        A dict shaped like:
        {
          "reading_type": "rainfall_rate",
          "as_of": "<the as_of value passed in>",
          "available": true | false,
          "value": <float, mm/h> | null,
          "unit": "mm/h" | null,
          "source_timestamp": "<ISO-8601 timestamp of the returned reading>" | null,
          "unavailable_reason":
              "no_reading_at_or_before_as_of" | "provider_unavailable_after_retries" | null,
          "baseline": {
            "available": true | false,
            "average": <float, mm/h> | null,
            "reading_count": <int>,
            "window_days": <int>,
            "unavailable_reason": "no_baseline_readings" | null
          } | null
        }
        `available` is false and `value`/`unit`/`source_timestamp` are null
        whenever no reading exists at or before `as_of`, or the provider
        remained unavailable after every retry — this reflects an
        unavailable reading type, not an error (Req 3.6, 3.8).
    """
    return _read_sensor("rainfall_rate", as_of, include_baseline, baseline_days)


@tool
def get_dam_release(as_of: str, include_baseline: bool = False, baseline_days: int = 30) -> SensorReading:
    """Get the latest upstream dam-release reading for the monitored river reach, in cubic metres per second.

    Use this to check current upstream discharge for a hazard sweep or an
    ad-hoc "how much water is the dam releasing" question. Retries the
    underlying Sensor_Provider up to 3 times, waiting at most 10 seconds per
    attempt (Req 3.1); does not raise when no reading is available — check
    the returned `available` field instead of assuming success.

    Args:
        as_of: The point in time to read as of, as an ISO-8601 timestamp
            with a timezone offset (e.g. "2026-09-04T23:00:00+05:30"). The
            latest reading at or before this timestamp is returned.
        include_baseline: When True, also compute and return the 30-day
            baseline average (mean dam release over the trailing
            `baseline_days` days ending at `as_of`), for anomaly-ratio
            calculations (Req 3.2). Defaults to False.
        baseline_days: Length of the baseline window in days, ending at
            `as_of`. Only used when `include_baseline` is True. Defaults to
            30 (Req 3.1's "stored 30-day baseline").

    Returns:
        A dict shaped like:
        {
          "reading_type": "dam_release",
          "as_of": "<the as_of value passed in>",
          "available": true | false,
          "value": <float, m3/s> | null,
          "unit": "m3/s" | null,
          "source_timestamp": "<ISO-8601 timestamp of the returned reading>" | null,
          "unavailable_reason":
              "no_reading_at_or_before_as_of" | "provider_unavailable_after_retries" | null,
          "baseline": {
            "available": true | false,
            "average": <float, m3/s> | null,
            "reading_count": <int>,
            "window_days": <int>,
            "unavailable_reason": "no_baseline_readings" | null
          } | null
        }
        `available` is false and `value`/`unit`/`source_timestamp` are null
        whenever no reading exists at or before `as_of`, or the provider
        remained unavailable after every retry — this reflects an
        unavailable reading type, not an error (Req 3.6, 3.8).
    """
    return _read_sensor("dam_release", as_of, include_baseline, baseline_days)
