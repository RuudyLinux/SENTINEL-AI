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
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .. import models, metrics, watchlist
from ..config import settings
from ..ws import manager, EventType
from . import risk
from .db_retry import safe_commit
from ..evidence_hash import sha256_file

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


def find_incident_for_alert(db: Session, alert_id: str) -> "models.Incident | None":
    """The incident an alert belongs to.

    Two paths on purpose: an alert that OPENED an incident is linked by
    `Incident.alert_id` (the original, still-supported relationship), while an
    alert CORRELATED into an existing incident is linked through
    `IncidentAlert`. Callers must not have to know which, so every lookup goes
    through here rather than querying `Incident.alert_id` directly.
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
    """An already-open incident this alert is part of, rather than a new event.

    Correlation is deliberately conservative — it only merges on evidence strong
    enough to be defensible:

    - **same vehicle**, still open, within the correlation window. A recognized
      plate is a hard identity, so a watchlisted vehicle crossing five cameras
      in ten minutes is one pursuit, not five incidents.
    - **same camera, no vehicle identified**, still open, within the window —
      repeated zone breaches at one location in one window are one situation.

    It never merges on visual similarity or proximity alone. Two different
    vehicles doing similar things are two incidents, and claiming otherwise
    would be an identity assertion this system cannot support.
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


