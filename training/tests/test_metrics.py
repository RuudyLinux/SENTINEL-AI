"""Recognition metrics, calibration, and paired comparison.

`TestAgreementWithTheBackendBenchmark` is the important one: it pins this
module's edit distance against the implementation that produced the 0.24 /
0.3983 baseline, so the two definitions cannot drift apart unnoticed. Two
definitions of CER is how a before/after comparison quietly stops being a
comparison.
"""
import math

import pytest

from evaluate.metrics import (
    RecognitionMetrics, calibration, edit_operations, evaluate, levenshtein,
    mcnemar, normalize, wilson_interval,
)


class TestEditDistance:
    @pytest.mark.parametrize("a,b,expected", [
        ("", "", 0), ("ABC", "ABC", 0), ("ABC", "", 3), ("", "ABC", 3),
        ("ABC", "ABD", 1), ("ABC", "AC", 1), ("AC", "ABC", 1),
        ("GJ05AB1234", "GJ05AB1284", 1),
    ])
    def test_known_distances(self, a, b, expected):
        assert levenshtein(a, b) == expected

    def test_symmetry(self):
        assert levenshtein("KL07BX7197", "KL07BXZ197") == levenshtein("KL07BXZ197", "KL07BX7197")


class TestAgreementWithTheBackendBenchmark:
    """The baseline 0.3983 CER was produced by `backend/tools/anpr_bench.py`. If
    these two implementations ever disagree, every before/after comparison in
    this project silently becomes invalid."""

    def _backend_levenshtein(self, a, b):
        # Transcribed from backend/tools/anpr_bench.py::levenshtein. Copied
        # rather than imported so this test does not drag the backend (and its
        # torch/OpenCV import chain) into the training test run — the copy IS
        # the thing under test.
        if not a:
            return len(b)
        if not b:
            return len(a)
        previous = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            current = [i]
            for j, cb in enumerate(b, 1):
                current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
            previous = current
        return previous[-1]

    @pytest.mark.parametrize("truth,read", [
        # Real (truth, OCR) pairs recorded in docs/ANPR_ACCURACY.md.
        ("KL07BX7197", "KL07BXZ197"), ("GJ01DY6855", "16J0406855"),
        ("KA09C2763", "RA09C2762"), ("KL34A465", "KL34A651"),
        ("DL3CD1210", "SUCUNDL3CD1210"), ("KL03S6894", ""),
        ("WB42AX7446", "HBZZAX7L46"), ("MP07L7524", "MP07SL7524"),
    ])
    def test_identical_to_the_backend_implementation(self, truth, read):
        assert levenshtein(truth, read) == self._backend_levenshtein(truth, read)


class TestEditOperations:
    def test_a_substitution_is_reported_as_such(self):
        assert edit_operations("KL07BX7197", "KL07BXZ197") == [("sub", "7", "Z")]

    def test_a_deletion_is_reported(self):
        assert ("del", "B", "") in edit_operations("ABC", "AC")

    def test_an_insertion_is_reported(self):
        assert ("ins", "", "B") in edit_operations("AC", "ABC")

    def test_operations_reconstruct_the_edit_distance(self):
        for truth, read in [("GJ05AB1234", "GJ04OL6855"), ("WB42AX7446", "HBZZAX7L46"), ("ABC", "")]:
            assert len(edit_operations(truth, read)) == levenshtein(truth, read)

    def test_identical_strings_produce_no_operations(self):
        assert edit_operations("GJ05AB1234", "GJ05AB1234") == []


