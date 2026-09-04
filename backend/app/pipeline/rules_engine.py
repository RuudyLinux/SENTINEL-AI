"""Zone + watchlist rule evaluation -> explainable Alert (+ Incident on CRITICAL).

Every alert carries a `reasons` list so the UI can show "why did this fire"
per doc §55 (Explainable Alert Model) — built from the actual rule that matched,
not a canned string.

Per-track cooldown: a single tracked object sitting in a zone gets re-detected
every inference cycle. Without de-duplication that floods the operator with a
new alert per frame — the opposite of the doc's "Event-centric intelligence:
group raw detections into incidents instead of flooding operators with
detections" principle (§56, §37 Product Principles). We key on (camera, track
or vehicle, rule) and suppress repeats within COOLDOWN_SECONDS.

Rule types (Phase 6): `watchlist_plate`, `zone_entry` — both unconditional for
every active Zone/watchlist entry, matching this project's existing behavior — and
`loitering`, which (unlike zone_entry) only applies to a zone when an active
`AlertRule(rule_type="loitering", zone_id=...)` row references it, per the
"configurable, not hardcoded" requirement. All three respect a zone's
`schedule_start`/`schedule_end` window (previously declared on the model/schema but
never actually read here — now enforced, see `_within_schedule`).
"""
import time
from datetime import datetime
from sqlalchemy.orm import Session

from .. import models
from ..ws import manager

COOLDOWN_SECONDS = 45.0
_last_alert_at: dict[tuple, float] = {}

# Loitering dwell-time tracking: (camera_id, zone_id, track_key) -> (first_seen_mono,
# last_seen_mono), monotonic wall-clock, mirroring _last_alert_at's style. Pruned of
# stale entries (track presumably left the zone) each call so this never grows
# unbounded across a long-running camera session.
_zone_presence: dict[tuple, tuple[float, float]] = {}
_PRESENCE_STALE_FLOOR_SECONDS = 300.0


def _bbox_center_in_zone(bbox: list[float], frame_w: int, frame_h: int, zone: models.Zone) -> bool:
    if frame_w <= 0 or frame_h <= 0:
        return False
    cx = ((bbox[0] + bbox[2]) / 2) / frame_w
    cy = ((bbox[1] + bbox[3]) / 2) / frame_h
    return zone.x1 <= cx <= zone.x2 and zone.y1 <= cy <= zone.y2


def _on_cooldown(key: tuple) -> bool:
    now = time.monotonic()
    last = _last_alert_at.get(key)
    if last is not None and now - last < COOLDOWN_SECONDS:
        return True
    _last_alert_at[key] = now
    return False


def _parse_hhmm(value: str) -> "tuple[int, int] | None":
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def _within_schedule(zone: models.Zone, at: datetime) -> bool:
    """True if `at`'s local clock time falls within the zone's schedule_start /
    schedule_end window (HH:MM, wraps past midnight, e.g. 22:00-06:00). A zone
    with an unparseable schedule is treated as always-on (fail open, same as a
    zone with the default 00:00-23:59) rather than silently never firing."""
    start = _parse_hhmm(zone.schedule_start)
    end = _parse_hhmm(zone.schedule_end)
    if start is None or end is None:
        return True
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    now_minutes = at.hour * 60 + at.minute
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes <= end_minutes
    return now_minutes >= start_minutes or now_minutes <= end_minutes  # wraps past midnight


def _prune_stale_presence(floor_seconds: float) -> None:
    now = time.monotonic()
    stale = [k for k, (_, last_seen) in _zone_presence.items() if now - last_seen > floor_seconds]
    for k in stale:
        _zone_presence.pop(k, None)


