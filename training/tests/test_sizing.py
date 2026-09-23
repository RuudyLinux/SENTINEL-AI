"""CCTV plate-size measurement (M1.5).

All synthetic. No footage, no OpenCV, no detector, no network — the statistics
core is deliberately separated from the video I/O so it can be verified in full
before any authorized footage exists.
"""
import pytest

from schema import (
    GLYPH_HEIGHT_FRACTION_DOUBLE_ROW, GLYPH_HEIGHT_FRACTION_SINGLE_ROW, estimate_glyph_px,
)
from sizing import (
    SOURCE_DETECTOR, SOURCE_GROUND_TRUTH, UNKNOWN, PlateObservation, aggregate,
    frame_indices, group_by, model_implication, percentile, percentile_summary,
    render_report, vehicle_weighted,
)


def observation(height=60.0, width=None, **overrides):
    """One synthetic observation. Height drives the glyph estimate, so it is the
    knob these tests turn."""
    width = width if width is not None else height * 4
    fields = dict(
        camera_id="C-001", frame_index=0, frame_width=1920, frame_height=1080,
        bbox=(100.0, 200.0, 100.0 + width, 200.0 + height),
    )
    fields.update(overrides)
    return PlateObservation(**fields)


class TestGeometry:
    def test_dimensions_and_aspect(self):
        obs = observation(height=60.0, width=240.0)
        assert obs.width == 240.0 and obs.height == 60.0
        assert obs.aspect_ratio == 4.0

    def test_zero_height_does_not_divide_by_zero(self):
        assert observation(height=0.0, width=100.0).aspect_ratio == 0.0

    def test_frame_area_fraction(self):
        obs = observation(height=100.0, width=200.0, frame_width=1000, frame_height=1000)
        assert obs.frame_area_fraction == pytest.approx(0.02)

    def test_vehicle_area_fraction_when_the_vehicle_box_is_known(self):
        obs = observation(height=50.0, width=200.0, vehicle_bbox=(0.0, 0.0, 400.0, 400.0))
        assert obs.vehicle_area_fraction == pytest.approx(10000 / 160000)

    def test_vehicle_fraction_is_none_rather_than_substituted(self):
        """Never silently replaced with the frame fraction — they are different
        measurements and conflating them would misreport both."""
        assert observation().vehicle_area_fraction is None


class TestGlyphEstimation:
    def test_single_row_uses_55_percent(self):
        assert estimate_glyph_px(100.0, "single") == pytest.approx(55.0)
        assert GLYPH_HEIGHT_FRACTION_SINGLE_ROW == 0.55

    def test_two_row_uses_27_point_5_percent(self):
        """A two-row plate stacks two character bands into the same box, so each
        band is about half a single-row plate's. Motorcycles are almost always
        two-row and are the smallest plates on the road — treating them as
        single-row would place the worst-case class one or two buckets too high
        and make CCTV legibility look better than it is."""
        assert estimate_glyph_px(100.0, "double") == pytest.approx(27.5)
        assert GLYPH_HEIGHT_FRACTION_DOUBLE_ROW == 0.275

    def test_a_two_row_plate_lands_in_a_smaller_bucket_than_a_single_row_one(self):
        single = observation(height=100.0, row_layout="single")
        double = observation(height=100.0, row_layout="double")
        assert single.bucket == "50-75px"
        assert double.bucket == "20-30px"

    def test_a_measured_glyph_height_overrides_the_estimate(self):
        obs = observation(height=100.0, measured_glyph_px=42.0)
        assert obs.glyph_px == 42.0
        assert obs.is_estimated_glyph is False

    def test_an_estimated_glyph_is_flagged_as_such(self):
        assert observation().is_estimated_glyph is True


class TestSizeBuckets:
    @pytest.mark.parametrize("height,expected", [
        (30.0, "<20px"),     # 16.5px glyph
        (45.0, "20-30px"),   # 24.75
        (80.0, "30-50px"),   # 44
        (120.0, "50-75px"),  # 66
        (170.0, "75-100px"), # 93.5
        (400.0, ">100px"),   # 220
    ])
    def test_bucket_assignment(self, height, expected):
        assert observation(height=height).bucket == expected

    def test_sub_20px_plates_are_counted_not_discarded(self):
        """They are the operational case for an explicit INSUFFICIENT_RESOLUTION
        state, so they must survive into the report."""
        distribution = aggregate([observation(height=20.0) for _ in range(5)])
        assert distribution.counts["<20px"] == 5
        assert distribution.total == 5


