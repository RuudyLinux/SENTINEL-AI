"""Number-plate localization inside an already-detected vehicle crop.

The missing stage in the original pipeline. Before this module, worker.py fed
the WHOLE vehicle bounding box straight to EasyOCR (`anpr.read_plate(crop)`),
so OCR was asked to read a car — bumper stickers, dealer badges, windscreen
text and the plate all at once — and the "best" text it returned was whichever
fragment happened to win. Narrowing OCR to an actual plate-shaped region is the
single largest accuracy lever available here, larger than swapping the OCR
engine itself.

Two strategies, in order:

1. **A dedicated plate-detection model** (`settings.plate_model_name`), if one
   is configured AND its weights file actually exists on disk. Not bundled —
   this repo ships only `yolov8n.pt` (COCO), which has no license-plate class.
   Configured-but-missing weights are reported once and then fall through to
   (2) rather than crashing a camera worker or silently pretending to run.

2. **Classical CV localization** — no extra asset, works offline, and is a real
   (if weaker) plate finder: edge density + morphological closing joins the
   glyph strokes of a plate into one blob, then candidates are filtered on the
   geometry a real plate actually has (aspect ratio, relative size, vertical
   position on the vehicle) and scored.

If neither finds a plausible region, this returns None and the caller falls
back to whole-crop OCR — i.e. exactly the pre-V2 behavior. That fallback is
deliberate: a localization miss must degrade to the old path, never drop the
read entirely.
"""
import logging
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..config import settings

logger = logging.getLogger("sentinel.plate_detect")

# Real Indian single-row plates are ~500x120mm (aspect ~4.2); two-row plates and
# oblique viewing angles push that down toward ~2.0. The ceiling is 8.0, not the
# plate's own ~4.2, because morphological closing joins the CHARACTER STROKES,
# not the plate border — the resulting blob is the text extent. A full 10-glyph
# plate ("GJ05AB1234") is roughly 440x65mm of text, i.e. aspect ~6.8, so a 6.5
# ceiling silently rejected correctly-detected full-length plates. Beyond 8.0 a
# region is a bumper edge or shadow line, not text.
_MIN_ASPECT = 1.8
_MAX_ASPECT = 8.0
_IDEAL_ASPECT = 4.2

# A plate occupies a small but not vanishing fraction of a vehicle crop. Below
# the floor there are not enough pixels for OCR to read anyway; above the
# ceiling the "plate" is really the whole vehicle face.
_MIN_AREA_FRACTION = 0.004
_MAX_AREA_FRACTION = 0.30

# Absolute pixel floor — OCR on a 20px-wide region returns noise, and passing it
# on would burn OCR time to produce a read the quality gate then throws away.
_MIN_PLATE_WIDTH_PX = 40
_MIN_PLATE_HEIGHT_PX = 12


@lru_cache(maxsize=1)
def _get_plate_model():
    """The optional dedicated plate detector. Cached (and cached as None on
    every failure path) so a missing/broken weights file costs one log line for
    the process lifetime, not one per frame per camera."""
    name = (settings.plate_model_name or "").strip()
    if not name:
        return None
    # Resolve relative to the backend dir, matching how settings.model_name
    # ("yolov8n.pt") is resolved by ultralytics from the working directory.
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


def _score_candidate(x: int, y: int, w: int, h: int, crop_w: int, crop_h: int) -> float:
    """Higher is more plate-like. Combines three real, independent signals:
    how close the aspect ratio is to a real plate, how far down the vehicle the
    region sits (plates are on the bumper/boot, not the roof), and size (bigger
    reads better, up to the area ceiling already enforced by the caller)."""
    aspect = w / h
    aspect_score = 1.0 - min(1.0, abs(aspect - _IDEAL_ASPECT) / _IDEAL_ASPECT)
    # Vertical center of the region as a 0-1 fraction of the vehicle crop.
    vertical = (y + h / 2) / crop_h if crop_h else 0.5
    # Peaks at 0.75 down the vehicle (typical plate height on a rear/front view),
    # falls off in both directions rather than hard-rejecting — a high-mounted
    # plate on a truck should score lower, not be discarded.
    position_score = 1.0 - min(1.0, abs(vertical - 0.75) / 0.75)
    area_score = min(1.0, (w * h) / max(1.0, crop_w * crop_h * 0.06))
    return 0.5 * aspect_score + 0.3 * position_score + 0.2 * area_score


