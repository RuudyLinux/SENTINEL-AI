"""CameraSource: the stable interface used by worker.py, routers and tests,
delegating to pipeline/adapters.py. New vendor adapters go there.
"""
import cv2
import numpy as np

from ..config import settings
from .adapters import CameraAdapter, get_adapter

# cv2 and settings are re-exported because test_source_rtsp.py patches them
# through this module; the explicit reference keeps pyflakes quiet.
_ = (cv2, settings)


class CameraSource:
    def __init__(self, source_type: str, source_uri: str):
        self.source_type = source_type
        self.source_uri = source_uri
        self._adapter: CameraAdapter = get_adapter(source_type, source_uri)

    @property
    def transport_forced_tcp(self) -> bool:
        """Only meaningful for the RTSP adapter; False for every other source_type."""
        return getattr(self._adapter, "transport_forced_tcp", False)

    def open(self) -> bool:
        return self._adapter.open()

    def pos_msec(self) -> float | None:
        """Raw source-relative position or None. How far to trust it depends
        on source_type, pipeline/timing.py decides."""
        return self._adapter.pos_msec()

    def read(self) -> tuple[bool, "np.ndarray | None"]:
        return self._adapter.read()

    @property
    def can_grab(self) -> bool:
        """Live stream adapters split decode (grab) from BGR conversion
        (retrieve); see adapters._GrabMixin."""
        return hasattr(self._adapter, "grab")

    def grab(self) -> bool:
        return self._adapter.grab()  # type: ignore[attr-defined]

    def retrieve(self) -> tuple[bool, "np.ndarray | None"]:
        return self._adapter.retrieve()  # type: ignore[attr-defined]

    def fps(self) -> float:
        return self._adapter.fps()

    def resolution(self) -> str:
        return self._adapter.resolution()

    def release(self) -> None:
        self._adapter.release()
