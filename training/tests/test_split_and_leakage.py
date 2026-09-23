"""Vehicle-disjoint splitting, determinism, and the leakage gate.

`TestCiLeakageGate` is the permanent regression test the M1 brief requires: a
dataset where one vehicle appears in both train and test must FAIL, and the
otherwise-identical dataset with distinct vehicles must PASS.
"""
import pytest

from fixtures import (
    clean_dataset, dataset_with_leakage, dataset_without_leakage, make_record,
)
from qc.checks import ERROR, check_dataset, has_blocking_errors
from split import (
    SplitConfig, assign_identity, assign_splits, find_identity_leakage,
    find_image_leakage, find_repeated_plate_text, plan_split,
)


class TestVehicleDisjointness:
    def test_every_frame_of_a_vehicle_lands_in_one_split(self):
        """The core guarantee. Consecutive frames are near-duplicates; splitting
        on frames scores the model on its own training data."""
        records = assign_splits(clean_dataset(vehicles=60, frames_per_vehicle=5))
        by_vehicle = {}
        for record in records:
            by_vehicle.setdefault(record.vehicle_id, set()).add(record.split)
        for vehicle, splits in by_vehicle.items():
            assert len(splits) == 1, f"{vehicle} was split across {splits}"

    def test_no_leakage_on_a_generated_corpus(self):
        records = assign_splits(clean_dataset(vehicles=100, frames_per_vehicle=4))
        assert find_identity_leakage(records) == {}

    def test_all_three_splits_are_populated(self):
        records = assign_splits(clean_dataset(vehicles=200, frames_per_vehicle=2))
        report = plan_split(records)
        for name in ("train", "val", "test"):
            assert report.unique_identities[name] > 0

    def test_ratios_are_approximately_respected(self):
        """Approximately, not exactly: identities are hashed independently, which
        buys growth stability at the cost of exact proportions."""
        records = assign_splits(clean_dataset(vehicles=600, frames_per_vehicle=1))
        percentages = plan_split(records).identity_percentages()
        assert percentages["train"] == pytest.approx(70, abs=6)
        assert percentages["val"] == pytest.approx(15, abs=6)
        assert percentages["test"] == pytest.approx(15, abs=6)

    def test_an_empty_identity_is_left_unassigned_not_defaulted_to_train(self):
        """A record whose vehicle is unknown cannot be guaranteed disjoint from
        anything, so it must never silently become training data."""
        records = assign_splits([make_record("", plate_text="GJ05AB1234")])
        assert records[0].split == ""


class TestDeterminism:
    def test_the_same_seed_gives_the_same_split(self):
        first = assign_splits(clean_dataset(vehicles=80), SplitConfig(seed="alpha"))
        second = assign_splits(clean_dataset(vehicles=80), SplitConfig(seed="alpha"))
        assert [r.split for r in first] == [r.split for r in second]

    def test_a_different_seed_gives_a_different_split(self):
        first = assign_splits(clean_dataset(vehicles=80), SplitConfig(seed="alpha"))
        second = assign_splits(clean_dataset(vehicles=80), SplitConfig(seed="beta"))
        assert [r.split for r in first] != [r.split for r in second]

    def test_assignment_does_not_depend_on_record_order(self):
        """Guards the specific failure the hashing design exists to prevent:
        a split that depends on dict/set iteration order or on the order records
        happen to appear in the file."""
        forward = clean_dataset(vehicles=50)
        backward = list(reversed(clean_dataset(vehicles=50)))
        assign_splits(forward)
        assign_splits(backward)
        forward_map = {r.image_id: r.split for r in forward}
        backward_map = {r.image_id: r.split for r in backward}
        assert forward_map == backward_map

    def test_adding_vehicles_does_not_move_existing_ones(self):
        """Growth stability. With shuffling, appending one record reshuffles
        everything and the frozen test set silently changes — while still
        looking like the same test set."""
        small = assign_splits(clean_dataset(vehicles=40))
        before = {r.vehicle_id: r.split for r in small}
        large = assign_splits(clean_dataset(vehicles=120))
        after = {r.vehicle_id: r.split for r in large}
        for vehicle, split in before.items():
            assert after[vehicle] == split, f"{vehicle} moved from {split} to {after[vehicle]}"

    def test_assignment_is_pure_function_of_identity(self):
        config = SplitConfig(seed="x")
        assert assign_identity("vehicle_001", config) == assign_identity("vehicle_001", config)

    def test_ratios_must_sum_to_one(self):
        with pytest.raises(ValueError):
            SplitConfig(train=0.8, val=0.3, test=0.1)

    def test_negative_ratios_are_refused(self):
        with pytest.raises(ValueError):
            SplitConfig(train=1.2, val=-0.1, test=-0.1)


