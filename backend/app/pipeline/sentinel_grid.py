"""Sentinel Camera Grid integration.

Discovery isn't a public JSON endpoint: GET {base_url}/cameras.json without a
session redirects (302) to /auth/login, a cookie login (POST /auth/login with
email/password form fields). So we log in once and fetch the catalogue in the
same httpx.AsyncClient, which carries the cookie.

Credentials come from .env only; never hardcoded, logged, put in an exception
message or returned by the API. Missing or rejected credentials raise
SentinelGridError, keeping "not configured" and "rejected" apart.

Sync only registers cameras (upsert_grid_cameras), like pipeline/catalog.py;
it never starts AI.
"""
from dataclasses import dataclass, field
from datetime import datetime
import uuid

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .catalog import _first  # same tolerant key lookup as the catalogue


class SentinelGridError(Exception):
    """Any grid login/fetch/parse failure. The sync endpoint turns it into a
    clear HTTP error; no fallback camera data."""


@dataclass
class GridCameraRecord:
    grid_id: str
    name: str = ""
    location: str = ""
    resolution: str = ""
    codec: str = ""
    lat: float = 0.0
    lng: float = 0.0
    missing_fields: list[str] = field(default_factory=list)


def _normalize_grid_record(record: dict) -> "GridCameraRecord | None":
    if not isinstance(record, dict):
        return None
    grid_id = _first(record, "id", "camera_id", "cameraId")
    if not grid_id:
        return None
    missing = []
    name = _first(record, "name", "label")
    location = _first(record, "location", "name", "location_name")
    if not location:
        missing.append("location")
    resolution = _first(record, "resolution", "res")
    codec = _first(record, "codec")
    lat = _first(record, "lat", "latitude", default=0.0)
    lng = _first(record, "lng", "lon", "longitude", default=0.0)
    try:
        lat = float(lat) if lat not in (None, "") else 0.0
        lng = float(lng) if lng not in (None, "") else 0.0
    except (TypeError, ValueError):
        lat, lng = 0.0, 0.0
    return GridCameraRecord(
        grid_id=str(grid_id), name=str(name), location=str(location),
        resolution=str(resolution), codec=str(codec), lat=lat, lng=lng,
        missing_fields=missing,
    )


async def _login(client: httpx.AsyncClient) -> None:
    if not settings.sentinel_grid_email or not settings.sentinel_grid_password:
        raise SentinelGridError(
            "SENTINEL_GRID_EMAIL/SENTINEL_GRID_PASSWORD are not configured — set them "
            "in .env before syncing the Sentinel Camera Grid. Never hardcoded."
        )
    try:
        resp = await client.post(
            "/auth/login",
            data={"email": settings.sentinel_grid_email, "password": settings.sentinel_grid_password},
        )
    except httpx.TimeoutException:
        raise SentinelGridError("Sentinel Camera Grid login timed out")
    except httpx.RequestError as exc:
        raise SentinelGridError(f"Sentinel Camera Grid host unreachable: {exc.__class__.__name__}")

    # a 3xx back to /auth/login (httpx doesn't follow here) or 401/403 means
    # rejected credentials, reported separately from "not configured" and
    # without the credentials in the message
    if resp.status_code in (401, 403):
        raise SentinelGridError(
            "Sentinel Camera Grid login rejected (AUTH_ERROR) — check "
            "SENTINEL_GRID_EMAIL/SENTINEL_GRID_PASSWORD in .env"
        )
    if resp.status_code in (301, 302, 303, 307, 308) and "/auth/login" in resp.headers.get("location", ""):
        raise SentinelGridError(
            "Sentinel Camera Grid login rejected (AUTH_ERROR) — check "
            "SENTINEL_GRID_EMAIL/SENTINEL_GRID_PASSWORD in .env"
        )
    if resp.status_code >= 400:
        raise SentinelGridError(f"Sentinel Camera Grid login failed (HTTP {resp.status_code})")


