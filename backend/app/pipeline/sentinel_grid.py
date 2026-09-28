"""Sentinel Camera Grid integration.

The catalogue (GET {base_url}/cameras.json) requires a cookie session from
POST /auth/login, so one httpx client logs in and fetches in the same session.

Credentials come from .env only and are never logged, included in errors or
returned. Missing and rejected credentials raise distinct SentinelGridErrors.
Sync only registers cameras; it never starts workers.
"""
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import uuid

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .catalog import _first  # same tolerant key lookup as the catalogue


class SentinelGridError(Exception):
    """Any grid login, fetch or parse failure; surfaced as an HTTP error by the
    sync endpoint."""


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


@lru_cache(maxsize=1)
def _known_locations() -> dict:
    path = Path(__file__).with_name("grid_locations.json")
    return {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("_")}


def known_location(grid_id: str, name: str) -> tuple[float, float] | None:
    """Looked-up position for a grid camera the catalogue gives none for.
    Only when the name still matches, so a renumbered grid can't move a
    camera onto someone else's spot."""
    entry = _known_locations().get(grid_id)
    if entry and entry["name"].strip().lower() == name.strip().lower():
        return entry["lat"], entry["lng"]
    return None


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

    # A redirect back to /auth/login or a 401/403 means rejected credentials,
    # reported without the credentials themselves.
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
    external_catalog_id. source_uri is the bare grid id; the credentialed URL
    exists only in memory at connect time. Cameras missing from the response are
    marked catalog_stale, never deleted."""
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
                # AI on by default, like the column default and POST
                # /api/cameras; PATCH turns it off per camera.
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
        # catalogue has no coordinates; fill from the lookup table, but never
        # over a position an operator set with PATCH
        if not camera.lat and not camera.lng:
            known = known_location(norm.grid_id, norm.name or norm.location)
            if known:
                camera.lat, camera.lng = known
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
