"""Person appearance signature for cross-camera similarity.

A small HSV histogram of the person crop (roughly clothing colour), used only
to rank sightings on other cameras for an investigator to check by hand. Not
face recognition and not identity.
"""
import numpy as np
import cv2


SIGNATURE_BINS = 16  # per channel


def compute_signature(crop: "np.ndarray") -> "list[float] | None":
    """HSV histogram of a person crop, 3 channels x SIGNATURE_BINS, each
    channel normalized to sum 1. None if the crop is too small."""
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    if h < 8 or w < 8:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    ranges = [(0, 180), (0, 256), (0, 256)]
    sig: list[float] = []
    for ch in range(3):
        lo, hi = ranges[ch]
        hist = cv2.calcHist([hsv], [ch], None, [SIGNATURE_BINS], [lo, hi])
        total = float(hist.sum())
        if total > 0:
            hist = hist / total
        sig.extend(float(v) for v in hist.flatten())
    return sig


# Hue is the main colour signal; Value (brightness) varies most between cameras,
# so it is weighted least. Channels are compared separately.
_CHANNEL_WEIGHTS = (0.6, 0.3, 0.1)  # H, S, V


def similarity(a: "list[float] | None", b: "list[float] | None") -> float:
    """0..1 similarity (1.0 identical). 0.0 if either is missing or malformed."""
    expected_len = 3 * SIGNATURE_BINS
    if not a or not b or len(a) != len(b) or len(a) != expected_len:
        return 0.0
    arr_a = np.asarray(a, dtype=np.float32)
    arr_b = np.asarray(b, dtype=np.float32)
    total = 0.0
    for i, weight in enumerate(_CHANNEL_WEIGHTS):
        lo, hi = i * SIGNATURE_BINS, (i + 1) * SIGNATURE_BINS
        score = cv2.compareHist(arr_a[lo:hi], arr_b[lo:hi], cv2.HISTCMP_CORREL)
        if score != score:  # NaN guard (e.g. a flat/empty histogram)
            score = 0.0
        total += weight * score
    return float(max(0.0, min(1.0, total)))
