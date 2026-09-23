"""Dedicated license-plate detection inside an already-detected vehicle crop.

Supersedes the single-box `plate_detect.locate_plate` (kept as a thin
compatibility shim). Three things this adds that the pipeline needs:

1. **Several candidate regions, not one.** A vehicle crop can legitimately
   contain more than one plate-shaped region — the plate, a dealer sticker, a
   bumper reflector strip. Returning only the top-scoring one means a wrong
   pick is unrecoverable; returning a ranked list lets the caller read the best
   and fall back.

2. **An explicit confidence AND its provenance.** A YOLO plate model's
   confidence is a model probability. The classical localizer's is a geometric
   plausibility score. Those are not the same quantity and must never be
   presented as if they were, so every box carries `source` ("model" or
   "heuristic") alongside `confidence`. Nothing downstream may compare or
   average the two.

3. **A quadrilateral, not just an axis-aligned box.** A plate seen off-axis is
   a rotated quad; `cv2.minAreaRect` recovers it, and `plate_preprocess` warps
   it front-on before OCR. The axis-aligned box is still reported, because that
   is what gets stored and drawn.

Detection strategy, in order:

1. **A dedicated plate-detection model** (`settings.plate_model_name`), if
   configured AND its weights exist on disk. Not bundled — this repo ships only
   `yolov8n.pt` (COCO), which has no license-plate class. Configured-but-missing
   weights are logged once and fall through to (2) rather than crashing a camera
   worker or silently pretending to run. No model is ever auto-downloaded.

2. **Classical CV localization** — no extra asset, works offline, and is a real
   (if weaker) plate finder: edge density plus morphological closing joins the
   glyph strokes of a plate into one blob, which is then filtered on the
   geometry a real plate has (aspect ratio, relative size, vertical position)
   and scored.

If neither finds a plausible region the caller falls back to whole-crop OCR —
exactly the pre-localization behavior. That fallback is deliberate: a
localization miss must degrade the read, never drop it.
"""
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..config import settings

logger = logging.getLogger("sentinel.plate_detector")

# Real Indian single-row plates are ~500x120mm (aspect ~4.2); two-row plates and
# oblique viewing angles push that down toward ~2.0. The ceiling is 8.0, not the
# plate's own ~4.2, because morphological closing joins the CHARACTER STROKES,
# not the plate border — the resulting blob is the text extent. A full 10-glyph
# plate ("GJ05AB1234") is roughly 440x65mm of text, i.e. aspect ~6.8, so a 6.5
# ceiling silently rejected correctly-detected full-length plates. Beyond 8.0 a
# region is a bumper edge or shadow line, not text.
MIN_ASPECT = 1.8
MAX_ASPECT = 8.0
IDEAL_ASPECT = 4.2

# A plate occupies a small but not vanishing fraction of a vehicle crop. Below
# the floor there are not enough pixels for OCR to read anyway; above the
# ceiling the "plate" is really the whole vehicle face.
MIN_AREA_FRACTION = 0.004
MAX_AREA_FRACTION = 0.30

# Absolute pixel floor — OCR on a 20px-wide region returns noise, and passing it
# on would burn OCR time to produce a read the quality gate then throws away.
MIN_PLATE_WIDTH_PX = 40
MIN_PLATE_HEIGHT_PX = 12

# How many candidate regions are ever returned. Bounded because each one the
# caller actually reads costs a full OCR pass — the most expensive operation in
# the camera loop.
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class PlateBox:
    """One candidate plate region within a vehicle crop.

    Coordinates are in the CROP's own space; the caller offsets them to
    full-frame. `confidence` is only ever comparable to another box from the
    same `source` — see the module docstring.
    """
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float
    source: str  # "model" (a trained detector's probability) | "heuristic" (geometric score)
    # Rotated corners, when the localizer recovered them. None from the model
    # path, which reports axis-aligned boxes only.
    quad: "list[list[float]] | None" = field(default=None)

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def as_list(self) -> list[float]:
        return [float(self.x1), float(self.y1), float(self.x2), float(self.y2)]