class TestPercentiles:
    def test_known_values(self):
        values = [float(v) for v in range(1, 101)]
        assert percentile(values, 50) == pytest.approx(50.5)
        assert percentile(values, 10) == pytest.approx(10.9)
        assert percentile(values, 95) == pytest.approx(95.05)

    def test_single_value(self):
        assert percentile([42.0], 50) == 42.0
        assert percentile([42.0], 95) == 42.0

    def test_empty_is_zero_not_an_error(self):
        assert percentile([], 50) == 0.0

    def test_interpolates_between_neighbours(self):
        assert percentile([0.0, 10.0], 50) == pytest.approx(5.0)

    def test_summary_reports_every_requested_point(self):
        summary = percentile_summary([float(v) for v in range(1, 101)])
        for key in ("p10", "p25", "p50", "p75", "p90", "p95", "min", "max", "mean", "n"):
            assert key in summary
        assert summary["n"] == 100 and summary["min"] == 1.0 and summary["max"] == 100.0

    def test_summary_of_nothing_is_zeroed_not_fabricated(self):
        summary = percentile_summary([])
        assert summary["n"] == 0 and summary["p50"] == 0.0


class TestVehicleWeightedSampling:
    def test_a_long_dwelling_vehicle_is_capped(self):
        """The headline bias control: one vehicle stationary for 200 frames must
        not contribute 200 observations and describe itself rather than the
        traffic."""
        observations = [observation(frame_index=i, track_id="v1") for i in range(200)]
        assert len(vehicle_weighted(observations, max_per_track=3)) == 3

    def test_vehicles_below_the_cap_are_untouched(self):
        observations = [observation(frame_index=i, track_id="v1") for i in range(2)]
        assert len(vehicle_weighted(observations, max_per_track=3)) == 2

    def test_every_vehicle_survives_the_cap(self):
        observations = [
            observation(frame_index=i, track_id=f"v{v}")
            for v in range(10) for i in range(20)
        ]
        kept = vehicle_weighted(observations, max_per_track=2)
        assert len({o.track_id for o in kept}) == 10
        assert len(kept) == 20

    def test_samples_are_spread_across_the_track_not_taken_from_the_start(self):
        """The first N frames of a track are its entry into the scene, all at a
        similar distance; taking those would bias the size distribution toward
        whatever size a vehicle is when it first appears."""
        observations = [observation(frame_index=i, track_id="v1") for i in range(100)]
        indices = sorted(o.frame_index for o in vehicle_weighted(observations, max_per_track=3))
        assert indices[0] == 0 and indices[-1] == 99
        assert indices[1] > 10, "middle sample must not be adjacent to the first"

    def test_a_zero_cap_disables_capping(self):
        observations = [observation(frame_index=i, track_id="v1") for i in range(50)]
        assert len(vehicle_weighted(observations, max_per_track=0)) == 50

    def test_sampling_is_deterministic(self):
        observations = [observation(frame_index=i, track_id="v1") for i in range(50)]
        first = [o.frame_index for o in vehicle_weighted(observations, 5)]
        second = [o.frame_index for o in vehicle_weighted(observations, 5)]
        assert first == second

    def test_observations_without_a_track_id_are_not_merged(self):
        """No tracker means every detection is its own observation; they must
        not all collapse into one pseudo-track."""
        observations = [observation(frame_index=i, track_id="") for i in range(10)]
        assert len(vehicle_weighted(observations, max_per_track=1)) == 10


class TestWeightingChangesTheAnswer:
    def test_frame_weighting_is_dominated_by_a_stationary_vehicle(self):
        """The exact bias this tooling exists to expose: one large, stationary
        plate versus many small moving ones."""
        stationary = [observation(height=400.0, frame_index=i, track_id="parked")
                      for i in range(100)]
        passing = [observation(height=40.0, frame_index=i, track_id=f"pass{i}")
                   for i in range(10)]
        everything = stationary + passing

        frame_weighted = aggregate(everything).percentages()
        weighted = aggregate(vehicle_weighted(everything, 3)).percentages()

        assert frame_weighted[">100px"] > 80, "frame-weighted is dominated by the parked car"
        assert weighted["20-30px"] > weighted[">100px"], (
            "vehicle-weighted must reflect the passing traffic, not the parked car"
        )


class TestFrameSampling:
    def test_interval_sampling_respects_the_requested_rate(self):
        indices = frame_indices(total_frames=1000, fps=25.0, sample_fps=5.0)
        assert indices[:3] == [0, 5, 10]

    def test_max_frames_thins_evenly_rather_than_truncating(self):
        """Truncating would silently restrict the measurement to the beginning
        of the footage — which is a time of day, a light level, and possibly one
        traffic phase."""
        indices = frame_indices(total_frames=10000, fps=25.0, sample_fps=25.0, max_frames=10)
        assert len(indices) == 10
        assert indices[-1] > 8000, "the cap must still reach the end of the footage"

    def test_max_seconds_bounds_the_window(self):
        indices = frame_indices(total_frames=10000, fps=25.0, sample_fps=25.0, max_seconds=4.0)
        assert max(indices) < 100

    def test_empty_footage(self):
        assert frame_indices(0, 25.0, 5.0) == []

    def test_zero_fps_does_not_divide_by_zero(self):
        assert frame_indices(100, 0.0, 5.0) == list(range(100))