class TestCiLeakageGate:
    """The permanent regression test required by the M1 brief."""

    def test_cross_split_vehicle_FAILS_the_build(self):
        records = dataset_with_leakage()
        leakage = find_identity_leakage(records)
        assert leakage == {"vehicle_001": {"train", "test"}}

        findings = check_dataset(records)
        assert has_blocking_errors(findings)
        leak_errors = [f for f in findings if f.code == "cross_split_identity_leakage"]
        assert len(leak_errors) == 1
        assert "vehicle_001" in leak_errors[0].message
        assert "test" in leak_errors[0].message and "train" in leak_errors[0].message

    def test_distinct_vehicles_in_distinct_splits_PASSES(self):
        records = dataset_without_leakage()
        assert find_identity_leakage(records) == {}
        findings = check_dataset(records)
        assert [f for f in findings if f.code == "cross_split_identity_leakage"] == []
        assert not has_blocking_errors(findings), [str(f) for f in findings if f.severity == ERROR]


class TestImageLeakage:
    def test_the_same_image_in_two_splits_is_an_error(self):
        """Distinct from identity leakage: one frame can hold two vehicles, and
        if they hash into different splits the same PIXELS are in both."""
        records = [
            make_record("vehicle_A", 0, "GJ05AB1234", split="train"),
            make_record("vehicle_B", 0, "MH12CD5678", split="test"),
        ]
        records[1].image_id = records[0].image_id  # one frame, two vehicles
        assert find_image_leakage(records) == {records[0].image_id: {"train", "test"}}
        assert any(f.code == "cross_split_image_leakage" for f in check_dataset(records))


class TestRepeatedPlateText:
    def test_repeated_text_is_reported_but_does_not_fail_the_build(self):
        """A repeated registration is usually one vehicle given two ids — but
        not always. Auto-failing would delete genuine data; ignoring it would
        hide a real identity bug. So: WARNING, and a human decides."""
        records = [
            make_record("vehicle_A", 0, "GJ05AB1234", split="train"),
            make_record("vehicle_B", 0, "GJ05AB1234", split="test"),
        ]
        assert find_repeated_plate_text(records) == {"GJ05AB1234": {"vehicle_A", "vehicle_B"}}
        findings = check_dataset(records)
        repeated = [f for f in findings if f.code == "repeated_plate_text"]
        assert len(repeated) == 1
        assert repeated[0].severity == "WARNING"


class TestSplitReport:
    def test_report_counts_distinct_units_separately(self):
        """unique vehicles, records, plates, images and cameras are different
        numbers, and conflating them is how dataset size gets inflated."""
        records = assign_splits(clean_dataset(vehicles=40, frames_per_vehicle=3))
        report = plan_split(records)
        assert sum(report.unique_identities.values()) == 40
        assert sum(report.records.values()) == 120
        assert sum(report.images.values()) == 120

    def test_report_renders_markdown(self):
        report = plan_split(assign_splits(clean_dataset(vehicles=20)))
        rendered = report.render()
        assert "# Split report" in rendered
        assert "| train |" in rendered
        assert "Plate-size distribution by split" in rendered
