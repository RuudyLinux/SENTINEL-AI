"""Person appearance-similarity signature (Phase 5 — cross-camera intelligence).

Explicitly NOT face recognition and NOT an identity claim. This computes a compact
color-histogram "visual signature" of a person's bounding-box crop — a lightweight,
non-biometric feature used only to *rank* candidate sightings across cameras by how
visually similar they look (clothing/color, roughly), for an investigator to review
and confirm manually. It cannot and does not identify who someone is. See
routers/persons.py and README.md → "Cross-camera intelligence" for the exact
honest framing used throughout the app.

No new dependency — built entirely on cv2/numpy, already in requirements.txt.
"""
import numpy as np
import cv2


SIGNATURE_BINS = 16  # per channel


def compute_signature(crop: "np.ndarray") -> "list[float] | None":
    """HSV color histogram of a person crop, 3 channels x SIGNATURE_BINS bins each,
    each channel independently normalized to sum to 1. Returns None (never a
    fabricated/zero vector) if the crop is too small to be meaningful."""
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


# Per-channel weights for the final score: Hue is the primary color signal;
# Value (brightness) is the least reliable across lighting/exposure differences
# between cameras, so it counts least. Comparing one flat concatenated vector
# instead (tried first) let two very differently-hued but similarly bright/
# saturated crops (e.g. pure red vs. pure blue) score misleadingly high, since
# a matching S/V spike outweighed a completely mismatched H spike.
_CHANNEL_WEIGHTS = (0.6, 0.3, 0.1)  # H, S, V


def similarity(a: "list[float] | None", b: "list[float] | None") -> float:
    """0..1 similarity between two signatures (1.0 = identical). Returns 0.0 if
    either signature is missing or malformed — never guessed."""
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
