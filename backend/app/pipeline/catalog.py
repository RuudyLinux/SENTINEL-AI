"""Official Gujarat Police camera catalogue client (Phase 3 P0).

Contract (per sentinel.gujarat.gov.in/resource): `GET {base_url}/api/ingest`
returns camera records carrying id, location, codec, live status, and stream
URLs (RTSP/WHEP/HLS). The exact response schema (field names) is not fully
pinned down in the official material available at build time, so
`_normalize_record` accepts a few plausible key spellings defensively and
records what it could not find rather than guessing — anything genuinely
unknown is left blank, never fabricated.

The host is NEVER hardcoded: `settings.camera_catalog_base_url` is empty by
default and `sync_catalog()` refuses to run until it's set via
`.env`/environment.

Catalogue sync only REGISTERS cameras (creates/updates Camera rows). It
never starts AI processing on any of them — that is a separate, explicit
operator action (POST /api/cameras/{id}/start, or bulk-start from the UI).
"""
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..self_heal.http_retry import request_with_retry


class CatalogError(Exception):
    """Raised for any catalogue fetch/parse failure — caller (the sync
    endpoint) turns this into a clear HTTP error rather than a stack trace,
    and never falls back to fabricated camera data."""


@dataclass
class NormalizedCameraRecord:
    external_id: str
    location: str = ""
    codec: str = ""
    live_status: str = ""
    rtsp_url: str = ""
    # WHEP (browser preview) and HLS (dashboard/mobile fallback) — genuinely
    # optional, per the official spec's three-URL-per-camera shape. Neither
    # is required to exist and neither is fabricated when absent.
    whep_url: str = ""
    hls_url: str = ""
    lat: float = 0.0
    lng: float = 0.0
    missing_fields: list[str] = field(default_factory=list)


def _first(record: dict, *keys, default=""):
    for k in keys:
        if "." in k:
            cur = record
            for part in k.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    cur = None
                    break
                cur = cur[part]
            if cur not in (None, ""):
                return cur
        elif k in record and record[k] not in (None, ""):
            return record[k]
    return default


def normalize_record(record: dict) -> NormalizedCameraRecord | None:
    """Tolerant normalization of one catalogue record. Returns None (and the
    caller should count it as a skipped/invalid record, not crash the whole
    sync) when there's no usable camera identifier at all — a record we
    truly cannot register rather than one we'd have to invent an id for."""
    if not isinstance(record, dict):
        return None
    external_id = _first(record, "id", "camera_id", "cameraId")
    if not external_id:
        return None

    missing = []
    rtsp_url = _first(record, "rtsp", "rtsp_url", "rtspUrl", "urls.rtsp")
    if not rtsp_url:
        missing.append("rtsp_url")
    # WHEP/HLS deliberately not added to `missing` — the spec doesn't
    # require either to exist (RTSP is what AI ingestion actually needs).
    whep_url = _first(record, "whep", "whep_url", "whepUrl", "urls.whep")
    hls_url = _first(record, "hls", "hls_url", "hlsUrl", "urls.hls")
    location = _first(record, "location", "name", "location_name")
    if not location:
        missing.append("location")
    codec = _first(record, "codec")
    live_status = _first(record, "live_status", "live", "status")

    lat = _first(record, "lat", "latitude", "location_lat", default=0.0)
    lng = _first(record, "lng", "lon", "longitude", "location_lng", default=0.0)
    try:
        lat = float(lat) if lat not in (None, "") else 0.0
        lng = float(lng) if lng not in (None, "") else 0.0
    except (TypeError, ValueError):
        lat, lng = 0.0, 0.0

    return NormalizedCameraRecord(
        external_id=str(external_id),
        location=str(location),
        codec=str(codec),
        live_status=str(live_status),
        rtsp_url=str(rtsp_url),
        whep_url=str(whep_url),
        hls_url=str(hls_url),
        lat=lat,
        lng=lng,
        missing_fields=missing,
    )


