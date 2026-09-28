"""Sentinel Camera Grid integration, no network: httpx is mocked (like
test_catalog_host_missing.py) and no real credentials are used. Missing
config, AUTH_ERROR, normalization, idempotent upsert, %40-encoded URLs."""
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
    assert cams[0].source_uri == "cam04"  # bare id, never a credentialed URL
    assert cams[0].camera_group == "Sentinel Grid"
    assert cams[0].status == "offline"  # registered only, never auto-connected

    summary2 = upsert_grid_cameras(db_session, [{"id": "cam04", "name": "Gate 4", "location": "North Gate (renamed)"}])
    assert summary2["created"] == 0
    assert summary2["updated"] == 1
    cams2 = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "grid:cam04").all()
    assert len(cams2) == 1  # still exactly one row, not duplicated
    assert cams2[0].location == "North Gate (renamed)"


def test_upsert_new_camera_defaults_ai_on(db_session):
    """New grid cameras default ai_person/ai_vehicle/ai_anpr to True, like
    the column default and POST /api/cameras. The supervisor still never
    writes them, and PATCH /api/cameras/{id} turns AI off per camera."""
    records = [{"id": "cam-ai-default-test", "name": "AI Default Test", "location": "X"}]
    upsert_grid_cameras(db_session, records)
    cam = db_session.query(models.Camera).filter(
        models.Camera.external_catalog_id == "grid:cam-ai-default-test"
    ).first()
    assert cam is not None
    assert cam.ai_person is True
    assert cam.ai_vehicle is True
    assert cam.ai_anpr is True


def test_a_re_synced_camera_keeps_its_operator_set_ai_flags(db_session):
    """A re-sync doesn't turn AI back on for a camera an operator switched
    off; only the create branch sets the flags."""
    records = [{"id": "cam-ai-keep-test", "name": "Keep Test", "location": "X"}]
    upsert_grid_cameras(db_session, records)
    cam = db_session.query(models.Camera).filter(
        models.Camera.external_catalog_id == "grid:cam-ai-keep-test"
    ).first()
    cam.ai_person = False
    cam.ai_vehicle = False
    cam.ai_anpr = False
    db_session.commit()

    upsert_grid_cameras(db_session, records)  # re-sync, same camera
    db_session.refresh(cam)
    assert cam.ai_person is False
    assert cam.ai_vehicle is False
    assert cam.ai_anpr is False


def test_upsert_skips_invalid_records_without_crashing(db_session):
    summary = upsert_grid_cameras(db_session, [{"name": "no id"}, {"id": "cam05"}])
    assert summary["skipped_invalid"] == 1
    assert summary["created"] == 1


def _thirty_catalogue_records() -> list[dict]:
    # Real catalogue shape: {"id": "cam01", "name": "..."}, no lat/lng/codec,
    # so the fixture doesn't invent them. "scaletest" ids because other tests
    # here register cam04/cam05 in the same DB, and a clash would turn
    # `created` into `updated`.
    return [{"id": f"scaletest{i:02d}", "name": f"{i:02d} Test Location"} for i in range(1, 31)]


def _scaletest_cameras(db_session):
    """Only this module's scaletest% cameras; the shared DB also has cam04/05."""
    return (
        db_session.query(models.Camera)
        .filter(models.Camera.external_catalog_id.like("grid:scaletest%"))
        .all()
    )


def _clear_scaletest_cameras(db_session) -> None:
    """Delete the scaletest cameras and their dependents.

    Three tests sync the same 30 records, so whichever ran first created
    them and `created == 30` depended on order (--random-order caught it).
    Dependents first: one test attaches a Detection to scaletest15, and with
    FKs on the camera alone can't be deleted.
    """
    camera_ids = [
        c.id for c in db_session.query(models.Camera).filter(
            models.Camera.external_catalog_id.like("grid:scaletest%")
        ).all()
    ]
    if not camera_ids:
        return
    db_session.query(models.Detection).filter(
        models.Detection.camera_id.in_(camera_ids)
    ).delete(synchronize_session=False)
    db_session.query(models.Camera).filter(
        models.Camera.id.in_(camera_ids)
    ).delete(synchronize_session=False)
    db_session.commit()


def test_upsert_handles_full_30_camera_catalogue_idempotently(db_session):
    """30 catalogue cameras -> 30 rows, and rediscovery never duplicates or
    drops any."""
    records = _thirty_catalogue_records()
    # make the precondition true, see _clear_scaletest_cameras
    _clear_scaletest_cameras(db_session)

    summary1 = upsert_grid_cameras(db_session, records)
    assert summary1["created"] == 30
    assert summary1["updated"] == 0

    grid_cams = _scaletest_cameras(db_session)
    assert len(grid_cams) == 30
    assert all(not c.catalog_stale for c in grid_cams)
    codes = [c.camera_code for c in grid_cams]
    assert len(codes) == len(set(codes))  # no duplicate camera_code

    # same 30 again: idempotent, no new rows
    summary2 = upsert_grid_cameras(db_session, records)
    assert summary2["created"] == 0
    assert summary2["updated"] == 30
    grid_cams2 = _scaletest_cameras(db_session)
    assert len(grid_cams2) == 30  # still exactly 30, not 60


def test_upsert_marks_removed_camera_stale_without_deleting_it(db_session):
    """A camera the catalogue drops is marked stale, never deleted; its
    history stays."""
    full = _thirty_catalogue_records()
    upsert_grid_cameras(db_session, full)

    # a real detection on scaletest15 first, so we check the history
    # survives, not just the row
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
    assert len(grid_cams) == 30  # nothing deleted, one now stale
    assert sum(1 for c in grid_cams if c.catalog_stale) == 1  # exactly the removed one

    # Camera reappearing in a later sync clears the stale flag again.
    upsert_grid_cameras(db_session, full)
    db_session.refresh(removed_camera)
    assert removed_camera.catalog_stale is False


def test_api_response_never_exposes_rtsp_credentials(client, admin_token):
    """Through the real API: sync 30 cameras, then GET /api/cameras never
    shows source_uri, an rtsp:// URL, or the grid host/credentials."""
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
    """The @ in the email is encoded as %40, and the URL is only handed to
    the internal RTSPAdapter (intercepted here), never exposed."""
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "officer@example.com")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "s3cret")
    monkeypatch.setattr(config.settings, "sentinel_grid_rtsp_host", "203.0.113.10")
    monkeypatch.setattr(config.settings, "sentinel_grid_rtsp_port", 8554)

    captured = {}
    from app.pipeline import adapters as adapters_mod

    class _FakeRTSPAdapter:
        def __init__(self, url, transport=None):
            captured["url"] = url
            captured["transport"] = transport

        def open(self):
            return True

    monkeypatch.setattr(adapters_mod, "RTSPAdapter", _FakeRTSPAdapter)

    adapter = SentinelGridAdapter("cam04")
    assert adapter.open() is True
    assert captured["url"] == "rtsp://officer%40example.com:s3cret@203.0.113.10:8554/stream/cam04"
    assert captured["transport"] == config.settings.sentinel_grid_rtsp_transport
    # SentinelGridAdapter's own public surface never returns the built URL anywhere
    assert not hasattr(adapter, "url")


def test_adapter_raises_clearly_when_credentials_not_configured(monkeypatch):
    monkeypatch.setattr(config.settings, "sentinel_grid_email", "")
    monkeypatch.setattr(config.settings, "sentinel_grid_password", "")
    adapter = SentinelGridAdapter("cam04")
    with pytest.raises(RuntimeError, match="credentials not configured"):
        adapter.open()
