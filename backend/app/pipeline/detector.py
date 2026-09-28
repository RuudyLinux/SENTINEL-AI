"""YOLO detection with a ByteTrack tracker per camera.

One YOLO model is shared by every camera; only tracker state is per camera, so
GPU memory doesn't limit how many cameras can run AI. Inference is serialised on
a lock because the ultralytics predictor isn't thread-safe.
"""
import threading
from typing import Any

import numpy as np
from ultralytics import YOLO
from ultralytics.trackers.byte_tracker import BYTETracker
from ultralytics.utils import YAML, IterableSimpleNamespace

from ..config import settings

# COCO class ids we care about for policing use-cases
PERSON_CLASSES = {0: "person"}
VEHICLE_CLASSES = {2: "car", 3: "motorbike", 5: "bus", 7: "truck"}
ALL_CLASSES = {**PERSON_CLASSES, **VEHICLE_CLASSES}

_MODEL: "YOLO | None" = None
_MODEL_LOCK = threading.Lock()   # guards loading and every predict call
_TRACKERS: dict[str, BYTETracker] = {}
_TRACKER_CFG: "IterableSimpleNamespace | None" = None

# ByteTrack matches boxes by position regardless of class, so an id can jump
# from a car to a nearby motorbike. An id whose object changes kind gets a fresh
# id, except person <-> motorbike (a rider and bike are one object).
_KIND = {"person": "rider", "motorbike": "rider", "car": "4w", "bus": "4w", "truck": "4w"}
_SPLIT_ID_BASE = 1_000_000  # above anything the tracker hands out in a session
# camera_id -> tracker id -> (kind, published id)
_IDS_BY_CAMERA: dict[str, dict[int, tuple[str, int]]] = {}
_NEXT_SPLIT_ID: dict[str, int] = {}


def get_model() -> YOLO:
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = YOLO(settings.model_name)
    return _MODEL


def warmup() -> None:
    """Load the model and run one frame so the first camera doesn't pay for model
    and CUDA initialisation."""
    model = get_model()
    blank = np.zeros((settings.detector_imgsz, settings.detector_imgsz, 3), dtype=np.uint8)
    with _MODEL_LOCK:
        model.predict(blank, imgsz=settings.detector_imgsz, verbose=False)


def _tracker(camera_id: str) -> BYTETracker:
    global _TRACKER_CFG
    tracker = _TRACKERS.get(camera_id)
    if tracker is None:
        if _TRACKER_CFG is None:
            _TRACKER_CFG = IterableSimpleNamespace(**YAML.load(settings.tracker_config))
        tracker = BYTETracker(args=_TRACKER_CFG)
        _TRACKERS[camera_id] = tracker
    return tracker


def release_model(camera_id: str) -> None:
    """Drop a camera's tracker state (camera stop, delete, AI off). The shared
    model stays loaded."""
    _TRACKERS.pop(camera_id, None)
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
    """One frame through the shared YOLO and this camera's ByteTrack. Returns
    [{cls, confidence, bbox: [x1,y1,x2,y2], track_id}, ...]
    """
    class_ids = []
    if want_person:
        class_ids += list(PERSON_CLASSES.keys())
    if want_vehicle:
        class_ids += list(VEHICLE_CLASSES.keys())
    if not class_ids:
        return []

    model = get_model()
    with _MODEL_LOCK:
        results = model.predict(
            frame,
            classes=class_ids,
            conf=min(settings.tracker_feed_confidence, settings.confidence_threshold),
            iou=settings.detector_iou,
            imgsz=settings.detector_imgsz,
            verbose=False,
        )
    if not results or results[0].boxes is None:
        return []
    boxes = results[0].boxes.cpu().numpy()
    # every frame goes to the tracker, empty ones too, so lost tracks age out.
    # rows: x1, y1, x2, y2, track_id, score, cls, idx
    tracks = _tracker(camera_id).update(boxes, frame)

    out = []
    if len(tracks):
        rows = [(t[:4], int(t[4]), float(t[5]), int(t[6])) for t in tracks]
    else:
        # nothing tracked yet (first frames): publish the raw boxes untracked,
        # like ultralytics' own track mode does
        rows = [(b.xyxy[0], None, float(b.conf[0]), int(b.cls[0])) for b in boxes]
    for xyxy, raw_id, conf, cls_id in rows:
        if conf < settings.confidence_threshold:
            continue  # fed to the tracker only; not a published detection
        cls = ALL_CLASSES.get(cls_id, str(cls_id))
        out.append({
            "cls": cls,
            "confidence": conf,
            "bbox": [float(v) for v in xyxy],
            "track_id": _published_track_id(camera_id, raw_id, cls) if raw_id is not None else None,
        })
    return out
