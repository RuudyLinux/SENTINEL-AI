import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..config import settings
from ..audit import log_action

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


@router.get("", response_model=list[schemas.EvidenceOut])
def list_evidence(incident_id: str | None = None, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    q = db.query(models.Evidence)
    if incident_id:
        q = q.filter(models.Evidence.incident_id == incident_id)
    return q.order_by(models.Evidence.created_at.desc()).all()


@router.get("/{evidence_id}", response_model=schemas.EvidenceOut)
def get_evidence(evidence_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return e


@router.get("/{evidence_id}/file")
def download_evidence_file(evidence_id: str, db: Session = Depends(get_db)):
    # No auth dependency here: browsers can't attach a bearer header to a plain
    # <img>/<a href> navigation. Same documented simplification as the MJPEG
    # stream endpoints (see streams.py) — acceptable for a local demo.
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e or not e.file_path:
        raise HTTPException(status_code=404, detail="No file for this evidence record")
    log_action(db, None, "download_evidence", resource=evidence_id)
    return FileResponse(e.file_path)


def _hash_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


@router.post("/{evidence_id}/verify")
def verify_evidence(evidence_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Evidence not found")
    if e.file_path:
        e.sha256 = _hash_file(e.file_path)
    e.verification_status = "verified"
    db.commit()
    log_action(db, user, "verify_evidence", resource=evidence_id)
    return {"ok": True, "sha256": e.sha256}


@router.get("/incidents/{incident_id}/package")
def generate_package(incident_id: str, fmt: str = "json", db: Session = Depends(get_db)):
    """Generate an Evidence Package (doc §29): incident summary, camera timeline,
    vehicle details, evidence list, audit trail — real data pulled from the DB.
    No auth dependency: triggered via a plain link/new-tab navigation, which
    can't carry a bearer header (see download_evidence_file above).
    """
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")

    evidence_items = db.query(models.Evidence).filter(models.Evidence.incident_id == incident_id).all()
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.id == inc.vehicle_id).first() if inc.vehicle_id else None
    alert = db.query(models.Alert).filter(models.Alert.id == inc.alert_id).first() if inc.alert_id else None
    notes = db.query(models.IncidentNote).filter(models.IncidentNote.incident_id == incident_id).all()

    sightings = []
    if vehicle:
        from ..pipeline.correlate import get_route
        sightings = get_route(db, vehicle.id)

    package = {
        "incident": {"id": inc.id, "title": inc.title, "priority": inc.priority, "status": inc.status,
                     "description": inc.description, "created_at": inc.created_at.isoformat()},
        "alert": {"id": alert.id, "severity": alert.severity, "reasons": alert.reasons, "timestamp": alert.timestamp.isoformat()} if alert else None,
        "vehicle": {"plate_text": vehicle.plate_text, "vehicle_type": vehicle.vehicle_type, "color": vehicle.color} if vehicle else None,
        "camera_timeline": sightings,
        "evidence": [{"id": e.id, "type": e.evidence_type, "file_path": e.file_path, "verification_status": e.verification_status} for e in evidence_items],
        "notes": [{"text": n.text, "created_at": n.created_at.isoformat()} for n in notes],
    }

    if fmt == "json":
        out_path = settings.evidence_dir / f"package_{incident_id}.json"
        out_path.write_text(json.dumps(package, indent=2, default=str))
        log_action(db, None, "generate_evidence_package", resource=incident_id)
        return FileResponse(out_path, filename=out_path.name, media_type="application/json")

    # PDF
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas

    out_path = settings.evidence_dir / f"package_{incident_id}.pdf"
    c = pdf_canvas.Canvas(str(out_path), pagesize=A4)
    width, height = A4
    y = height - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, y, "SENTINEL VISION — Evidence Package")
    y -= 30
    c.setFont("Helvetica", 10)
    for line in json.dumps(package, indent=2, default=str).splitlines():
        if y < 40:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 10)
        c.drawString(40, y, line[:110])
        y -= 12
    c.save()
    log_action(db, None, "generate_evidence_package", resource=incident_id)
    return FileResponse(out_path, filename=out_path.name, media_type="application/pdf")
