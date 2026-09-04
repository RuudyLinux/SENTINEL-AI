import hashlib
import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, create_resource_token, get_user_from_resource_token
from ..config import settings
from ..audit import log_action

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


def _safe_evidence_path(raw_path: str) -> Path:
    """Every current write path for Evidence.file_path is server-generated
    (worker.py._save_snapshot, pipeline/clips.py — timestamped filenames
    under settings.evidence_dir, never client input), so this is defense in
    depth rather than a fix for a reachable exploit today: resolves the
    stored path and refuses to serve anything outside the evidence
    directory, so a future bug (or a bad row from some other path) can
    never turn this endpoint into an arbitrary-file-read."""
    resolved = Path(raw_path).resolve()
    evidence_root = settings.evidence_dir.resolve()
    if evidence_root not in resolved.parents and resolved != evidence_root:
        raise HTTPException(status_code=404, detail="Evidence file not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Evidence file not found")
    return resolved


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


@router.get("/{evidence_id}/file-token")
def get_evidence_file_token(evidence_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """RBAC-checked (login required) + audited step that hands out a
    short-lived token scoped to exactly this evidence file (P0-E)."""
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Evidence not found")
    log_action(db, user, "request_evidence_file_token", resource=evidence_id)
    return {"token": create_resource_token("evidence_file", evidence_id, user, settings.evidence_token_ttl_seconds)}


@router.get("/{evidence_id}/file")
def download_evidence_file(evidence_id: str, token: str, db: Session = Depends(get_db)):
    # Browsers can't attach an Authorization: Bearer header to a plain
    # <img src>/<a href> navigation, so this validates a short-lived signed
    # resource token instead of dropping auth entirely (P0-E). The token is
    # only obtainable via the RBAC-checked, audited /file-token endpoint above.
    user = get_user_from_resource_token("evidence_file", evidence_id, token, db)
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e or not e.file_path:
        raise HTTPException(status_code=404, detail="No file for this evidence record")
    safe_path = _safe_evidence_path(e.file_path)
    log_action(db, user, "download_evidence", resource=evidence_id)
    return FileResponse(safe_path)


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


@router.get("/incidents/{incident_id}/package-token")
def get_package_token(incident_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    log_action(db, user, "request_evidence_package_token", resource=incident_id)
    return {"token": create_resource_token("evidence_package", incident_id, user, settings.evidence_token_ttl_seconds)}


@router.get("/incidents/{incident_id}/package")
def generate_package(incident_id: str, token: str, fmt: str = "json", db: Session = Depends(get_db)):
    """Generate an Evidence Package (doc §29): incident summary, camera timeline,
    vehicle details, evidence list, notes, and audit trail — real data pulled
    from the DB. Triggered via a plain link/new-tab navigation (can't carry a
    bearer header), so it validates a short-lived signed resource token
    instead of dropping auth entirely (P0-E) — see /package-token above.
    """
    user = get_user_from_resource_token("evidence_package", incident_id, token, db)
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

    # Chain-of-custody: actual AuditLog rows touching this incident or any
    # of its evidence items, not just a label claiming one exists.
    audit_resources = [incident_id] + [e.id for e in evidence_items]
    audit_trail = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.resource.in_(audit_resources))
        .order_by(models.AuditLog.timestamp.asc())
        .all()
    )

    package = {
        "incident": {"id": inc.id, "title": inc.title, "priority": inc.priority, "status": inc.status,
                     "description": inc.description, "created_at": inc.created_at.isoformat()},
        "alert": {"id": alert.id, "severity": alert.severity, "reasons": alert.reasons, "timestamp": alert.timestamp.isoformat()} if alert else None,
        "vehicle": {"plate_text": vehicle.plate_text, "vehicle_type": vehicle.vehicle_type, "color": vehicle.color} if vehicle else None,
        "camera_timeline": sightings,
        "evidence": [{"id": e.id, "type": e.evidence_type, "file_path": e.file_path, "verification_status": e.verification_status, "sha256": e.sha256} for e in evidence_items],
        "notes": [{"text": n.text, "created_at": n.created_at.isoformat()} for n in notes],
        "audit_trail": [{"timestamp": a.timestamp.isoformat(), "username": a.username, "action": a.action,
                          "resource": a.resource, "result": a.result} for a in audit_trail],
        "generated_by": user.username,
    }

    if fmt == "json":
        out_path = settings.evidence_dir / f"package_{incident_id}.json"
        out_path.write_text(json.dumps(package, indent=2, default=str))
        log_action(db, user, "generate_evidence_package", resource=incident_id)
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
    log_action(db, user, "generate_evidence_package", resource=incident_id)
    return FileResponse(out_path, filename=out_path.name, media_type="application/pdf")