def _locate_classical(crop: np.ndarray) -> "tuple[int, int, int, int] | None":
    """Edge-density + morphology plate finder. Returns (x, y, w, h) within
    `crop`, or None when nothing plausible is present."""
    crop_h, crop_w = crop.shape[:2]
    if crop_w < _MIN_PLATE_WIDTH_PX or crop_h < _MIN_PLATE_HEIGHT_PX:
        return None

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    # Bilateral filter smooths panel/paint noise while keeping the hard glyph
    # and plate-border edges Canny needs.
    gray = cv2.bilateralFilter(gray, 11, 17, 17)
    edges = cv2.Canny(gray, 30, 200)
    # A wide, short kernel joins the vertical strokes of adjacent characters
    # into one horizontal blob — that blob is the plate. Sized relative to the
    # crop so it works on both a distant small vehicle and a close large one.
    kernel_w = max(5, int(crop_w * 0.06) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    crop_area = float(crop_w * crop_h)
    best = None
    best_score = 0.0
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if h <= 0 or w < _MIN_PLATE_WIDTH_PX or h < _MIN_PLATE_HEIGHT_PX:
            continue
        aspect = w / h
        if not (_MIN_ASPECT <= aspect <= _MAX_ASPECT):
            continue
        area_fraction = (w * h) / crop_area
        if not (_MIN_AREA_FRACTION <= area_fraction <= _MAX_AREA_FRACTION):
            continue
        score = _score_candidate(x, y, w, h, crop_w, crop_h)
        if score > best_score:
            best_score, best = score, (x, y, w, h)
    return best


def _locate_model(crop: np.ndarray) -> "tuple[int, int, int, int] | None":
    model = _get_plate_model()
    if model is None:
        return None
    try:
        results = model.predict(crop, conf=settings.plate_detect_confidence, verbose=False)
    except Exception:
        logger.exception("plate model inference failed — falling back to classical localization")
        return None
    if not results or results[0].boxes is None or len(results[0].boxes) == 0:
        return None
    # Highest-confidence box wins; ultralytics already sorts by confidence, but
    # this does not depend on that ordering.
    boxes = results[0].boxes
    best_idx = int(np.argmax([float(b.conf[0]) for b in boxes]))
    x1, y1, x2, y2 = [int(v) for v in boxes[best_idx].xyxy[0]]
    w, h = x2 - x1, y2 - y1
    if w < _MIN_PLATE_WIDTH_PX or h < _MIN_PLATE_HEIGHT_PX:
        return None
    return x1, y1, w, h


def _preprocess_for_ocr(plate_crop: np.ndarray) -> np.ndarray:
    """Upscale + contrast-normalize the plate region before OCR.

    OCR accuracy on real CCTV plate crops is dominated by effective glyph
    height. A plate that is 18px tall in the source frame is at the edge of
    what EasyOCR resolves; upscaling it to a comfortable height before
    recognition is a real, measurable improvement that costs a few
    milliseconds — and unlike changing the OCR engine, it cannot regress the
    reads that already work.
    """
    h, w = plate_crop.shape[:2]
    if h <= 0 or w <= 0:
        return plate_crop
    target_h = settings.plate_ocr_target_height
    if h < target_h:
        scale = target_h / h
        plate_crop = cv2.resize(
            plate_crop, (int(w * scale), target_h), interpolation=cv2.INTER_CUBIC
        )
    gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY) if plate_crop.ndim == 3 else plate_crop
    # CLAHE rather than a global equalizeHist: plates are frequently half in
    # shadow (overhang, headlight glare), and a global histogram stretch blows
    # out the lit half to read the dark one.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def locate_plate(vehicle_crop: np.ndarray) -> "tuple[np.ndarray, list[float]] | None":
    """Find the plate inside a vehicle crop.

    Returns `(ocr_ready_plate_image, [x1, y1, x2, y2])` where the bbox is in
    the vehicle crop's own coordinate space, or None when no plausible plate
    region was found (caller falls back to whole-crop OCR).
    """
    if vehicle_crop is None or vehicle_crop.size == 0:
        return None
    box = _locate_model(vehicle_crop) or _locate_classical(vehicle_crop)
    if box is None:
        return None
    x, y, w, h = box
    # Small pad: the localizer tends to hug the glyphs and clip the plate's
    # first/last character edge, which costs a real character in the read.
    pad_x = max(2, int(w * 0.04))
    pad_y = max(2, int(h * 0.12))
    crop_h, crop_w = vehicle_crop.shape[:2]
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(crop_w, x + w + pad_x)
    y2 = min(crop_h, y + h + pad_y)
    plate_crop = vehicle_crop[y1:y2, x1:x2]
    if plate_crop.size == 0:
        return None
    return _preprocess_for_ocr(plate_crop), [float(x1), float(y1), float(x2), float(y2)]
