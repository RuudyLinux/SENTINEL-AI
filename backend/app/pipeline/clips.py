"""Per-camera event clips, bounded.

A small ring buffer of recent JPEG frames per camera feeds the pre-event
window and any clip being built. Entries age out after
clip_pre_event_seconds, and a clip build only listens for new frames for
clip_post_event_seconds, then unregisters. Nothing buffers a whole stream.

worker.py pushes the JPEG it already encodes via push_frame(), and on an
alert runs build_event_clip() as a background task so the camera loop never
waits out the post-event window.
"""
import asyncio
import logging
import itertools
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import imageio_ffmpeg

from .. import models
from ..config import settings
from ..evidence_hash import sha256_file
from ..db import SessionLocal
from ..audit import log_action

logger = logging.getLogger(__name__)

_RING: dict[str, deque[tuple[float, bytes]]] = {}

# max clips encoding at once. one 1080p libx264 encode already uses several
# cores, and a busy zone fires many alerts a second; unbounded encodes
# starved the camera loops and ran out of RAM
_ENCODE_SLOTS = threading.BoundedSemaphore(2)
_ENCODE_THREADS = 2  # per encode, so clips use at most 4 threads in all
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
            q.put_nowait((now, jpeg_bytes))


def release_camera(camera_id: str) -> None:
    """Drop a camera's ring buffer, called from stop_worker."""
    _RING.pop(camera_id, None)


def _recent_frames(camera_id: str) -> list[bytes]:
    return [b for _, b in _RING.get(camera_id, deque())]


def _timed_recent_frames(camera_id: str) -> list[tuple[float, bytes]]:
    return list(_RING.get(camera_id, deque()))


def _playback_fps(timed_frames: list[tuple[float, bytes]]) -> float:
    """The rate frames were actually captured at, so clips play in real time.
    The loop only takes the newest frame now (~2-3fps under AI), and encoding
    at the nominal 10fps played evidence 3-5x too fast."""
    if len(timed_frames) < 2:
        return settings.clip_fps
    span = timed_frames[-1][0] - timed_frames[0][0]
    if span <= 0:
        return settings.clip_fps
    return max(0.5, min(30.0, (len(timed_frames) - 1) / span))


def _encode_clip(frames: list[bytes], path: str, fps: "float | None" = None) -> bool:
    """CPU-bound: decode each buffered JPEG and encode a bounded MP4. Run via
    to_thread. False (nothing written) if no frame decodes.

    Uses a piped ffmpeg (libx264) instead of cv2.VideoWriter. This OpenCV
    build has no working H.264 encoder (its OpenH264 DLL won't load, every
    avc1/h264/X264 fourcc fails), and the one fourcc that works, mp4v, isn't
    something browsers decode: Chrome's <video> sat at NETWORK_NO_SOURCE.
    imageio-ffmpeg (already a dependency) ships a static libx264, which gives
    a normal H.264/yuv420p MP4."""
    with _ENCODE_SLOTS:
        return _encode_clip_now(frames, path, fps)


def _encode_clip_now(frames: list[bytes], path: str, fps: "float | None") -> bool:
    # Decode one frame at a time while feeding ffmpeg. Decoding the whole
    # batch first held every raw frame: 6.2 MB each at 1080p, ~750 MB for a
    # 15s clip at 8 fps, and a few clips from a busy zone ran out of memory.
    decoded = (
        arr for arr in (cv2.imdecode(np.frombuffer(b, dtype=np.uint8), cv2.IMREAD_COLOR) for b in frames)
        if arr is not None
    )
    first = next(decoded, None)
    if first is None:
        return False

    h, w = first.shape[:2]
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps or settings.clip_fps:.3f}",
        "-i", "-",
        # -threads: libx264 grabs every core otherwise. A busy zone alerting
        # every ~2s kept the box at 90-100% CPU and halved the AI rate.
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-threads", str(_ENCODE_THREADS), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(path),
    ]
    # stderr to a temp FILE, not a pipe, on purpose. Nobody reads a PIPE
    # while we're writing frames, so a chatty ffmpeg fills the OS buffer and
    # blocks on stderr while we block on stdin: deadlock, and wait(timeout)
    # is never reached because we're stuck in the frame loop. communicate()
    # afterwards has the same hole. `-loglevel error` only hides it. A file
    # has no limit and gets read after exit.
    #
    # `with proc` closes stdin however the block exits (the "unclosed file"
    # ResourceWarning in tests).
    err = b""
    with tempfile.TemporaryFile() as errfile:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errfile)
        try:
            with proc:
                for frame in itertools.chain([first], decoded):
                    if frame.shape[:2] != (h, w):
                        frame = cv2.resize(frame, (w, h))
                    proc.stdin.write(frame.tobytes())
                proc.stdin.close()
                proc.wait(timeout=60)
        except Exception as exc:
            proc.kill()
            proc.wait(timeout=10)
            # usually BrokenPipeError: ffmpeg rejected its args and exited
            # while we were still writing. log it, or a broken encoder looks
            # just like an empty ring buffer
            errfile.seek(0)
            detail = errfile.read().decode("utf-8", "replace").strip()[-500:]
            logger.warning("clip encode failed (%s): %s", type(exc).__name__, detail or exc)
            return False
        errfile.seek(0)
        err = errfile.read()

    if proc.returncode != 0:
        # still just False for the caller, but log why
        logger.warning(
            "clip encode failed (ffmpeg exit %s): %s",
            proc.returncode,
            err.decode("utf-8", "replace").strip()[-500:],
        )
        return False
    return Path(path).exists() and Path(path).stat().st_size > 0


async def build_event_clip(
    camera_id: str,
    camera_code: str,
    alert_id: str,
    detection_id: str | None,
    incident_id: str | None,
    event_type: str,
    source_timestamp: datetime | None,
) -> None:
    """Background task: collect frames for the post-event window, stitch pre +
    post into a bounded MP4 and write an Evidence(evidence_type="clip") row.
    Never blocks the camera loop. No frames (camera dropped) = no clip and no
    Evidence row."""
    pre_frames = _timed_recent_frames(camera_id)

    max_post_frames = int(settings.clip_post_event_seconds * 30) + 10  # generous upper bound, still finite
    q: asyncio.Queue = asyncio.Queue(maxsize=max_post_frames)
    _SUBSCRIBERS.setdefault(camera_id, []).append(q)
    post_frames: list[tuple[float, bytes]] = []
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

    timed = pre_frames + post_frames
    frames = [jpeg for _, jpeg in timed]
    if not frames:
        return

    fname = f"{camera_code}_clip_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.mp4"
    path = settings.evidence_dir / fname
    # CPU-bound encode, off the event loop every camera shares
    wrote = await asyncio.to_thread(_encode_clip, frames, str(path), _playback_fps(timed))
    if not wrote:
        return

    # in a thread: a contended commit (30s busy timeout) on the loop froze
    # every camera with it
    await asyncio.to_thread(
        _record_clip_evidence, camera_id, alert_id, detection_id, incident_id,
        event_type, source_timestamp, str(path),
    )


def _record_clip_evidence(
    camera_id: str, alert_id: str, detection_id: "str | None", incident_id: "str | None",
    event_type: str, source_timestamp: "datetime | None", path: str,
) -> None:
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
            file_path=path,
            sha256=sha256_file(path),
            verification_status="unverified",
            model_version=settings.model_version,
            rule_version=settings.rule_version,
        )
        db.add(evidence)
        db.commit()
        log_action(db, None, "generate_evidence_clip", resource=evidence.id, actor="system")
    finally:
        db.close()
