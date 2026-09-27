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

# ByteTrack associates boxes by position only, whatever their class. Replayed on
# real night footage (docs/AI_ACCURACY.md) a lost car's ID was picked up seconds
# later by a motorbike rider, and a rider's ID by a car. One ID spanning two
# objects mixes their plate votes and loitering time, so an ID whose object
# changes kind is given a fresh ID from here on. person <-> motorbike is the
# exception: a rider and the bike under them trade the ID constantly, and that
# is one moving object, not two.
_KIND = {"person": "rider", "motorbike": "rider", "car": "4w", "bus": "4w", "truck": "4w"}
_SPLIT_ID_BASE = 1_000_000  # above anything ultralytics hands out in a session
# camera_id -> tracker id -> (kind, published id)
_IDS_BY_CAMERA: dict[str, dict[int, tuple[str, int]]] = {}
_NEXT_SPLIT_ID: dict[str, int] = {}


def get_model(camera_id: str) -> YOLO:
    model = _MODELS_BY_CAMERA.get(camera_id)
    if model is None:
        model = YOLO(settings.model_name)
        _MODELS_BY_CAMERA[camera_id] = model
    return model


def release_model(camera_id: str) -> None:
    """Drop a camera's model/tracker instance (call on camera stop/delete)."""
    _MODELS_BY_CAMERA.pop(camera_id, None)
    _IDS_BY_CAMERA.pop(camera_id, None)
    _NEXT_SPLIT_ID.pop(camera_id, None)


def _published_track_id(camera_id: str, track_id: int, cls: str) -> int:
    """The tracker's ID, unless its object has changed kind since last seen."""
    ids = _IDS_BY_CAMERA.setdefault(camera_id, {})
    kind = _KIND.get(cls, cls)
    known = ids.get(track_id)
    if known is None:
        ids[track_id] = (kind, track_id)
        return track_id
    if known[0] == kind:
        return known[1]
    split_id = _NEXT_SPLIT_ID.get(camera_id, _SPLIT_ID_BASE)
    _NEXT_SPLIT_ID[camera_id] = split_id + 1
    ids[track_id] = (kind, split_id)
    return split_id


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
        conf=min(settings.tracker_feed_confidence, settings.confidence_threshold),
        iou=settings.detector_iou,
        imgsz=settings.detector_imgsz,
        persist=True,
        tracker=settings.tracker_config,
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
        if conf < settings.confidence_threshold:
            continue  # fed to the tracker only; not a published detection
        x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
        cls = ALL_CLASSES.get(cls_id, str(cls_id))
        track_id = _published_track_id(camera_id, int(box.id[0]), cls) if box.id is not None else None
        out.append({
            "cls": cls,
            "confidence": conf,
            "bbox": [x1, y1, x2, y2],
            "track_id": track_id,
        })
    return out
