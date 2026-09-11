"""Unit tests for policy/escalation_policy.py (task 3.1).

Covers the confidence floor, always-ask / never-ask category sets,
irreversible-action escalation, the mass-notification audience threshold,
the anomaly-ratio threshold, POLICY_VERSION stability/sensitivity, and the
fail-safe behaviour when validate_policy() finds a defect (Req 10.1, 10.2,
10.4, 10.5, 10.6, 10.7, 10.8, 10.10, 10.12).
"""

import pytest

from policy import escalation_policy as ep


# ---------------------------------------------------------------------------
# Confidence floor (Req 10.4)
# ---------------------------------------------------------------------------


def test_confidence_below_floor_always_escalates():
    below = ep.CONFIDENCE_FLOOR.value - 0.01
    result = ep.decide("hazard_monitoring", below, action="execute")
    assert result["escalate"] is True
    assert result["determining_entry"] == "CONFIDENCE_FLOOR"


def test_confidence_at_floor_does_not_escalate_on_confidence_alone():
    at_floor = ep.CONFIDENCE_FLOOR.value
    result = ep.decide("hazard_monitoring", at_floor, action="execute")
    assert result["escalate"] is False


# ---------------------------------------------------------------------------
# Always-ask categories (Req 10.1, 10.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(ep.ALWAYS_ASK_CATEGORIES))
def test_always_ask_categories_escalate_regardless_of_confidence(category):
    result = ep.decide(category, 1.0, action="execute")
    assert result["escalate"] is True
    assert result["determining_entry"] == "ALWAYS_ASK_CATEGORIES"


# ---------------------------------------------------------------------------
# Never-ask categories (Req 10.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(ep.NEVER_ASK_CATEGORIES))
def test_never_ask_categories_do_not_escalate_given_sufficient_confidence(category):
    result = ep.decide(category, 1.0, action="execute")
    assert result["escalate"] is False
    assert result["determining_entry"] == "NEVER_ASK_CATEGORIES"


def test_never_ask_category_still_escalates_below_confidence_floor():
    category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
    below = ep.CONFIDENCE_FLOOR.value - 0.01
    result = ep.decide(category, below, action="execute")
    assert result["escalate"] is True
    assert result["determining_entry"] == "CONFIDENCE_FLOOR"


# ---------------------------------------------------------------------------
# Irreversible actions (Req 10.6, 10.12, cross-check Req 9.2)
# ---------------------------------------------------------------------------


def test_irreversible_action_always_escalates_regardless_of_category_or_confidence():
    category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
    result = ep.decide(category, 1.0, action="execute", is_irreversible_action=True)
    assert result["escalate"] is True
    assert result["determining_entry"] == "is_irreversible_action"


# ---------------------------------------------------------------------------
# Explicit ask_human action (Req 10.5)
# ---------------------------------------------------------------------------


def test_action_ask_human_always_escalates_regardless_of_confidence():
    category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
    result = ep.decide(category, 1.0, action="ask_human")
    assert result["escalate"] is True
    assert result["determining_entry"] == "action"


# ---------------------------------------------------------------------------
# Mass-notification audience threshold (Req 10.1, 10.6)
# ---------------------------------------------------------------------------


def test_audience_count_above_mass_notification_threshold_escalates():
    over = ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD.value + 1
    result = ep.decide(
        "alert_release", 1.0, action="execute", audience_count=over
    )
    assert result["escalate"] is True
    assert result["determining_entry"] == "MASS_NOTIFICATION_AUDIENCE_THRESHOLD"


def test_audience_count_at_or_below_threshold_does_not_escalate_via_that_check():
    at_threshold = ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD.value
    result = ep.decide(
        "alert_release", 1.0, action="execute", audience_count=at_threshold
    )
    assert result["escalate"] is False


# ---------------------------------------------------------------------------
# Anomaly ratio threshold (Req 10.1, 10.7)
# ---------------------------------------------------------------------------


def test_anomaly_ratio_above_threshold_escalates():
    over = ep.ANOMALY_RATIO_THRESHOLD.value + 0.01
    result = ep.decide(
        "hazard_monitoring", 1.0, action="execute", anomaly_ratio=over
    )
    assert result["escalate"] is True
    assert result["determining_entry"] == "ANOMALY_RATIO_THRESHOLD"


def test_anomaly_ratio_at_or_below_threshold_does_not_escalate_via_that_check():
    at_threshold = ep.ANOMALY_RATIO_THRESHOLD.value
    result = ep.decide(
        "hazard_monitoring", 1.0, action="execute", anomaly_ratio=at_threshold
    )
    assert result["escalate"] is False


# ---------------------------------------------------------------------------
# POLICY_VERSION stability / sensitivity (Req 10.2, Property 8)
# ---------------------------------------------------------------------------


def test_policy_version_is_stable_across_recomputation():
    recomputed = ep.compute_policy_version(
        ep.CATEGORY_TABLE,
        ep.CONFIDENCE_FLOOR,
        ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD,
        ep.ANOMALY_RATIO_THRESHOLD,
        ep.RESPONSE_DEADLINE_MINUTES,
        ep.DEFAULT_ACTION,
    )
    assert recomputed == ep.POLICY_VERSION