async def evaluate(
    db: Session,
    camera: models.Camera,
    detection: models.Detection,
    frame_w: int,
    frame_h: int,
    vehicle: models.Vehicle | None = None,
) -> list[models.Alert]:
    alerts: list[models.Alert] = []
    reasons: list[str] = []
    severity = "MEDIUM"
    rule_id = None

    # --- watchlist_plate rule (cooldown per camera+vehicle) ---
    if vehicle and vehicle.watchlist_flag and not _on_cooldown((camera.id, "watchlist", vehicle.id)):
        rule = db.query(models.AlertRule).filter(
            models.AlertRule.rule_type == "watchlist_plate", models.AlertRule.active == True  # noqa: E712
        ).first()
        reasons.append(f"Watchlist signal: plate {vehicle.plate_text} matches an active watchlist entry")
        severity = "CRITICAL"
        rule_id = rule.id if rule else None

    # --- zone_entry / loitering rules for this camera (cooldown per camera+zone+track) ---
    at = detection.source_timestamp or detection.timestamp or datetime.utcnow()
    zones = db.query(models.Zone).filter(models.Zone.camera_id == camera.id, models.Zone.active == True).all()  # noqa: E712
    track_key = detection.track_id or f"det:{detection.id}"
    _prune_stale_presence(_PRESENCE_STALE_FLOOR_SECONDS)
    for zone in zones:
        if detection.cls not in ("car", "truck", "bus", "motorbike", "person"):
            continue
        if not _bbox_center_in_zone(detection.bbox, frame_w, frame_h, zone):
            continue
        if not _within_schedule(zone, at):
            continue

        if not _on_cooldown((camera.id, "zone", zone.id, track_key)):
            reasons.append(f"Restricted-zone entry: '{zone.name}' on {camera.camera_code}")
            if zone.severity == "CRITICAL" or severity != "CRITICAL":
                severity = zone.severity if zone.severity in ("HIGH", "CRITICAL") else severity
            rule = db.query(models.AlertRule).filter(
                models.AlertRule.rule_type == "zone_entry", models.AlertRule.zone_id == zone.id
            ).first()
            rule_id = rule.id if rule else rule_id

        # --- loitering (dwell-time), only for zones an active loitering AlertRule
        # actually targets — configurable-by-rule, unlike zone_entry above which
        # stays unconditional (existing behavior, not changed here). ---
        if zone.loitering_seconds:
            loitering_rule = db.query(models.AlertRule).filter(
                models.AlertRule.rule_type == "loitering",
                models.AlertRule.zone_id == zone.id,
                models.AlertRule.active == True,  # noqa: E712
            ).first()
            if loitering_rule:
                presence_key = (camera.id, zone.id, track_key)
                now_mono = time.monotonic()
                first_seen, _ = _zone_presence.get(presence_key, (now_mono, now_mono))
                _zone_presence[presence_key] = (first_seen, now_mono)
                dwell = now_mono - first_seen
                if dwell >= zone.loitering_seconds and not _on_cooldown((camera.id, "loitering", zone.id, track_key)):
                    reasons.append(
                        f"Loitering: object present in '{zone.name}' on {camera.camera_code} "
                        f"for over {int(zone.loitering_seconds)}s"
                    )
                    if severity != "CRITICAL":
                        severity = zone.severity if zone.severity in ("HIGH", "CRITICAL") else severity
                    rule_id = loitering_rule.id if rule_id is None else rule_id

    if not reasons:
        return alerts

    alert = models.Alert(
        camera_id=camera.id,
        rule_id=rule_id,
        severity=severity,
        vehicle_id=vehicle.id if vehicle else None,
        detection_id=detection.id,
        confidence=detection.confidence,
        reasons=reasons,
        snapshot_path=detection.snapshot_path,
        source_timestamp=detection.source_timestamp,
    )
    db.add(alert)
    db.flush()
    alerts.append(alert)

    # Auto-create an incident for CRITICAL alerts (doc §60 flagship flow)
    if severity == "CRITICAL":
        incident = models.Incident(
            title=f"Potential match — {vehicle.plate_text if vehicle else detection.cls} on {camera.camera_code}",
            incident_type="watchlist_match" if vehicle else "zone_entry",
            priority="CRITICAL",
            status="open",
            location=camera.location,
            description="; ".join(reasons),
            camera_id=camera.id,
            alert_id=alert.id,
            vehicle_id=vehicle.id if vehicle else None,
        )
        db.add(incident)
        db.flush()
        if detection.snapshot_path:
            db.add(models.Evidence(
                incident_id=incident.id,
                evidence_type="snapshot",
                camera_id=camera.id,
                file_path=detection.snapshot_path,
                verification_status="unverified",
            ))

    db.commit()
    await manager.broadcast("alert", {
        "id": alert.id,
        "camera_id": camera.id,
        "camera_code": camera.camera_code,
        "severity": alert.severity,
        "reasons": alert.reasons,
        "timestamp": alert.timestamp.isoformat(),
    })
    return alerts
