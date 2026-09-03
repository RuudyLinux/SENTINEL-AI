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
"""
import time
from datetime import datetime
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..ws import manager

COOLDOWN_SECONDS = 45.0
_last_alert_at: dict[tuple, float] = {}


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

    # --- zone_entry rules for this camera (cooldown per camera+zone+track) ---
    zones = db.query(models.Zone).filter(models.Zone.camera_id == camera.id, models.Zone.active == True).all()  # noqa: E712
    track_key = detection.track_id or f"det:{detection.id}"
    for zone in zones:
        if detection.cls not in ("car", "truck", "bus", "motorbike", "person"):
            continue
        if not _bbox_center_in_zone(detection.bbox, frame_w, frame_h, zone):
            continue
        if _on_cooldown((camera.id, "zone", zone.id, track_key)):
            continue
        reasons.append(f"Restricted-zone entry: '{zone.name}' on {camera.camera_code}")
        if zone.severity == "CRITICAL" or severity != "CRITICAL":
            severity = zone.severity if zone.severity in ("HIGH", "CRITICAL") else severity
        rule = db.query(models.AlertRule).filter(
            models.AlertRule.rule_type == "zone_entry", models.AlertRule.zone_id == zone.id
        ).first()
        rule_id = rule.id if rule else rule_id

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
