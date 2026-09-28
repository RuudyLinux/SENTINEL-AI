"""Latest-frame reader: decoding runs independently of frame processing.

Live streams keep sending at their own rate, so reading between processing
steps lets frames queue up and the picture fall behind. One thread per session
reads continuously and keeps only the newest frame; the loop takes it when
ready. Sequence numbers only increase, so the tracker still sees frames in
order.

The reader thread owns the source from start() until it exits: a VideoCapture
can't be shared between threads.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sentinel.worker")

# wait after a failed read so a dead stream doesn't spin
FAILURE_BACKOFF_S = 0.5


@dataclass
class FrameResult:
    frame: Any  # np.ndarray, or None for a failure/timeout
    seq: int  # seq of `frame`, or of the latest frame seen if None
    fail_seq: int  # failures so far, caller passes it back to wait past them
    pos_msec: "float | None" = None
    age_ms: float = 0.0  # how old `frame` was when handed over
    timed_out: bool = False


class LatestFrameReader:
    def __init__(self, source: Any, name: str, pace_fps: "float | None" = None):
        """pace_fps paces sources that don't pace themselves (files, synthetic
        feeds); live streams pass None."""
        self._source = source
        self._name = name
        self._pace_s = (1.0 / pace_fps) if pace_fps and pace_fps > 0 else None
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._frame: Any = None
        self._pos_msec: "float | None" = None
        self._frame_at = 0.0
        self._seq = 0          # frames decoded
        self._frame_seq = 0    # decode number of the frame we're holding
        self._fail_seq = 0
        self.frames_read = 0
        # Grab-capable sources decode every frame but convert to BGR only when
        # the consumer is waiting for one.
        self._grab = bool(getattr(source, "can_grab", False)) and pace_fps is None
        self._wanted = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"reader-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to stop. It releases the source itself after its
        current read, so this never touches the capture."""
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
                skipped = False
                try:
                    if self._grab:
                        ok, frame = self._source.grab(), None
                        if ok and self._wanted.is_set():
                            ok, frame = self._source.retrieve()
                        elif ok:
                            skipped = True
                    else:
                        ok, frame = self._source.read()
                    pos = self._source.pos_msec() if ok and frame is not None else None
                except Exception:
                    logger.exception("reader %s: read raised", self._name)
                    ok, frame, pos = False, None, None
                if skipped:
                    # decoded, nobody waiting: count it so dropped-frame
                    # stats stay right, don't publish
                    with self._cond:
                        self._seq += 1
                        self.frames_read += 1
                elif ok and frame is not None:
                    self._wanted.clear()
                    with self._cond:
                        self._frame, self._pos_msec = frame, pos
                        self._frame_at = time.monotonic()
                        self._seq += 1
                        self._frame_seq = self._seq
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
        """Newest frame after after_seq; otherwise frame=None on a failure after
        after_fail_seq or after `timeout`. A newer frame takes precedence over a
        failure."""
        deadline = time.monotonic() + timeout
        with self._cond:
            # a frame newer than after_seq that we already hold is good enough
            if self._frame_seq <= after_seq:
                self._wanted.set()
            while (self._frame_seq <= after_seq and self._fail_seq <= after_fail_seq
                   and not self._stop.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if self._frame_seq > after_seq:
                return FrameResult(
                    frame=self._frame, seq=self._frame_seq, fail_seq=self._fail_seq, pos_msec=self._pos_msec,
                    age_ms=(time.monotonic() - self._frame_at) * 1000,
                )
            return FrameResult(
                frame=None, seq=self._frame_seq, fail_seq=self._fail_seq,
                timed_out=self._fail_seq <= after_fail_seq,
            )
