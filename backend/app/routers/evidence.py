import json
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, create_resource_token, get_user_from_resource_token
from ..config import settings
from ..audit import log_action
from ..evidence_hash import sha256_file

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


def _safe_evidence_path(raw_path: str) -> Path:
    """Refuse to serve anything outside the evidence directory. All write paths
    are server-generated; this is defence in depth against a bad row or a future
    bug turning downloads into arbitrary file reads."""
    resolved = Path(raw_path).resolve()
    evidence_root = settings.evidence_dir.resolve()
    if evidence_root not in resolved.parents and resolved != evidence_root:
        raise HTTPException(status_code=404, detail="Evidence file not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Evidence file not found")
    return resolved


@router.get("", response_model=list[schemas.EvidenceOut])
def list_evidence(
    incident_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Most recent evidence, newest first, optionally for one incident. Bounded;
    ge=1 because SQLite treats LIMIT -1 as unlimited.
    """
    q = db.query(models.Evidence)
    if incident_id:
        q = q.filter(models.Evidence.incident_id == incident_id)
    return q.order_by(models.Evidence.created_at.desc()).limit(limit).all()


@router.get("/{evidence_id}", response_model=schemas.EvidenceOut)
def get_evidence(evidence_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return e


@router.get("/{evidence_id}/file-token")
def get_evidence_file_token(evidence_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Issue a short-lived token for exactly this evidence file (audited)."""
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Evidence not found")
    log_action(db, user, "request_evidence_file_token", resource=evidence_id)
    return {"token": create_resource_token("evidence_file", evidence_id, user, settings.evidence_token_ttl_seconds)}


@router.get("/{evidence_id}/file")
def download_evidence_file(evidence_id: str, token: str, db: Session = Depends(get_db)):
    # <img src>/<a href> can't send a bearer header, so this takes a
    # short-lived signed token, only obtainable from /file-token above
    user = get_user_from_resource_token("evidence_file", evidence_id, token, db)
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e or not e.file_path:
        raise HTTPException(status_code=404, detail="No file for this evidence record")
    safe_path = _safe_evidence_path(e.file_path)
    log_action(db, user, "download_evidence", resource=evidence_id)
    return FileResponse(safe_path)


@router.post("/{evidence_id}/verify")
def verify_evidence(evidence_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Re-hash an evidence file and compare it with the digest taken at capture.

    - verified: matches the capture digest
    - tampered: doesn't match; the stored digest is never overwritten
    - unverifiable: file missing or unreadable
    - no_baseline: no capture digest exists; one is recorded now, but this call
      can't claim the file is unaltered
    """
    e = db.query(models.Evidence).filter(models.Evidence.id == evidence_id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Evidence not found")

    current = sha256_file(e.file_path)
    baseline = (e.sha256 or "").strip()

    if not e.file_path or not current:
        outcome, matches = "unverifiable", False
        detail = "The evidence file is missing or could not be read."
    elif not baseline:
        e.sha256 = current
        outcome, matches = "no_baseline", False
        detail = (
            "No capture-time digest existed for this record, so integrity could not be "
            "confirmed. The current digest has been stored as the baseline for future checks."
        )
    elif current == baseline:
        outcome, matches = "verified", True
        detail = "The file matches the digest recorded when it was captured."
    else:
        outcome, matches = "tampered", False
        detail = "The file does NOT match its capture-time digest. The original digest is preserved."

    e.verification_status = outcome
    db.commit()
    # audited with the real outcome; a tamper result is exactly what the
    # trail is for
    log_action(
        db, user, "verify_evidence", resource=evidence_id,
        result="SUCCESS" if matches else "FAILURE",
    )
    return {
        "ok": matches,
        "status": outcome,
        "detail": detail,
        "sha256": e.sha256,
        "computed_sha256": current,
    }


def _mask_plate(plate: str) -> str:
    """GJ05AB1234 -> GJ******34: enough to match documents about the same
    vehicle without disclosing the registration."""
    if len(plate) <= 4:
        return "*" * len(plate)
    return f"{plate[:2]}{'*' * (len(plate) - 4)}{plate[-2:]}"


def _redact_package(package: dict, plate: "str | None") -> dict:
    """Mask a registration everywhere in the package.

    Whole-document substitution, because the plate also appears in alert
    reasons, incident text and audit resources, not just vehicle.plate_text.
    Integrity data (ids, digests, verification status) is left intact, so a
    redacted package still verifies.
    """
    if not plate:
        return package
    masked = _mask_plate(plate)
    # serialize, substitute, re-parse: catches nested and future keys too
    blob = json.dumps(package, default=str).replace(plate, masked)
    redacted = json.loads(blob)
    redacted["redaction"] = {
        "applied": True,
        "scheme": "registration masked (first two and last two characters retained)",
        "note": "Evidence ids, SHA-256 digests and verification statuses are NOT redacted, "
                "so this package can still be verified against the source evidence.",
    }
    return redacted


@router.get("/incidents/{incident_id}/package-token")
def get_package_token(incident_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    inc = db.query(models.Incident).filter(models.Incident.id == incident_id).first()
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    log_action(db, user, "request_evidence_package_token", resource=incident_id)
    return {"token": create_resource_token("evidence_package", incident_id, user, settings.evidence_token_ttl_seconds)}


@router.get("/incidents/{incident_id}/package")
def generate_package(
    incident_id: str, token: str, fmt: str = "json", redact: bool = False,
    db: Session = Depends(get_db),
):
    """Evidence package: incident summary, camera timeline, vehicle details,
    evidence list, notes and audit trail. Opened by plain navigation, so it
    takes a short-lived signed token from /package-token instead of a bearer
    header.

    redact=true masks the registration throughout for wider distribution. The
    unredacted package is the evidentiary artefact; the mode used is recorded
    in the package and the audit trail.
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

    # chain of custody: the actual AuditLog rows for this incident and its evidence
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

    if redact:
        package = _redact_package(package, vehicle.plate_text if vehicle else None)
    # redacted and full exports are different disclosures, record which
    audit_action = "generate_evidence_package_redacted" if redact else "generate_evidence_package"
    suffix = "_redacted" if redact else ""

    if fmt == "json":
        out_path = settings.evidence_dir / f"package_{incident_id}{suffix}.json"
        out_path.write_text(json.dumps(package, indent=2, default=str))
        log_action(db, user, audit_action, resource=incident_id)
        return FileResponse(out_path, filename=out_path.name, media_type="application/json")

    # PDF
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas

    out_path = settings.evidence_dir / f"package_{incident_id}{suffix}.pdf"
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
    log_action(db, user, audit_action, resource=incident_id)
    return FileResponse(out_path, filename=out_path.name, media_type="application/pdf")
