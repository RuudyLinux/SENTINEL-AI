"""QC checks and reports.

The governing rule under test: **unusual plate strings are WARNING, never
ERROR.** Auto-rejecting labels that fail the Indian registration format would
delete exactly the non-standard plates Phase 2 measured as 5 of 10 recognition
failures, and would restrict the corpus to plates the system already reads.
"""
import pytest

from fixtures import (
    clean_dataset, make_record, qc_problem_records, rare_character_dataset,
    size_bucket_dataset,
)
from qc.checks import (
    ERROR, INFO, WARNING, check_dataset, check_record, errors, has_blocking_errors, run_all,
)
from qc.report import (
    character_coverage, render_character_coverage, render_qc_report,
    render_size_distribution, size_distribution,
)
from split import assign_splits


def codes(findings):
    return {f.code for f in findings}


class TestPerRecordChecks:
    @pytest.mark.parametrize("code", sorted(qc_problem_records()))
    def test_each_intentional_problem_is_detected(self, code):
        record = qc_problem_records()[code]
        assert code in codes(check_record(record)), (
            f"{code} was not reported for its own fixture"
        )

    def test_a_clean_record_produces_no_errors(self):
        assert errors(check_record(make_record("vehicle_001", split="train"))) == []


class TestSeverityOfUnusualPlates:
    """The distinction the whole QC design turns on."""

    @pytest.mark.parametrize("text", ["KL34F", "KL498262", "QQ00QQ0000", "XX12AB1234"])
    def test_an_unusual_registration_is_a_warning_not_an_error(self, text):
        findings = check_record(make_record("v", plate_text=text))
        assert not has_blocking_errors(findings), (
            f"{text} must not BLOCK the dataset — it is a valid annotation of an unusual plate"
        )

    def test_the_unusual_format_flag_is_raised_for_review(self):
        findings = check_record(make_record("v", plate_text="KL34F"))
        flagged = [f for f in findings if f.code == "unusual_registration_format"]
        assert len(flagged) == 1 and flagged[0].severity == WARNING

    def test_a_well_formed_registration_is_not_flagged(self):
        assert "unusual_registration_format" not in codes(
            check_record(make_record("v", plate_text="GJ05AB1234")))

    def test_bh_series_is_not_flagged_as_unusual(self):
        assert "unusual_registration_format" not in codes(
            check_record(make_record("v", plate_text="23BH1234AA")))

    def test_illegal_CHARACTERS_are_still_an_error(self):
        """Unusual is fine; a transcription containing punctuation is a
        normalization bug and must block."""
        findings = check_record(make_record("v", plate_text="GJ05-AB 1234"))
        assert has_blocking_errors(findings)
        assert "illegal_characters" in codes(findings)

    def test_lowercase_is_an_error(self):
        assert "illegal_characters" in codes(check_record(make_record("v", plate_text="gj05ab1234")))


class TestUnreadablePlates:
    def test_a_deliberately_unreadable_plate_is_info_not_an_error(self):
        """Kept on purpose: it trains the detector and is needed to measure
        honest rejection."""
        findings = check_record(make_record("v", plate_text="", quality="unreadable"))
        assert not has_blocking_errors(findings)
        assert "unreadable_plate" in codes(findings)
        assert [f.severity for f in findings if f.code == "unreadable_plate"] == [INFO]

    def test_an_empty_label_without_the_unreadable_flag_is_a_warning(self):
        findings = check_record(make_record("v", plate_text="", quality="clear"))
        assert "empty_plate_text" in codes(findings)


class TestTestSetHygiene:
    def test_an_uncertain_label_in_test_is_an_error(self):
        findings = check_record(make_record("v", split="test", label_confidence="uncertain"))
        assert "uncertain_label_in_test" in codes(findings)
        assert has_blocking_errors(findings)

    def test_an_uncertain_label_in_train_is_allowed(self):
        findings = check_record(make_record("v", split="train", label_confidence="uncertain"))
        assert not has_blocking_errors(findings)

    def test_a_partial_plate_in_test_is_warned_about(self):
        findings = check_record(make_record("v", split="test", visibility="partial"))
        assert "partial_plate_in_test" in codes(findings)


