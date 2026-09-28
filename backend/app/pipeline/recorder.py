"""Manual recordings: the REC button.

Writes a camera's annotated live view (the MJPEG frames, AI boxes included)
to an H.264 MP4 and stores it as hashed Evidence, like the event clips. One
thread per recording feeds ffmpeg at a fixed rate, repeating the last frame
when the camera is slower, so the file plays in real time.

It stops when asked, at recording_max_seconds, when the worker stops, or when
the picture hasn't changed for _STALL_S (stream gone). Whatever was recorded
up to then is kept. The file is hashed at the end, before anyone can touch it.
"""
import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import cv2
import imageio_ffmpeg
import numpy as np

from .. import models
from ..audit import log_action
from ..config import settings
from ..db import SessionLocal
from ..evidence_hash import sha256_file

logger = logging.getLogger("sentinel.recorder")

# no new frame for this long = the stream is gone, finish the file
_STALL_S = 15.0
# waiting for the very first frame
_FIRST_FRAME_WAIT_S = 10.0


class RecordingError(Exception):
    pass


@dataclass
class Recording:
    camera_id: str
    camera_code: str
    user_id: "str | None"
    username: str
    path: str
    started_at: datetime
    started_mono: float
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: "threading.Thread | None" = None
    frames_written: int = 0
    ended_mono: "float | None" = None
    evidence_id: "str | None" = None
    stop_reason: str = ""
    error: str = ""

    def elapsed(self) -> float:
        return (self.ended_mono or time.monotonic()) - self.started_mono

    def as_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "recording": self.thread is not None and self.thread.is_alive(),
            "started_at": self.started_at.isoformat(),
            "elapsed_seconds": round(self.elapsed(), 1),
            "max_seconds": settings.recording_max_seconds,
            "started_by": self.username,
            "evidence_id": self.evidence_id,
            "stop_reason": self.stop_reason,
            "error": self.error,
        }


_ACTIVE: dict[str, Recording] = {}
_LOCK = threading.Lock()


def is_recording(camera_id: str) -> bool:
    rec = _ACTIVE.get(camera_id)
    return rec is not None and rec.thread is not None and rec.thread.is_alive()


def status(camera_id: str) -> "dict | None":
    rec = _ACTIVE.get(camera_id)
    return rec.as_dict() if rec else None


def start(camera_id: str, camera_code: str, get_frame: Callable[[], "bytes | None"],
          user_id: "str | None" = None, username: str = "") -> Recording:
    with _LOCK:
        if is_recording(camera_id):
            raise RecordingError("This camera is already recording.")
        running = sum(1 for cid in _ACTIVE if is_recording(cid))
        if running >= settings.recording_max_concurrent:
            raise RecordingError(
                f"{running} recordings are already running (limit {settings.recording_max_concurrent}). "
                "Stop one first."
            )
        if get_frame() is None:
            raise RecordingError("This camera has no live picture to record. Connect it first.")
        now = datetime.utcnow()
        path = settings.evidence_dir / f"{camera_code}_rec_{now.strftime('%Y%m%d%H%M%S%f')}.mp4"
        rec = Recording(camera_id=camera_id, camera_code=camera_code, user_id=user_id, username=username,
                        path=str(path), started_at=now, started_mono=time.monotonic())
        rec.thread = threading.Thread(target=_run, args=(rec, get_frame), name=f"rec:{camera_code}", daemon=True)
        _ACTIVE[camera_id] = rec
        rec.thread.start()
    return rec


def request_stop(camera_id: str, reason: str = "stopped") -> "Recording | None":
    """Ask a recording to finish; returns at once. The thread writes the
    evidence row itself. Safe to call from the event loop."""
    rec = _ACTIVE.get(camera_id)
    if rec is not None and not rec.stop_event.is_set():
        rec.stop_reason = rec.stop_reason or reason
        rec.stop_event.set()
    return rec


def stop(camera_id: str, timeout: float = 120.0) -> "Recording | None":
    """Stop and wait for the file and evidence row. Blocking, run it in a thread."""
    rec = request_stop(camera_id, "stopped by operator")
    if rec is not None and rec.thread is not None:
        rec.thread.join(timeout)
    return rec


