from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..audit import log_action

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
