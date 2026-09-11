"""Unit tests for `policy/rule_engine_rules.py` — the deterministic threshold
table, staleness limit, and Rule_Set_Version content hash (Req 2.1, 2.7;
design.md §3.3)."""

import copy

from policy.rule_engine_rules import (
    RULE_SET_VERSION,
    STALENESS_LIMIT_S,
    THRESHOLDS,
    compute_rule_set_version,
)

EXPECTED_READING_TYPES = {"river_level", "rainfall_rate", "dam_release"}


def test_thresholds_has_entry_for_every_reading_type_used_elsewhere():
    """THRESHOLDS must have exactly the three reading types Req 2.1 names
    and that schemas/decisions.py::HazardAssessment keys rate_of_change /
    anomaly_indicator by."""
    assert set(THRESHOLDS.keys()) == EXPECTED_READING_TYPES


def test_each_reading_spec_has_ascending_rules_and_a_unit():
    for reading_type, spec in THRESHOLDS.items():
        assert spec.unit, f"{reading_type} spec must declare a unit"
        assert len(spec.rules_ascending) >= 1
        thresholds = [rule.threshold for rule in spec.rules_ascending]
        assert thresholds == sorted(thresholds), (
            f"{reading_type} rules_ascending must be sorted ascending by threshold"
        )
        # valid_range must be a proper inclusive (min, max) pair.
        low, high = spec.valid_range
        assert low < high


def test_staleness_limit_is_a_positive_int():
    assert isinstance(STALENESS_LIMIT_S, int)
    assert STALENESS_LIMIT_S > 0
    # Req 2.5 states the staleness limit is exactly 15 minutes.
    assert STALENESS_LIMIT_S == 15 * 60


def test_rule_set_version_is_stable_and_reproducible():
    """Calling compute_rule_set_version twice with identical inputs must
    produce the identical hash (deterministic, no non-deterministic
    elements like timestamps or dict-iteration-order sensitivity)."""
    first = compute_rule_set_version(THRESHOLDS, STALENESS_LIMIT_S)
    second = compute_rule_set_version(THRESHOLDS, STALENESS_LIMIT_S)
    assert first == second
    assert first == RULE_SET_VERSION


def test_rule_set_version_changes_when_a_threshold_value_changes():
    """Mutating a copy of THRESHOLDS (changing one threshold value) must
    produce a different hash than the original."""
    mutated = copy.deepcopy(THRESHOLDS)
    original_spec = mutated["river_level"]
    mutated_rule = original_spec.rules_ascending[0]
    bumped_rule = type(mutated_rule)(
        rule_id=mutated_rule.rule_id,
        band=mutated_rule.band,
        threshold=mutated_rule.threshold + 0.1,
    )
    mutated["river_level"] = type(original_spec)(
        unit=original_spec.unit,
        valid_range=original_spec.valid_range,
        rules_ascending=(bumped_rule, *original_spec.rules_ascending[1:]),
    )

    original_hash = compute_rule_set_version(THRESHOLDS, STALENESS_LIMIT_S)
    mutated_hash = compute_rule_set_version(mutated, STALENESS_LIMIT_S)
    assert original_hash != mutated_hash


def test_rule_set_version_changes_when_staleness_limit_changes():
    original_hash = compute_rule_set_version(THRESHOLDS, STALENESS_LIMIT_S)
    mutated_hash = compute_rule_set_version(THRESHOLDS, STALENESS_LIMIT_S + 1)
    assert original_hash != mutated_hash


def test_rule_set_version_is_a_short_hex_string():
    assert isinstance(RULE_SET_VERSION, str)
    assert len(RULE_SET_VERSION) == 12
    int(RULE_SET_VERSION, 16)  # must be valid hex
