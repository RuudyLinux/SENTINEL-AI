"""Real Sentinel Camera Grid integration (final integration task).

Discovery is NOT a bare public JSON endpoint, despite how it's often described:
`GET {base_url}/cameras.json` unauthenticated redirects (302) to
`{base_url}/auth/login` — a session-cookie web login (`POST /auth/login` with
`email`/`password` form fields, confirmed by fetching the real login page's markup;
`{base_url}` and that shape are not secret). So discovery here is: log in once to
get a session cookie, then fetch the catalogue with it, all inside one
`httpx.AsyncClient` so the cookie is carried automatically.

Credentials (`settings.sentinel_grid_email/password`) come from `.env` only — never
hardcoded, never logged, never included in any exception message, never returned by
any API response. A missing/rejected credential fails loudly and specifically
(`SentinelGridError`, distinguishing "not configured" from "rejected by the grid")
rather than silently no-op'ing or fabricating camera data.

Sync here only REGISTERS cameras (see `upsert_grid_cameras`) — it never starts AI
processing, matching the existing official-catalogue sync's contract
(`pipeline/catalog.py`).
"""
from dataclasses import dataclass, field
from datetime import datetime
import uuid

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from .catalog import _first  # same tolerant multi-key-spelling lookup, reused not duplicated


class SentinelGridError(Exception):
    """Raised for any grid login/fetch/parse failure. The caller (the sync
    endpoint) turns this into a clear HTTP error, never a stack trace, and never
    falls back to fabricated camera data."""


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

    # A 3xx back to /auth/login (httpx does not auto-follow by default here) or a
    # 401/403 both mean the credentials were rejected — reported as AUTH_ERROR-class,
    # distinct from "not configured" above, and never includes the credentials
    # themselves in the message.
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
            # Logged in but the session wasn't accepted for this request — report
            # as an honest auth failure rather than an opaque parse error.
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
    """Idempotent, register-only sync — matched by a `grid:<id>` marker stored in
    the existing `external_catalog_id` column (distinct prefix so it can never
    collide with an official-catalogue id in the same column). `source_uri` is
    set to the BARE grid camera id, never a credentialed URL — the real RTSP URL
    is built in memory, at connect time, by `pipeline/adapters.SentinelGridAdapter`
    and is never persisted anywhere. A grid camera absent from this sync's response
    (removed/deprecated in the catalogue) is marked `catalog_stale=True` — same field
    and convention as the official-catalogue sync (`pipeline/catalog.py`) — never
    deleted, so its detections/alerts/incidents/evidence history survives."""
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
                status="offline",  # registered only — not connected
                # AI OFF by default on discovery — the model's column default
                # is True, which would make Camera.model's own default enable
                # full YOLO/ByteTrack/ANPR the moment the 24/7 auto-connect
                # supervisor (pipeline/supervisor.py) starts this camera's
                # RTSP connection. "Registered/connected" and "AI processing"
                # must stay independent by design; AI is explicit opt-in per
                # camera via PATCH /api/cameras/{id}, never a side effect of
                # being discovered or auto-connected.
                ai_person=False,
                ai_vehicle=False,
                ai_anpr=False,
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