def test_policy_version_changes_when_confidence_floor_changes():
    mutated_floor = ep.ThresholdSpec(
        value=ep.CONFIDENCE_FLOOR.value + 0.01,
        unit=ep.CONFIDENCE_FLOOR.unit,
        valid_range=ep.CONFIDENCE_FLOOR.valid_range,
        rationale=ep.CONFIDENCE_FLOOR.rationale,
    )
    mutated_version = ep.compute_policy_version(
        ep.CATEGORY_TABLE,
        mutated_floor,
        ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD,
        ep.ANOMALY_RATIO_THRESHOLD,
        ep.RESPONSE_DEADLINE_MINUTES,
        ep.DEFAULT_ACTION,
    )
    assert mutated_version != ep.POLICY_VERSION


def test_policy_version_changes_when_category_table_changes():
    mutated_table = dict(ep.CATEGORY_TABLE)
    mutated_table["hazard_monitoring"] = ep.PolicyEntry(
        category="hazard_monitoring",
        never_ask=True,
        rationale="mutated for test purposes",
    )
    mutated_version = ep.compute_policy_version(
        mutated_table,
        ep.CONFIDENCE_FLOOR,
        ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD,
        ep.ANOMALY_RATIO_THRESHOLD,
        ep.RESPONSE_DEADLINE_MINUTES,
        ep.DEFAULT_ACTION,
    )
    assert mutated_version != ep.POLICY_VERSION


def test_policy_version_unchanged_when_nothing_changes():
    same_version_a = ep.compute_policy_version(
        ep.CATEGORY_TABLE,
        ep.CONFIDENCE_FLOOR,
        ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD,
        ep.ANOMALY_RATIO_THRESHOLD,
        ep.RESPONSE_DEADLINE_MINUTES,
        ep.DEFAULT_ACTION,
    )
    same_version_b = ep.compute_policy_version(
        dict(ep.CATEGORY_TABLE),
        ep.CONFIDENCE_FLOOR,
        ep.MASS_NOTIFICATION_AUDIENCE_THRESHOLD,
        ep.ANOMALY_RATIO_THRESHOLD,
        dict(ep.RESPONSE_DEADLINE_MINUTES),
        dict(ep.DEFAULT_ACTION),
    )
    assert same_version_a == same_version_b


# ---------------------------------------------------------------------------
# validate_policy() correctness and the fail-safe integration (Req 10.10,
# Property 7)
# ---------------------------------------------------------------------------


def test_validate_policy_passes_on_the_real_policy_content():
    assert ep.validate_policy() == []
    assert ep._POLICY_IS_VALID is True


def test_validate_policy_detects_category_in_both_always_and_never_ask():
    conflicting = "routine_dispatch_ambulatory"
    mutated_table = dict(ep.CATEGORY_TABLE)
    mutated_table[conflicting] = ep.PolicyEntry(
        category=conflicting,
        always_ask=True,
        never_ask=False,  # constructed directly to avoid PolicyEntry's own guard
        rationale="test-mutated entry",
    )
    # Force both flags true without tripping PolicyEntry.__post_init__'s own
    # guard, by mutating the frozen dataclass's underlying fields via a
    # dict-shaped stand-in instead. Simpler: build a lightweight namespace
    # object exposing the same two boolean attributes validate_policy reads.
    class _FakeEntry:
        def __init__(self, category, rationale):
            self.category = category
            self.always_ask = True
            self.never_ask = True
            self.response_deadline_minutes_override = None
            self.rationale = rationale

    mutated_table[conflicting] = _FakeEntry(conflicting, "test-mutated: both always and never ask")

    defects = ep.validate_policy(category_table=mutated_table)
    assert any("both always-ask and never-ask" in d for d in defects)


def test_validate_policy_detects_out_of_range_confidence_floor():
    bad_floor = ep.ThresholdSpec(
        value=1.5,  # outside [0.0, 1.0]
        unit=ep.CONFIDENCE_FLOOR.unit,
        valid_range=ep.CONFIDENCE_FLOOR.valid_range,
        rationale="test-mutated",
    )
    defects = ep.validate_policy(confidence_floor=bad_floor)
    assert any("confidence_floor" in d for d in defects)


def test_validate_policy_detects_higher_severity_longer_deadline():
    mutated_deadlines = dict(ep.RESPONSE_DEADLINE_MINUTES)
    mutated_deadlines["EVACUATE"] = ep.ThresholdSpec(
        value=200,  # longer than WARNING's deadline; also outside its own
        # declared range, which is fine -- both defect classes may fire.
        unit="minutes",
        valid_range=(1, 200),
        rationale="test-mutated: deliberately too long",
    )
    defects = ep.validate_policy(response_deadline_minutes=mutated_deadlines)
    assert any("exceeds" in d for d in defects)


def test_validate_policy_detects_missing_default_action_entry():
    mutated_defaults = dict(ep.DEFAULT_ACTION)
    del mutated_defaults["hazard_monitoring"]
    defects = ep.validate_policy(default_action=mutated_defaults)
    assert any("missing entry" in d for d in defects)


def test_decide_falls_back_to_universal_ask_human_when_policy_invalid(monkeypatch):
    monkeypatch.setattr(ep, "_POLICY_IS_VALID", False)
    monkeypatch.setattr(ep, "_POLICY_DEFECTS", ["simulated defect for test"])

    never_ask_category = sorted(ep.NEVER_ASK_CATEGORIES)[0]
    result = ep.decide(never_ask_category, 1.0, action="execute")

    assert result["escalate"] is True
    assert result["determining_entry"] == "_POLICY_IS_VALID"