async def fetch_catalog() -> list[dict]:
    if not settings.camera_catalog_base_url:
        raise CatalogError(
            "CAMERA_CATALOG_BASE_URL is not configured — set it in .env before "
            "syncing the official camera catalogue. Never hardcoded."
        )
    url = f"{settings.camera_catalog_base_url.rstrip('/')}/api/ingest"
    try:
        async with httpx.AsyncClient(timeout=settings.camera_catalog_timeout_seconds) as client:
            # Bounded retry (self_heal/http_retry.py) on a transient
            # network blip or a 5xx/408/429 from the catalogue host — never
            # on a genuine "host unreachable"/timeout that persists across
            # every attempt, which still raises below exactly as before.
            resp = await request_with_retry(lambda: client.get(url), "camera_catalog fetch")
    except httpx.TimeoutException:
        raise CatalogError(f"Camera catalogue at {settings.camera_catalog_base_url} timed out")
    except httpx.RequestError as exc:
        raise CatalogError(f"Camera catalogue host unreachable: {exc.__class__.__name__}")

    if resp.status_code != 200:
        raise CatalogError(f"Camera catalogue returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise CatalogError("Camera catalogue returned a non-JSON response")

    if isinstance(data, dict):
        # tolerate a wrapper object like {"cameras": [...]} in addition to a bare list
        for key in ("cameras", "results", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        raise CatalogError("Camera catalogue response has no recognizable camera list")
    if not isinstance(data, list):
        raise CatalogError("Camera catalogue response was not a list of camera records")
    return data


def upsert_from_catalog(db: Session, raw_records: list[dict]) -> dict:
    """Idempotent sync: existing cameras (matched by external_catalog_id)
    are updated in place, new ones are created, and any previously-synced
    camera absent from this response is marked catalog_stale=True (never
    deleted — history/evidence linked to it must survive). No worker is
    started for any camera here."""
    seen_ids: set[str] = set()
    created, updated, skipped_invalid = 0, 0, 0
    now = datetime.utcnow()

    for raw in raw_records:
        norm = normalize_record(raw)
        if norm is None:
            skipped_invalid += 1
            continue
        seen_ids.add(norm.external_id)

        camera = db.query(models.Camera).filter(models.Camera.external_catalog_id == norm.external_id).first()
        if camera is None:
            # camera_code must stay unique/stable — prefer the catalogue's
            # own id (preserves the official camera ID, per requirement).
            code = norm.external_id
            if db.query(models.Camera).filter(models.Camera.camera_code == code).first():
                code = f"CAT-{norm.external_id}"
            camera = models.Camera(
                camera_code=code,
                name=norm.location or norm.external_id,
                source_type="rtsp",
                source_uri=norm.rtsp_url,
                external_catalog_id=norm.external_id,
                status="offline",  # registered only — not connected
            )
            db.add(camera)
            created += 1
        else:
            updated += 1

        camera.location = norm.location or camera.location
        camera.lat = norm.lat or camera.lat
        camera.lng = norm.lng or camera.lng
        camera.catalog_codec = norm.codec
        camera.catalog_live_status = norm.live_status
        camera.catalog_synced_at = now
        camera.catalog_stale = False
        if norm.rtsp_url:
            camera.source_uri = norm.rtsp_url  # never logged/exposed — see EvidenceOut/CameraOut
        # WHEP/HLS: only overwrite when THIS sync actually supplied a value —
        # a record that omits one this time doesn't erase a previously-known
        # one (the catalogue's own omission, not evidence it changed).
        if norm.whep_url:
            camera.whep_url = norm.whep_url
        if norm.hls_url:
            camera.hls_url = norm.hls_url

    stale = 0
    previously_synced = db.query(models.Camera).filter(models.Camera.external_catalog_id.isnot(None)).all()
    for camera in previously_synced:
        if camera.external_catalog_id not in seen_ids and not camera.catalog_stale:
            camera.catalog_stale = True
            stale += 1

    db.commit()
    return {
        "total_in_catalogue": len(raw_records),
        "created": created,
        "updated": updated,
        "marked_stale": stale,
        "skipped_invalid": skipped_invalid,
    }
