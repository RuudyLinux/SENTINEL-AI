"""Global and advanced search. The "natural language" part is a keyword/regex
parser that maps text to filters (doc §58), not an NLP model.
"""
import re
from fastapi import APIRouter, Depends, Query
from sqlalchemy import extract
from sqlalchemy.orm import Session

from .. import models
from ..db import LIKE_ESCAPE, get_db, like_pattern
from ..security import get_current_user
from ..pipeline.anpr import normalize_plate

router = APIRouter(prefix="/api/search", tags=["search"])

# \b so it doesn't match inside a longer word: ZTIME1BC234 got ME1BC234
# carved out as a plate and ZTI used as the text filter
PLATE_TOKEN_RE = re.compile(r"\b[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{3,4}\b", re.IGNORECASE)
TIME_AFTER_RE = re.compile(r"after\s+(\d{1,2})\s*(am|pm)?", re.IGNORECASE)
TIME_BEFORE_RE = re.compile(r"before\s+(\d{1,2})\s*(am|pm)?", re.IGNORECASE)


def _to_24_hour(hour: int, meridiem: str) -> int:
    """12h to 24h. 12pm is noon (12) and 12am is midnight (0); the am case was
    missing, so "after 12am" became noon."""
    meridiem = (meridiem or "").lower()
    if meridiem == "pm" and hour != 12:
        return hour + 12
    if meridiem == "am" and hour == 12:
        return 0
    return hour


def parse_natural_language(text: str) -> dict:
    """Tiny heuristic parser: a plate token and after/before hour hints.

    Also returns `text`, the query with recognized phrases removed, for the
    free-text match. Leaving them in meant "GJ05AB1234 after 6pm" searched
    for that literal string and found nothing, while the response said it had
    understood the plate and the time.
    """
    filters: dict = {"raw_query": text}
    residual = text

    def _consume(match) -> None:
        nonlocal residual
        if match:
            residual = residual.replace(match.group(0), " ", 1)

    m = PLATE_TOKEN_RE.search(text)
    if m:
        filters["plate"] = normalize_plate(m.group(0))
        _consume(m)
    m2 = TIME_AFTER_RE.search(text)
    if m2:
        filters["after_hour"] = _to_24_hour(int(m2.group(1)), m2.group(2))
        _consume(m2)
    m3 = TIME_BEFORE_RE.search(text)
    if m3:
        filters["before_hour"] = _to_24_hour(int(m3.group(1)), m3.group(2))
        _consume(m3)

    lowered = text.lower()
    if "person" in lowered:
        filters["entity"] = "person"
        residual = re.sub(r"\bperson\b", " ", residual, flags=re.IGNORECASE)
    elif "vehicle" in lowered or "car" in lowered:
        filters["entity"] = "vehicle"
        residual = re.sub(r"\b(vehicles?|cars?)\b", " ", residual, flags=re.IGNORECASE)

    filters["text"] = " ".join(residual.split())
    return filters


@router.get("")
def global_search(q: str = Query(...), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    filters = parse_natural_language(q)
    results: dict = {"query": q, "parsed_filters": filters, "cameras": [], "vehicles": [], "plates": [], "incidents": [], "alerts": []}

    # Text pattern is the query minus recognized phrases so text and filters
    # combine. Nothing left (just "after 6pm") means no text constraint, not
    # "%%", which matches everything.
    text = filters.get("text") or ""
    like = like_pattern(text) if text else None

    if like:
        results["cameras"] = [
            {"id": c.id, "camera_code": c.camera_code, "name": c.name}
            for c in db.query(models.Camera).filter(
                models.Camera.retired == False,  # noqa: E712
                (models.Camera.camera_code.ilike(like, escape=LIKE_ESCAPE))
                | (models.Camera.name.ilike(like, escape=LIKE_ESCAPE))
                | (models.Camera.location.ilike(like, escape=LIKE_ESCAPE))
            ).limit(20)
        ]
    # no text, nothing to match cameras on (they have no timestamp for the
    # hour filter), so that section stays empty

    # Hour hints are applied, not just echoed back in parsed_filters; the page
    # used to show after_hour: 18 next to unfiltered results.
    #
    # Hour of day, not a date range: "after 6pm" asks what happens in the
    # evenings. extract() works on SQLite and PostgreSQL.
    def _hour_conditions(column):
        conditions = []
        if "after_hour" in filters:
            conditions.append(extract("hour", column) >= filters["after_hour"])
        if "before_hour" in filters:
            conditions.append(extract("hour", column) < filters["before_hour"])
        return conditions

    # entity works by hiding the sections it excludes. there's no persons
    # section to add, but it can stop returning vehicles nobody asked for
    wants_vehicles = filters.get("entity") != "person"

    # Each section applies only the constraints the query has and returns
    # nothing when it has none for it. like is None for a query that's all
    # recognized phrases (a bare plate), and .ilike(None) raised
    # ArgumentError, a 500 on a plain plate search.
    if wants_vehicles:
        plate_filter = filters.get("plate")
        vehicle_hours = _hour_conditions(models.Vehicle.last_seen)
        if plate_filter or like or vehicle_hours:
            vehicles_q = db.query(models.Vehicle)
            if plate_filter:
                vehicles_q = vehicles_q.filter(
                    models.Vehicle.plate_text.ilike(like_pattern(plate_filter), escape=LIKE_ESCAPE)
                )
            elif like:
                vehicles_q = vehicles_q.filter(models.Vehicle.plate_text.ilike(like, escape=LIKE_ESCAPE))
            for condition in vehicle_hours:
                vehicles_q = vehicles_q.filter(condition)
            results["vehicles"] = [
                {"id": v.id, "plate_text": v.plate_text, "watchlist_flag": v.watchlist_flag}
                for v in vehicles_q.limit(20)
            ]

    incident_hours = _hour_conditions(models.Incident.created_at)
    if like or incident_hours:
        incidents_q = db.query(models.Incident)
        if like:
            incidents_q = incidents_q.filter(models.Incident.title.ilike(like, escape=LIKE_ESCAPE))
        for condition in incident_hours:
            incidents_q = incidents_q.filter(condition)
        results["incidents"] = [
            {"id": i.id, "title": i.title, "status": i.status, "priority": i.priority}
            for i in incidents_q.limit(20)
        ]

    # alerts by the camera they fired on (code/name from above) and the plate;
    # this used to ilike the opaque camera_id column, so it was always empty
    alert_filters = []
    camera_ids = [c["id"] for c in results["cameras"]]
    if camera_ids:
        alert_filters.append(models.Alert.camera_id.in_(camera_ids))
    vehicle_ids = [v["id"] for v in results["vehicles"]]
    if vehicle_ids:
        alert_filters.append(models.Alert.vehicle_id.in_(vehicle_ids))
    if alert_filters:
        from sqlalchemy import or_
        alerts_q = db.query(models.Alert).filter(or_(*alert_filters))
        # same hour scoping as vehicles and incidents
        for condition in _hour_conditions(models.Alert.timestamp):
            alerts_q = alerts_q.filter(condition)
        alert_rows = alerts_q.order_by(models.Alert.timestamp.desc()).limit(20).all()
        results["alerts"] = [
            {
                "id": a.id, "severity": a.severity, "status": a.status,
                "camera_id": a.camera_id, "vehicle_id": a.vehicle_id,
                "reasons": a.reasons or [], "timestamp": a.timestamp,
            }
            for a in alert_rows
        ]
    return results
