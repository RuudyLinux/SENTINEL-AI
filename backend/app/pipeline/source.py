"""Camera source — backward-compat wrapper over the adapter interface.

`CameraSource` is kept as the stable public class (same name, same methods) that
`worker.py`, `routers/cameras.py`, and the test suite already import — it now just
delegates to `pipeline/adapters.py`'s `CameraAdapter` interface (doc §48
"vendor-agnostic adapter"; Model 3 — VMS Federation/Middleware). See `adapters.py`
for the actual per-source-type logic (webcam/video_file/rtsp/mock_vms/onvif) and for
how a future vendor-specific VMS adapter plugs in without touching this file, the
detector, ANPR, correlation, or rules-engine code.
"""
import cv2
import numpy as np

from ..config import settings
from .adapters import CameraAdapter, get_adapter

# `cv2`/`settings` are never referenced below by name — they're kept as
# module attributes here (not truly "unused") because test_source_rtsp.py
# monkeypatches them via `source_mod.cv2.VideoCapture` / `source_mod.settings.*`.
# Since `cv2` is a single shared module object, patching it through this
# name also affects adapters.py's own `import cv2` (same object in
# sys.modules) — that's the actual mechanism the test relies on. A bare
# `# noqa: F401` doesn't silence this for plain `pyflakes` (only flake8
# honors noqa), so this explicit reference is what actually keeps the
# import from being flagged as dead code without deleting something a real
# test depends on.
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
        """Raw source-relative position, or None if unavailable. Reliability
        varies by source_type — see pipeline/timing.py, which is the module that
        actually decides whether to trust this value."""
        return self._adapter.pos_msec()

    def read(self) -> tuple[bool, "np.ndarray | None"]:
        return self._adapter.read()

    def fps(self) -> float:
        return self._adapter.fps()

    def resolution(self) -> str:
        return self._adapter.resolution()

    def release(self) -> None:
        self._adapter.release()
