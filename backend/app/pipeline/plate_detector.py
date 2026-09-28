"""License-plate detection inside a vehicle crop.

Replaces plate_detect.locate_plate (kept as a shim). What it adds:

- Several ranked candidates, not one. A crop can hold a plate, a dealer
  sticker and a reflector strip; with only the top box a wrong pick can't be
  recovered.
- Confidence with its source. A YOLO model gives a probability, the
  classical localizer a geometric plausibility score. Different things, so
  every box has `source` ("model" | "heuristic") and the two are never
  compared or averaged.
- A quad as well as the axis-aligned box, so plate_preprocess can warp an
  off-axis plate front-on. The box is what's stored and drawn.

Order: the dedicated model (settings.plate_model_name) if its weights exist.
Not bundled, never auto-downloaded; missing weights log once and fall
through. Then classical CV: edge density plus morphological closing merges
glyph strokes into one blob, filtered on plate geometry (aspect, size,
position) and scored.

Nothing found = the caller reads the whole crop. A localization miss should
make the read worse, not drop it.
"""
import logging
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..config import BASE_DIR, settings

logger = logging.getLogger("sentinel.plate_detector")

# Indian single-row plates are ~500x120mm (aspect ~4.2), two-row plates and
# oblique angles push it toward ~2.0. Ceiling is 8.0, not 4.2, because the
# closing joins the character strokes, not the border, so the blob is the
# text: a full GJ05AB1234 is ~440x65mm of text, aspect ~6.8, and 6.5 was
# rejecting good full-length plates. Past 8.0 it's a bumper edge or shadow.
MIN_ASPECT = 1.8
MAX_ASPECT = 8.0
IDEAL_ASPECT = 4.2

# below the floor OCR can't read it anyway, above the ceiling it's the whole
# front of the vehicle
MIN_AREA_FRACTION = 0.004
MAX_AREA_FRACTION = 0.30

# OCR on a 20px-wide region is noise the gate throws away anyway
MIN_PLATE_WIDTH_PX = 40
MIN_PLATE_HEIGHT_PX = 12

# each candidate the caller reads is a full OCR pass
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class PlateBox:
    """Candidate plate region in crop coordinates (caller offsets to full
    frame). confidence only compares with boxes of the same source."""
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float
    source: str  # "model" (a trained detector's probability) | "heuristic" (geometric score)
    # rotated corners if the localizer found them; the model path gives None
    quad: "list[list[float]] | None" = field(default=None)

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def as_list(self) -> list[float]:
        return [float(self.x1), float(self.y1), float(self.x2), float(self.y2)]


# one plate model shared by every camera; the ultralytics predictor isn't
# thread-safe, so predicts take turns
_PLATE_MODEL_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def get_plate_model():
    """Optional dedicated plate detector. Cached, and cached as None on any
    failure, so a bad weights file costs one log line, not one per frame."""
    name = (settings.plate_model_name or "").strip()
    if not name:
        return None
    # relative to the backend dir, like model_name
    candidate = Path(name)
    if not candidate.is_absolute():
        # BASE_DIR not the DB folder: DB_PATH can be anywhere (docker volume,
        # test dir), the weights ship with the backend
        candidate = BASE_DIR / name
    if not candidate.exists():
        logger.warning(
            "PLATE_MODEL_NAME=%s is configured but the weights file does not exist (%s) — "
            "falling back to classical plate localization. Nothing is broken; this is the "
            "documented no-asset path.", name, candidate,
        )
        return None
    try:
        from ultralytics import YOLO
        return YOLO(str(candidate))
    except Exception:
        logger.exception("plate model %s failed to load — falling back to classical localization", candidate)
        return None


def heuristic_score(x: int, y: int, w: int, h: int, crop_w: int, crop_h: int) -> float:
    """How plate-like a region looks, 0..1: aspect vs a real plate, how far
    down the vehicle it sits, and size.

    Plausibility, not a detection probability ("shaped and placed like a
    plate" is a much weaker claim than a trained detector's). PlateBox.source
    keeps the two apart.
    """
    if h <= 0 or crop_h <= 0:
        return 0.0
    aspect = w / h
    aspect_score = 1.0 - min(1.0, abs(aspect - IDEAL_ASPECT) / IDEAL_ASPECT)
    vertical = (y + h / 2) / crop_h
    # peaks 0.75 of the way down (usual plate height) and tapers both ways;
    # a high-mounted truck plate scores lower instead of being rejected
    position_score = 1.0 - min(1.0, abs(vertical - 0.75) / 0.75)
    area_score = min(1.0, (w * h) / max(1.0, crop_w * crop_h * 0.06))
    return 0.5 * aspect_score + 0.3 * position_score + 0.2 * area_score


