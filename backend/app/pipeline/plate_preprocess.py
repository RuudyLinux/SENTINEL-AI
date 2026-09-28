"""Plate crop preprocessing variants and perspective correction.

Sits between plate_detector (region found) and anpr (text read):
- perspective correction warps a skewed quad front-on before OCR;
- variants (grayscale, CLAHE, denoise, sharpen, adaptive/Otsu threshold) let
  OCR read the same crop several ways for anpr.select_candidate to compare.

Multiple variants cost one OCR pass each, so the default is a single variant;
the trade-off is documented in docs/ANPR_ACCURACY.md.
"""
import cv2
import numpy as np

from ..config import settings

# Names allowed in PLATE_PREPROCESS_VARIANTS. Each takes an already-upscaled
# crop. "clahe" (upscale -> gray -> CLAHE) is the default single variant.
VARIANT_NAMES = ("original", "gray", "clahe", "denoise", "sharpen", "adaptive", "otsu")

# Best cost/benefit set when multi-variant is enabled; shared with the benchmark.
RECOMMENDED_RECOVERY_VARIANTS = ("original", "sharpen", "adaptive")


def _to_gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def _clahe(image: np.ndarray) -> np.ndarray:
    """CLAHE rather than a global histogram stretch, which blows out the lit half
    of a plate in partial shadow."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(_to_gray(image))


def _denoise(image: np.ndarray) -> np.ndarray:
    """Bilateral filter: removes noise while keeping the glyph edges OCR needs."""
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
    """Upscale a small crop to a readable glyph height; crops already tall
    enough are returned unchanged.
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

    boxPoints' order depends on rotation, and unordered corners warp to a
    mirrored or rotated plate.
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
    """Warp a quad plate region to a front-on rectangle. None for a degenerate
    quad; the caller then keeps the unwarped crop."""
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
    """Whether the quad is skewed enough to be worth warping. `tolerance` is how
    much opposite edges may differ, as a fraction of their size.
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
    """Enabled variant names. Unknown names are dropped rather than raising, and
    an empty result falls back to the default so OCR always gets an image.
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
    """Plate crop as [(variant_name, image), ...] for OCR. Perspective correction
    and upscaling happen once before the variants branch.
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
