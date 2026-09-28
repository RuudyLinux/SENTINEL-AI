"""Cameras near a point. PostGIS if available, plain Python otherwise.

On PostgreSQL + PostGIS (docker-compose) migration 20260928_0700 adds
cameras.geog, a generated geography point with a GiST index, and the search
is one ST_DWithin with ST_Distance on the spheroid. SQLite (dev, tests) and
PostgreSQL without the extension do a haversine over cameras with a
position. Both skip 0,0, the stored form of an unknown position.
"""
import math

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from . import models

_EARTH_RADIUS_M = 6_371_008.8  # mean radius; the fallback's only approximation
_postgis: "bool | None" = None


def postgis_ready(db: Session) -> bool:
    """Does cameras.geog exist. Checked once; it only changes with a
    migration, which needs a restart anyway."""
    global _postgis
    if _postgis is None:
        bind = db.get_bind()
        _postgis = bind.dialect.name == "postgresql" and any(
            c["name"] == "geog" for c in inspect(bind).get_columns("cameras")
        )
    return _postgis


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def nearby_cameras(
    db: Session, lat: float, lng: float, radius_m: float, limit: int, exclude_id: "str | None" = None,
) -> list[tuple[models.Camera, float]]:
    """Active cameras within `radius_m` metres of (lat, lng), nearest first."""
    if postgis_ready(db):
        rows = db.execute(text(
            "SELECT id, ST_Distance(geog, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) AS d "
            "FROM cameras "
            "WHERE retired = false AND geog IS NOT NULL AND (CAST(:exclude AS text) IS NULL OR id <> :exclude) "
            "AND ST_DWithin(geog, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, :r) "
            "ORDER BY d LIMIT :limit"
        ), {"lat": lat, "lng": lng, "r": radius_m, "limit": limit, "exclude": exclude_id}).all()
        by_id = {c.id: c for c in db.query(models.Camera).filter(models.Camera.id.in_([r.id for r in rows])).all()}
        return [(by_id[r.id], float(r.d)) for r in rows if r.id in by_id]

    candidates = db.query(models.Camera).filter(
        models.Camera.retired == False,  # noqa: E712
        ~((models.Camera.lat == 0) & (models.Camera.lng == 0)),
    ).all()
    found = []
    for camera in candidates:
        if camera.id == exclude_id:
            continue
        d = haversine_m(lat, lng, float(camera.lat), float(camera.lng))
        if d <= radius_m:
            found.append((camera, d))
    found.sort(key=lambda pair: pair[1])
    return found[:limit]
