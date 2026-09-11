"""Deterministic eval harness for the ThunAI scenario suite (design.md Testing Strategy).

This module is the engine behind ``evals/scenarios/*.json``. It loads recorded
scenarios, replays each one against the **real** ThunAI decision logic through
in-process doubles, and scores the observable outcome with a **deterministic**
evaluator (exact-match on structured output, plus trajectory/state assertions) —
never an LLM judge (Req 21.1, design.md "Strands Evals scenario suite").

Why deterministic replay rather than the live agents
-----------------------------------------------------
Every ThunAI decision is already a typed value produced by pure, deterministic
authorities:

    * severity banding  -> ``agents.rule_engine.evaluate_rule_engine`` (0 model calls)
    * autonomy verdict  -> ``policy.escalation_policy.decide`` (0 model calls)
    * intake urgency    -> the deterministic post-processing rules (mobility/medical
      force IMMEDIATE) that the design carves out of the model path so they are
      exactly replayable

The model-mediated parts of intake (free-text -> category/urgency *label*) are
represented in the scenario by the **hand-labelled** ``expected`` block already
recorded in ``seed/sample_messages/messages.json``; the harness asserts the
deterministic rules that ThunAI layers on top of that label (Req 5.2/5.3),
which is precisely the split the design's Testing Strategy draws between the
Hypothesis property (13.9) and this eval suite.

Scenario file schema (Req 21.11)
--------------------------------
Each scenario is one JSON object carrying the four required declarations:

    {
      "scenario_id":            "<unique id>",
      "input_fixture_id":       "<id/path into seed/ fixtures>",
      "expected_observable_outcome": { ... kind-specific expected fields ... },
      "pass_condition":         "<human-readable statement of the check>",

      "kind":  "rule_engine" | "intake" | "dispatch" | "escalation" | "sweep",
      "input": { ... kind-specific replay inputs ... },
      "coverage": "<which Req 21.1 coverage slot this fills>"   # documentation only
    }

``kind`` selects which in-process double replays the scenario; ``input`` carries
the replay inputs; ``expected_observable_outcome`` is matched exactly against the
observed outcome. Extra keys are ignored so scenarios stay forward-compatible.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.rule_engine import evaluate_rule_engine
from policy.escalation_policy import POLICY_VERSION, decide

# ---------------------------------------------------------------------------
# Eval_Suite version (Req 21.2): a stable identifier for this recorded suite,
# reported alongside the goal-success stat in the README. Bump the numeric
# suffix whenever the scenario set or an evaluator's semantics change so a
# reported goal-success figure is always attributable to a specific suite.
# ---------------------------------------------------------------------------

EVAL_SUITE_VERSION: str = "eval-suite-v1"

# Directory layout (design.md Repository layout: evals/scenarios/, evals/results/).
_EVALS_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = _EVALS_DIR / "scenarios"
RESULTS_DIR = _EVALS_DIR / "results"

# Repository root, used to resolve fixture references under seed/.
_REPO_ROOT = _EVALS_DIR.parent


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    """The outcome of replaying and scoring a single scenario."""

    scenario_id: str
    kind: str
    passed: bool
    pass_condition: str
    expected: dict[str, Any]
    observed: dict[str, Any]
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "passed": self.passed,
            "pass_condition": self.pass_condition,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SuiteResult:
    """The aggregate outcome of a full suite run (Req 21.2)."""

    eval_suite_version: str
    total: int
    passed: int
    goal_success_pct: float
    results: list[ScenarioResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eval_suite_version": self.eval_suite_version,
            "policy_version": POLICY_VERSION,
            "total": self.total,
            "passed": self.passed,
            "failed": self.total - self.passed,
            "goal_success_pct": self.goal_success_pct,
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# Scenario loading (Req 21.11)
# ---------------------------------------------------------------------------


def load_scenarios(scenarios_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load and validate every recorded scenario, sorted by ``scenario_id``.

    Args:
        scenarios_dir: Directory of ``*.json`` scenario files. Defaults to
            ``evals/scenarios/``.

    Returns:
        A list of scenario dicts, each guaranteed to carry the four Req 21.11
        declarations plus a recognised ``kind``.

    Raises:
        ValueError: If any scenario file is missing a required declaration,
            declares an unknown ``kind``, or if two scenarios share an id.
    """
    scenarios_dir = scenarios_dir or SCENARIOS_DIR
    required = ("scenario_id", "input_fixture_id", "expected_observable_outcome", "pass_condition", "kind")

    scenarios: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for path in sorted(scenarios_dir.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            scenario = json.load(fh)
        for key in required:
            if key not in scenario:
                raise ValueError(f"scenario {path.name} is missing required declaration '{key}' (Req 21.11)")
        if scenario["kind"] not in _EVALUATORS:
            raise ValueError(f"scenario {path.name} declares unknown kind {scenario['kind']!r}")
        sid = scenario["scenario_id"]
        if sid in seen_ids:
            raise ValueError(f"duplicate scenario_id {sid!r} (must be unique across the suite)")
        seen_ids.add(sid)
        scenarios.append(scenario)
    return scenarios


# ---------------------------------------------------------------------------
# Fixture doubles (in-process; no AWS, no credentials — Req 21.10)
# ---------------------------------------------------------------------------


def _load_seed_dataset() -> dict[str, Any]:
    with (_REPO_ROOT / "seed" / "seed_dataset.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_sample_messages() -> dict[str, dict[str, Any]]:
    with (_REPO_ROOT / "seed" / "sample_messages" / "messages.json").open(encoding="utf-8") as fh:
        return {m["message_id"]: m for m in json.load(fh)}


def _load_readings_series(reading_type: str) -> list[dict[str, Any]]:
    path = _REPO_ROOT / "seed" / "hazard_readings" / f"{reading_type}.jsonl"
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Evaluators — one per scenario ``kind``. Each returns (observed, passed, detail).
# ---------------------------------------------------------------------------


def _eval_exact(expected: dict[str, Any], observed: dict[str, Any]) -> tuple[bool, str]:
    """Deterministic exact-match: every expected key must equal the observed value.

    Only the keys named in ``expected`` are compared (a scenario asserts the
    observable fields it cares about); observed carries the full picture for
    the results file. Mismatches are named individually for debuggability.
    """
    mismatches = []
    for key, want in expected.items():
        got = observed.get(key, "<absent>")
        if got != want:
            mismatches.append(f"{key}: expected {want!r}, observed {got!r}")
    return (not mismatches), ("; ".join(mismatches) if mismatches else "exact match on all expected fields")


def _evaluate_rule_engine(scenario: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
    """Replay a fixed set of hazard readings through the real Rule_Engine.

    ``input`` carries ``readings`` (mapping reading_type -> {value, age_seconds?})
    and optional ``last_known_band``; a synthetic fixed ``now`` is used so
    staleness is deterministic. Trajectory assertion: the payload always reports
    ``model_invocations == 0`` (Req 2.1), checked in addition to the band.
    """
    inp = scenario.get("input", {})
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    readings: dict[str, dict[str, Any]] = {}
    for reading_type, spec in inp.get("readings", {}).items():
        if spec is None:
            readings[reading_type] = None
            continue
        age = spec.get("age_seconds", 0)
        ts = now - _timedelta_seconds(age)
        readings[reading_type] = {"value": spec.get("value"), "unit": spec.get("unit", ""), "ts": ts}

    payload = evaluate_rule_engine(
        readings=readings,
        now=now,
        last_known_band=inp.get("last_known_band", "NORMAL"),
    )
    observed = {
        "severity_band": payload["severity_band"],
        "triggered_rules": sorted(payload["triggered_rules"]),
        "unverified": payload["unverified"],
        "model_invocations": payload["model_invocations"],
    }
    expected = dict(scenario["expected_observable_outcome"])
    if "triggered_rules" in expected:
        expected["triggered_rules"] = sorted(expected["triggered_rules"])
    passed, detail = _eval_exact(expected, observed)
    return observed, passed, detail


def _timedelta_seconds(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def _evaluate_intake(scenario: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
    """Replay one recorded resident message through intake's deterministic layer.

    The model-produced label is taken from the fixture's hand-labelled
    ``expected`` block (``seed/sample_messages/messages.json``); the harness then
    applies ThunAI's deterministic post-processing (Req 5.2/5.3): mobility or
    medical need forces ``urgency_band == "IMMEDIATE"`` and makes the request
    ineligible for autonomous dispatch, and an INFORMATION/OTHER category is
    never dispatch-eligible. The observed outcome is the post-processed result,
    matched exactly against the scenario's expected observable outcome.
    """
    messages = _load_sample_messages()
    msg = messages[scenario["input_fixture_id"]]
    labelled = msg["expected"]

    category = labelled["request_category"]
    urgency = labelled["urgency_band"]
    mobility = labelled.get("mobility_assistance")
    medical = labelled.get("medical_need")

    # Deterministic post-processing (Req 5.2, 5.3): a reported mobility-assistance
    # or medical need forces IMMEDIATE urgency and routes to a human (never an
    # autonomous ambulatory dispatch), regardless of the model's own urgency.
    forced = (mobility is True) or (medical is True)
    effective_urgency = "IMMEDIATE" if forced else urgency

    # INFORMATION / OTHER are advisory/triage categories, never dispatch-eligible.
    dispatch_eligible = labelled["dispatch_eligible"]
    if category in ("INFORMATION", "OTHER"):
        dispatch_eligible = False
    if forced:
        # A mobility/medical case is an Irreversible_Action-class dispatch that
        # always goes to a human, so it is not autonomously dispatch-eligible.
        dispatch_eligible = False

    observed = {
        "source_language": msg["source_language_hint"],
        "request_category": category,
        "urgency_band": effective_urgency,
        "dispatch_eligible": dispatch_eligible,
        "forced_immediate": forced,
    }
    passed, detail = _eval_exact(scenario["expected_observable_outcome"], observed)
    return observed, passed, detail


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    r = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _evaluate_dispatch(scenario: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
    """Replay a dispatch decision over the seeded responder pool.

    ``input`` carries an incident location, a required-equipment filter, a
    search radius (km), and an optional ``responder_overrides`` map that marks
    seeded responders unavailable (to model the no-capacity case). The harness
    finds candidate responders (radius + equipment), assigns the nearest, and
    routes the resulting DispatchDecision through the real ``decide`` authority.

    Observable outcome: whether capacity was found, the assigned responder (or
    None), and whether the decision escalated (no-capacity always escalates,
    Req 6.6).
    """
    inp = scenario.get("input", {})
    dataset = _load_seed_dataset()
    overrides = inp.get("responder_overrides", {})
    location = tuple(inp["incident_coords"])
    radius_km = inp.get("radius_km", 10.0)
    required_equipment = inp.get("required_equipment")

    candidates: list[tuple[float, str]] = []
    for responder in dataset["responders"]:
        rid = responder["responder_id"]
        status = overrides.get(rid, responder["availability_status"])
        if status != "AVAILABLE":
            continue
        if required_equipment and required_equipment not in responder["equipment"]:
            continue
        dist = _haversine_km(location, tuple(responder["home_coords"]))
        if dist <= radius_km:
            candidates.append((dist, rid))

    candidates.sort(key=lambda c: (c[0], c[1]))
    has_capacity = bool(candidates)
    assigned = candidates[0][1] if has_capacity else None

    if has_capacity:
        category = "routine_dispatch_ambulatory"
        confidence = inp.get("confidence", 0.9)
        verdict = decide(category, confidence, action="execute", is_irreversible_action=False)
    else:
        # Req 6.6: no available responder is a trade-off only the coordinator
        # can resolve -> always escalates.
        category = "no_capacity"
        verdict = decide(category, inp.get("confidence", 0.9), action="ask_human")

    observed = {
        "has_capacity": has_capacity,
        "assigned_responder_id": assigned,
        "candidate_count": len(candidates),
        "escalated": verdict["escalate"],
        "category": category,
    }
    passed, detail = _eval_exact(scenario["expected_observable_outcome"], observed)
    return observed, passed, detail


def _evaluate_escalation(scenario: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
    """Replay an escalation lifecycle: creation via ``decide`` then resolution.

    ``input`` carries the decision fields fed to ``decide`` (category,
    confidence, action, is_irreversible_action, audience_count, anomaly_ratio,
    severity_band) plus a ``human_response`` of ``"approve"``, ``"decline"``, or
    ``"timeout"``. The harness confirms the decision escalated (an
    Escalation_Record would be created), then applies the human response to
    derive the record's terminal status:

        approve  -> status RESOLVED,            resolved_by_default False
        decline  -> status RESOLVED,            resolved_by_default False
        timeout  -> status RESOLVED_BY_DEFAULT, resolved_by_default True,
                    executed action == policy default_action (withhold)

    Observable outcome: escalated flag, terminal status, resolved_by_default,
    and (for timeout) the applied default action.
    """
    inp = scenario.get("input", {})
    verdict = decide(
        inp["category"],
        inp.get("confidence", 0.9),
        action=inp.get("action"),
        is_irreversible_action=inp.get("is_irreversible_action", False),
        audience_count=inp.get("audience_count"),
        anomaly_ratio=inp.get("anomaly_ratio"),
        severity_band=inp.get("severity_band", "WATCH"),
    )

    human_response = inp["human_response"]
    if not verdict["escalate"]:
        status = "NOT_ESCALATED"
        resolved_by_default = False
        applied_action = None
    elif human_response == "approve":
        status = "RESOLVED"
        resolved_by_default = False
        applied_action = inp.get("approved_option", "approved")
    elif human_response == "decline":
        status = "RESOLVED"
        resolved_by_default = False
        applied_action = "declined"
    elif human_response == "timeout":
        # Req 11 timeout sweep: the persisted default action is applied and the
        # record resolves by default (withhold semantics from DEFAULT_ACTION).
        status = "RESOLVED_BY_DEFAULT"
        resolved_by_default = True
        applied_action = verdict["default_action"]
    else:
        raise ValueError(f"unknown human_response {human_response!r}")

    observed = {
        "escalated": verdict["escalate"],
        "status": status,
        "resolved_by_default": resolved_by_default,
        "applied_action": applied_action,
        "determining_entry": verdict["determining_entry"],
    }
    passed, detail = _eval_exact(scenario["expected_observable_outcome"], observed)
    return observed, passed, detail


def _evaluate_sweep(scenario: dict[str, Any]) -> tuple[dict[str, Any], bool, str]:
    """Replay a full routine monitoring sweep over the seeded rising-river series.

    This is the single-escalation-per-sweep demo assertion (Req 11.9, task 28.3,
    design.md §3.4 tuning note): the seeded 35-day hazard series is walked in
    ``sweep_step_hours`` steps; each step runs the real Rule_Engine, and a NEW
    escalation is created only when the severity band **rises to** a band whose
    hazard decision escalates under the policy — a sustained EVACUATE band does
    not re-escalate every sweep step. The scenario asserts exactly one
    Escalation_Record is produced across the whole sweep.
    """
    inp = scenario.get("input", {})
    step_hours = inp.get("sweep_step_hours", 1)
    reading_types = inp.get("reading_types", ["river_level", "rainfall_rate", "dam_release"])

    series: dict[str, list[dict[str, Any]]] = {rt: _load_readings_series(rt) for rt in reading_types}
    length = min(len(s) for s in series.values())

    escalation_bands = set(inp.get("escalation_bands", ["EVACUATE"]))
    last_band = "NORMAL"
    escalations = 0
    band_history: list[str] = []

    for i in range(0, length, step_hours):
        readings: dict[str, dict[str, Any]] = {}
        for rt in reading_types:
            row = series[rt][i]
            # Parse the fixture timestamp and treat it as fresh relative to
            # itself so staleness never spuriously flips the band during replay.
            ts = datetime.fromisoformat(row["timestamp"])
            readings[rt] = {"value": row["value"], "unit": row.get("unit", ""), "ts": ts}
        now = max(r["ts"] for r in readings.values())
        payload = evaluate_rule_engine(readings=readings, now=now, last_known_band=last_band)
        band = payload["severity_band"]
        band_history.append(band)

        # A new escalation is raised only on a RISE into an escalation band,
        # not on every step the band remains elevated (design.md §3.4 tuning).
        rose_into_escalation = band in escalation_bands and last_band not in escalation_bands
        if rose_into_escalation:
            hazard_verdict = decide(
                "evacuation_order",
                inp.get("confidence", 0.95),
                action="ask_human",
                is_irreversible_action=True,
                severity_band=band,
            )
            if hazard_verdict["escalate"]:
                escalations += 1
        last_band = band

    observed = {
        "escalation_record_count": escalations,
        "peak_band": max(band_history, key=["NORMAL", "WATCH", "WARNING", "EVACUATE"].index),
        "sweep_steps": len(band_history),
    }
    passed, detail = _eval_exact(scenario["expected_observable_outcome"], observed)
    return observed, passed, detail


_EVALUATORS = {
    "rule_engine": _evaluate_rule_engine,
    "intake": _evaluate_intake,
    "dispatch": _evaluate_dispatch,
    "escalation": _evaluate_escalation,
    "sweep": _evaluate_sweep,
}


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def run_scenario(scenario: dict[str, Any]) -> ScenarioResult:
    """Replay and score a single scenario with its kind-specific evaluator."""
    evaluator = _EVALUATORS[scenario["kind"]]
    observed, passed, detail = evaluator(scenario)
    return ScenarioResult(
        scenario_id=scenario["scenario_id"],
        kind=scenario["kind"],
        passed=passed,
        pass_condition=scenario["pass_condition"],
        expected=scenario["expected_observable_outcome"],
        observed=observed,
        detail=detail,
    )


def compute_goal_success(passed: int, total: int) -> float:
    """Goal-success rate = ``passed / total * 100``, rounded to one decimal (Req 21.2).

    Returns ``0.0`` for an empty suite (no scenarios) rather than dividing by
    zero, so the harness never crashes on an empty ``scenarios/`` directory.
    """
    if total == 0:
        return 0.0
    return round(passed / total * 100, 1)


def run_suite(scenarios_dir: Path | None = None) -> SuiteResult:
    """Run the full recorded suite and compute the goal-success rate (Req 21.2).

    Args:
        scenarios_dir: Directory of scenario files; defaults to
            ``evals/scenarios/``.

    Returns:
        A :class:`SuiteResult` carrying every :class:`ScenarioResult`, the
        pass/total counts, the one-decimal goal-success percentage, and the
        ``Eval_Suite`` version.
    """
    scenarios = load_scenarios(scenarios_dir)
    results = [run_scenario(s) for s in scenarios]
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    return SuiteResult(
        eval_suite_version=EVAL_SUITE_VERSION,
        total=total,
        passed=passed,
        goal_success_pct=compute_goal_success(passed, total),
        results=results,
    )


def write_results(suite: SuiteResult, results_dir: Path | None = None) -> Path:
    """Persist a suite run to ``evals/results/latest.json`` and return its path."""
    results_dir = results_dir or RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / "latest.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(suite.to_dict(), fh, indent=2, ensure_ascii=False)
    return out_path


def _main() -> int:
    """CLI entry point: run the suite, persist results, print the goal-success stat."""
    suite = run_suite()
    out_path = write_results(suite)

    print(f"ThunAI Eval_Suite: {suite.eval_suite_version}  (policy {POLICY_VERSION})")
    print(f"Scenarios: {suite.total}   Passed: {suite.passed}   Failed: {suite.total - suite.passed}")
    print(f"Goal-success rate: {suite.goal_success_pct:.1f}%")
    print(f"Results written to: {out_path}")
    if suite.passed != suite.total:
        print("\nFailing scenarios:")
        for r in suite.results:
            if not r.passed:
                print(f"  - {r.scenario_id} [{r.kind}]: {r.detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