async def fetch_grid_cameras() -> list[dict]:
    async with httpx.AsyncClient(base_url=settings.sentinel_grid_base_url, timeout=settings.sentinel_grid_timeout_seconds) as client:
        await _login(client)
        try:
            resp = await client.get("/cameras.json")
        except httpx.TimeoutException:
            raise SentinelGridError("Sentinel Camera Grid catalogue request timed out")
        except httpx.RequestError as exc:
            raise SentinelGridError(f"Sentinel Camera Grid host unreachable: {exc.__class__.__name__}")

        if resp.status_code in (301, 302, 303, 307, 308) and "/auth/login" in resp.headers.get("location", ""):
            # logged in but the session wasn't accepted, say so instead of a
            # confusing parse error
            raise SentinelGridError("Sentinel Camera Grid rejected the authenticated session (AUTH_ERROR)")
        if resp.status_code != 200:
            raise SentinelGridError(f"Sentinel Camera Grid catalogue returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise SentinelGridError("Sentinel Camera Grid catalogue returned a non-JSON response")

    if isinstance(data, dict):
        for key in ("cameras", "results", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        raise SentinelGridError("Sentinel Camera Grid catalogue response has no recognizable camera list")
    if not isinstance(data, list):
        raise SentinelGridError("Sentinel Camera Grid catalogue response was not a list of camera records")
    return data


def upsert_grid_cameras(db: Session, raw_records: list[dict]) -> dict:
    """Idempotent register-only sync, matched on a `grid:<id>` marker in
    external_catalog_id (prefix keeps it apart from official catalogue ids).
    source_uri is the bare grid id; the credentialed RTSP URL is only built in
    memory by adapters.SentinelGridAdapter at connect time, never stored.
    Cameras missing from the response get catalog_stale=True (same as
    catalog.py) and are never deleted, so their history stays."""
    created, updated, skipped_invalid = 0, 0, 0
    seen_markers: set[str] = set()
    for raw in raw_records:
        norm = _normalize_grid_record(raw)
        if norm is None:
            skipped_invalid += 1
            continue
        marker = f"grid:{norm.grid_id}"
        seen_markers.add(marker)

        camera = db.query(models.Camera).filter(models.Camera.external_catalog_id == marker).first()
        if camera is None:
            code = f"GRID-{norm.grid_id}"
            if db.query(models.Camera).filter(models.Camera.camera_code == code).first():
                code = f"GRID-{norm.grid_id}-{uuid.uuid4().hex[:4]}"
            camera = models.Camera(
                camera_code=code,
                name=norm.name or norm.location or norm.grid_id,
                source_type="sentinel_grid",
                source_uri=norm.grid_id,
                external_catalog_id=marker,
                camera_group="Sentinel Grid",
                status="offline",  # registered, not connected
                # AI on by default, every camera stays connected and running
                # AI. Same as the column default and POST /api/cameras. The
                # supervisor still never touches ai_* itself; PATCH
                # /api/cameras/{id} turns AI off per camera.
                ai_person=True,
                ai_vehicle=True,
                ai_anpr=True,
            )
            db.add(camera)
            created += 1
        else:
            updated += 1

        camera.location = norm.location or camera.location
        camera.lat = norm.lat or camera.lat
        camera.lng = norm.lng or camera.lng
        camera.catalog_codec = norm.codec or camera.catalog_codec
        camera.resolution = norm.resolution or camera.resolution
        camera.catalog_stale = False
        camera.catalog_synced_at = datetime.utcnow()

    marked_stale = 0
    previously_synced = db.query(models.Camera).filter(
        models.Camera.source_type == "sentinel_grid",
        models.Camera.external_catalog_id.isnot(None),
    ).all()
    for camera in previously_synced:
        if camera.external_catalog_id not in seen_markers and not camera.catalog_stale:
            camera.catalog_stale = True
            marked_stale += 1

    db.commit()
    return {
        "total_in_grid": len(raw_records),
        "created": created,
        "updated": updated,
        "marked_stale": marked_stale,
        "skipped_invalid": skipped_invalid,
    }
