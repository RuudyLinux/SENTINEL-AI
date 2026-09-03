from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..audit import log_action

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("", response_model=list[schemas.AlertRuleOut])
def list_rules(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.query(models.AlertRule).all()


@router.post("", response_model=schemas.AlertRuleOut)
def create_rule(payload: schemas.AlertRuleCreate, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    rule = models.AlertRule(**payload.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    log_action(db, user, "create_rule", resource=rule.name)
    return rule


@router.post("/{rule_id}/disable")
def disable_rule(rule_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    rule = db.query(models.AlertRule).filter(models.AlertRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.active = False
    db.commit()
    log_action(db, user, "disable_rule", resource=rule_id)
    return {"ok": True}


@router.delete("/{rule_id}")
def delete_rule(rule_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator"))):
    rule = db.query(models.AlertRule).filter(models.AlertRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    db.delete(rule)
    db.commit()
    log_action(db, user, "delete_rule", resource=rule_id)
    return {"ok": True}
