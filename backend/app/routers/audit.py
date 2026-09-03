from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
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
        q = q.filter(models.AuditLog.username.ilike(f"%{actor}%"))
    if action:
        q = q.filter(models.AuditLog.action.ilike(f"%{action}%"))
    return q.order_by(models.AuditLog.timestamp.desc()).limit(500).all()
