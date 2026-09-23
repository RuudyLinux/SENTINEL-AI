"""Plate-crop preprocessing variants and perspective correction.

The seam between "we found a plate region" (`plate_detector`) and "read it"
(`anpr`). Two jobs:

1. **Perspective correction.** A plate photographed off-axis is a
   quadrilateral, not a rectangle. When the detector supplies a rotated quad
   (the classical localizer does, via `cv2.minAreaRect`), a four-point warp
   rectifies it before OCR sees it.

2. **Preprocessing variants.** The same crop rendered several ways — grayscale,
   CLAHE, denoise, sharpen, adaptive/Otsu threshold — so OCR can be run over
   more than one and the candidates compared. `anpr.select_candidate` does the
   comparing; this module only produces the images.

Why variants are NOT on by default
----------------------------------
Measured on the 25-plate labelled corpus (docs/ANPR_ACCURACY.md), running every
variant and selecting by agreement:

| strategy | exact | CER | false-pos | OCR calls/plate |
|---|---|---|---|---|
| `clahe` alone (production) | 0.24 | 0.3896 | 0.28 | 1 |
| `original+sharpen+adaptive` | 0.28 | 0.3030 | 0.28 | 3 |
| all seven | 0.28 | 0.2814 | 0.32 | 7 |

The exact-match move is ONE sample out of 25 — inside the documented noise band
for this corpus, so it is not an accuracy improvement and is not claimed as
one. The CER gain is real (it aggregates over ~250 characters rather than 25
binary outcomes) but costs 3-7x the OCR time, which is the single most
expensive operation in the camera loop. For a CCTV deployment that trade is the
operator's to make, not a default to impose — so `PLATE_PREPROCESS_VARIANTS`
ships as the single variant that reproduces today's behavior exactly, and the
multi-variant path is an opt-in recovery/diagnostic mode.

Escalation (run variants only when the cheap read fails the gate) was measured
and REJECTED: it fires only when the first read fails, but the crops variants
actually rescue are ones where the first read PASSES with a wrong answer. Exact
match stayed at 0.24 and false positives rose. The numbers are in
docs/ANPR_ACCURACY.md so nobody re-derives them.
"""
import cv2
import numpy as np

from ..config import settings

# Every variant a deployment may name in PLATE_PREPROCESS_VARIANTS. The value
# is a callable taking an ALREADY-UPSCALED crop and returning an OCR-ready
# image; upscaling is done once up front rather than per variant because it is
# the same work for all of them and it dominates their cost.
#
# "clahe" is the production default because it is literally what the pipeline
# already did before this module existed (plate_detect._preprocess_for_ocr =
# upscale -> gray -> CLAHE). Naming it as a variant rather than reimplementing
# it is what makes "variants disabled" mean "byte-for-byte the old behavior".
VARIANT_NAMES = ("original", "gray", "clahe", "denoise", "sharpen", "adaptive", "otsu")

# The variant set measured as the best cost/benefit point if a deployment does
# enable multi-variant reading. Not a default — exported so operators and the
# benchmark refer to the same measured set instead of each picking their own.
RECOMMENDED_RECOVERY_VARIANTS = ("original", "sharpen", "adaptive")


def _to_gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def _clahe(image: np.ndarray) -> np.ndarray:
    """Contrast-limited adaptive histogram equalization.

    CLAHE rather than a global equalizeHist: plates are frequently half in
    shadow (overhang, headlight glare), and a global histogram stretch blows out
    the lit half in order to read the dark one.
    """
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(_to_gray(image))


def _denoise(image: np.ndarray) -> np.ndarray:
    """Bilateral filter: smooths sensor/compression noise while keeping the hard
    glyph edges OCR segments on. A Gaussian blur would soften both."""
    return cv2.bilateralFilter(_clahe(image), 7, 50, 50)


def _sharpen(image: np.ndarray) -> np.ndarray:
    """Unsharp mask — recovers glyph edges softened by motion blur or upscaling."""
    base = _clahe(image)
    return cv2.addWeighted(base, 1.6, cv2.GaussianBlur(base, (0, 0), 3), -0.6, 0)


def _adaptive(image: np.ndarray) -> np.ndarray:
    """Per-region thresholding. Handles a plate lit unevenly across its width,
    where any single global threshold loses one end or the other."""
    return cv2.adaptiveThreshold(
        _clahe(image), 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 11,
    )


def _otsu(image: np.ndarray) -> np.ndarray:
    """Global threshold at the automatically-chosen optimum. Complements
    `_adaptive`: better on an evenly-lit plate, worse on an uneven one."""
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
    """Scale a small plate crop up to a comfortable glyph height.

    OCR accuracy on real CCTV crops is dominated by effective glyph height: a
    plate 18px tall in the source frame is at the edge of what EasyOCR
    resolves. Upscaling costs a few milliseconds and, unlike changing the OCR
    engine, cannot regress reads that already work. Crops already at or above
    the target are returned untouched — enlarging them adds cost and no
    information.
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
    """Order four corners as top-left, top-right, bottom-right, bottom-left.

    `cv2.minAreaRect`/`boxPoints` return corners in an order that depends on the
    rectangle's rotation, so warping without normalizing the order produces a
    mirrored or 90°-rotated plate for some angles. Ordered by the standard
    coordinate-sum/difference trick: the top-left has the smallest x+y, the
    bottom-right the largest; the top-right has the smallest y-x.
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
    """Warp a quadrilateral plate region to a front-on rectangle.

    Returns None when the quad is degenerate (collinear points, or an output
    smaller than a readable plate), so the caller keeps the un-warped crop
    rather than handing OCR a smear.
    """
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
    """Whether a quad is skewed enough that warping is worth the pixels.

    An axis-aligned (or near-axis-aligned) box warps to approximately itself,
    so the transform costs time and introduces resampling blur for no gain.
    `tolerance` is the fraction of the region's own size by which opposite
    edges may differ before it counts as skewed.
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
    """The variant names this deployment has enabled, validated.

    Unknown names are dropped with the rest kept rather than raising: a typo in
    an env var must not take a camera worker down. An empty/all-invalid setting
    falls back to the production default so OCR always has exactly one image to
    read.
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
    """Render a plate crop as `[(variant_name, image), ...]`, ready for OCR.

    Perspective correction (when `quad` is supplied and actually skewed) and
    upscaling are applied ONCE, before the variants branch — they are identical
    work for every variant, and doing them per-variant would multiply the cost
    of the expensive part for no benefit.

    With the default single-variant configuration this returns exactly one
    image, produced by exactly the operations the pipeline already performed,
    so enabling this module changes nothing until an operator opts in.
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
            # A variant that cannot be produced for this particular crop (e.g.
            # a threshold on a degenerate single-row image) is skipped, not
            # fatal — the others still give OCR something to read.
            continue
        if image is not None and image.size > 0:
            variants.append((name, image))
    return variants
