"""Explainable 0-100 risk score.

Deliberately NOT a machine-learned score. There is no trained risk model behind
this system and inventing an opaque number would be exactly the kind of
unfalsifiable AI claim the rest of this codebase refuses to make (see the
appearance-similarity and ANPR-confidence handling for the same stance). This
is a transparent weighted sum over signals the platform genuinely observed:
every point in the total is attributable to a named factor with the real
evidence behind it, so an operator — or a court — can ask "why 87?" and get a
complete answer.

    Risk 87/100 — HIGH
      +45  Watchlist match        plate GJ05AB1234, CRITICAL priority entry
      +20  Restricted zone entry  'Secure Yard' on C-014
      + 9  Plate read quality     94% peak OCR confidence over 4 reads
      + 8  Multi-camera activity  observed by 4 cameras
      + 5  Night-time activity    02:41

The weights below are a documented policing-priority judgement, not a measured
quantity — they are stated openly here rather than hidden in a model file, and
are the one thing to tune if an operator says the scores feel wrong.
"""
from dataclasses import dataclass
from datetime import datetime

# Maximum contribution of each factor. A factor never exceeds its cap and never
# contributes negative points: the score answers "how much reason for concern
# has accumulated", so a weak signal adds little, it does not subtract evidence
# that genuinely exists.
WATCHLIST_POINTS = {"CRITICAL": 45, "HIGH": 40, "MEDIUM": 30, "LOW": 20}
ZONE_POINTS = {"CRITICAL": 25, "HIGH": 20, "MEDIUM": 12, "LOW": 6}
LOITERING_POINTS = 15
PLATE_QUALITY_MAX = 10
MULTI_CAMERA_MAX = 10
REPEAT_SIGHTING_MAX = 5
NIGHT_POINTS = 5
PRIOR_INCIDENT_POINTS = 10
RELATED_ALERTS_MAX = 10

# Local clock hours treated as night-time. Offence rates and the operational
# significance of an unexplained vehicle both rise outside working hours; this
# is a policing judgement, stated rather than buried.
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 5

SEVERITY_BANDS = ((75, "CRITICAL"), (50, "HIGH"), (25, "MEDIUM"), (0, "LOW"))


@dataclass(frozen=True)
class RiskFactor:
    factor: str   # stable machine key, safe to filter/aggregate on
    label: str    # short human label for the UI
    points: int
    detail: str   # the actual evidence — never a generic sentence

    def as_dict(self) -> dict:
        return {"factor": self.factor, "label": self.label, "points": self.points, "detail": self.detail}


@dataclass(frozen=True)
class RiskAssessment:
    score: int
    severity: str
    factors: list[RiskFactor]

    def as_dicts(self) -> list[dict]:
        return [f.as_dict() for f in self.factors]

    def explain(self) -> list[str]:
        """Plain-text lines, in the same shape as the existing `Alert.reasons`
        list, so the score can be shown anywhere reasons already are."""
        return [f"+{f.points} {f.label}: {f.detail}" for f in self.factors]


@dataclass
class RiskSignals:
    """Everything the score is computed from. A signal left at its default is
    genuinely absent — never a stand-in for 'unknown'."""
    watchlist_priority: str | None = None      # entry priority if this vehicle is watchlisted
    plate_text: str = ""
    zone_severity: str | None = None           # severity of a restricted zone actually entered
    zone_name: str = ""
    camera_code: str = ""
    loitering_seconds: float | None = None     # dwell that breached a loitering rule
    plate_confidence: float = 0.0
    plate_reads: int = 0
    cameras_visited: int = 0
    total_sightings: int = 0
    at: datetime | None = None                 # time of the event being scored
    prior_incidents: int = 0
    related_alerts: int = 0


def _severity_for(score: int) -> str:
    for threshold, severity in SEVERITY_BANDS:
        if score >= threshold:
            return severity
    return "LOW"


def _is_night(at: datetime) -> bool:
    return at.hour >= NIGHT_START_HOUR or at.hour < NIGHT_END_HOUR


def assess(signals: RiskSignals) -> RiskAssessment:
    """Score one event or one vehicle. Pure — no DB, no clock, no I/O — so the
    arithmetic is directly testable and the same inputs always give the same
    answer, which is what makes the score defensible."""
    factors: list[RiskFactor] = []

    if signals.watchlist_priority:
        priority = signals.watchlist_priority.upper()
        points = WATCHLIST_POINTS.get(priority, WATCHLIST_POINTS["MEDIUM"])
        detail = f"plate {signals.plate_text or 'unknown'} matches an active {priority} watchlist entry"
        factors.append(RiskFactor("watchlist_match", "Watchlist match", points, detail))

    if signals.zone_severity:
        severity = signals.zone_severity.upper()
        points = ZONE_POINTS.get(severity, ZONE_POINTS["MEDIUM"])
        where = f"'{signals.zone_name}'" if signals.zone_name else "a restricted zone"
        if signals.camera_code:
            where += f" on {signals.camera_code}"
        factors.append(RiskFactor("zone_entry", "Restricted zone entry", points, f"entered {where}"))

    if signals.loitering_seconds:
        factors.append(RiskFactor(
            "loitering", "Loitering", LOITERING_POINTS,
            f"remained in the zone for {int(signals.loitering_seconds)}s",
        ))

    # Read quality raises the score because a confidently-identified vehicle is
    # a more actionable lead than an uncertain one — it is a measure of how much
    # the rest of the score can be trusted, and is reported as such.
    if signals.plate_confidence > 0:
        points = int(round(min(1.0, signals.plate_confidence) * PLATE_QUALITY_MAX))
        if points:
            reads = f" over {signals.plate_reads} reads" if signals.plate_reads > 1 else " (single read)"
            factors.append(RiskFactor(
                "plate_quality", "Plate read quality", points,
                f"{signals.plate_confidence * 100:.0f}% peak OCR confidence{reads}",
            ))

    if signals.cameras_visited > 1:
        # Saturates at 5 cameras: beyond that, "seen on many cameras" stops
        # adding information about how unusual the movement is.
        points = int(round(min(1.0, (signals.cameras_visited - 1) / 4) * MULTI_CAMERA_MAX))
        factors.append(RiskFactor(
            "multi_camera", "Multi-camera activity", points,
            f"observed by {signals.cameras_visited} cameras",
        ))

    if signals.total_sightings > 1:
        points = int(round(min(1.0, (signals.total_sightings - 1) / 9) * REPEAT_SIGHTING_MAX))
        if points:
            factors.append(RiskFactor(
                "repeat_sightings", "Repeat sightings", points,
                f"{signals.total_sightings} recorded sightings",
            ))

    if signals.at is not None and _is_night(signals.at):
        factors.append(RiskFactor(
            "night_activity", "Night-time activity", NIGHT_POINTS,
            f"observed at {signals.at.strftime('%H:%M')}",
        ))

    if signals.prior_incidents > 0:
        factors.append(RiskFactor(
            "prior_incident", "Prior incident association", PRIOR_INCIDENT_POINTS,
            f"linked to {signals.prior_incidents} existing incident(s)",
        ))

    if signals.related_alerts > 1:
        points = int(round(min(1.0, (signals.related_alerts - 1) / 4) * RELATED_ALERTS_MAX))
        if points:
            factors.append(RiskFactor(
                "related_alerts", "Multiple related alerts", points,
                f"{signals.related_alerts} alerts involve this vehicle",
            ))

    score = min(100, sum(f.points for f in factors))
    return RiskAssessment(score=score, severity=_severity_for(score), factors=factors)
