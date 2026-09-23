"""Privacy / governance controls (10/10 roadmap P13).

This module provides MECHANISM (configurable retention, an audited purge
workflow, operator accountability), never POLICY. `settings.evidence_retention_days`
is not a legal claim — this platform cannot know which retention period
applies to a given deployment's jurisdiction, agency policy, or an active
investigation. See docs/PRIVACY_GOVERNANCE.md.
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
    """What is configured, and how many evidence rows are currently eligible
    for purge under it — read-only, safe for an Auditor to check at any time
    without the ability to trigger deletion (that requires Administrator)."""
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
    """Purge evidence older than `evidence_retention_days`. Dry-run by
    default: `dry_run=True` (or omitted) lists what WOULD be purged without
    touching anything. Real deletion requires BOTH `dry_run=False` AND
    `confirm=True` in the same request. Every real purge is fully audited —
    which evidence ids, by whom, when — because an unaudited deletion of
    evidence is exactly the kind of action a chain-of-custody system must
    never allow silently."""
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
                # A file that cannot be removed must not silently pretend to
                # have been purged — its DB row is left in place so the
                # discrepancy is visible, not swallowed.
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