@lru_cache(maxsize=1)
def get_plate_model():
    """The optional dedicated plate detector.

    Cached — and cached as None on every failure path — so a missing or broken
    weights file costs one log line for the process lifetime rather than one per
    frame per camera.
    """
    name = (settings.plate_model_name or "").strip()
    if not name:
        return None
    # Resolved relative to the backend directory, matching how
    # settings.model_name ("yolov8n.pt") is resolved by ultralytics.
    candidate = Path(name)
    if not candidate.is_absolute():
        candidate = Path(settings.db_path).parent / name
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
    """Geometric plausibility of a region being a plate, in 0..1.

    Combines three real, independent signals: how close the aspect ratio is to a
    real plate, how far down the vehicle the region sits (plates are on the
    bumper or boot, not the roof), and size (bigger reads better, up to the area
    ceiling the caller already enforces).

    This is a PLAUSIBILITY score, not a detection probability. It says "this
    region is shaped and placed like a plate", which is a much weaker claim than
    a trained detector's "this is a plate". `PlateBox.source` records which one
    a caller is looking at so the two are never conflated.
    """
    if h <= 0 or crop_h <= 0:
        return 0.0
    aspect = w / h
    aspect_score = 1.0 - min(1.0, abs(aspect - IDEAL_ASPECT) / IDEAL_ASPECT)
    vertical = (y + h / 2) / crop_h
    # Peaks at 0.75 down the vehicle (typical plate height on a front/rear view)
    # and falls off in both directions rather than hard-rejecting — a
    # high-mounted plate on a truck should score lower, not be discarded.
    position_score = 1.0 - min(1.0, abs(vertical - 0.75) / 0.75)
    area_score = min(1.0, (w * h) / max(1.0, crop_w * crop_h * 0.06))
    return 0.5 * aspect_score + 0.3 * position_score + 0.2 * area_score


def _detect_classical(crop: np.ndarray) -> list[PlateBox]:
    """Edge-density + morphology plate finder. Returns candidates ranked by
    geometric plausibility, highest first."""
    crop_h, crop_w = crop.shape[:2]
    if crop_w < MIN_PLATE_WIDTH_PX or crop_h < MIN_PLATE_HEIGHT_PX:
        return []

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    # Bilateral filter smooths panel/paint noise while keeping the hard glyph
    # and plate-border edges Canny needs.
    gray = cv2.bilateralFilter(gray, 11, 17, 17)
    edges = cv2.Canny(gray, 30, 200)
    # A wide, short kernel joins the vertical strokes of adjacent characters into
    # one horizontal blob — that blob is the plate. Sized relative to the crop so
    # it works on both a distant small vehicle and a close large one.
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
        # The rotated rectangle recovers the plate's real orientation, which is
        # what makes perspective correction possible downstream. Kept alongside
        # the axis-aligned box rather than replacing it: the axis-aligned box is
        # what gets stored and drawn on evidence.
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
    """Dedicated plate model inference. Empty list when no model is configured,
    the weights are missing, or inference fails — every one of which falls
    through to the classical path rather than failing the read."""
    model = get_plate_model()
    if model is None:
        return []
    try:
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
    """Find candidate plate regions in a vehicle crop, best first.

    The model path wins outright when it returns anything: mixing a trained
    detector's boxes with geometric guesses would produce a candidate list whose
    confidences mean two different things. Returns `[]` when no plausible region
    exists, which the caller must treat as "read the whole crop", not "no plate".
    """
    if vehicle_crop is None or vehicle_crop.size == 0:
        return []
    return _detect_model(vehicle_crop) or _detect_classical(vehicle_crop)


def crop_plate(vehicle_crop: np.ndarray, box: PlateBox) -> "np.ndarray | None":
    """Cut a detected plate region out of the vehicle crop, with a small pad.

    The pad exists because the localizer tends to hug the glyphs and clip the
    first or last character's outer edge, which costs a real character in the
    read. Padding is proportional so it scales with the plate's size in frame.
    """
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
    """Translate a box's quad into the padded plate crop's coordinate space.

    `crop_plate` cuts the region out, so the quad's vehicle-crop coordinates no
    longer describe it. Returns None when the box has no quad, so the caller
    simply skips perspective correction.
    """
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
