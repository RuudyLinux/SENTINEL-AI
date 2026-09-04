"""Real OCR-based ANPR. Accuracy is whatever EasyOCR actually achieves on the
crop — per the doc's "AI honesty rule" we do not fabricate or floor-clamp
confidence. Garbage reads are kept with their real (low) confidence rather
than silently discarded, so ANPR quality can be measured honestly.
"""
import re
from functools import lru_cache

import numpy as np

from ..config import settings

PLATE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4}$")


@lru_cache(maxsize=1)
def get_reader():
    import easyocr
    return easyocr.Reader(["en"], gpu=False, verbose=False)


def normalize_plate(raw: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
    return cleaned


def read_plate(crop: np.ndarray) -> tuple[str, str, float]:
    """Returns (raw_text, normalized_text, confidence). Confidence is the
    real mean OCR confidence over detected text fragments; 0.0 if nothing read.
    """
    if crop is None or crop.size == 0:
        return "", "", 0.0
    reader = get_reader()
    results = reader.readtext(crop)
    if not results:
        return "", "", 0.0
    # concatenate fragments left-to-right, average their confidence
    results.sort(key=lambda r: r[0][0][0])  # sort by left x of bbox
    raw = "".join(r[1] for r in results)
    confidence = sum(r[2] for r in results) / len(results)
    normalized = normalize_plate(raw)
    return raw, normalized, float(confidence)


def looks_like_plate(normalized: str) -> bool:
    return bool(PLATE_RE.match(normalized))


def passes_anpr_gate(normalized: str, confidence: float) -> bool:
    """The single quality gate (P0-C): a normalized OCR read only becomes a
    Vehicle/Plate correlation record when it looks like a plate AND clears
    the configured confidence floor. Extracted as its own function so it's
    directly unit-testable without a real OCR/frame pipeline."""
    return bool(normalized) and looks_like_plate(normalized) and confidence >= settings.plate_min_confidence