class TestGrouping:
    def test_grouping_by_camera(self):
        observations = [observation(camera_id="C-001"), observation(camera_id="C-002")]
        groups = group_by(observations, "camera_id")
        assert set(groups) == {"C-001", "C-002"}
        assert groups["C-001"].total == 1

    def test_grouping_by_time_of_day(self):
        observations = [observation(time_of_day="day"), observation(time_of_day="night")]
        assert set(group_by(observations, "time_of_day")) == {"day", "night"}

    def test_missing_metadata_becomes_unknown_never_a_guess(self):
        assert set(group_by([observation()], "time_of_day")) == {UNKNOWN}

    def test_unique_vehicles_counted_separately_from_observations(self):
        observations = [observation(frame_index=i, track_id="v1") for i in range(5)]
        distribution = aggregate(observations)
        assert distribution.total == 5
        assert distribution.unique_vehicles == 1


class TestModelImplication:
    def _distribution(self, heights):
        return aggregate([observation(height=h) for h in heights])

    def test_scenario_A_when_plates_are_large(self):
        implication = model_implication(self._distribution([200.0] * 10))
        assert implication.scenario == "A"
        assert implication.over_50 >= 50.0

    def test_scenario_B_in_the_small_plate_regime(self):
        # 44px and 24.75px glyphs -> 30-50px and 20-30px bands
        implication = model_implication(self._distribution([80.0] * 6 + [45.0] * 4))
        assert implication.scenario == "B"

    def test_scenario_C_when_most_plates_are_below_20px(self):
        implication = model_implication(self._distribution([20.0] * 9 + [400.0]))
        assert implication.scenario == "C"
        assert "INSUFFICIENT_RESOLUTION" in implication.detail
        assert "capture" in implication.detail.lower()

    def test_bands_are_reported_separately_and_sum_sensibly(self):
        implication = model_implication(self._distribution([20.0, 45.0, 80.0, 200.0]))
        assert implication.under_30 == pytest.approx(
            implication.under_20 + implication.band_20_30)
        total = implication.under_30 + implication.band_30_50 + implication.over_50
        assert total == pytest.approx(100.0, abs=0.1)


class TestReport:
    def test_no_observations_reports_nothing_rather_than_zeros(self):
        report = render_report([])
        assert "No observations" in report
        assert "No statistics are reported" in report

    def test_detector_provenance_is_stated_prominently(self):
        """Detector boxes are not ground truth — mean IoU 0.136 on the labelled
        benchmark — and a report that did not say so would be misleading."""
        report = render_report([observation(source=SOURCE_DETECTOR)])
        assert "DETECTOR-ESTIMATED" in report
        assert "0.136" in report

    def test_ground_truth_provenance_is_stated_differently(self):
        report = render_report([observation(source=SOURCE_GROUND_TRUTH)])
        assert "human-annotated ground-truth boxes" in report
        assert "DETECTOR-ESTIMATED" not in report

    def test_estimated_glyph_heights_are_disclosed(self):
        report = render_report([observation()])
        assert "ESTIMATED" in report and "27.5%" in report

    def test_both_weightings_are_shown(self):
        observations = [observation(frame_index=i, track_id="v1") for i in range(20)]
        report = render_report(observations)
        assert "frame-weighted" in report and "vehicle-weighted" in report
        assert "Largest shift between weightings" in report

    def test_percentiles_appear(self):
        report = render_report([observation(height=h) for h in range(30, 200, 10)])
        for point in ("p10", "p25", "p50", "p75", "p90", "p95"):
            assert point in report

    def test_per_camera_section_is_produced(self):
        report = render_report([observation(camera_id="C-001"), observation(camera_id="C-002")])
        assert "## Per camera" in report
        assert "C-001" in report and "C-002" in report

    def test_absent_metadata_is_declared_not_invented(self):
        report = render_report([observation()])
        assert "Not reported — the footage carried no such metadata." in report

    def test_the_scenario_recommendation_is_included(self):
        report = render_report([observation(height=200.0) for _ in range(5)])
        assert "Model-selection implications" in report
        assert "Scenario A" in report

    def test_sub_20px_retention_is_stated(self):
        report = render_report([observation(height=20.0)])
        assert "INSUFFICIENT_RESOLUTION" in report

    def test_report_contains_no_plate_text_field(self):
        """This tool measures geometry. It never reads, stores or prints plate
        text — which keeps it usable at a lower privacy tier than annotation."""
        assert not hasattr(observation(), "plate_text")
