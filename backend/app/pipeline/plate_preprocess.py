"""Plate crop preprocessing variants and perspective correction.

Sits between plate_detector (found a region) and anpr (read it):
- perspective correction: an off-axis plate is a quad; when the detector
  gives one (the classical localizer does, via minAreaRect) a four-point
  warp straightens it before OCR
- variants: the same crop as grayscale, CLAHE, denoise, sharpen,
  adaptive/Otsu threshold, so OCR can read several and
  anpr.select_candidate can compare them

Variants are off by default. On the 25-plate corpus (docs/ANPR_ACCURACY.md),
selecting by agreement:

    clahe alone (production)     exact 0.24  CER 0.3896  FP 0.28  1 OCR call
    original+sharpen+adaptive    exact 0.28  CER 0.3030  FP 0.28  3 calls
    all seven                    exact 0.28  CER 0.2814  FP 0.32  7 calls

The exact-match change is one sample in 25, inside the noise, so it isn't an
accuracy gain. The CER gain is real (~250 characters, not 25 yes/no) but costs
3-7x the OCR time, the most expensive thing in the camera loop. That's the
operator's call, so PLATE_PREPROCESS_VARIANTS defaults to the one variant
that matches the old behaviour, and multi-variant is opt-in.

Escalating (variants only when the cheap read fails the gate) was tried and
dropped: variants rescue crops where the first read passes with the wrong
text, and escalation never fires on those. Exact stayed 0.24, false positives
went up. Numbers in docs/ANPR_ACCURACY.md.
"""
import cv2
import numpy as np

from ..config import settings

# Names allowed in PLATE_PREPROCESS_VARIANTS. Each takes an already-upscaled
# crop; upscaling happens once up front since it's the same for all of them
# and dominates their cost.
#
# "clahe" is the default because it's exactly what the pipeline did before
# (plate_detect._preprocess_for_ocr: upscale -> gray -> CLAHE), so variants
# off means byte-for-byte the old behaviour.
VARIANT_NAMES = ("original", "gray", "clahe", "denoise", "sharpen", "adaptive", "otsu")

# best measured cost/benefit set if someone turns multi-variant on. exported
# so operators and the benchmark use the same one
RECOMMENDED_RECOVERY_VARIANTS = ("original", "sharpen", "adaptive")