def _plate_is_corroborated(vehicle: models.Vehicle) -> bool:
    """Whether this vehicle's plate has ever been corroborated across frames.

    Read straight off the Vehicle row rather than queried from its sightings:
    rule evaluation runs per detection, and an extra query per alert is real
    cost on a multi-camera deployment. `correlate.upsert_vehicle_for_plate`
    maintains the flag.

    NULL/False — rows predating the column, and the legacy single-frame ANPR
    path — read as NOT corroborated. The safe default for a missing safety
    signal is "not satisfied".
    """
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
    # Structured inputs for the risk score, collected alongside the human-
    # readable `reasons` as each rule matches — so the score is computed from
    # what actually fired, never re-derived by parsing the reason strings.
    signals = risk.RiskSignals(camera_code=str(camera.camera_code))

    # --- watchlist_plate rule (cooldown per camera+vehicle) ---
    #
    # The entry is looked up BEFORE the cooldown check, and the alert now
    # requires one. Previously this fired on `vehicle.watchlist_flag` alone and
    # substituted priority "HIGH" when no entry was found — so once a vehicle
    # was flagged, deactivating or expiring its watchlist entry did not stop
    # the alerts, and each one asserted in its own reason string that the plate
    # "matches an active watchlist entry" when none existed. The flag is a
    # cache (see app/watchlist.py); the entry is the authority.
    #
    # Order matters: `_on_cooldown` RECORDS the time when it returns False, so
    # calling it first and then declining to fire would consume the cooldown
    # window of an alert that was never sent.
    entry = watchlist.plate_entry_in_force(db, vehicle.plate_text) if vehicle and vehicle.watchlist_flag else None
    if entry is not None and not _on_cooldown((camera.id, "watchlist", vehicle.id)):
        rule = db.query(models.AlertRule).filter(
            models.AlertRule.rule_type == "watchlist_plate", models.AlertRule.active == True  # noqa: E712
        ).first()
        rule_id = rule.id if rule else None
        signals.watchlist_priority = entry.priority
        signals.plate_text = str(vehicle.plate_text or "")

        # Confidence-aware intelligence: the match itself is never suppressed
        # for being uncertain, but its severity must not overstate how sure
        # the plate read actually is. Below the floor, cap at HIGH and say so
        # in the reason string — the operator sees "needs confirmation" rather
        # than an unqualified CRITICAL that looks identical to a confident hit.
        #
        # CORROBORATION IS A SEPARATE GATE, and it is not optional (A1, 2026-09-12).
        #
        # Measured on the labelled benchmark: OCR confidence does NOT separate
        # correct reads from wrong ones. Correct reads span 0.262-0.990; wrong
        # plate-shaped reads span 0.260-0.956, and SIX OF SEVEN wrong reads sit
        # at or above the lowest correct read's confidence. No threshold on this
        # corpus reaches precision above 0.5 — see docs/ANPR_ACCURACY.md, "A1".
        #
        # The concrete failure that forces this: `UP84AE9889` was misread as
        # `UP81AE9889` at confidence 0.956. Under a confidence-only gate that
        # single uncorroborated frame clears the 0.60 floor, raises a CRITICAL
        # watchlist alert and auto-opens an incident — naming a vehicle that was
        # never there. In a police deployment that is a wrongful-stop risk, and
        # it is exactly what the temporal layer was built to prevent.
        #
        # So CRITICAL now requires BOTH a confident read AND corroboration across
        # frames. An uncorroborated match is still raised — a real watchlist hit
        # is never silenced — but capped at HIGH and labelled, so an operator
        # confirms before acting. `corroborated=None` (rows written before this
        # existed, and the legacy single-frame path) is treated as NOT
        # corroborated: unknown provenance must not buy CRITICAL severity.
        plate_confidence = float(vehicle.plate_confidence or 0.0)
        corroborated = _plate_is_corroborated(vehicle)
        signals.plate_corroborated = corroborated
        # The escape hatch is real, not decoration: a deployment that would
        # rather have the previous confidence-only escalation can set
        # WATCHLIST_REQUIRE_CORROBORATION=false in one env var.
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
            signals.zone_severity = str(zone.severity or "MEDIUM")
            signals.zone_name = str(zone.name or "")

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
                    signals.loitering_seconds = dwell

    if not reasons:
        return alerts

    # --- Risk score ---
    # Vehicle-history signals are only counted when a vehicle was actually
    # identified. For a bare zone_entry with no plate there is no vehicle
    # history to speak of, and inventing zeroes as if there were would be a
    # different (wrong) statement from having no data.
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

    # The rule-derived severity acts as a FLOOR, never a ceiling. The risk score
    # can only escalate an alert, never quietly downgrade one that an explicit
    # rule already classified as CRITICAL — a scoring change must not be able to
    # make an existing rule matter less than it did before.
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
    db.flush()
    alerts.append(alert)
    metrics.ALERTS_TOTAL.labels(camera_code=str(camera.camera_code), severity=severity).inc()

    # Auto-create — or CORRELATE INTO — an incident for CRITICAL alerts.
    #
    # Pre-V2 every CRITICAL alert opened its own incident, so one real event
    # (watchlisted vehicle enters a restricted zone, then crosses three more
    # cameras) became four separate incidents an operator had to mentally
    # reassemble. Now a qualifying alert is attached to the open incident it
    # belongs to, and the incident's own description/priority/title grow to
    # describe the whole event.
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
            # Captured into locals BEFORE assignment: `existing` is persistent,
            # so a rollback expires these mutations back to their committed
            # values and the retry must reassign from here, not re-read them.
            subject = f"Watchlisted vehicle {vehicle.plate_text}" if vehicle else f"Restricted-zone activity on {camera.camera_code}"
            target_title = (
                f"{subject} — {len(linked_alert_ids)} correlated alerts across "
                f"{len(cameras_involved)} camera(s)"
            )
            # New reasons are appended, never replacing the incident's history —
            # the description is the running narrative of the whole event.
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
            db.flush()

        # Every alert belonging to an incident is linked here, including the one
        # that opened it — so a caller never has to check two relationships to
        # enumerate an incident's alerts.
        incident_link = models.IncidentAlert(
            incident_id=incident.id, alert_id=alert.id, correlation_reason=correlation_reason,
        )
        db.add(incident_link)
        db.flush()
        # Split by outcome so the correlation's actual effect is measurable:
        # a rising `correlated` share against a flat `opened` share is the
        # alert-noise reduction this feature exists to deliver.
        metrics.INCIDENTS_TOTAL.labels(outcome="correlated" if incident_targets else "opened").inc()
        if detection.snapshot_path:
            # Final-demo-readiness-phase finding: this Evidence row omitted
            # alert_id/detection_id/event_type/source_timestamp — fields
            # worker.py's OWN evidence-backfill block (the other real path
            # that creates an Evidence row) already sets for the identical
            # model. Found live: a real CRITICAL watchlist-match evidence
            # record showed "Alert: —" in the UI despite a real alert
            # having triggered it. Matched to worker.py's shape, not
            # inventing new fields.
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
                # Provenance (10/10 roadmap P8) — see worker.py's identical
                # evidence-creation site for why this is stamped once, here,
                # rather than derived later from settings' current value.
                model_version=settings.model_version,
                rule_version=settings.rule_version,
            )
            db.add(incident_evidence)

    # alert / incident_link / incident_evidence are all freshly db.add()'d in
    # this same call — never persisted before — so on a transient SQLite lock a
    # rollback only detaches them; their already-set Python attributes
    # (including each one's client-side-generated PK from the db.flush()
    # calls above) survive untouched, so re-add() alone correctly restores
    # them for a retry (verified empirically — see pipeline/db_retry.py).
    #
    # `incident` is the exception: when this alert CORRELATED into an existing
    # incident, that row is persistent and was mutated in place, so a rollback
    # reverts its fields rather than merely detaching it. Those fields are
    # therefore reassigned from `incident_targets`, captured above.
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
    # Never batched: an operator waiting even a fraction of a second longer for
    # a CRITICAL watchlist hit is the wrong trade (see ws.py).
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
        # Only a NEWLY opened incident is announced. A correlated alert joining
        # an existing incident is not a new incident, and announcing it as one
        # would recreate exactly the operator-flooding correlation exists to fix.
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
