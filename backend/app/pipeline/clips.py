"""Bounded per-camera event video clips (Phase 3 P0).

A small ring buffer of recently-seen JPEG-encoded frames per camera feeds
both the pre-event window and any active clip build — bounded, never
unbounded: old ring entries are evicted by wall-clock age
(`clip_pre_event_seconds`), and a clip build only subscribes to new frames
for its configured post-event window (`clip_post_event_seconds`) before it
unregisters itself. Nothing here ever buffers a whole camera stream.

The camera loop (`worker.py`) calls `push_frame()` once per frame with the
same JPEG bytes it already encodes for the MJPEG/snapshot endpoints — no
extra encoding work. On a real alert, `worker.py` calls `build_event_clip()`
as a background task so the camera's own read/inference loop is never
blocked waiting for the post-event window to elapse.
"""
import asyncio
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import imageio_ffmpeg

from .. import models
from ..config import settings
from ..db import SessionLocal
from ..audit import log_action

_RING: dict[str, deque[tuple[float, bytes]]] = {}
_SUBSCRIBERS: dict[str, list[asyncio.Queue]] = {}


def push_frame(camera_id: str, jpeg_bytes: bytes) -> None:
    now = time.monotonic()
    buf = _RING.setdefault(camera_id, deque())
    buf.append((now, jpeg_bytes))
    cutoff = now - settings.clip_pre_event_seconds
    while buf and buf[0][0] < cutoff:
        buf.popleft()

    for q in _SUBSCRIBERS.get(camera_id, []):
        if not q.full():
            q.put_nowait(jpeg_bytes)


def release_camera(camera_id: str) -> None:
    """Drop a camera's ring buffer (call on stop_worker — mirrors
    detector.release_model)."""
    _RING.pop(camera_id, None)


def _recent_frames(camera_id: str) -> list[bytes]:
    return [b for _, b in _RING.get(camera_id, deque())]


def _encode_clip(frames: list[bytes], path: str) -> bool:
    """Synchronous, CPU-bound: decode each buffered JPEG and encode it to a
    bounded MP4. Runs off the event loop via asyncio.to_thread (see caller).
    Returns False (and writes nothing) if no frame in the batch decodes.

    Encodes via a piped ffmpeg subprocess (libx264/H.264), not
    cv2.VideoWriter — this machine's OpenCV FFMPEG backend has no working
    H.264 encoder (its bundled OpenH264 DLL fails to load; confirmed live:
    every avc1/h264/H264/X264 fourcc fails to open), and the only fourcc
    that DOES work there, mp4v (MPEG-4 Part 2 / FMP4), is not a codec
    browsers can decode — a clip written that way loaded as a real 768x432
    file but Chrome's <video> reported networkState NETWORK_NO_SOURCE and
    never played a frame. ffmpeg's own statically-linked libx264 (bundled
    via imageio-ffmpeg, already a transitive dependency) sidesteps the
    missing system codec entirely and produces an ordinary browser-playable
    H.264/yuv420p MP4."""
    decoded = []
    for b in frames:
        arr = cv2.imdecode(np.frombuffer(b, dtype=np.uint8), cv2.IMREAD_COLOR)
        if arr is not None:
            decoded.append(arr)
    if not decoded:
        return False

    h, w = decoded[0].shape[:2]
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(settings.clip_fps),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for frame in decoded:
            if frame.shape[:2] != (h, w):
                frame = cv2.resize(frame, (w, h))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait(timeout=60)
    except Exception:
        proc.kill()
        proc.wait(timeout=10)
        return False
    return proc.returncode == 0 and Path(path).exists() and Path(path).stat().st_size > 0


async def build_event_clip(
    camera_id: str,
    camera_code: str,
    alert_id: str,
    detection_id: str | None,
    incident_id: str | None,
    event_type: str,
    source_timestamp: datetime | None,
) -> None:
    """Background task: waits out the bounded post-event window collecting
    live frames as the camera loop produces them, stitches pre+post into a
    bounded MP4, and writes an Evidence(evidence_type="clip") row. Never
    blocks the camera's own loop. If the camera drops mid-capture and no
    frames end up available, no clip/Evidence row is created — no fake
    evidence."""
    pre_frames = _recent_frames(camera_id)

    max_post_frames = int(settings.clip_post_event_seconds * 30) + 10  # generous upper bound, still finite
    q: asyncio.Queue = asyncio.Queue(maxsize=max_post_frames)
    _SUBSCRIBERS.setdefault(camera_id, []).append(q)
    post_frames: list[bytes] = []
    try:
        deadline = time.monotonic() + settings.clip_post_event_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await asyncio.wait_for(q.get(), timeout=remaining)
                post_frames.append(frame)
            except asyncio.TimeoutError:
                break
    finally:
        subs = _SUBSCRIBERS.get(camera_id, [])
        if q in subs:
            subs.remove(q)

    frames = pre_frames + post_frames
    if not frames:
        return

    fname = f"{camera_code}_clip_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.mp4"
    path = settings.evidence_dir / fname
    # Decode + VideoWriter encoding is CPU-bound OpenCV work — offloaded to a
    # worker thread so it never blocks THIS process's single asyncio event
    # loop (which every camera's own read/inference loop also shares).
    wrote = await asyncio.to_thread(_encode_clip, frames, str(path))
    if not wrote:
        return

    db = SessionLocal()
    try:
        evidence = models.Evidence(
            incident_id=incident_id,
            evidence_type="clip",
            camera_id=camera_id,
            alert_id=alert_id,
            detection_id=detection_id,
            event_type=event_type,
            source_timestamp=source_timestamp,
            file_path=str(path),
            verification_status="unverified",
        )
        db.add(evidence)
        db.commit()
        log_action(db, None, "generate_evidence_clip", resource=evidence.id)
    finally:
        db.close()