def _to_gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def _clahe(image: np.ndarray) -> np.ndarray:
    """CLAHE, not global equalizeHist: plates are often half in shadow
    (overhang, headlight glare) and a global stretch blows out the lit half."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(_to_gray(image))


def _denoise(image: np.ndarray) -> np.ndarray:
    """Bilateral filter: smooths sensor/compression noise but keeps the hard
    glyph edges OCR needs. Gaussian would soften both."""
    return cv2.bilateralFilter(_clahe(image), 7, 50, 50)


def _sharpen(image: np.ndarray) -> np.ndarray:
    """Unsharp mask, gets glyph edges back after motion blur or upscaling."""
    base = _clahe(image)
    return cv2.addWeighted(base, 1.6, cv2.GaussianBlur(base, (0, 0), 3), -0.6, 0)


def _adaptive(image: np.ndarray) -> np.ndarray:
    """Per-region threshold, for plates lit unevenly across their width."""
    return cv2.adaptiveThreshold(
        _clahe(image), 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 11,
    )


def _otsu(image: np.ndarray) -> np.ndarray:
    """Otsu. Better than _adaptive on an evenly lit plate, worse on an uneven one."""
    _, thresholded = cv2.threshold(_clahe(image), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresholded


_VARIANT_FUNCTIONS = {
    "original": lambda image: image,
    "gray": _to_gray,
    "clahe": _clahe,
    "denoise": _denoise,
    "sharpen": _sharpen,
    "adaptive": _adaptive,
    "otsu": _otsu,
}


def upscale_for_ocr(image: np.ndarray, target_height: int | None = None) -> np.ndarray:
    """Upscale a small crop to a readable glyph height.

    Glyph height is what decides OCR accuracy on real CCTV; an 18px plate is
    at the edge of what EasyOCR resolves. A few ms, and unlike swapping the
    OCR engine it can't break reads that already work. Crops already tall
    enough are returned as is.
    """
    if image is None or image.size == 0:
        return image
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return image
    target = int(target_height if target_height is not None else settings.plate_ocr_target_height)
    if height >= target:
        return image
    scale = target / height
    return cv2.resize(image, (max(1, int(width * scale)), target), interpolation=cv2.INTER_CUBIC)


def order_quad(points: np.ndarray) -> np.ndarray:
    """Order corners top-left, top-right, bottom-right, bottom-left.

    boxPoints' order depends on rotation, and warping unordered corners gives
    a mirrored or 90°-turned plate at some angles. Top-left has the smallest
    x+y, bottom-right the largest, top-right the smallest y-x.
    """
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)
    coordinate_sum = points.sum(axis=1)
    ordered[0] = points[np.argmin(coordinate_sum)]
    ordered[2] = points[np.argmax(coordinate_sum)]
    coordinate_difference = np.diff(points, axis=1).ravel()  # y - x
    ordered[1] = points[np.argmin(coordinate_difference)]
    ordered[3] = points[np.argmax(coordinate_difference)]
    return ordered


def four_point_transform(image: np.ndarray, quad) -> "np.ndarray | None":
    """Warp a quad plate region to a front-on rectangle. None for a
    degenerate quad (collinear, or smaller than a readable plate), and the
    caller keeps the unwarped crop."""
    if image is None or image.size == 0 or quad is None:
        return None
    try:
        ordered = order_quad(quad)
    except (ValueError, TypeError):
        return None
    (top_left, top_right, bottom_right, bottom_left) = ordered
    width = int(max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left)))
    height = int(max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left)))
    if width < 8 or height < 4:
        return None
    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32,
    )
    try:
        matrix = cv2.getPerspectiveTransform(ordered, destination)
        return cv2.warpPerspective(image, matrix, (width, height))
    except cv2.error:
        return None


def needs_perspective_correction(quad, tolerance: float = 0.08) -> bool:
    """Is the quad skewed enough to be worth warping? A near-axis-aligned box
    warps to about itself, so it's just time and resampling blur.
    `tolerance` is how much opposite edges may differ, as a fraction of size.
    """
    if quad is None:
        return False
    try:
        ordered = order_quad(quad)
    except (ValueError, TypeError):
        return False
    (top_left, top_right, bottom_right, bottom_left) = ordered
    top_width = float(np.linalg.norm(top_right - top_left))
    bottom_width = float(np.linalg.norm(bottom_right - bottom_left))
    left_height = float(np.linalg.norm(bottom_left - top_left))
    right_height = float(np.linalg.norm(bottom_right - top_right))
    if min(top_width, bottom_width, left_height, right_height) <= 0:
        return False
    width_skew = abs(top_width - bottom_width) / max(top_width, bottom_width)
    height_skew = abs(left_height - right_height) / max(left_height, right_height)
    return max(width_skew, height_skew) > tolerance


def configured_variants() -> tuple[str, ...]:
    """Enabled variant names. Unknown ones are dropped, not raised: a typo in
    an env var shouldn't kill a camera worker. Empty/all invalid falls back
    to the default so OCR always gets one image.
    """
    raw = (settings.plate_preprocess_variants or "").strip()
    if not raw:
        return (settings.plate_preprocess_default_variant,)
    names = tuple(
        name for name in (part.strip().lower() for part in raw.split(",")) if name in _VARIANT_FUNCTIONS
    )
    return names or (settings.plate_preprocess_default_variant,)


def build_variants(
    plate_crop: np.ndarray,
    quad=None,
    variant_names: "tuple[str, ...] | None" = None,
) -> list[tuple[str, np.ndarray]]:
    """Plate crop as [(variant_name, image), ...] for OCR.

    Perspective correction (given a skewed quad) and upscaling happen once
    before branching, same work for every variant. With the default single
    variant this is exactly the old pipeline's one image.
    """
    if plate_crop is None or plate_crop.size == 0:
        return []
    prepared = plate_crop
    if quad is not None and needs_perspective_correction(quad):
        warped = four_point_transform(plate_crop, quad)
        if warped is not None and warped.size > 0:
            prepared = warped
    prepared = upscale_for_ocr(prepared)

    names = variant_names if variant_names is not None else configured_variants()
    variants: list[tuple[str, np.ndarray]] = []
    for name in names:
        function = _VARIANT_FUNCTIONS.get(name)
        if function is None:
            continue
        try:
            image = function(prepared)
        except cv2.error:
            # a variant that can't be made for this crop (threshold on a
            # degenerate one-row image) is skipped, the rest still work
            continue
        if image is not None and image.size > 0:
            variants.append((name, image))
    return variants
