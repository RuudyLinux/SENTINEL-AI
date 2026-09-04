"""1. Catalogue normalization  2. Catalogue upsert/idempotency"""
from app import models
from app.pipeline.catalog import normalize_record, upsert_from_catalog


def test_normalize_record_accepts_common_key_spellings():
    rec = normalize_record({"camera_id": "CAM-001", "location": "MG Road", "rtsp_url": "rtsp://h/1", "codec": "H264"})
    assert rec is not None
    assert rec.external_id == "CAM-001"
    assert rec.rtsp_url == "rtsp://h/1"
    assert rec.missing_fields == []


def test_normalize_record_nested_urls_and_lat_lng_alias():
    rec = normalize_record({"id": "CAM-002", "urls": {"rtsp": "rtsp://h/2"}, "latitude": "23.03", "longitude": "72.58"})
    assert rec.rtsp_url == "rtsp://h/2"
    assert rec.lat == 23.03 and rec.lng == 72.58


def test_normalize_record_preserves_all_three_stream_urls():
    rec = normalize_record({
        "id": "CAM-010",
        "urls": {"rtsp": "rtsp://h/10", "whep": "http://h:8889/stream/10/whep", "hls": "http://h/live/stream/10/index.m3u8"},
    })
    assert rec.rtsp_url == "rtsp://h/10"
    assert rec.whep_url == "http://h:8889/stream/10/whep"
    assert rec.hls_url == "http://h/live/stream/10/index.m3u8"


def test_normalize_record_missing_whep_hls_stays_empty_not_fabricated():
    rec = normalize_record({"id": "CAM-011", "rtsp_url": "rtsp://h/11"})
    assert rec.whep_url == ""
    assert rec.hls_url == ""
    # neither is required, so neither is flagged missing the way rtsp/location are
    assert "whep_url" not in rec.missing_fields
    assert "hls_url" not in rec.missing_fields


def test_normalize_record_flags_missing_fields_without_inventing_data():
    rec = normalize_record({"id": "CAM-003"})
    assert rec is not None
    assert rec.rtsp_url == ""
    assert "rtsp_url" in rec.missing_fields
    assert "location" in rec.missing_fields


def test_normalize_record_none_without_any_id():
    assert normalize_record({"location": "no id here"}) is None
    assert normalize_record("not a dict") is None


def test_upsert_creates_then_updates_without_duplicating(db_session):
    records = [{"id": "CAM-100", "location": "Zone A", "rtsp_url": "rtsp://h/100"}]
    first = upsert_from_catalog(db_session, records)
    assert first["created"] == 1 and first["updated"] == 0

    records[0]["location"] = "Zone A (renamed)"
    second = upsert_from_catalog(db_session, records)
    assert second["created"] == 0 and second["updated"] == 1

    rows = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "CAM-100").all()
    assert len(rows) == 1  # no duplicate row
    assert rows[0].location == "Zone A (renamed)"
    assert rows[0].status == "offline"  # sync registers only, never connects


def test_upsert_marks_absent_camera_stale_not_deleted(db_session):
    upsert_from_catalog(db_session, [{"id": "CAM-200", "location": "Zone B", "rtsp_url": "rtsp://h/200"}])
    # second sync no longer lists CAM-200
    upsert_from_catalog(db_session, [{"id": "CAM-201", "location": "Zone C", "rtsp_url": "rtsp://h/201"}])

    row = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "CAM-200").first()
    assert row is not None  # not deleted
    assert row.catalog_stale is True


def test_upsert_preserves_whep_and_hls_urls(db_session):
    upsert_from_catalog(db_session, [{
        "id": "CAM-300", "location": "Zone D", "rtsp_url": "rtsp://h/300",
        "whep_url": "http://h:8889/stream/300/whep", "hls_url": "http://h/live/stream/300/index.m3u8",
    }])
    row = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "CAM-300").first()
    assert row.whep_url == "http://h:8889/stream/300/whep"
    assert row.hls_url == "http://h/live/stream/300/index.m3u8"


def test_upsert_leaves_whep_hls_null_when_catalogue_never_supplied_one(db_session):
    upsert_from_catalog(db_session, [{"id": "CAM-301", "location": "Zone E", "rtsp_url": "rtsp://h/301"}])
    row = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "CAM-301").first()
    assert row.whep_url is None
    assert row.hls_url is None


def test_upsert_does_not_erase_whep_hls_on_a_resync_that_omits_them(db_session):
    """Idempotent re-sync: a later catalogue response that happens not to
    repeat the whep/hls fields must not be read as 'they were removed'."""
    upsert_from_catalog(db_session, [{
        "id": "CAM-302", "location": "Zone F", "rtsp_url": "rtsp://h/302",
        "whep_url": "http://h:8889/stream/302/whep",
    }])
    upsert_from_catalog(db_session, [{"id": "CAM-302", "location": "Zone F (renamed)", "rtsp_url": "rtsp://h/302"}])

    row = db_session.query(models.Camera).filter(models.Camera.external_catalog_id == "CAM-302").first()
    assert row.location == "Zone F (renamed)"  # the resync's real update did apply
    assert row.whep_url == "http://h:8889/stream/302/whep"  # but this wasn't wiped


def test_existing_camera_row_without_whep_hls_migrates_safely(db_session):
    """Simulates a camera row that predates this column (as ensure_columns'
    additive migration would leave one) — reading whep_url/hls_url on it
    must not error, and must come back as None, not a crash or a
    fabricated value."""
    camera = models.Camera(
        camera_code="C-PRE-EXISTING", name="pre-existing", source_type="rtsp",
        source_uri="rtsp://h/pre", external_catalog_id="CAM-PRE",
    )
    db_session.add(camera)
    db_session.commit()
    db_session.refresh(camera)
    assert camera.whep_url is None
    assert camera.hls_url is None

    # a later sync can then populate it going forward without issue
    upsert_from_catalog(db_session, [{
        "id": "CAM-PRE", "location": "x", "rtsp_url": "rtsp://h/pre",
        "hls_url": "http://h/live/stream/pre/index.m3u8",
    }])
    db_session.refresh(camera)
    assert camera.hls_url == "http://h/live/stream/pre/index.m3u8"
