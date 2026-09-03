from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..audit import log_action

router = APIRouter(prefix="/api/zones", tags=["zones"])


@router.get("", response_model=list[schemas.ZoneOut])
def list_zones(camera_id: str | None = None, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    q = db.query(models.Zone)
    if camera_id:
        q = q.filter(models.Zone.camera_id == camera_id)
    return q.all()


@router.post("", response_model=schemas.ZoneOut)
def create_zone(payload: schemas.ZoneCreate, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    zone = models.Zone(**payload.model_dump())
    db.add(zone)
    db.commit()
    db.refresh(zone)
    log_action(db, user, "create_zone", resource=zone.name)
    return zone


@router.delete("/{zone_id}")
def delete_zone(zone_id: str, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    zone = db.query(models.Zone).filter(models.Zone.id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=404, detail="Zone not found")
    zone.active = False
    db.commit()
    log_action(db, user, "disable_zone", resource=zone_id)
    return {"ok": True}