def stop_all(timeout: float = 120.0) -> None:
    for cid in list(_ACTIVE):
        request_stop(cid, "server shutting down")
    deadline = time.monotonic() + timeout
    for rec in list(_ACTIVE.values()):
        if rec.thread is not None:
            rec.thread.join(max(0.0, deadline - time.monotonic()))


def _frame_size(jpeg: bytes) -> "tuple[int, int] | None":
    arr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return None
    h, w = arr.shape[:2]
    return w - w % 2, h - h % 2  # yuv420p wants even sizes


def _run(rec: Recording, get_frame: Callable[[], "bytes | None"]) -> None:
    fps = max(1.0, settings.recording_fps)
    interval = 1.0 / fps

    first = None
    deadline = time.monotonic() + _FIRST_FRAME_WAIT_S
    while first is None and not rec.stop_event.is_set() and time.monotonic() < deadline:
        first = get_frame()
        if first is None:
            time.sleep(0.1)
    size = _frame_size(first) if first else None
    if size is None:
        rec.ended_mono = time.monotonic()
        rec.error = "no decodable frame from the camera"
        rec.stop_reason = rec.stop_reason or "no picture"
        return

    w, h = size
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", f"{fps:.3f}", "-i", "-",
        # fixed output size: the stream can reconnect at another resolution
        # and libx264 can't change size mid-file
        "-vf", f"scale={w}:{h}",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        rec.path,
    ]
    # stderr to a temp file, not a pipe (see clips._encode_clip_now: an unread
    # pipe can fill and deadlock the encode)
    with tempfile.TemporaryFile() as errfile:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errfile)
        last = first
        last_change = time.monotonic()
        next_at = time.monotonic()
        try:
            with proc:
                while not rec.stop_event.is_set():
                    if rec.elapsed() >= settings.recording_max_seconds:
                        rec.stop_reason = rec.stop_reason or "maximum length reached"
                        break
                    frame = get_frame()
                    if frame is not None and frame is not last:
                        last, last_change = frame, time.monotonic()
                    elif time.monotonic() - last_change > _STALL_S:
                        rec.stop_reason = rec.stop_reason or "camera stopped sending frames"
                        break
                    # repeat the last frame when the camera is slower than
                    # fps, so the file keeps real time
                    proc.stdin.write(last)
                    rec.frames_written += 1
                    next_at += interval
                    rec.stop_event.wait(max(0.0, next_at - time.monotonic()))
                rec.ended_mono = time.monotonic()
                proc.stdin.close()
                proc.wait(timeout=120)
        except Exception as exc:
            rec.ended_mono = rec.ended_mono or time.monotonic()
            proc.kill()
            proc.wait(timeout=10)
            errfile.seek(0)
            rec.error = (errfile.read().decode("utf-8", "replace").strip()[-300:] or str(exc))
            logger.warning("recording %s failed: %s", rec.camera_code, rec.error)
            return
        if proc.returncode != 0:
            errfile.seek(0)
            rec.error = errfile.read().decode("utf-8", "replace").strip()[-300:] or f"ffmpeg exit {proc.returncode}"
            logger.warning("recording %s failed: %s", rec.camera_code, rec.error)
            return

    try:
        _save_evidence(rec)
    except Exception as exc:
        rec.error = f"recorded, but the evidence row could not be saved: {exc}"
        logger.exception("recording %s: evidence row failed", rec.camera_code)


def _save_evidence(rec: Recording) -> None:
    db = SessionLocal()
    try:
        evidence = models.Evidence(
            evidence_type="recording",
            camera_id=rec.camera_id,
            event_type="manual_recording",
            source_timestamp=rec.started_at,
            file_path=rec.path,
            sha256=sha256_file(rec.path),
            uploaded_by=rec.user_id,
            verification_status="unverified",
            model_version=settings.model_version,
            rule_version=settings.rule_version,
        )
        db.add(evidence)
        db.commit()
        rec.evidence_id = str(evidence.id)
        user = db.get(models.User, rec.user_id) if rec.user_id else None
        log_action(db, user, "save_recording",
                   resource=f"{evidence.id} ({rec.camera_code}, {rec.elapsed():.0f}s, {rec.stop_reason})")
    finally:
        db.close()
