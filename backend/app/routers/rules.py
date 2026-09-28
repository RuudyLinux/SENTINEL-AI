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


# rule types rules_engine actually evaluates. anything else would sit in the
# list looking active and never fire
_RULE_TYPES = ("watchlist_plate", "zone_entry", "loitering")


@router.post("", response_model=schemas.AlertRuleOut)
def create_rule(payload: schemas.AlertRuleCreate, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    """Create an alert rule, refusing ones that can never fire.

    Unknown rule_type is refused, and an unknown zone_id (a 500 once FKs were
    enforced) is a 404. A loitering rule needs a zone; rules_engine only
    applies loitering to the zone a rule names, so without one it watches
    nothing.
    """
    if payload.rule_type not in _RULE_TYPES:
        raise HTTPException(status_code=400, detail=f"rule_type must be one of {list(_RULE_TYPES)}")
    if payload.rule_type == "loitering" and not payload.zone_id:
        raise HTTPException(status_code=400, detail="A loitering rule must name the zone it applies to")
    if payload.zone_id and not db.query(models.Zone).filter(models.Zone.id == payload.zone_id).first():
        raise HTTPException(status_code=404, detail="Zone not found")
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
