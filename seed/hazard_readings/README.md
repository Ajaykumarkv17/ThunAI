# `seed/hazard_readings/` — synthetic hazard-sensor fixture data

This directory holds the **only synthetic-backend data** in ThunAI (Req 19.5).
It is replayed by `SyntheticSensorProvider` (task 10.1,
`integrations/sensor_provider.py`) when `SYNTHETIC_SENSORS=1` (the default —
see `.env.example`). Every other interface in ThunAI is live in every
environment; this is the one exception, because a real flood cannot be
summoned on demand for a demonstration (Req 3.1, 17.3, 19.5; design.md §3.9).

## File format

Three files, one per reading type, in [JSON Lines](https://jsonlines.org/)
format (one JSON object per line, no wrapping array — trivial to stream/replay
row-by-row without loading the whole file):

- `river_level.jsonl`
- `rainfall_rate.jsonl`
- `dam_release.jsonl`

Each line is a JSON object with this shape:

```json
{"reading_id": "rl-00000", "reading_type": "river_level", "timestamp": "2026-08-01T00:00:00+05:30", "value": 2.105, "unit": "m"}
```

| Field | Type | Notes |
|---|---|---|
| `reading_id` | string | Per-row identifier, unique within its file. Prefix `rl-`/`rf-`/`dr-` + zero-padded index. Useful for replay bookmarking/dedup. |
| `reading_type` | string | One of `"river_level"`, `"rainfall_rate"`, `"dam_release"`. Redundant with the filename, included so a consumer that concatenates files (or a future combined-file format) can still disambiguate. |
| `timestamp` | string | ISO-8601 with a fixed `+05:30` (India Standard Time) offset, matching the convention already used in `schemas/entities.py` test fixtures. |
| `value` | number | The reading value in the unit below. |
| `unit` | string | `"m"` for river level, `"mm/h"` for rainfall rate, `"m3/s"` for dam release — matches the units used in Req 2.1 / `schemas/decisions.py`. |

**Why three separate files rather than one combined file:** each reading type
is replayed independently by `SyntheticSensorProvider`'s three tool-facing
reads (`get_river_level`, `get_rainfall_rate`, `get_dam_release` — task 12.1),
and keeping them separate means a consumer can load only the reading type it
needs without filtering. If a future implementer prefers a single combined
file, that is a reasonable adaptation — but the per-row shape above (in
particular, keeping `reading_type` on every row) should still be followed so
the change is a straightforward concatenation, not a schema rewrite.

## Reading frequency and coverage

- **Frequency:** hourly, 24 readings per day, one row per reading type per
  hour. Timestamps within a file are strictly increasing and evenly spaced by
  exactly one hour.
- **Coverage:** 35 consecutive days, starting `2026-08-01T00:00:00+05:30` and
  ending `2026-09-04T23:00:00+05:30` inclusive.
- **Row count:** exactly `35 * 24 = 840` rows per file (2,520 rows total
  across the three files). No row is missing or null anywhere in the window.

## The 35-day scenario

| Days | Hours (0-indexed) | Period | Intended trajectory |
|---|---|---|---|
| 1–30 | 0–719 | **Baseline** | Mild, plausible fluctuation around a steady-state normal range. Used by `Monitor_Agent`'s 30-day baseline / anomaly-ratio calculation (`schemas/decisions.py::HazardAssessment.anomaly_indicator`, Req 3.2, 3.9, 17.3). |
| 31–35 | 720–839 | **Rising-river demo scenario** | `river_level` rises from baseline through the assumed **WATCH → WARNING → EVACUATE** bands by the final reading, to exercise the escalation path end-to-end in the demo. `rainfall_rate` and `dam_release` rise in step, as plausible physical drivers/contributors of the flood scenario. |

Baseline period detail, per reading type:

- **`river_level`**: oscillates in roughly a **1.8–2.4 m** band (diurnal sine
  wave centred at 2.1 m, ±0.3 m, plus ±0.05 m noise) — comfortably under any
  WATCH threshold.
- **`rainfall_rate`**: mostly low (0.0–0.5 mm/h), with occasional light-rain
  bursts (~15% of hours, 1.0–8.0 mm/h).
- **`dam_release`**: steady around **55 m³/s** with small variation (±3 m³/s).

Rising-scenario detail (days 31–35), per reading type:

- **`river_level`** rises from the last baseline value (~2.1–2.4 m) to a
  final reading of **5.5 m**.
- **`rainfall_rate`** rises to a final reading of **55.0 mm/h**.
- **`dam_release`** rises to a final reading of **2750.0 m³/s**.

The ramp uses an accelerating curve (gentle at first, steeper near the end)
with small random noise and a bounded natural-backslide tolerance, so the
rise is monotonic-ish rather than a perfectly straight line, while still
guaranteeing the final hour of each series lands above the real EVACUATE
threshold for that reading type (see "Threshold cross-check" below).

## Threshold cross-check

`policy/rule_engine_rules.py` (task 3.2) is now implemented, and the
generator (`_generate_fixtures.py`) imports its `THRESHOLDS` table directly
rather than assuming its own values. The real thresholds are:

| Reading type | WATCH | WARNING | EVACUATE |
|---|---|---|---|
| `river_level` (m) | 3.5 | 4.2 | 5.0 |
| `rainfall_rate` (mm/h) | 20.0 | 35.0 | 50.0 |
| `dam_release` (m³/s) | 1000.0 | 1800.0 | 2500.0 |

Each `*_END` constant in `_generate_fixtures.py` is computed as the real
EVACUATE threshold for that reading type times a `1.10` margin (~10% above
threshold, matching the proportional margin the `river_level` ramp already
used), so the day 31–35 rising scenario's final reading clearly exceeds the
real EVACUATE threshold for every reading type by construction:

| Reading type | Real EVACUATE | Final reading (×1.10) |
|---|---|---|
| `river_level` (m) | 5.0 | 5.5 |
| `rainfall_rate` (mm/h) | 50.0 | 55.0 |
| `dam_release` (m³/s) | 2500.0 | 2750.0 |

If `policy/rule_engine_rules.py::THRESHOLDS` ever changes, simply re-run
`python seed/hazard_readings/_generate_fixtures.py` — the `*_END` constants
are derived from the real thresholds at generation time, so the demo
scenario stays correct with no manual number-chasing required.

## Regenerating the fixtures

The committed `.jsonl` files were produced by the committed generator script,
using a fixed random seed for reproducibility:

```bash
python seed/hazard_readings/_generate_fixtures.py
```

Re-running it produces byte-identical output. The generator is not imported
by application code — `SyntheticSensorProvider` reads the committed `.jsonl`
files directly.

## Contract for `integrations/sensor_provider.py` (task 10.1)

`SyntheticSensorProvider` should:

1. Load each `.jsonl` file once (e.g. at construction), parsing each line as
   one reading.
2. Given a requested timestamp (or "current" simulated time), replay the
   reading whose `timestamp` is the latest one at or before that time for the
   requested `reading_type`.
3. Look back 30 days from that point for the 30-day baseline (Req 3.1, 17.3);
   look back to the immediately preceding hourly reading for "the most
   recently completed sweep."
4. Return `value` and `unit` as-is; the `reading_id` and `reading_type`
   fields are available if useful for logging/dedup but are not required to
   satisfy the sensor read contract.

If this contract needs to change once task 10.1 is implemented, the file
format above should be treated as adaptable — but any change should be
reflected back into this README so the two tasks stay in sync.