class TestDatasetChecks:
    def test_a_clean_corpus_has_no_blocking_errors(self):
        records = assign_splits(clean_dataset(vehicles=40, frames_per_vehicle=3))
        findings = check_dataset(records)
        assert not has_blocking_errors(findings), [str(f) for f in errors(findings)]

    def test_duplicate_records_for_one_vehicle_are_an_error(self):
        duplicate = make_record("vehicle_001", 0, split="train")
        findings = check_dataset([duplicate, duplicate])
        assert "duplicate_record" in codes(findings) or "duplicate_annotation" in codes(findings)
        assert has_blocking_errors(findings)

    def test_two_vehicles_in_one_frame_is_info_not_an_error(self):
        """A legitimate case that a naive duplicate-image check would reject."""
        first = make_record("vehicle_A", 0, "GJ05AB1234", split="train")
        second = make_record("vehicle_B", 0, "MH12CD5678", split="train")
        second.image_id = first.image_id
        second.plate_bbox = [500.0, 200.0, 720.0, 260.0]
        findings = check_dataset([first, second])
        assert "multi_vehicle_frame" in codes(findings)
        assert not has_blocking_errors(findings), [str(f) for f in errors(findings)]

    def test_identical_geometry_across_frames_is_flagged_as_near_duplicate(self):
        records = [make_record("vehicle_001", frame, split="train") for frame in range(5)]
        assert "suspicious_duplicate_frames" in codes(check_dataset(records))

    def test_an_empty_dataset_is_an_error(self):
        assert "empty_dataset" in codes(check_dataset([]))

    def test_unassigned_records_are_warned_about(self):
        assert "unassigned_records" in codes(check_dataset([make_record("v", split="")]))


class TestRunAll:
    def test_parse_errors_are_surfaced_as_blocking(self):
        findings = run_all([make_record("v", split="train")], parse_errors=[(7, "malformed JSON")])
        assert "malformed_line" in codes(findings)
        assert has_blocking_errors(findings)


class TestCharacterCoverage:
    def test_missing_characters_are_identified(self):
        """Reproduces the exact gap measured in the n=25 benchmark corpus."""
        coverage = character_coverage(rare_character_dataset())
        for character in "IOQVZ":
            assert coverage[character]["occurrences"] == 0

    def test_counts_are_split_aware(self):
        records = assign_splits(clean_dataset(vehicles=60, frames_per_vehicle=2))
        coverage = character_coverage(records)
        for row in coverage.values():
            assert row["occurrences"] >= row["train"] or row["occurrences"] == 0

    def test_unique_plates_and_vehicles_are_counted_separately(self):
        """Three frames of one vehicle is one vehicle, not three."""
        records = [make_record("vehicle_001", f, "GJ05AB1234", split="train") for f in range(3)]
        coverage = character_coverage(records)
        assert coverage["G"]["unique_vehicles"] == 1
        assert coverage["G"]["unique_plates"] == 1
        assert coverage["G"]["occurrences"] == 3

    def test_the_report_shouts_about_zero_coverage(self):
        rendered = render_character_coverage(rare_character_dataset())
        assert "ZERO COVERAGE" in rendered
        for character in "IOQVZ":
            assert character in rendered

    def test_the_report_renders_all_36_classes(self):
        rendered = render_character_coverage(clean_dataset(vehicles=20))
        for character in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            assert f"| {character} |" in rendered


class TestSizeDistribution:
    def test_every_bucket_is_populated_by_the_fixture(self):
        distribution = size_distribution(size_bucket_dataset())
        for name in ("<20px", "20-30px", "30-50px", "50-75px", "75-100px", ">100px"):
            assert distribution[name]["count"] == 1, f"{name} not populated"

    def test_accuracy_is_absent_until_predictions_exist(self):
        """No fabricated numbers: without predictions there is no accuracy."""
        distribution = size_distribution(size_bucket_dataset())
        assert all(entry["exact_match"] is None for entry in distribution.values())

    def test_accuracy_is_computed_per_bucket_when_predictions_are_supplied(self):
        records = size_bucket_dataset()
        predictions = {f"{r.image_id}/{r.vehicle_id}": r.plate_text for r in records}
        distribution = size_distribution(records, predictions)
        for entry in distribution.values():
            assert entry["exact_match"] == 1.0

    def test_a_wrong_prediction_scores_zero_in_its_bucket(self):
        records = size_bucket_dataset()
        predictions = {f"{r.image_id}/{r.vehicle_id}": "WRONG1234" for r in records}
        distribution = size_distribution(records, predictions)
        for entry in distribution.values():
            assert entry["exact_match"] == 0.0

    def test_the_report_renders(self):
        rendered = render_size_distribution(size_bucket_dataset())
        assert "# Plate-size distribution" in rendered
        assert "<20px" in rendered


class TestQcReportRendering:
    def test_a_blocked_dataset_says_so(self):
        rendered = render_qc_report(run_all(list(qc_problem_records().values())))
        assert "BLOCKED" in rendered

    def test_a_clean_dataset_says_so(self):
        records = assign_splits(clean_dataset(vehicles=30, frames_per_vehicle=2))
        rendered = render_qc_report(run_all(records))
        assert "No blocking errors" in rendered
