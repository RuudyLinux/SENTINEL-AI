"""V2 Phase 3 — explainable risk score.

The contract these lock down is explainability, not a particular number: the
score must be reproducible, bounded, and fully attributable to named factors.
An opaque score would be untestable, which is precisely why this one is not
machine-learned.
"""
from datetime import datetime

import pytest

from app.pipeline import risk


def test_no_signals_scores_zero_with_no_invented_reasons():
    assessment = risk.assess(risk.RiskSignals())
    assert assessment.score == 0
    assert assessment.severity == "LOW"
    assert assessment.factors == []


def test_every_point_is_attributable_to_a_named_factor():
    """The whole justification for a hand-weighted score: the total must equal
    the sum of its stated reasons, or the explanation is a decoration."""
    assessment = risk.assess(risk.RiskSignals(
        watchlist_priority="CRITICAL", plate_text="GJ05AB1234",
        zone_severity="HIGH", zone_name="Secure Yard", camera_code="C-014",
        plate_confidence=0.94, plate_reads=4, cameras_visited=4,
        total_sightings=6, at=datetime(2026, 3, 1, 2, 41), prior_incidents=1,
    ))
    assert assessment.score == sum(f.points for f in assessment.factors)
    assert all(f.detail for f in assessment.factors), "every factor states its real evidence"
    assert all(f.points > 0 for f in assessment.factors), "a listed factor always contributed"


def test_score_is_capped_at_100():
    """Every factor at maximum must still produce a valid 0-100 score."""
    assessment = risk.assess(risk.RiskSignals(
        watchlist_priority="CRITICAL", zone_severity="CRITICAL", loitering_seconds=300,
        plate_confidence=1.0, plate_reads=20, cameras_visited=12, total_sightings=40,
        at=datetime(2026, 3, 1, 3, 0), prior_incidents=5, related_alerts=20,
    ))
    assert assessment.score == 100
    assert assessment.severity == "CRITICAL"


def test_a_watchlist_match_alone_is_serious_but_not_maximal():
    """A watchlist hit must be actionable on its own, while leaving room for
    corroborating signals to raise it — otherwise the score has no dynamic range
    on exactly the events that matter most."""
    assessment = risk.assess(risk.RiskSignals(watchlist_priority="CRITICAL", plate_text="GJ05AB1234"))
    assert assessment.score == risk.WATCHLIST_POINTS["CRITICAL"]
    assert assessment.severity == "MEDIUM"
    assert 0 < assessment.score < 100


def test_watchlist_priority_scales_the_contribution():
    scores = {
        priority: risk.assess(risk.RiskSignals(watchlist_priority=priority)).score
        for priority in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    }
    assert scores["LOW"] < scores["MEDIUM"] < scores["HIGH"] < scores["CRITICAL"]


def test_zone_severity_scales_the_contribution():
    scores = {
        severity: risk.assess(risk.RiskSignals(zone_severity=severity)).score
        for severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    }
    assert scores["LOW"] < scores["MEDIUM"] < scores["HIGH"] < scores["CRITICAL"]


def test_an_unknown_priority_falls_back_rather_than_crashing():
    """Watchlist priority is operator-entered data; an unexpected value must
    degrade to a sane weight, never take down alert evaluation."""
    assessment = risk.assess(risk.RiskSignals(watchlist_priority="URGENT"))
    assert assessment.score == risk.WATCHLIST_POINTS["MEDIUM"]


@pytest.mark.parametrize("score,expected", [
    (0, "LOW"), (24, "LOW"), (25, "MEDIUM"), (49, "MEDIUM"),
    (50, "HIGH"), (74, "HIGH"), (75, "CRITICAL"), (100, "CRITICAL"),
])
def test_severity_bands(score, expected):
    assert risk._severity_for(score) == expected


class TestIndividualFactors:
    def test_plate_quality_scales_with_confidence(self):
        low = risk.assess(risk.RiskSignals(plate_confidence=0.4)).score
        high = risk.assess(risk.RiskSignals(plate_confidence=0.95)).score
        assert low < high <= risk.PLATE_QUALITY_MAX

    def test_a_single_camera_sighting_adds_no_multi_camera_points(self):
        assessment = risk.assess(risk.RiskSignals(cameras_visited=1))
        assert [f.factor for f in assessment.factors] == []

    def test_multi_camera_activity_raises_the_score(self):
        one = risk.assess(risk.RiskSignals(cameras_visited=1)).score
        many = risk.assess(risk.RiskSignals(cameras_visited=5)).score
        assert many > one
        assert many <= risk.MULTI_CAMERA_MAX

    def test_night_activity_is_flagged_with_the_real_time(self):
        assessment = risk.assess(risk.RiskSignals(at=datetime(2026, 3, 1, 2, 41)))
        factor = next(f for f in assessment.factors if f.factor == "night_activity")
        assert "02:41" in factor.detail

    def test_daytime_activity_is_not_flagged_as_night(self):
        assessment = risk.assess(risk.RiskSignals(at=datetime(2026, 3, 1, 14, 30)))
        assert "night_activity" not in [f.factor for f in assessment.factors]

    def test_night_window_wraps_past_midnight(self):
        assert risk._is_night(datetime(2026, 3, 1, 23, 0)) is True
        assert risk._is_night(datetime(2026, 3, 1, 4, 0)) is True
        assert risk._is_night(datetime(2026, 3, 1, 6, 0)) is False

    def test_loitering_reports_the_real_dwell(self):
        assessment = risk.assess(risk.RiskSignals(loitering_seconds=185.4))
        factor = next(f for f in assessment.factors if f.factor == "loitering")
        assert "185s" in factor.detail


def test_assessment_is_deterministic():
    """Same inputs, same answer — a score used in an evidence package cannot
    depend on when it was computed."""
    signals = risk.RiskSignals(
        watchlist_priority="HIGH", zone_severity="HIGH", plate_confidence=0.9,
        cameras_visited=3, total_sightings=5, at=datetime(2026, 3, 1, 23, 15),
    )
    first, second = risk.assess(signals), risk.assess(signals)
    assert first.score == second.score
    assert first.as_dicts() == second.as_dicts()


def test_explain_produces_reason_lines_matching_the_alert_reasons_shape():
    assessment = risk.assess(risk.RiskSignals(watchlist_priority="HIGH", plate_text="GJ05AB1234"))
    lines = assessment.explain()
    assert len(lines) == len(assessment.factors)
    assert all(isinstance(line, str) and line.startswith("+") for line in lines)
