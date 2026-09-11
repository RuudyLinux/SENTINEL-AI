from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..audit import log_action
from .. import watchlist

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


@router.get("", response_model=list[schemas.IncidentOut])
def list_incidents(status: str | None = None, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    q = db.query(models.Incident)
    if status:
        q = q.filter(models.Incident.status == status)
    return q.order_by(models.Incident.created_at.desc()).all()


@router.post("", response_model=schemas.IncidentOut)
def create_incident(payload: schemas.IncidentCreate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    incident = models.Incident(**payload.model_dump())
    db.add(incident)
    db.commit()
    db.refresh(incident)
    log_action(db, user, "create_incident", resource=incident.id)
    return incident


@router.get("/{incident_id}", response_model=schemas.IncidentOut)
def get_incident(incident_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc


@router.get("/{incident_id}/summary")
def incident_summary(incident_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Investigator Summary (10/10 roadmap P10): everything backing "what
    happened, why was it flagged, where was it seen, what evidence supports
    it, how confident are we" gathered into one call, so the incident page
    does not require an operator to manually chase five different endpoints.
    Every value here is read from rows already computed elsewhere (risk.py,
    plate_tracker via the Plate table, correlate.get_route, evidence.verify) —
    nothing is recomputed or invented for this view.
    """
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    linked_ids = {row.alert_id for row in db.query(models.IncidentAlert).filter(models.IncidentAlert.incident_id == incident_id).all()}
    if inc.alert_id:
        linked_ids.add(inc.alert_id)
    alerts = db.query(models.Alert).filter(models.Alert.id.in_(linked_ids)).order_by(models.Alert.timestamp.asc()).all() if linked_ids else []
    primary_alert = next((a for a in alerts if a.id == inc.alert_id), alerts[0] if alerts else None)

    vehicle = db.query(models.Vehicle).filter(models.Vehicle.id == inc.vehicle_id).first() if inc.vehicle_id else None
    # Read from the entry, not from the vehicle's cached flag: the summary
    # tells an investigator this vehicle is on the watchlist, so it must be
    # true at read time. A stale-True flag would assert a match that had been
    # deactivated; a stale-False one would hide a live match.
    watchlist_entry = watchlist.plate_entry_in_force(db, vehicle.plate_text) if vehicle else None

    route = []
    plate_reads_total = 0
    if vehicle:
        from ..pipeline.correlate import get_route
        route = get_route(db, vehicle.id)
        plate_reads_total = sum(hop.get("reads_count", 1) for hop in route)

    evidence_items = db.query(models.Evidence).filter(models.Evidence.incident_id == incident_id).all()

    return {
        "incident_id": incident_id,
        "what": inc.title,
        "why": [reason for a in alerts for reason in (a.reasons or [])],
        "risk": {
            "score": primary_alert.risk_score if primary_alert else 0,
            "factors": primary_alert.risk_factors if primary_alert else [],
        },
        "vehicle": {
            "plate_text": vehicle.plate_text if vehicle else None,
            "plate_confidence": vehicle.plate_confidence if vehicle else None,
            "total_plate_reads": plate_reads_total,
            "watchlist_match": {
                "priority": watchlist_entry.priority, "reason": watchlist_entry.reason,
            } if watchlist_entry else None,
        } if vehicle else None,
        "where": {
            "cameras_visited": len({hop["camera_id"] for hop in route}),
            "route": route,
        },
        "related_alerts": [
            {
                "id": a.id, "severity": a.severity, "risk_score": a.risk_score,
                "reasons": a.reasons, "timestamp": a.timestamp.isoformat(),
                "feedback": a.feedback,
            }
            for a in alerts
        ],
        "evidence": [
            {
                "id": e.id, "evidence_type": e.evidence_type,
                "verification_status": e.verification_status,
                "sha256": e.sha256, "model_version": e.model_version, "rule_version": e.rule_version,
            }
            for e in evidence_items
        ],
        "evidence_fully_verified": bool(evidence_items) and all(e.verification_status == "verified" for e in evidence_items),
    }


@router.get("/{incident_id}/timeline")
def incident_timeline(incident_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    events = []
    if inc.alert_id:
        alert = db.query(models.Alert).filter(models.Alert.id == inc.alert_id).first()
        if alert:
            events.append({"timestamp": alert.timestamp, "label": f"Alert fired: {', '.join(alert.reasons)}"})
    if inc.vehicle_id:
        from ..pipeline.correlate import get_route
        for s in get_route(db, inc.vehicle_id):
            events.append({"timestamp": s["timestamp"], "label": f"Vehicle sighted on {s['camera_code']} ({s['camera_name']})"})
    notes = db.query(models.IncidentNote).filter(models.IncidentNote.incident_id == incident_id).all()
    for n in notes:
        events.append({"timestamp": n.created_at, "label": f"Note: {n.text}"})
    events.sort(key=lambda e: e["timestamp"])
    return {"incident_id": incident_id, "events": events}


@router.post("/{incident_id}/notes")
def add_note(incident_id: str, payload: schemas.IncidentNoteCreate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    note = models.IncidentNote(incident_id=incident_id, author_id=user.id, text=payload.text)
    db.add(note)
    inc.updated_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "add_incident_note", resource=incident_id)
    return {"ok": True}


@router.post("/{incident_id}/assign")
def assign_incident(incident_id: str, assignee_user_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    inc.assigned_to = assignee_user_id
    inc.status = "in_progress"
    inc.updated_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "assign_incident", resource=incident_id)
    return {"ok": True}


@router.post("/{incident_id}/close")
def close_incident(incident_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    inc.status = "closed"
    inc.updated_at = datetime.utcnow()
    db.commit()
    log_action(db, user, "close_incident", resource=incident_id)
    return {"ok": True}