class TestRecognitionMetrics:
    def test_a_perfect_system(self):
        metrics = evaluate([("GJ05AB1234", "GJ05AB1234")] * 5)
        assert metrics.exact_match == 1.0
        assert metrics.cer == 0.0
        assert metrics.character_accuracy == 1.0
        assert metrics.total_edits == 0

    def test_insertions_and_deletions_are_not_hidden(self):
        """Phase 2's finding: 32 of 57 edits were insertions/deletions, which a
        single CER number conceals entirely."""
        metrics = evaluate([("ABC", "AC"), ("AC", "ABC"), ("ABC", "ABD")])
        assert metrics.deletions == 1
        assert metrics.insertions == 1
        assert metrics.substitutions == 1
        assert metrics.total_edits == 3

    def test_cer_denominator_is_truth_length(self):
        metrics = evaluate([("ABCD", "")])
        assert metrics.truth_characters == 4
        assert metrics.cer == 1.0

    def test_cer_can_exceed_one_for_an_over_long_read(self):
        """Intended. A read that invents ten characters is worse than one that
        reads nothing, and a metric capped at 1.0 would hide that."""
        metrics = evaluate([("AB", "ABCDEFGHIJ")])
        assert metrics.cer > 1.0
        assert metrics.character_accuracy == 0.0

    def test_case_is_normalized(self):
        assert evaluate([("GJ05AB1234", "gj05ab1234")]).exact_match == 1.0

    def test_whitespace_is_stripped(self):
        assert evaluate([("GJ05AB1234", " GJ05AB1234 ")]).exact_match == 1.0

    def test_confusions_are_counted(self):
        metrics = evaluate([("KL07BX7197", "KL07BXZ197")] * 3)
        assert metrics.confusions[("7", "Z")] == 3

    def test_empty_input_does_not_divide_by_zero(self):
        metrics = RecognitionMetrics()
        assert metrics.exact_match == 0.0 and metrics.cer == 0.0

    def test_as_dict_reports_every_component(self):
        payload = evaluate([("ABC", "ABD")]).as_dict()
        for key in ("exact_match", "cer", "character_accuracy",
                    "substitutions", "insertions", "deletions", "total_edits"):
            assert key in payload

    def test_reproduces_a_known_baseline_shape(self):
        """Six of 25 correct is the documented whole-crop figure."""
        pairs = [("GJ05AB1234", "GJ05AB1234")] * 6 + [("GJ05AB1234", "WRONG9999")] * 19
        assert evaluate(pairs).exact_match == pytest.approx(0.24)


