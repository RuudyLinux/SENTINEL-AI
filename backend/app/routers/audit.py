from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..audit import verify_chain
from ..db import LIKE_ESCAPE, get_db, like_pattern
from ..security import require_roles

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[schemas.AuditOut])
def list_audit(
    actor: Optional[str] = None,
    action: Optional[str] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_roles("Administrator", "Auditor")),
):
    q = db.query(models.AuditLog)
    if actor:
        q = q.filter(models.AuditLog.username.ilike(like_pattern(actor), escape=LIKE_ESCAPE))
    if action:
        q = q.filter(models.AuditLog.action.ilike(like_pattern(action), escape=LIKE_ESCAPE))
    return q.order_by(models.AuditLog.timestamp.desc()).limit(500).all()


@router.get("/verify-chain")
def verify_audit_chain(
    db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Auditor")),
):
    """Walks the full tamper-evident hash chain (10/10 roadmap P9) and reports
    whether it is intact, or exactly where it first breaks. See app/audit.py
    for what the chain actually covers and cannot cover (pre-chain rows)."""
    return verify_chain(db)
