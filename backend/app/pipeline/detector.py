"""Real YOLOv8 detection + built-in ByteTrack tracking (ultralytics)."""
from functools import lru_cache

from ultralytics import YOLO

from ..config import settings

# COCO class ids we care about for policing use-cases
PERSON_CLASSES = {0: "person"}
VEHICLE_CLASSES = {2: "car", 3: "motorbike", 5: "bus", 7: "truck"}
ALL_CLASSES = {**PERSON_CLASSES, **VEHICLE_CLASSES}


@lru_cache(maxsize=1)
def get_model() -> YOLO:
    return YOLO(settings.model_name)


def detect_and_track(frame, want_person: bool = True, want_vehicle: bool = True):
    """Runs one frame through YOLO + ByteTrack. Returns a list of dicts:
    {cls, confidence, bbox: [x1,y1,x2,y2], track_id}
    """
    model = get_model()
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
