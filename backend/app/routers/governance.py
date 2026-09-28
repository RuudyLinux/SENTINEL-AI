"""Privacy / governance controls.

Mechanism only (configurable retention, an audited purge, accountability),
never policy. evidence_retention_days isn't a legal claim; the platform can't
know what period applies to a jurisdiction, agency or open investigation.
See docs/PRIVACY_GOVERNANCE.md.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..audit import log_action
from ..config import settings
from ..db import get_db
from ..security import require_roles

router = APIRouter(prefix="/api/governance", tags=["governance"])


def _expired_evidence_query(db: Session):
    if settings.evidence_retention_days is None:
        return None
    cutoff = datetime.utcnow() - timedelta(days=settings.evidence_retention_days)
    return db.query(models.Evidence).filter(models.Evidence.created_at < cutoff)


@router.get("/retention-policy")
def retention_policy(db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Auditor"))):
    """What's configured and how many evidence rows are eligible for purge.
    Read-only, fine for an Auditor; purging needs Administrator."""
    q = _expired_evidence_query(db)
    return {
        "evidence_retention_days": settings.evidence_retention_days,
        "configured": settings.evidence_retention_days is not None,
        "eligible_for_purge": q.count() if q is not None else 0,
        "note": (
            "evidence_retention_days is operator/policy-configured, not a legal "
            "compliance claim — see docs/PRIVACY_GOVERNANCE.md."
        ),
    }


@router.post("/purge-expired")
def purge_expired(
    payload: schemas.PurgeExpiredRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator")),
):
    """Purge evidence older than evidence_retention_days. Dry run by default;
    deleting requires dry_run=False and confirm=True. Every purge is audited
    (ids, actor, time)."""
    if settings.evidence_retention_days is None:
        raise HTTPException(
            status_code=400,
            detail="evidence_retention_days is not configured — no automatic retention policy is active. "
                   "Set it explicitly (see docs/PRIVACY_GOVERNANCE.md) before purging.",
        )

    q = _expired_evidence_query(db)
    eligible = q.all()
    summary = {
        "evidence_retention_days": settings.evidence_retention_days,
        "eligible_count": len(eligible),
        "eligible_ids": [e.id for e in eligible],
        "dry_run": True,
        "deleted_count": 0,
    }

    if payload.dry_run or not payload.confirm:
        log_action(db, user, "governance_purge_dry_run", resource=f"{len(eligible)} eligible")
        return summary

    deleted_ids = []
    for evidence in eligible:
        if evidence.file_path:
            try:
                import os
                if os.path.exists(evidence.file_path):
                    os.remove(evidence.file_path)
            except OSError:
                # couldn't remove the file: keep the DB row so the mismatch shows
                continue
        db.delete(evidence)
        deleted_ids.append(evidence.id)
    db.commit()

    log_action(
        db, user, "governance_purge_expired",
        resource=f"{len(deleted_ids)} evidence rows: {','.join(deleted_ids)[:500]}",
    )
    summary["dry_run"] = False
    summary["deleted_count"] = len(deleted_ids)
    summary["eligible_ids"] = deleted_ids
    return summary


def _purgeable_detections_query(db: Session):
    """Old detections not referenced by an alert, plate read or evidence item."""
    if settings.detection_retention_days is None:
        return None
    cutoff = datetime.utcnow() - timedelta(days=settings.detection_retention_days)
    referenced = (
        db.query(models.Alert.detection_id).filter(models.Alert.detection_id.isnot(None))
        .union(db.query(models.Plate.detection_id).filter(models.Plate.detection_id.isnot(None)))
        .union(db.query(models.Evidence.detection_id).filter(models.Evidence.detection_id.isnot(None)))
    )
    return db.query(models.Detection).filter(
        models.Detection.timestamp < cutoff,
        models.Detection.id.notin_(referenced),
    )


@router.post("/purge-detections")
def purge_detections(
    payload: schemas.PurgeExpiredRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator")),
):
    """Purge raw detections older than detection_retention_days.

    Same rules as /purge-expired (dry run unless dry_run=False and
    confirm=True, always audited). Alerts, incidents, evidence, plate reads,
    tracks, the audit log and any detection they reference are untouched.
    """
    if settings.detection_retention_days is None:
        raise HTTPException(
            status_code=400,
            detail="detection_retention_days is not configured — no detection retention policy is active.",
        )
    q = _purgeable_detections_query(db)
    eligible = q.count()
    summary = {
        "detection_retention_days": settings.detection_retention_days,
        "eligible_count": eligible,
        "dry_run": True,
        "deleted_count": 0,
    }
    if payload.dry_run or not payload.confirm:
        log_action(db, user, "governance_detection_purge_dry_run", resource=f"{eligible} eligible")
        return summary
    deleted = q.delete(synchronize_session=False)
    db.commit()
    log_action(db, user, "governance_purge_detections", resource=f"{deleted} detection rows")
    summary.update(dry_run=False, deleted_count=deleted)
    return summary
