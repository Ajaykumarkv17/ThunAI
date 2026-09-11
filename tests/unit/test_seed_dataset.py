"""Validation tests for seed/seed_dataset.json (task 11.1).

Validates: Requirements 18.1, 18.2; Design §3.9.
"""

from __future__ import annotations

import json
from pathlib import Path

from schemas.entities import Responder, Shelter

DATASET_PATH = Path(__file__).resolve().parents[2] / "seed" / "seed_dataset.json"


def _load_dataset() -> dict:
    with DATASET_PATH.open(encoding="utf-8") as f:
        return json.load(f)


class TestSeedDatasetResponders:
    def test_exactly_fourteen_responders(self) -> None:
        dataset = _load_dataset()
        assert len(dataset["responders"]) == 14

    def test_every_responder_constructs_as_schema(self) -> None:
        """Each row must deserialise into schemas.entities.Responder with no
        validation error and with zero transformation."""
        dataset = _load_dataset()
        responders = [Responder(**row) for row in dataset["responders"]]
        assert len(responders) == 14

    def test_all_responders_available_and_unassigned_at_seed(self) -> None:
        dataset = _load_dataset()
        responders = [Responder(**row) for row in dataset["responders"]]
        for responder in responders:
            assert responder.availability_status == "AVAILABLE"
            assert responder.active_assignment_count == 0
            assert responder.active_assignment_id is None

    def test_responder_ids_are_unique(self) -> None:
        dataset = _load_dataset()
        ids = [row["responder_id"] for row in dataset["responders"]]
        assert len(ids) == len(set(ids))


class TestSeedDatasetShelters:
    def test_exactly_three_shelters(self) -> None:
        dataset = _load_dataset()
        assert len(dataset["shelters"]) == 3

    def test_every_shelter_constructs_as_schema(self) -> None:
        dataset = _load_dataset()
        shelters = [Shelter(**row) for row in dataset["shelters"]]
        assert len(shelters) == 3

    def test_total_capacity_sums_to_120(self) -> None:
        dataset = _load_dataset()
        shelters = [Shelter(**row) for row in dataset["shelters"]]
        assert sum(s.total_capacity for s in shelters) == 120

    def test_available_capacity_equals_total_capacity_at_seed(self) -> None:
        dataset = _load_dataset()
        shelters = [Shelter(**row) for row in dataset["shelters"]]
        for shelter in shelters:
            assert shelter.available_capacity == shelter.total_capacity

    def test_shelter_ids_are_unique(self) -> None:
        dataset = _load_dataset()
        ids = [row["shelter_id"] for row in dataset["shelters"]]
        assert len(ids) == len(set(ids))


class TestSeedDatasetResidentPoints:
    def test_approximately_nine_hundred_points(self) -> None:
        dataset = _load_dataset()
        count = len(dataset["resident_points"])
        assert 850 <= count <= 950

    def test_every_point_has_positive_occupant_count(self) -> None:
        dataset = _load_dataset()
        for point in dataset["resident_points"]:
            assert point["occupant_count"] > 0

    def test_point_ids_are_unique(self) -> None:
        dataset = _load_dataset()
        ids = [point["point_id"] for point in dataset["resident_points"]]
        assert len(ids) == len(set(ids))

    def test_no_pii_fields_present(self) -> None:
        """Req 18.1/18.2: resident points must be aggregate-only — no name,
        no contact identifier, no household-level location detail beyond an
        area label and coordinate."""
        dataset = _load_dataset()
        allowed_keys = {"point_id", "area_id", "coords", "occupant_count"}
        for point in dataset["resident_points"]:
            assert set(point.keys()) <= allowed_keys
