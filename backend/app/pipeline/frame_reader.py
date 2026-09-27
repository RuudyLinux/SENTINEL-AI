"""Latest-frame reader: decouples stream decoding from frame processing.

The camera loop used to call `source.read()` itself, then run the clip buffer,
inference, persistence and the MJPEG encode before reading again. A live RTSP
source keeps producing frames at 25-30fps whatever the consumer does, so every
frame the loop could not keep up with queued behind it: measured on the real
grid, the loop consumed ~5fps and the picture ran at ~0.2x real time, falling
about 48s further behind every minute until the server dropped the connection.

Here one thread per capture session reads continuously and keeps only the most
recent frame. The loop takes whatever is newest when it is ready, and frames it
never took are simply dropped. Memory is bounded at one frame, the loop can
never be further behind than one of its own iterations, and frames still reach
the tracker in capture order, because sequence numbers only move forward.

The reader thread OWNS the source from `start()` until it exits: every read,
`pos_msec()` and the final `release()` happen on that one thread, since a
cv2.VideoCapture must not be used from two threads at once.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sentinel.worker")

#: After a failed read the thread waits this long before trying again, so a dead
#: stream reports failures at a bounded rate instead of spinning.
FAILURE_BACKOFF_S = 0.5


@dataclass
class FrameResult:
    frame: Any  # np.ndarray, or None when this result reports a failure/timeout
    seq: int  # sequence number of `frame` (or the latest frame seen, if None)
    fail_seq: int  # failures seen so far; the caller passes it back to wait past them
    pos_msec: "float | None" = None
    age_ms: float = 0.0  # how old `frame` was when handed over
    timed_out: bool = False


class LatestFrameReader:
    def __init__(self, source: Any, name: str, pace_fps: "float | None" = None):
        """`pace_fps` is for sources that do not pace themselves: a video file
        or a synthetic feed would otherwise be read as fast as the CPU allows.
        A live stream is paced by the camera and passes None."""
        self._source = source
        self._name = name
        self._pace_s = (1.0 / pace_fps) if pace_fps and pace_fps > 0 else None
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._frame: Any = None
        self._pos_msec: "float | None" = None
        self._frame_at = 0.0
        self._seq = 0
        self._fail_seq = 0
        self.frames_read = 0
        self._thread = threading.Thread(target=self._run, name=f"reader-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to finish. It releases the source itself once its
        current read returns, so this never touches the capture."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    ok, frame = self._source.read()
                    pos = self._source.pos_msec() if ok and frame is not None else None
                except Exception:
                    logger.exception("reader %s: read raised", self._name)
                    ok, frame, pos = False, None, None
                if ok and frame is not None:
                    with self._cond:
                        self._frame, self._pos_msec = frame, pos
                        self._frame_at = time.monotonic()
                        self._seq += 1
                        self.frames_read += 1
                        self._cond.notify_all()
                    if self._pace_s is not None:
                        self._stop.wait(max(0.0, self._pace_s - (time.monotonic() - started)))
                else:
                    with self._cond:
                        self._fail_seq += 1
                        self._cond.notify_all()
                    self._stop.wait(FAILURE_BACKOFF_S)
        finally:
            try:
                self._source.release()
            except Exception:
                logger.exception("reader %s: release failed", self._name)

    def next_frame(self, after_seq: int, after_fail_seq: int, timeout: float) -> FrameResult:
        """The newest frame newer than `after_seq`; otherwise a result with
        `frame=None` once a failure newer than `after_fail_seq` is reported, or
        `timeout` passes (a stalled stream that neither delivers nor errors).
        A newer frame always wins over a failure: the stream is evidently fine."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while (self._seq <= after_seq and self._fail_seq <= after_fail_seq
                   and not self._stop.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if self._seq > after_seq:
                return FrameResult(
                    frame=self._frame, seq=self._seq, fail_seq=self._fail_seq, pos_msec=self._pos_msec,
                    age_ms=(time.monotonic() - self._frame_at) * 1000,
                )
            return FrameResult(
                frame=None, seq=self._seq, fail_seq=self._fail_seq,
                timed_out=self._fail_seq <= after_fail_seq,
            )
