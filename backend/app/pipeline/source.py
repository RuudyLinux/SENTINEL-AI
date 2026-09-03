"""Camera source adapter.

Supports: webcam (device index) and video_file (looped local file) — both real
frames, real OpenCV capture. `rtsp` is intentionally NOT implemented: no real
CCTV/VMS/RTSP source is available in this environment. The interface is shaped
so an RTSP/ONVIF adapter can be dropped in later without touching the detector,
ANPR, correlation or rules-engine code (see doc §48 "vendor-agnostic adapter").
"""
import cv2


class CameraSource:
    def __init__(self, source_type: str, source_uri: str):
        self.source_type = source_type
        self.source_uri = source_uri
        self.cap: cv2.VideoCapture | None = None

    def open(self) -> bool:
        if self.source_type == "webcam":
            self.cap = cv2.VideoCapture(int(self.source_uri))
        elif self.source_type == "video_file":
            self.cap = cv2.VideoCapture(self.source_uri)
        elif self.source_type == "rtsp":
            raise NotImplementedError(
                "RTSP/ONVIF/VMS adapters are not implemented in this build — "
                "no real CCTV source is available in this environment. "
                "Use 'webcam' or 'video_file' instead."
            )
        else:
            raise ValueError(f"Unknown source_type: {self.source_type}")
        return self.cap.isOpened()

    def read(self):
        if self.cap is None:
            return False, None
        ok, frame = self.cap.read()
        if not ok and self.source_type == "video_file":
            # loop the file so a short demo clip behaves like a continuous feed
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return ok, frame

    def fps(self) -> float:
        if self.cap is None:
            return 0.0
        return self.cap.get(cv2.CAP_PROP_FPS) or 0.0

    def resolution(self) -> str:
        if self.cap is None:
            return ""
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return f"{w}x{h}" if w and h else ""

    def release(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
