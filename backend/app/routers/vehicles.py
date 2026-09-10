from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..security import get_current_user
from ..pipeline.correlate import get_route, get_vehicle_summary
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


# Declared BEFORE /vehicles/{vehicle_id} so the literal path segment is matched
# as a route, never captured as an id.
@router.get("/vehicles/by-plate/{plate}", response_model=schemas.VehicleOut)
def get_vehicle_by_plate(plate: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Resolve a plate straight to its vehicle.

    The V2 investigation flow starts from a plate an officer types, not from an
    internal vehicle id — previously the frontend had to list-search then take
    the first result, which quietly picked an arbitrary match when the search
    was a substring. This normalizes the input the same way the ANPR pipeline
    normalizes an OCR read, so 'GJ 05 AB 1234' and 'gj05ab1234' both resolve.
    """
    normalized = normalize_plate(plate)
    if not normalized:
        raise HTTPException(status_code=400, detail="A plate is required")
    vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate_text == normalized).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail=f"No vehicle has been recognized with plate {normalized}")
    return vehicle


@router.get("/vehicles/{vehicle_id}", response_model=schemas.VehicleOut)
def get_vehicle(vehicle_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    v = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return v


@router.get("/vehicles/{vehicle_id}/summary", response_model=schemas.VehicleSummaryOut)
def vehicle_summary(vehicle_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Investigation header: current/last camera, journey size, linked alerts
    and incidents, and an explainable risk score (see pipeline/risk.py)."""
    summary = get_vehicle_summary(db, vehicle_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return summary


@router.get("/vehicles/{vehicle_id}/route", response_model=schemas.VehicleRouteOut)
def vehicle_route(vehicle_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    v = db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"vehicle": v, "sightings": get_route(db, vehicle_id)}


@router.get("/vehicles/{vehicle_id}/sightings", response_model=list[schemas.PlateOut])
def vehicle_sightings(vehicle_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Raw, uncollapsed sighting records for a vehicle.

    `/route` collapses consecutive same-camera hops for readability; this is the
    underlying evidence, which an investigator needs to see unmodified.
    """
    if not db.query(models.Vehicle).filter(models.Vehicle.id == vehicle_id).first():
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return (
        db.query(models.Plate)
        .filter(models.Plate.vehicle_id == vehicle_id)
        .order_by(models.Plate.timestamp.desc())
        .limit(500)
        .all()
    )


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
