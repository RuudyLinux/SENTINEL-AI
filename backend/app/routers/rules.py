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


# The rule types pipeline/rules_engine.py actually evaluates. A rule stored
# with any other type is inert: nothing reads it, so it sits in the rules list
# looking like an active control while firing nothing.
_RULE_TYPES = ("watchlist_plate", "zone_entry", "loitering")


@router.post("", response_model=schemas.AlertRuleOut)
def create_rule(payload: schemas.AlertRuleCreate, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    """Create an alert rule, refusing configurations that cannot ever fire.

    `rule_type` was a free string, and `zone_id` went unchecked — which since
    foreign keys were enforced meant an unknown zone raised an unhandled
    IntegrityError (a 500 carrying a raw database error). A loitering rule
    without a zone is the quiet version of the same problem: rules_engine
    applies loitering only to the zone a rule names, so one with no zone
    watches nothing.
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
