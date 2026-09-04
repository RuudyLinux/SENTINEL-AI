"""Camera/VMS adapter interface (Model 3 — VMS Federation/Middleware Layer).

`CameraAdapter` is the common internal representation every camera/VMS source is
normalized to before it ever reaches the AI pipeline (detector/ANPR/correlation/
rules-engine code only ever talks to this interface, never to a specific vendor).

Concrete adapters:
- `WebcamAdapter` / `VideoFileAdapter` / `RTSPAdapter` — real, tested, used in
  production by every camera in this build (logic moved verbatim from the old
  monolithic `CameraSource`, see `source.py`'s backward-compat wrapper).
- `MockVMSAdapter` — a real, working generic-VMS adapter that produces genuine
  synthetic frames. Exists to prove the adapter boundary is actually pluggable
  end-to-end (a "Generic VMS" can be wired in without touching detector/ANPR/
  rules-engine code), not a stand-in for a specific vendor.
- `ONVIFAdapter` — an honest interface stub. No real ONVIF device was available to
  implement/test discovery, PTZ, or vendor-specific auth against in this build, so
  `open()` fails loudly with `NotImplementedError` rather than pretending to work.
  Per the project's own rule: never claim an integration that hasn't actually been
  exercised against something real.

Future vendor-specific VMS adapters (Milestone, Genetec, Hikvision CMS, ...) drop in
here as additional `CameraAdapter` subclasses registered in `get_adapter()` — nothing
elsewhere in the pipeline needs to change.
"""
import os
import time
from abc import ABC, abstractmethod
from urllib.parse import quote

import cv2
import numpy as np

from ..config import settings

# Fail fast on a dead/unreachable RTSP endpoint instead of hanging the worker
# thread indefinitely. No effect on webcam/video_file (ignored by those backends).
_OPEN_TIMEOUT_MS = 5000
_READ_TIMEOUT_MS = 5000


class CameraAdapter(ABC):
    """Common interface every camera/VMS source is normalized to."""

    @abstractmethod
    def open(self) -> bool: ...

    @abstractmethod
    def read(self) -> "tuple[bool, np.ndarray | None]": ...

    @abstractmethod
    def pos_msec(self) -> "float | None": ...

    @abstractmethod
    def fps(self) -> float: ...

    @abstractmethod
    def resolution(self) -> str: ...

    @abstractmethod
    def release(self) -> None: ...


class WebcamAdapter(CameraAdapter):
    def __init__(self, source_uri: str):
        self.source_uri = source_uri
        self.cap: cv2.VideoCapture | None = None

    def open(self) -> bool:
        self.cap = cv2.VideoCapture(int(self.source_uri))
        return self.cap.isOpened()

    def read(self) -> "tuple[bool, np.ndarray | None]":
        if self.cap is None:
            return False, None
        return self.cap.read()

    def pos_msec(self) -> "float | None":
        return _safe_pos_msec(self.cap)

    def fps(self) -> float:
        return _safe_fps(self.cap)

    def resolution(self) -> str:
        return _safe_resolution(self.cap)

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class VideoFileAdapter(CameraAdapter):
    def __init__(self, source_uri: str):
        self.source_uri = source_uri
        self.cap: cv2.VideoCapture | None = None

    def open(self) -> bool:
        self.cap = cv2.VideoCapture(self.source_uri)
        return self.cap.isOpened()

    def read(self) -> "tuple[bool, np.ndarray | None]":
        if self.cap is None:
            return False, None
        ok, frame = self.cap.read()
        if not ok:
            # loop the file so a short demo clip behaves like a continuous feed
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return ok, frame

    def pos_msec(self) -> "float | None":
        return _safe_pos_msec(self.cap)

    def fps(self) -> float:
        return _safe_fps(self.cap)

    def resolution(self) -> str:
        return _safe_resolution(self.cap)

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class RTSPAdapter(CameraAdapter):
    """RTSP transport (Phase 3): the official Gujarat sandbox requires TCP ("UDP
    fails across NAT/firewalls" — its own resource docs). OpenCV's FFmpeg backend
    has no per-VideoCapture-instance API for this; the documented mechanism is the
    process-level `OPENCV_FFMPEG_CAPTURE_OPTIONS` environment variable, read by
    FFmpeg's demuxer options parser on every `open()` call — set immediately before
    each open, not once at import time."""

    def __init__(self, source_uri: str):
        self.source_uri = source_uri
        self.cap: cv2.VideoCapture | None = None
        self.transport_forced_tcp = False

    def open(self) -> bool:
        if settings.rtsp_force_tcp:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
            self.transport_forced_tcp = True
        self.cap = cv2.VideoCapture(self.source_uri, cv2.CAP_FFMPEG)
        try:
            self.cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, _OPEN_TIMEOUT_MS)
            self.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, _READ_TIMEOUT_MS)
        except Exception:
            pass
        return self.cap.isOpened()

    def read(self) -> "tuple[bool, np.ndarray | None]":
        if self.cap is None:
            return False, None
        return self.cap.read()

    def pos_msec(self) -> "float | None":
        return _safe_pos_msec(self.cap)

    def fps(self) -> float:
        return _safe_fps(self.cap)

    def resolution(self) -> str:
        return _safe_resolution(self.cap)

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class SentinelGridAdapter(CameraAdapter):
    """Real Sentinel Camera Grid RTSP adapter (final integration task). Delegates
    actual capture to `RTSPAdapter` (same TCP-forcing, same timeouts) — one real
    RTSP implementation, not two.

    `source_uri` here is the BARE grid camera id (e.g. "cam04"), never a URL —
    the authenticated `rtsp://email:password@host:port/stream/<id>` URL is built
    in memory, fresh, only inside `open()`, from `settings.sentinel_grid_*` (env
    only). It is held only by the local `RTSPAdapter` instance for the lifetime
    of the underlying `cv2.VideoCapture` — never stored on `self` beyond that,
    never logged, never returned by any method here. The email is percent-encoded
    (`@` -> `%40`) since it appears inside the URL's userinfo component."""

    def __init__(self, source_uri: str):
        self.grid_camera_id = source_uri
        self._rtsp: "RTSPAdapter | None" = None

    def open(self) -> bool:
        if not settings.sentinel_grid_email or not settings.sentinel_grid_password:
            raise RuntimeError(
                "Sentinel Camera Grid credentials not configured — set "
                "SENTINEL_GRID_EMAIL/SENTINEL_GRID_PASSWORD in .env"
            )
        email = quote(settings.sentinel_grid_email, safe="")
        password = quote(settings.sentinel_grid_password, safe="")
        url = (
            f"rtsp://{email}:{password}@{settings.sentinel_grid_rtsp_host}:"
            f"{settings.sentinel_grid_rtsp_port}/stream/{self.grid_camera_id}"
        )
        self._rtsp = RTSPAdapter(url)
        return self._rtsp.open()

    def read(self) -> "tuple[bool, np.ndarray | None]":
        if self._rtsp is None:
            return False, None
        return self._rtsp.read()

    def pos_msec(self) -> "float | None":
        return self._rtsp.pos_msec() if self._rtsp else None

    def fps(self) -> float:
        return self._rtsp.fps() if self._rtsp else 0.0

    def resolution(self) -> str:
        return self._rtsp.resolution() if self._rtsp else ""

    def release(self) -> None:
        if self._rtsp is not None:
            self._rtsp.release()
            self._rtsp = None


