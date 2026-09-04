"""Real Sentinel Camera Grid integration (final integration task). Network-free:
mocks httpx like test_catalog_host_missing.py does, and never uses real
credentials. Proves the code paths (missing-config error, AUTH_ERROR detection,
normalization, idempotent upsert, %40-encoded URL construction) without depending
on the live external grid."""
import asyncio

import httpx
import pytest

from app import config, models
from app.pipeline.sentinel_grid import (
    fetch_grid_cameras, upsert_grid_cameras, _normalize_grid_record, SentinelGridError,
)
from app.pipeline.adapters import SentinelGridAdapter


def test_fetch_raises_clear_error_when_credentials_unset(monkeypatch):
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "")
    with pytest.raises(SentinelGridError, match="not configured"):
        asyncio.run(fetch_grid_cameras())


def test_fetch_reports_auth_error_on_rejected_login(monkeypatch):
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "someone@example.com")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "wrong-password")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(401)
        raise AssertionError("should never reach cameras.json after a rejected login")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(SentinelGridError, match="AUTH_ERROR"):
        asyncio.run(fetch_grid_cameras())


def test_fetch_returns_camera_list_on_successful_login(monkeypatch):
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "someone@example.com")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "correct-password")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, headers={"set-cookie": "session=abc123"})
        if request.url.path == "/cameras.json":
            return httpx.Response(200, json={"cameras": [{"id": "cam04", "name": "Gate 4"}]})
        raise AssertionError(f"unexpected request: {request.url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw))
    records = asyncio.run(fetch_grid_cameras())
    assert records == [{"id": "cam04", "name": "Gate 4"}]


def test_normalize_grid_record_requires_an_id():
    assert _normalize_grid_record({"name": "no id here"}) is None
    norm = _normalize_grid_record({"id": "cam04", "name": "Gate 4", "location": "North Gate"})
    assert norm is not None
    assert norm.grid_id == "cam04"
    assert norm.location == "North Gate"


def test_upsert_creates_then_updates_without_duplicating(db_session):
    records = [{"id": "cam04", "name": "Gate 4", "location": "North Gate"}]
    summary1 = upsert_grid_cameras(db_session, records)
    assert summary1["created"] == 1
    assert summary1["updated"] == 0

    cams = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "grid:cam04").all()
    assert len(cams) == 1
    assert cams[0].source_type == "sentinel_grid"
    assert cams[0].source_uri == "cam04"  # bare id — never a credentialed URL
    assert cams[0].camera_group == "Sentinel Grid"
    assert cams[0].status == "offline"  # registered only, never auto-connected

    summary2 = upsert_grid_cameras(db_session, [{"id": "cam04", "name": "Gate 4", "location": "North Gate (renamed)"}])
    assert summary2["created"] == 0
    assert summary2["updated"] == 1
    cams2 = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "grid:cam04").all()
    assert len(cams2) == 1  # still exactly one row, not duplicated
    assert cams2[0].location == "North Gate (renamed)"


def test_upsert_skips_invalid_records_without_crashing(db_session):
    summary = upsert_grid_cameras(db_session, [{"name": "no id"}, {"id": "cam05"}])
    assert summary["skipped_invalid"] == 1
    assert summary["created"] == 1


def _thirty_catalogue_records() -> list[dict]:
    # Real catalogue shape confirmed live this engagement: {"id": "cam01", "name": "..."} —
    # no lat/lng/resolution/codec actually supplied by the real grid, so this fixture
    # matches that honestly rather than inventing fields the real response doesn't have.
    # "scaletest" prefix (not "cam01".."cam30") — this module's other tests already
    # register "cam04"/"cam05" against the same on-disk shared test DB; a real id
    # collision here would silently turn an expected `created` into an `updated`.
    return [{"id": f"scaletest{i:02d}", "name": f"{i:02d} Test Location"} for i in range(1, 31)]


def _scaletest_cameras(db_session):
    """Scoped to this module's own "scaletest%" marker prefix — the shared on-disk
    test DB also carries cam04/cam05 rows from earlier tests in this same file, so
    an unscoped `source_type=="sentinel_grid"` query would count those too."""
    return (
        db_session.query(models.Camera)
        .filter(models.Camera.external_catalog_id.like("grid:scaletest%"))
        .all()
    )


