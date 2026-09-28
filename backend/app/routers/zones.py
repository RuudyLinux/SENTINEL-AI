from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user, require_roles
from ..audit import log_action

router = APIRouter(prefix="/api/zones", tags=["zones"])


@router.get("", response_model=list[schemas.ZoneOut])
def list_zones(
    camera_id: str | None = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """Active zones by default, include_inactive=true for all.

    DELETE is a soft delete, and listing every row kept deleted zones on the
    map and in the rules page dropdown. A rule on a deleted zone never fires
    (rules_engine only looks at active zones), so it looked configured and
    did nothing.
    """
    q = db.query(models.Zone)
    if not include_inactive:
        q = q.filter(models.Zone.active == True)  # noqa: E712
    if camera_id:
        q = q.filter(models.Zone.camera_id == camera_id)
    return q.all()


@router.post("", response_model=schemas.ZoneOut)
def create_zone(payload: schemas.ZoneCreate, db: Session = Depends(get_db), user: models.User = Depends(require_roles("Administrator", "Supervisor"))):
    """Create a zone on a camera.

    404 for an unknown camera. 400 for coordinates outside 0..1 or an inverted
    box, which would create a zone that can never fire (the rules engine checks
    x1 <= cx <= x2).
    """
    if not db.query(models.Camera).filter(models.Camera.id == payload.camera_id).first():
        raise HTTPException(status_code=404, detail="Camera not found")
    for name, value in (("x1", payload.x1), ("y1", payload.y1), ("x2", payload.x2), ("y2", payload.y2)):
        if not 0.0 <= value <= 1.0:
            raise HTTPException(status_code=400, detail=f"{name} must be a frame fraction between 0 and 1")
    if payload.x1 >= payload.x2 or payload.y1 >= payload.y2:
        raise HTTPException(
            status_code=400,
            detail="Zone box is empty or inverted (x1 must be < x2 and y1 < y2) — it could never match a detection",
        )
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
