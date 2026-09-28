"""Camera/VMS adapters.

Every source is wrapped in a CameraAdapter, so detection, ANPR and rules code
never deals with a vendor.

- Webcam, VideoFile, RTSP: the real camera sources (wrapped by source.py).
- SentinelGridAdapter: grid cameras over RTSP.
- MockVMSAdapter: synthetic frames, demonstrating a generic VMS end to end.
- ONVIFAdapter: interface stub; open() raises NotImplementedError.

A vendor VMS would be another subclass registered in _ADAPTERS.
"""
import os
import time
from abc import ABC, abstractmethod
from urllib.parse import quote

import cv2
import numpy as np

from ..config import settings

# so a dead RTSP host doesn't hang the thread; webcam/file backends ignore it
_OPEN_TIMEOUT_MS = 5000
_READ_TIMEOUT_MS = 5000


class CameraAdapter(ABC):
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


class _GrabMixin:
    """grab()/retrieve() split for live streams: grab() decodes, retrieve()
    converts to BGR (about half the cost), so only frames that will be used are
    converted."""

    def grab(self) -> bool:
        return self.cap is not None and self.cap.grab()

    def retrieve(self) -> "tuple[bool, np.ndarray | None]":
        if self.cap is None:
            return False, None
        return self.cap.retrieve()


class RTSPAdapter(_GrabMixin, CameraAdapter):
    """transport: "tcp" or "udp"; None lets rtsp_force_tcp decide.

    OpenCV only exposes this through the process-wide
    OPENCV_FFMPEG_CAPTURE_OPTIONS variable, read on each open(), so it is set
    just before opening. Deliberately unlocked: an open can block for its whole
    timeout, and a lock would stall every other camera behind it.
    """

    def __init__(self, source_uri: str, transport: "str | None" = None):
        self.source_uri = source_uri
        self.transport = transport
        self.cap: cv2.VideoCapture | None = None
        self.transport_forced_tcp = False

    def open(self) -> bool:
        transport = self.transport or ("tcp" if settings.rtsp_force_tcp else None)
        options = []
        if transport:
            options.append(f"rtsp_transport;{transport}")
        if transport == "udp":
            # bigger socket buffer: fewer lost packets (grey smears in HEVC)
            # when 30 streams arrive at once
            options.append(f"buffer_size;{settings.rtsp_udp_buffer_bytes}")
        if options:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(options)
        self.transport_forced_tcp = transport == "tcp"
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
    """Sentinel Camera Grid over RTSP.

    source_uri is the bare grid id (e.g. "cam04"). The credentialed RTSP URL is
    built from settings inside open() and is never stored, logged or returned.
    The email is percent-encoded for the userinfo part."""

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
        self._rtsp = RTSPAdapter(url, transport=settings.sentinel_grid_rtsp_transport or None)
        return self._rtsp.open()

    def read(self) -> "tuple[bool, np.ndarray | None]":
        if self._rtsp is None:
            return False, None
        return self._rtsp.read()

    def grab(self) -> bool:
        return self._rtsp is not None and self._rtsp.grab()

    def retrieve(self) -> "tuple[bool, np.ndarray | None]":
        if self._rtsp is None:
            return False, None
        return self._rtsp.retrieve()

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
    """Synthetic VMS feed (a moving box) that exercises the adapter boundary
    without a vendor backend. source_uri is unused."""

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
    """Stub. Nothing to test ONVIF against, so open() fails loudly."""

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
