"""Real YOLOv8 detection + built-in ByteTrack tracking (ultralytics).

Tracker isolation: ultralytics keeps ByteTrack state (next track id,
active tracklets) on the `YOLO`/predictor instance itself when calling
`.track(..., persist=True)`. A single model instance shared across cameras
would let concurrent camera workers race on that shared state and corrupt
each other's track IDs. So we keep one YOLO instance PER CAMERA — each
camera's worker loop calls inference sequentially, so its own instance is
never touched concurrently.
"""
from typing import Any

import numpy as np
from ultralytics import YOLO

from ..config import settings

# COCO class ids we care about for policing use-cases
PERSON_CLASSES = {0: "person"}
VEHICLE_CLASSES = {2: "car", 3: "motorbike", 5: "bus", 7: "truck"}
ALL_CLASSES = {**PERSON_CLASSES, **VEHICLE_CLASSES}

_MODELS_BY_CAMERA: dict[str, YOLO] = {}


def get_model(camera_id: str) -> YOLO:
    model = _MODELS_BY_CAMERA.get(camera_id)
    if model is None:
        model = YOLO(settings.model_name)
        _MODELS_BY_CAMERA[camera_id] = model
    return model


def release_model(camera_id: str) -> None:
    """Drop a camera's model/tracker instance (call on camera stop/delete)."""
    _MODELS_BY_CAMERA.pop(camera_id, None)


def detect_and_track(
    frame: np.ndarray, camera_id: str, want_person: bool = True, want_vehicle: bool = True
) -> list[dict[str, Any]]:
    """Runs one frame through YOLO + ByteTrack for a single camera's own
    model instance. Returns a list of dicts:
    {cls, confidence, bbox: [x1,y1,x2,y2], track_id}
    """
    model = get_model(camera_id)
    class_ids = []
    if want_person:
        class_ids += list(PERSON_CLASSES.keys())
    if want_vehicle:
        class_ids += list(VEHICLE_CLASSES.keys())
    if not class_ids:
        return []

    results = model.track(
        frame,
        classes=class_ids,
        conf=settings.confidence_threshold,
        persist=True,
        tracker="bytetrack.yaml",
        verbose=False,
    )
    out = []
    if not results:
        return out
    r = results[0]
    if r.boxes is None:
        return out
    for box in r.boxes:
        cls_id = int(box.cls[0])
        conf = float(box.conf[0])
        x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
        track_id = int(box.id[0]) if box.id is not None else None
        out.append({
            "cls": ALL_CLASSES.get(cls_id, str(cls_id)),
            "confidence": conf,
            "bbox": [x1, y1, x2, y2],
            "track_id": track_id,
        })
    return out
