"""Global + advanced search. 'Natural-language search' is a keyword/regex
parser mapping free text to structured filters (doc §58) — not an LLM/NLP
model; documented non-goal.
"""
import re
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..security import get_current_user
from ..pipeline.anpr import normalize_plate

router = APIRouter(prefix="/api/search", tags=["search"])

PLATE_TOKEN_RE = re.compile(r"[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{3,4}", re.IGNORECASE)
TIME_AFTER_RE = re.compile(r"after\s+(\d{1,2})\s*(am|pm)?", re.IGNORECASE)
TIME_BEFORE_RE = re.compile(r"before\s+(\d{1,2})\s*(am|pm)?", re.IGNORECASE)


def parse_natural_language(text: str) -> dict:
    """Very small heuristic parser: extracts a plate token and after/before hour hints."""
    filters: dict = {"raw_query": text}
    m = PLATE_TOKEN_RE.search(text)
    if m:
        filters["plate"] = normalize_plate(m.group(0))
    m2 = TIME_AFTER_RE.search(text)
    if m2:
        hour = int(m2.group(1))
        if (m2.group(2) or "").lower() == "pm" and hour != 12:
            hour += 12
        filters["after_hour"] = hour
    m3 = TIME_BEFORE_RE.search(text)
    if m3:
        hour = int(m3.group(1))
        if (m3.group(2) or "").lower() == "pm" and hour != 12:
            hour += 12
        filters["before_hour"] = hour
    if "person" in text.lower():
        filters["entity"] = "person"
    elif "vehicle" in text.lower() or "car" in text.lower():
        filters["entity"] = "vehicle"
    return filters


@router.get("")
def global_search(q: str = Query(...), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    filters = parse_natural_language(q)
    results: dict = {"query": q, "parsed_filters": filters, "cameras": [], "vehicles": [], "plates": [], "incidents": [], "alerts": []}

    like = f"%{q}%"
    results["cameras"] = [
        {"id": c.id, "camera_code": c.camera_code, "name": c.name}
        for c in db.query(models.Camera).filter(
            (models.Camera.camera_code.ilike(like)) | (models.Camera.name.ilike(like)) | (models.Camera.location.ilike(like))
        ).limit(20)
    ]

    plate_filter = filters.get("plate")
    if plate_filter:
        vehicles_q = db.query(models.Vehicle).filter(models.Vehicle.plate_text.ilike(f"%{plate_filter}%"))
    else:
        vehicles_q = db.query(models.Vehicle).filter(models.Vehicle.plate_text.ilike(like))
    vehicles = vehicles_q.limit(20).all()
    results["vehicles"] = [{"id": v.id, "plate_text": v.plate_text, "watchlist_flag": v.watchlist_flag} for v in vehicles]

    results["incidents"] = [
        {"id": i.id, "title": i.title, "status": i.status, "priority": i.priority}
        for i in db.query(models.Incident).filter(models.Incident.title.ilike(like)).limit(20)
    ]
    results["alerts"] = [
        {"id": a.id, "severity": a.severity, "camera_id": a.camera_id}
        for a in db.query(models.Alert).filter(models.Alert.camera_id.ilike(like)).limit(20)
    ]
    return results
