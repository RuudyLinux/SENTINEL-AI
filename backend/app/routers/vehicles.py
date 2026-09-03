from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..pipeline.correlate import get_route
from ..pipeline.anpr import normalize_plate

router = APIRouter(prefix="/api", tags=["vehicles"])


@router.get("/vehicles", response_model=list[schemas.VehicleOut])
def list_vehicles(
    plate: Optional[str] = None,
    watchlist_only: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.Vehicle)
    if plate:
        q = q.filter(models.Vehicle.plate_text.ilike(f"%{normalize_plate(plate)}%"))
    if watchlist_only:
        q = q.filter(models.Vehicle.watchlist_flag == True)  # noqa: E712
    return q.order_by(models.Vehicle.last_seen.desc()).limit(200).all()


@router.get("/vehicles/{vehicle_id}", response_model=schemas.VehicleOut)
def get_vehicle(vehicle_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    v = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return v


@router.get("/vehicles/{vehicle_id}/route", response_model=schemas.VehicleRouteOut)
def vehicle_route(vehicle_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    v = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"vehicle": v, "sightings": get_route(db, vehicle_id)}


@router.get("/plates", response_model=list[schemas.PlateOut])
def search_plates(
    plate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.Plate)
    if plate:
        q = q.filter(models.Plate.plate_text_normalized.ilike(f"%{normalize_plate(plate)}%"))
    return q.order_by(models.Plate.timestamp.desc()).limit(200).all()
