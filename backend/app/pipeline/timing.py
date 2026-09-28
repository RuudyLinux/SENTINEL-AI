"""Source time (PTS) reconstruction.

CAP_PROP_POS_MSEC depends on the backend:
- video_file: generally reliable, presentation time from the start of the file
- rtsp: often fine when the server sends proper RTP timestamps, not
  guaranteed, and OpenCV can't tell a real RTP PTS from a made-up one
- webcam: known to be stuck at zero or wall-clock-ish, never trusted

POS_MSEC is relative to when the source opened, so a trusted value is
anchored to the open time to get an absolute source_timestamp. Best effort,
not a synced clock. When it can't be trusted callers use the processing
timestamp; the two are separate columns (models.py).
"""
from datetime import datetime, timedelta

# Backends where POS_MSEC is usable. Not webcam. sentinel_grid is RTSP
# underneath (SentinelGridAdapter), same trust as rtsp.
TRUSTED_SOURCE_TYPES = {"video_file", "rtsp", "sentinel_grid"}


def compute_source_timestamp(
    source_type: str,
    session_opened_at: datetime,
    pos_msec: float | None,
    last_pos_msec: float | None,
) -> datetime | None:
    """source_timestamp, or None when POS_MSEC can't be trusted: backend not
    in the list, value missing or negative, or it didn't move forward since
    the last accepted value (some FFmpeg/RTSP builds get stuck or jump back).
    """
    if source_type not in TRUSTED_SOURCE_TYPES:
        return None
    if pos_msec is None or pos_msec < 0:
        return None
    if last_pos_msec is not None and pos_msec <= last_pos_msec:
        return None
    return session_opened_at + timedelta(milliseconds=pos_msec)
