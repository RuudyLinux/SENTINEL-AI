"""Zone + watchlist rules -> explainable Alert (+ Incident on CRITICAL).

Every alert carries a `reasons` list built from the rule that actually
matched, so the UI can show why it fired (doc §55).

Cooldown per track: an object sitting in a zone gets re-detected every
inference cycle, and without de-duplication that's an alert per frame. Keyed
on (camera, track or vehicle, rule), repeats within COOLDOWN_SECONDS are
suppressed.

Rule types: watchlist_plate and zone_entry fire for every active watchlist
entry / zone. loitering only applies to a zone an active
AlertRule(rule_type="loitering", zone_id=...) points at. All three respect the
zone's schedule_start/schedule_end window (_within_schedule).
"""
import asyncio
import time
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .. import models, metrics, watchlist
from ..config import settings
from ..ws import manager, EventType
from . import risk
from .db_retry import locked_flush, safe_commit
from .. import runtime_state
from ..evidence_hash import sha256_file

COOLDOWN_SECONDS = 45.0
# Not a dict of time.monotonic() readings: monotonic time has a per-process
# origin, so a second worker can't compare (every alert doubles) and a restart
# re-fires everything the cooldown was holding back. See app/runtime_state.py.
# Redis-backed when redis_url is set and reachable at startup, in-process otherwise.
_alert_claims = runtime_state.build_claims_store("alert_cooldown", settings)

# (camera_id, zone_id, track_key) -> (first_seen, last_seen, frames seen in
# zone), monotonic. Dwell time for loitering and the frame count behind
# zone_entry_min_frames. Stale entries are pruned each call.
_zone_presence: dict[tuple, tuple[float, float, int]] = {}
_PRESENCE_STALE_FLOOR_SECONDS = 300.0


def _bbox_center_in_zone(bbox: list[float], frame_w: int, frame_h: int, zone: models.Zone) -> bool:
    if frame_w <= 0 or frame_h <= 0:
        return False
    cx = ((bbox[0] + bbox[2]) / 2) / frame_w
    cy = ((bbox[1] + bbox[3]) / 2) / frame_h
    return zone.x1 <= cx <= zone.x2 and zone.y1 <= cy <= zone.y2


async def _on_cooldown(key: tuple) -> bool:
    """True when this key fired recently and the alert should be suppressed.

    Checking records the firing (inverse of claim()); callers rely on that,
    see the ordering note on the watchlist rule.

    Async only for the Redis store. evaluate() is awaited straight from the
    frame loop, so a blocking round trip here would stall every camera. The
    local store is a dict op and runs inline; only Redis goes to a thread.
    """
    if _alert_claims.is_local:
        return not _alert_claims.claim(key, COOLDOWN_SECONDS)
    return not await asyncio.to_thread(_alert_claims.claim, key, COOLDOWN_SECONDS)


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
    """Whether `at` is inside the zone's HH:MM window (can wrap midnight, e.g.
    22:00-06:00). An unparseable schedule counts as always on, same as the
    default 00:00-23:59, rather than never firing."""
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
    stale = [k for k, v in _zone_presence.items() if now - v[1] > floor_seconds]
    for k in stale:
        _zone_presence.pop(k, None)


def find_incident_for_alert(db: Session, alert_id: str) -> "models.Incident | None":
    """The incident an alert belongs to.

    An alert that opened an incident is linked by Incident.alert_id; one
    correlated into an existing incident goes through IncidentAlert. Look up
    through here so callers don't need to know which.
    """
    link = db.query(models.IncidentAlert).filter(models.IncidentAlert.alert_id == alert_id).first()
    if link is not None:
        incident = db.query(models.Incident).filter(models.Incident.id == link.incident_id).first()
        if incident is not None:
            return incident
    return db.query(models.Incident).filter(models.Incident.alert_id == alert_id).first()


_PRIORITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def _find_correlatable_incident(
    db: Session, vehicle: "models.Vehicle | None", camera: models.Camera, at: datetime,
) -> "models.Incident | None":
    """An open incident this alert belongs to instead of opening a new one.

    Conservative on purpose. Same vehicle within the window (a plate is a hard
    identity, one car over five cameras is one pursuit), or same camera with
    no vehicle identified (repeat zone breaches at one spot). Never merged on
    looks or proximity alone; two similar vehicles are two incidents.
    """
    window_start = at - timedelta(seconds=settings.incident_correlation_window_seconds)
    query = db.query(models.Incident).filter(
        models.Incident.status != "closed",
        models.Incident.created_at >= window_start,
    )
    if vehicle is not None:
        query = query.filter(models.Incident.vehicle_id == vehicle.id)
    else:
        query = query.filter(
            models.Incident.camera_id == camera.id,
            models.Incident.vehicle_id.is_(None),
        )
    return query.order_by(models.Incident.created_at.desc()).first()


