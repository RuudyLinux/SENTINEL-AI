"""Global + advanced search. 'Natural-language search' is a keyword/regex
parser mapping free text to structured filters (doc §58) — not an LLM/NLP
model; documented non-goal.
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

PLATE_TOKEN_RE = re.compile(r"[A-Z]{2}\s?\d{1,2}\s?[A-Z]{1,3}\s?\d{3,4}", re.IGNORECASE)
TIME_AFTER_RE = re.compile(r"after\s+(\d{1,2})\s*(am|pm)?", re.IGNORECASE)
TIME_BEFORE_RE = re.compile(r"before\s+(\d{1,2})\s*(am|pm)?", re.IGNORECASE)


def _to_24_hour(hour: int, meridiem: str) -> int:
    """12-hour clock to 24-hour.

    Both ends of the clock are special and only one used to be handled: `12pm`
    is noon (12, not 24) and `12am` is MIDNIGHT (0, not 12). The `am` case was
    missing, so "after 12am" parsed to hour 12 — noon — and an operator asking
    for overnight activity was quietly given an afternoon filter.
    """
    meridiem = (meridiem or "").lower()
    if meridiem == "pm" and hour != 12:
        return hour + 12
    if meridiem == "am" and hour == 12:
        return 0
    return hour


def parse_natural_language(text: str) -> dict:
    """Very small heuristic parser: extracts a plate token and after/before hour hints.

    Also returns `text`: the query with every recognized phrase REMOVED, for
    the caller to use as the free-text match. Without that, a recognized
    phrase stayed in the pattern and text+filter searches could never match
    anything — "GJ05AB1234 after 6pm" searched for the literal string
    "%GJ05AB1234 after 6pm%", which no camera name or incident title contains,
    so the sections came back empty while the response advertised that it had
    understood both the plate and the time.
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

    # The free-text pattern is built from the query with recognized phrases
    # REMOVED (see parse_natural_language), so text and filters compose. An
    # empty residual means the operator typed only filter phrases ("after
    # 6pm"); that must not become "%%", which matches every row — it means
    # "no text constraint", and the hour filters below carry the query.
    text = filters.get("text") or ""
    like = like_pattern(text) if text else None

    if like:
        results["cameras"] = [
            {"id": c.id, "camera_code": c.camera_code, "name": c.name}
            for c in db.query(models.Camera).filter(
                (models.Camera.camera_code.ilike(like, escape=LIKE_ESCAPE))
                | (models.Camera.name.ilike(like, escape=LIKE_ESCAPE))
                | (models.Camera.location.ilike(like, escape=LIKE_ESCAPE))
            ).limit(20)
        ]
    # With no text there is nothing to match a camera on: a camera has no
    # timestamp, so an hour filter cannot select one. The section stays empty
    # rather than returning every camera in the system.

    # The parsed hour hints are now APPLIED, not merely reported. They used to
    # be extracted, returned in `parsed_filters`, and rendered to the operator
    # by the search page — while every query ignored them. Someone searching
    # "vehicles after 6pm" was shown `after_hour: 18` next to results that had
    # never been filtered by time, which in an investigative tool invites a
    # wrong conclusion from a screen that looks correct.
    #
    # Hour-of-day, not a date range: "after 6pm" is a recurring-time question
    # ("what moves through here in the evening"), which is what the phrasing
    # actually asks. `extract` compiles on both SQLite and PostgreSQL.
    def _hour_conditions(column):
        conditions = []
        if "after_hour" in filters:
            conditions.append(extract("hour", column) >= filters["after_hour"])
        if "before_hour" in filters:
            conditions.append(extract("hour", column) < filters["before_hour"])
        return conditions

    # `entity` is applied by SUPPRESSING the sections it excludes. There is no
    # persons section in this response, so a person-focused query cannot add
    # one — but it can stop returning vehicles the operator did not ask about,
    # which is the honest half of the behaviour the filter advertises.
    wants_vehicles = filters.get("entity") != "person"

    # Every section below is built the same way: apply only the constraints the
    # query actually carries, and return nothing when it carries none for that
    # section. `like` is None for a query made entirely of recognized phrases
    # (searching a bare plate leaves no residual text) — passing that straight
    # to `.ilike()` raises ArgumentError, which turned an ordinary plate search
    # into a 500.
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

    # Bug fix: this previously filtered `Alert.camera_id.ilike(like)` — matching
    # an opaque internal id column against the operator's free text, so the
    # alerts section of a global search was permanently empty. Alerts are now
    # found the way an operator would actually look for them: by the camera they
    # fired on (code/name, resolved above) and by the vehicle plate involved.
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
        # Same hour scoping as the vehicle and incident sections. Leaving
        # alerts unfiltered would reproduce, in one section, exactly the
        # inconsistency this change exists to remove: a response that claims
        # a time filter while part of it ignores that filter.
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