def _detect_classical(crop: np.ndarray) -> list[PlateBox]:
    """Edge density + morphology plate finder, best first."""
    crop_h, crop_w = crop.shape[:2]
    if crop_w < MIN_PLATE_WIDTH_PX or crop_h < MIN_PLATE_HEIGHT_PX:
        return []

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    # bilateral filter smooths paint noise but keeps glyph/border edges for Canny
    gray = cv2.bilateralFilter(gray, 11, 17, 17)
    edges = cv2.Canny(gray, 30, 200)
    # wide short kernel joins adjacent characters' vertical strokes into one
    # blob, the plate. sized off the crop so near and far vehicles both work
    kernel_width = max(5, int(crop_w * 0.06) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    crop_area = float(crop_w * crop_h)
    candidates: list[PlateBox] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if h <= 0 or w < MIN_PLATE_WIDTH_PX or h < MIN_PLATE_HEIGHT_PX:
            continue
        if not (MIN_ASPECT <= w / h <= MAX_ASPECT):
            continue
        if not (MIN_AREA_FRACTION <= (w * h) / crop_area <= MAX_AREA_FRACTION):
            continue
        # rotated rect gives the real orientation for perspective correction
        # later. the axis-aligned box stays, that's what's stored and drawn
        quad = None
        try:
            rotated = cv2.minAreaRect(contour)
            quad = [[float(px), float(py)] for px, py in cv2.boxPoints(rotated)]
        except cv2.error:
            quad = None
        candidates.append(PlateBox(
            x1=x, y1=y, x2=x + w, y2=y + h,
            confidence=heuristic_score(x, y, w, h, crop_w, crop_h),
            source="heuristic", quad=quad,
        ))
    candidates.sort(key=lambda box: box.confidence, reverse=True)
    return candidates[:MAX_CANDIDATES]


def _detect_model(crop: np.ndarray) -> list[PlateBox]:
    """Plate model inference. [] when there's no model, no weights, or it
    fails; all of those fall through to the classical path."""
    model = get_plate_model()
    if model is None:
        return []
    try:
        with _PLATE_MODEL_LOCK:
            results = model.predict(crop, conf=settings.plate_detect_confidence, verbose=False)
    except Exception:
        logger.exception("plate model inference failed — falling back to classical localization")
        return []
    if not results or results[0].boxes is None or len(results[0].boxes) == 0:
        return []
    candidates: list[PlateBox] = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
        if (x2 - x1) < MIN_PLATE_WIDTH_PX or (y2 - y1) < MIN_PLATE_HEIGHT_PX:
            continue
        candidates.append(PlateBox(
            x1=x1, y1=y1, x2=x2, y2=y2,
            confidence=float(box.conf[0]), source="model", quad=None,
        ))
    candidates.sort(key=lambda box: box.confidence, reverse=True)
    return candidates[:MAX_CANDIDATES]


def detect_plates(vehicle_crop: np.ndarray) -> list[PlateBox]:
    """Candidate plate regions in a vehicle crop, best first.

    If the model returns anything it wins outright; mixing its boxes with
    geometric guesses would mix two meanings of confidence. [] means "read
    the whole crop", not "no plate".
    """
    if vehicle_crop is None or vehicle_crop.size == 0:
        return []
    return _detect_model(vehicle_crop) or _detect_classical(vehicle_crop)


def crop_plate(vehicle_crop: np.ndarray, box: PlateBox) -> "np.ndarray | None":
    """Cut the plate out of the vehicle crop with a small proportional pad.
    The localizer hugs the glyphs and tends to clip the first or last
    character, which costs a character in the read."""
    if vehicle_crop is None or vehicle_crop.size == 0:
        return None
    crop_h, crop_w = vehicle_crop.shape[:2]
    pad_x = max(2, int(box.width * 0.04))
    pad_y = max(2, int(box.height * 0.12))
    x1 = max(0, box.x1 - pad_x)
    y1 = max(0, box.y1 - pad_y)
    x2 = min(crop_w, box.x2 + pad_x)
    y2 = min(crop_h, box.y2 + pad_y)
    if x2 <= x1 or y2 <= y1:
        return None
    plate_crop = vehicle_crop[y1:y2, x1:x2]
    return plate_crop if plate_crop.size > 0 else None


def quad_in_crop(box: PlateBox, vehicle_crop: np.ndarray) -> "list[list[float]] | None":
    """Box's quad in the padded plate crop's coordinates (crop_plate moved
    the origin). None without a quad, then perspective correction is skipped."""
    if box.quad is None or vehicle_crop is None or vehicle_crop.size == 0:
        return None
    crop_h, crop_w = vehicle_crop.shape[:2]
    pad_x = max(2, int(box.width * 0.04))
    pad_y = max(2, int(box.height * 0.12))
    origin_x = max(0, box.x1 - pad_x)
    origin_y = max(0, box.y1 - pad_y)
    if origin_x >= crop_w or origin_y >= crop_h:
        return None
    return [[point[0] - origin_x, point[1] - origin_y] for point in box.quad]
