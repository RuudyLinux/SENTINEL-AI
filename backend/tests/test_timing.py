"""9. Timestamp propagation — SOURCE PTS reconstruction, backend-aware."""
from datetime import datetime, timedelta

from app.pipeline.timing import compute_source_timestamp


def test_webcam_never_trusted_even_with_a_plausible_reading():
    opened = datetime(2026, 9, 3, 12, 0, 0)
    assert compute_source_timestamp("webcam", opened, 1234.0, None) is None


def test_video_file_anchors_pos_msec_to_session_open_time():
    opened = datetime(2026, 9, 3, 12, 0, 0)
    ts = compute_source_timestamp("video_file", opened, 2500.0, None)
    assert ts == opened + timedelta(milliseconds=2500.0)


def test_rtsp_rejects_a_reading_that_did_not_advance():
    opened = datetime(2026, 9, 3, 12, 0, 0)
    assert compute_source_timestamp("rtsp", opened, 500.0, 500.0) is None
    assert compute_source_timestamp("rtsp", opened, 400.0, 500.0) is None  # went backwards


def test_negative_or_missing_reading_rejected():
    opened = datetime(2026, 9, 3, 12, 0, 0)
    assert compute_source_timestamp("video_file", opened, -1.0, None) is None
    assert compute_source_timestamp("video_file", opened, None, None) is None
