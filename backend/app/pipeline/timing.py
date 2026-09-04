"""Source-time (PTS) reconstruction — Phase 3.

CAP_PROP_POS_MSEC's reliability is backend-dependent. This is the honest
answer to "investigate whether CAP_PROP_POS_MSEC provides reliable source
position" — it does not, uniformly, and this module does not pretend it does:

- video_file (FFmpeg demuxing a local file): generally reliable — reports
  the decoded frame's presentation time relative to the start of the file.
- rtsp (FFmpeg demuxing a live RTSP stream): often reliable when the
  camera/server sends proper RTP timestamps, but this is not guaranteed
  across vendors — some servers under/over-report, and OpenCV exposes no
  way to distinguish a trustworthy RTP-derived PTS from a synthesized one.
- webcam (DirectShow/MSMF/V4L2 capture devices): CAP_PROP_POS_MSEC is
  well-documented as unreliable/stuck-at-zero/wall-clock-ish on these
  backends — never trusted here.

POS_MSEC is stream-relative (milliseconds since the source was opened), not
an absolute clock, so a trusted reading is anchored to the wall-clock time
the source was opened to produce a usable absolute `source_timestamp` for
the database. This is a best-effort approximation, not a synchronized
clock — documented, not hidden. When it isn't trustworthy, callers fall
back to `processing_timestamp` (wall clock when SENTINEL handled the
frame) — the two concepts are stored separately (see models.py), never
conflated.
"""
from datetime import datetime, timedelta

# Backends where POS_MSEC is documented/workable enough to anchor a
# source_timestamp. Webcam is deliberately excluded. sentinel_grid is real RTSP
# under the hood (pipeline/adapters.SentinelGridAdapter delegates to RTSPAdapter)
# — same trust profile as "rtsp", not a separate case.
TRUSTED_SOURCE_TYPES = {"video_file", "rtsp", "sentinel_grid"}


def compute_source_timestamp(
    source_type: str,
    session_opened_at: datetime,
    pos_msec: float | None,
    last_pos_msec: float | None,
) -> datetime | None:
    """Best-effort source_timestamp, or None when POS_MSEC isn't trustworthy
    for this backend/reading.

    Guards:
    - backend must be one POS_MSEC is workable for (not webcam)
    - value must be present and non-negative
    - value must have advanced since the last accepted reading (guards a
      known stuck/backwards-jump failure mode on some FFmpeg/RTSP builds)
    """
    if source_type not in TRUSTED_SOURCE_TYPES:
        return None
    if pos_msec is None or pos_msec < 0:
        return None
    if last_pos_msec is not None and pos_msec <= last_pos_msec:
        return None
    return session_opened_at + timedelta(milliseconds=pos_msec)