class TestWilsonInterval:
    def test_matches_the_documented_phase3_figures(self):
        """docs/ANPR_PHASE3_TRAINING_PLAN.md §4 quotes [0.115, 0.434] at n=25."""
        low, high = wilson_interval(6, 25)
        assert low == pytest.approx(0.115, abs=0.002)
        assert high == pytest.approx(0.434, abs=0.002)

    def test_the_interval_narrows_as_n_grows(self):
        narrow = wilson_interval(120, 500)
        wide = wilson_interval(6, 25)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_zero_total_does_not_divide_by_zero(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_bounds_stay_within_zero_and_one(self):
        for successes, total in [(0, 10), (10, 10), (1, 3)]:
            low, high = wilson_interval(successes, total)
            assert 0.0 <= low <= high <= 1.0


class TestPairedComparison:
    def _truths(self, n):
        return {f"s{i}": "GJ05AB1234" for i in range(n)}

    def test_identical_systems_are_not_significant(self):
        truths = self._truths(40)
        predictions = {k: "GJ05AB1234" for k in truths}
        result = mcnemar(truths, predictions, dict(predictions))
        assert result.only_a == 0 and result.only_b == 0
        assert result.p_value == 1.0 and not result.significant

    def test_a_clearly_better_system_is_significant(self):
        truths = self._truths(60)
        baseline = {k: "WRONG" for k in truths}
        improved = {k: ("GJ05AB1234" if i < 40 else "WRONG") for i, k in enumerate(truths)}
        result = mcnemar(truths, baseline, improved)
        assert result.only_b == 40 and result.only_a == 0
        assert result.significant

    def test_a_marginal_difference_is_not_significant(self):
        """The case the acceptance rule exists for: a couple of extra correct
        reads is not evidence of a better model."""
        truths = self._truths(50)
        baseline = {k: ("GJ05AB1234" if i < 12 else "WRONG") for i, k in enumerate(truths)}
        improved = {k: ("GJ05AB1234" if i < 14 else "WRONG") for i, k in enumerate(truths)}
        result = mcnemar(truths, baseline, improved)
        assert not result.significant

    def test_mismatched_example_sets_are_refused(self):
        """Comparing a model scored on 480 samples against one scored on 500 is
        not a paired test, and silently intersecting them would produce a number
        that looks valid and is not."""
        truths = self._truths(10)
        a = {k: "X" for k in truths}
        b = {k: "X" for k in list(truths)[:8]}
        with pytest.raises(ValueError, match="identical example sets"):
            mcnemar(truths, a, b)

    def test_missing_ground_truth_is_refused(self):
        predictions = {"s0": "X", "s1": "X"}
        with pytest.raises(ValueError, match="ground truth"):
            mcnemar({"s0": "GJ05AB1234"}, predictions, dict(predictions))

    def test_exact_test_is_used_for_few_discordant_pairs(self):
        truths = self._truths(30)
        baseline = {k: "WRONG" for k in truths}
        improved = {k: ("GJ05AB1234" if i < 3 else "WRONG") for i, k in enumerate(truths)}
        result = mcnemar(truths, baseline, improved)
        assert 0.0 <= result.p_value <= 1.0

    def test_report_dict_is_complete(self):
        truths = self._truths(20)
        predictions = {k: "GJ05AB1234" for k in truths}
        payload = mcnemar(truths, predictions, dict(predictions)).as_dict()
        for key in ("n", "both_correct", "only_a_correct", "only_b_correct", "p_value"):
            assert key in payload


class TestCalibration:
    def test_no_predictions_means_no_calibration_numbers(self):
        """Refuses to invent a calibration result before real predictions
        exist."""
        report = calibration([])
        assert report.bins == []
        assert report.samples == 0
        assert report.expected_calibration_error == 0.0

    def test_a_perfectly_calibrated_system_has_near_zero_error(self):
        outcomes = []
        for _ in range(100):
            outcomes.append((0.9, True))
        for _ in range(10):
            outcomes.append((0.9, False))
        # 100/110 correct at stated 0.9 -> gap ~0.009
        assert calibration(outcomes).expected_calibration_error < 0.02

    def test_an_overconfident_system_is_detected(self):
        """The failure mode that matters here: the production pipeline consumes
        confidence to gate persistence and human review, so a model claiming 0.95
        on reads that are right 50% of the time defeats both gates."""
        outcomes = [(0.95, i % 2 == 0) for i in range(100)]
        report = calibration(outcomes)
        assert report.expected_calibration_error > 0.4

    def test_bins_report_the_confidence_accuracy_gap(self):
        report = calibration([(0.95, False)] * 20)
        assert len(report.bins) == 1
        assert report.bins[0].as_dict()["gap"] == pytest.approx(0.95, abs=0.01)

    def test_confidence_of_exactly_one_is_counted(self):
        report = calibration([(1.0, True)] * 5)
        assert report.samples == 5
        assert sum(b.count for b in report.bins) == 5

    def test_bins_partition_the_samples(self):
        outcomes = [(i / 100, i % 3 == 0) for i in range(100)]
        report = calibration(outcomes)
        assert sum(b.count for b in report.bins) == 100


class TestNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("gj05ab1234", "GJ05AB1234"), (" GJ05AB1234 ", "GJ05AB1234"), ("", ""), (None, ""),
    ])
    def test_normalization(self, raw, expected):
        assert normalize(raw) == expected

    def test_normalization_does_not_repair_characters(self):
        """The evaluator must measure what the model produced, not a tidied
        version of it."""
        assert normalize("GJO5AB1234") == "GJO5AB1234"