def test_upsert_handles_full_30_camera_catalogue_idempotently(db_session):
    """The actual scale this task cares about: 30 catalogue cameras -> 30 database
    cameras, and re-running discovery never duplicates or drops any of them."""
    records = _thirty_catalogue_records()

    summary1 = upsert_grid_cameras(db_session, records)
    assert summary1["created"] == 30
    assert summary1["updated"] == 0

    grid_cams = _scaletest_cameras(db_session)
    assert len(grid_cams) == 30
    assert all(not c.catalog_stale for c in grid_cams)
    codes = [c.camera_code for c in grid_cams]
    assert len(codes) == len(set(codes))  # no duplicate camera_code

    # Second discovery of the exact same 30 — idempotent, no duplicates, no new rows.
    summary2 = upsert_grid_cameras(db_session, records)
    assert summary2["created"] == 0
    assert summary2["updated"] == 30
    grid_cams2 = _scaletest_cameras(db_session)
    assert len(grid_cams2) == 30  # still exactly 30, not 60


def test_upsert_marks_removed_camera_stale_without_deleting_it(db_session):
    """A camera the catalogue stops listing must be marked catalog_stale=True, never
    deleted — its detections/alerts/incidents/evidence history must survive."""
    full = _thirty_catalogue_records()
    upsert_grid_cameras(db_session, full)

    # Attach a real detection to scaletest15 before it "disappears" from the
    # catalogue, to prove the history genuinely survives, not just the row.
    removed_camera = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "grid:scaletest15").first()
    assert removed_camera is not None
    det = models.Detection(camera_id=removed_camera.id, cls="car", confidence=0.9, bbox=[0, 0, 10, 10])
    db_session.add(det)
    db_session.commit()

    reduced = [r for r in full if r["id"] != "scaletest15"]
    upsert_grid_cameras(db_session, reduced)

    db_session.refresh(removed_camera)
    assert removed_camera.catalog_stale is True
    assert removed_camera.id is not None  # row still exists, not deleted

    still_there = db_session.query(models.Detection).filter(models.Detection.id == det.id).first()
    assert still_there is not None  # history preserved

    grid_cams = _scaletest_cameras(db_session)
    assert len(grid_cams) == 30  # nothing deleted — still 30 rows, one now stale
    assert sum(1 for c in grid_cams if c.catalog_stale) == 1  # exactly the removed one

    # Camera reappearing in a later sync clears the stale flag again.
    upsert_grid_cameras(db_session, full)
    db_session.refresh(removed_camera)
    assert removed_camera.catalog_stale is False


def test_api_response_never_exposes_rtsp_credentials(client, admin_token):
    """End-to-end through the real API: sync 30 real-shaped cameras, then confirm
    GET /api/cameras never returns source_uri, an rtsp:// URL, or anything
    resembling the configured grid host/credentials."""
    import json
    from app.db import SessionLocal
    from app.pipeline.sentinel_grid import upsert_grid_cameras as _upsert

    db = SessionLocal()
    try:
        _upsert(db, _thirty_catalogue_records())
    finally:
        db.close()

    resp = client.get("/api/cameras", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    body = resp.json()
    grid_cams = [c for c in body if c["source_type"] == "sentinel_grid"]
    assert len(grid_cams) >= 30
    for cam in grid_cams:
        assert "source_uri" not in cam
        assert "password" not in cam

    raw = json.dumps(body)
    assert "rtsp://" not in raw
    assert config.settings.sentinel_grid_rtsp_host not in raw


def test_adapter_builds_correctly_encoded_rtsp_url_and_never_returns_it(monkeypatch):
    """The '@' in the email MUST be percent-encoded (%40) per the task spec, and
    the built URL must never be exposed by any adapter method — only handed to
    the internal RTSPAdapter, which this test intercepts to inspect it."""
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "officer@example.com")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "s3cret")
    monkeypatch.setattr(config.settings, "sentinel_grid_rtsp_host", "203.0.113.10")
    monkeypatch.setattr(config.settings, "sentinel_grid_rtsp_port", 8554)

    captured = {}
    from app.pipeline import adapters as adapters_mod

    class _FakeRTSPAdapter:
        def __init__(self, url):
            captured["url"] = url

        def open(self):
            return True

    monkeypatch.setattr(adapters_mod, "RTSPAdapter", _FakeRTSPAdapter)

    adapter = SentinelGridAdapter("cam04")
    assert adapter.open() is True
    assert captured["url"] == "rtsp://officer%40example.com:s3cret@203.0.113.10:8554/stream/cam04"
    # SentinelGridAdapter's own public surface never returns the built URL anywhere
    assert not hasattr(adapter, "url")


def test_adapter_raises_clearly_when_credentials_not_configured(monkeypatch):
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "")
    adapter = SentinelGridAdapter("cam04")
    with pytest.raises(RuntimeError, match="credentials not configured"):
        adapter.open()