def _rule_switched_off(db: Session, rule_type: str, zone_id: "str | None" = None) -> bool:
    """True when every rule of this type (for this zone) has been disabled.

    Watchlist and zone-entry alerts fire with no AlertRule row at all, that's
    the default. Once a rule exists though, the Disable button in Admin ->
    Rules has to actually stop it (it used to only label the alert). One
    active rule of the type keeps it on.
    """
    query = db.query(models.AlertRule.active).filter(models.AlertRule.rule_type == rule_type)
    if zone_id is not None:
        query = query.filter(models.AlertRule.zone_id == zone_id)
    states = [bool(active) for (active,) in query.all()]
    return bool(states) and not any(states)


def _plate_is_corroborated(vehicle: models.Vehicle) -> bool:
    # Read off the Vehicle row (correlate.upsert_vehicle_for_plate keeps it
    # up to date), not queried from sightings, this runs per detection.
    # NULL (old rows, legacy single-frame path) counts as not corroborated.
    return bool(getattr(vehicle, "plate_corroborated", False))


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
    # risk score inputs, collected as rules match so it's computed from what
    # fired, never by parsing reason strings
    signals = risk.RiskSignals(camera_code=str(camera.camera_code))

    # watchlist_plate (cooldown per camera+vehicle)
    #
    # The entry is the authority, the flag is just a cache (app/watchlist.py).
    # Firing on the flag alone kept alerting after an entry was deactivated or
    # expired, with a reason string claiming an active entry that didn't exist.
    #
    # Look up the entry BEFORE the cooldown: _on_cooldown records a firing, so
    # checking it first and then not firing would burn the window for nothing.
    entry = (
        watchlist.plate_entry_in_force(db, vehicle.plate_text)
        if vehicle and vehicle.watchlist_flag and not _rule_switched_off(db, "watchlist_plate")
        else None
    )
    if entry is not None and not await _on_cooldown((camera.id, "watchlist", vehicle.id)):
        rule = db.query(models.AlertRule).filter(
            models.AlertRule.rule_type == "watchlist_plate", models.AlertRule.active == True  # noqa: E712
        ).first()
        rule_id = rule.id if rule else None
        signals.watchlist_priority = entry.priority
        signals.plate_text = str(vehicle.plate_text or "")

        # A match is never suppressed for being uncertain, but severity must
        # not overstate how sure the read is. Below the floor it's capped at
        # HIGH and the reason says it needs confirmation.
        #
        # Corroboration is a separate gate. OCR confidence doesn't separate
        # right from wrong reads on the benchmark: correct reads span
        # 0.262-0.990, wrong plate-shaped ones 0.260-0.956, and 6 of 7 wrong
        # reads sit at or above the lowest correct one. No threshold gets
        # precision over 0.5 (docs/ANPR_ACCURACY.md, "A1").
        #
        # Concretely UP84AE9889 was read as UP81AE9889 at 0.956. Confidence
        # alone would raise a CRITICAL and open an incident naming a vehicle
        # that was never there, a wrongful-stop risk. So CRITICAL needs a
        # confident read AND corroboration across frames. Uncorroborated
        # matches still alert, capped at HIGH and labelled. corroborated=None
        # counts as not corroborated.
        plate_confidence = float(vehicle.plate_confidence or 0.0)
        corroborated = _plate_is_corroborated(vehicle)
        signals.plate_corroborated = corroborated
        # WATCHLIST_REQUIRE_CORROBORATION=false restores confidence-only escalation
        corroboration_satisfied = corroborated or not settings.watchlist_require_corroboration
        if plate_confidence >= settings.watchlist_high_confidence_floor and corroboration_satisfied:
            reasons.append(f"Watchlist signal: plate {vehicle.plate_text} matches an active watchlist entry")
            severity = "CRITICAL"
        elif plate_confidence >= settings.watchlist_high_confidence_floor:
            reasons.append(
                f"Watchlist signal (UNCORROBORATED, read {plate_confidence * 100:.0f}%): plate "
                f"{vehicle.plate_text} matches an active watchlist entry, but only ONE frame "
                f"supports the read — requires confirmation"
            )
            severity = "HIGH"
        else:
            reasons.append(
                f"Watchlist signal (LOW CONFIDENCE {plate_confidence * 100:.0f}%): plate "
                f"{vehicle.plate_text} matches an active watchlist entry — requires confirmation"
            )
            severity = "HIGH"

    # zone_entry / loitering (cooldown per camera+zone+track)
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

        # Presence in this zone, counted per tracked object across frames.
        presence_key = (camera.id, zone.id, track_key)
        now_mono = time.monotonic()
        first_seen, _, frames_in_zone = _zone_presence.get(presence_key, (now_mono, now_mono, 0))
        frames_in_zone += 1
        _zone_presence[presence_key] = (first_seen, now_mono, frames_in_zone)
        # Alert only on confident detections, and for a tracked object only
        # once it's been in the zone for zone_entry_min_frames frames: a
        # one-frame ghost box shouldn't raise a CRITICAL. Untracked detections
        # can't be counted, so they aren't held back.
        confident = float(detection.confidence or 0.0) >= settings.zone_alert_min_confidence
        confirmed = detection.track_id is None or frames_in_zone >= settings.zone_entry_min_frames
        if not (confident and confirmed):
            continue

        # same ordering rule as the watchlist: check the rule is on first
        if not _rule_switched_off(db, "zone_entry", zone.id) and not await _on_cooldown((camera.id, "zone", zone.id, track_key)):
            reasons.append(f"Restricted-zone entry: '{zone.name}' on {camera.camera_code}")
            if zone.severity == "CRITICAL" or severity != "CRITICAL":
                severity = zone.severity if zone.severity in ("HIGH", "CRITICAL") else severity
            rule = db.query(models.AlertRule).filter(
                models.AlertRule.rule_type == "zone_entry", models.AlertRule.zone_id == zone.id
            ).first()
            rule_id = rule.id if rule else rule_id
            signals.zone_severity = str(zone.severity or "MEDIUM")
            signals.zone_name = str(zone.name or "")

        # loitering only where an active loitering rule targets the zone
        if zone.loitering_seconds:
            loitering_rule = db.query(models.AlertRule).filter(
                models.AlertRule.rule_type == "loitering",
                models.AlertRule.zone_id == zone.id,
                models.AlertRule.active == True,  # noqa: E712
            ).first()
            if loitering_rule:
                dwell = now_mono - first_seen
                if dwell >= zone.loitering_seconds and not await _on_cooldown((camera.id, "loitering", zone.id, track_key)):
                    reasons.append(
                        f"Loitering: object present in '{zone.name}' on {camera.camera_code} "
                        f"for over {int(zone.loitering_seconds)}s"
                    )
                    if severity != "CRITICAL":
                        severity = zone.severity if zone.severity in ("HIGH", "CRITICAL") else severity
                    rule_id = loitering_rule.id if rule_id is None else rule_id
                    signals.loitering_seconds = dwell

    if not reasons:
        return alerts

    # Risk score. Vehicle history only counts when a vehicle was identified;
    # a zone entry with no plate has no history, and zeroes would claim it does.
    signals.at = detection.source_timestamp or detection.timestamp or datetime.utcnow()
    if vehicle is not None:
        signals.plate_confidence = float(vehicle.plate_confidence or 0.0)
        signals.plate_text = signals.plate_text or str(vehicle.plate_text or "")
        sightings = db.query(models.Plate).filter(models.Plate.vehicle_id == vehicle.id).all()
        signals.total_sightings = len(sightings)
        signals.cameras_visited = len({p.camera_id for p in sightings})
        signals.plate_reads = sum(p.reads_count or 1 for p in sightings)
        signals.prior_incidents = db.query(models.Incident).filter(
            models.Incident.vehicle_id == vehicle.id
        ).count()
        signals.related_alerts = db.query(models.Alert).filter(
            models.Alert.vehicle_id == vehicle.id
        ).count()
    assessment = risk.assess(signals)

    # rule severity is a floor. the score can escalate, never downgrade a rule's CRITICAL
    if _PRIORITY_ORDER.get(assessment.severity, 0) > _PRIORITY_ORDER.get(severity, 0):
        severity = assessment.severity

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
        risk_score=assessment.score,
        risk_factors=assessment.as_dicts(),
    )
    db.add(alert)
    # was a bare db.flush() on the event loop: waiting on a locked database
    # froze every camera and request for up to the busy_timeout
    await locked_flush(db)
    alerts.append(alert)
    metrics.ALERTS_TOTAL.labels(camera_code=str(camera.camera_code), severity=severity).inc()

    # CRITICAL alerts open an incident, or join the open one they belong to,
    # so a watchlisted car crossing four cameras is one incident whose
    # title/description/priority grow with it, not four.
    incident = None
    incident_link = None
    incident_evidence = None
    incident_targets: dict | None = None
    if severity == "CRITICAL":
        existing = _find_correlatable_incident(db, vehicle, camera, signals.at)
        if existing is not None:
            linked_alert_ids = {
                row.alert_id for row in db.query(models.IncidentAlert).filter(
                    models.IncidentAlert.incident_id == existing.id
                ).all()
            }
            linked_alert_ids.add(str(existing.alert_id) if existing.alert_id else "")
            linked_alert_ids.discard("")
            linked_alert_ids.add(str(alert.id))
            cameras_involved = {
                row.camera_id for row in db.query(models.Alert).filter(
                    models.Alert.id.in_(linked_alert_ids)
                ).all()
            }
            correlation_reason = (
                f"same vehicle ({vehicle.plate_text}) within the correlation window"
                if vehicle is not None
                else f"same camera ({camera.camera_code}) within the correlation window"
            )
            # `existing` is persistent, rollback would revert these, so the
            # retry reassigns from the locals
            subject = f"Watchlisted vehicle {vehicle.plate_text}" if vehicle else f"Restricted-zone activity on {camera.camera_code}"
            target_title = (
                f"{subject} — {len(linked_alert_ids)} correlated alerts across "
                f"{len(cameras_involved)} camera(s)"
            )
            # append to the running description, never replace it
            new_reasons = [r for r in reasons if r not in (existing.description or "")]
            target_description = "; ".join(filter(None, [existing.description or "", *new_reasons]))
            target_priority = (
                severity if _PRIORITY_ORDER.get(severity, 0) > _PRIORITY_ORDER.get(str(existing.priority), 0)
                else existing.priority
            )
            target_updated_at = datetime.utcnow()
            existing.title = target_title
            existing.description = target_description
            existing.priority = target_priority
            existing.updated_at = target_updated_at
            incident = existing
            incident_targets = {
                "title": target_title, "description": target_description,
                "priority": target_priority, "updated_at": target_updated_at,
            }
        else:
            correlation_reason = "opened this incident"
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
            await locked_flush(db)

        # every alert of an incident gets a link, the opening one too, so
        # listing an incident's alerts is one query
        incident_link = models.IncidentAlert(
            incident_id=incident.id, alert_id=alert.id, correlation_reason=correlation_reason,
        )
        db.add(incident_link)
        await locked_flush(db)
        # split by outcome so the noise reduction from correlation is visible
        metrics.INCIDENTS_TOTAL.labels(outcome="correlated" if incident_targets else "opened").inc()
        if detection.snapshot_path:
            # same shape as worker.py's evidence backfill; without
            # alert_id/detection_id the UI showed "Alert: —"
            incident_evidence = models.Evidence(
                incident_id=incident.id,
                evidence_type="snapshot",
                camera_id=camera.id,
                file_path=detection.snapshot_path,
                sha256=sha256_file(detection.snapshot_path),
                alert_id=alert.id,
                detection_id=detection.id,
                event_type="watchlist_match" if vehicle else "zone_entry",
                source_timestamp=detection.source_timestamp,
                verification_status="unverified",
                # versions at capture time, see worker.py
                model_version=settings.model_version,
                rule_version=settings.rule_version,
            )
            db.add(incident_evidence)

    # alert, incident_link and incident_evidence are new in this call, so a
    # rollback only detaches them and re-add() restores them (db_retry.py).
    # A correlated `incident` is persistent and was mutated in place, so its
    # fields are reassigned from incident_targets.
    def _reapply():
        db.add(alert)
        if incident is not None:
            db.add(incident)
            for name, value in (incident_targets or {}).items():
                setattr(incident, name, value)
        if incident_link is not None:
            db.add(incident_link)
        if incident_evidence is not None:
            db.add(incident_evidence)

    await safe_commit(db, f"camera {camera.camera_code}", reapply=_reapply)
    # never batched, see ws.py
    await manager.publish(EventType.ALERT_CREATED, {
        "id": alert.id,
        "camera_id": camera.id,
        "camera_code": camera.camera_code,
        "severity": alert.severity,
        "reasons": alert.reasons,
        "risk_score": alert.risk_score,
        "risk_factors": alert.risk_factors,
        "vehicle_id": alert.vehicle_id,
        "plate_text": str(vehicle.plate_text) if vehicle is not None else None,
        "incident_id": incident.id if incident is not None else None,
        "timestamp": alert.timestamp.isoformat(),
    })
    if incident is not None and incident_targets is None:
        # only announce new incidents; an alert joining one isn't a new incident
        await manager.publish(EventType.INCIDENT_CREATED, {
            "id": incident.id,
            "title": incident.title,
            "priority": incident.priority,
            "camera_id": incident.camera_id,
            "vehicle_id": incident.vehicle_id,
            "alert_id": alert.id,
            "created_at": incident.created_at.isoformat() if incident.created_at else None,
        })
    return alerts