class MockVMSAdapter(CameraAdapter):
    """Generic/dev VMS adapter — genuinely working, but synthetic. Proves a
    "Generic VMS" can be plugged into the adapter boundary end-to-end (registered,
    opened, read, fed through the real AI pipeline) without a real vendor backend.
    Not a stand-in for any specific vendor's protocol; `source_uri` is unused beyond
    optionally seeding the deterministic pattern."""

    def __init__(self, source_uri: str):
        self.source_uri = source_uri
        self._opened = False
        self._t0 = 0.0
        self._w, self._h = 640, 480

    def open(self) -> bool:
        self._opened = True
        self._t0 = time.monotonic()
        return True

    def read(self) -> "tuple[bool, np.ndarray | None]":
        if not self._opened:
            return False, None
        frame = np.full((self._h, self._w, 3), (40, 40, 40), dtype=np.uint8)
        t = time.monotonic() - self._t0
        x = int((self._w - 80) * (0.5 + 0.5 * np.sin(t)))
        cv2.rectangle(frame, (x, 200), (x + 80, 280), (0, 200, 0), -1)
        cv2.putText(frame, "MOCK VMS FEED", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        return True, frame

    def pos_msec(self) -> "float | None":
        if not self._opened:
            return None
        return (time.monotonic() - self._t0) * 1000

    def fps(self) -> float:
        return 15.0

    def resolution(self) -> str:
        return f"{self._w}x{self._h}"

    def release(self) -> None:
        self._opened = False


class ONVIFAdapter(CameraAdapter):
    """Interface stub only. No real ONVIF device (discovery, PTZ, vendor-specific
    auth) was available to implement or test against in this build, so this fails
    loudly rather than pretending to connect — never claim an integration that
    hasn't actually been exercised against something real."""

    def __init__(self, source_uri: str):
        self.source_uri = source_uri

    def open(self) -> bool:
        raise NotImplementedError(
            "ONVIF adapter is an interface stub — no ONVIF device was available to "
            "implement/test discovery or auth against in this build. Registered here "
            "to prove the adapter boundary is ready for it; not a working integration."
        )

    def read(self) -> "tuple[bool, np.ndarray | None]":
        return False, None

    def pos_msec(self) -> "float | None":
        return None

    def fps(self) -> float:
        return 0.0

    def resolution(self) -> str:
        return ""

    def release(self) -> None:
        pass


_ADAPTERS: dict[str, type[CameraAdapter]] = {
    "webcam": WebcamAdapter,
    "video_file": VideoFileAdapter,
    "rtsp": RTSPAdapter,
    "mock_vms": MockVMSAdapter,
    "onvif": ONVIFAdapter,
    "sentinel_grid": SentinelGridAdapter,
}


def get_adapter(source_type: str, source_uri: str) -> CameraAdapter:
    cls = _ADAPTERS.get(source_type)
    if cls is None:
        raise ValueError(f"Unknown source_type: {source_type}")
    return cls(source_uri)


def _safe_pos_msec(cap: "cv2.VideoCapture | None") -> "float | None":
    if cap is None:
        return None
    try:
        v = cap.get(cv2.CAP_PROP_POS_MSEC)
    except Exception:
        return None
    if v is None or v != v:  # NaN check
        return None
    return float(v)


def _safe_fps(cap: "cv2.VideoCapture | None") -> float:
    if cap is None:
        return 0.0
    return cap.get(cv2.CAP_PROP_FPS) or 0.0


def _safe_resolution(cap: "cv2.VideoCapture | None") -> str:
    if cap is None:
        return ""
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    return f"{w}x{h}" if w and h else ""
