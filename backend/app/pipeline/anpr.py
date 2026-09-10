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

# --- Positional character disambiguation ------------------------------------
# Measured with tools/anpr_bench.py: the single most common real failure is not
# a wrong plate, it is a character-class confusion in an otherwise perfect read
# — "GJ05AB1234" coming back as "GJO5AB1234" (digit 0 read as letter O). Those
# reads then fail `looks_like_plate` and are silently discarded, so a plate the
# system genuinely read correctly is thrown away over one glyph.
#
# An Indian registration has a known GRAMMAR — two state letters, a 1-2 digit
# RTO code, a 1-3 letter series, a 3-4 digit number — so the expected character
# CLASS at every position is known. That makes this a targeted correction
# against a real constraint, not a guess: a substitution is only ever applied
# where the grammar demands the other class, and only if the result then parses
# as a valid plate. A read that already parses is never touched.
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A"})

# Most substitutions a single repair may make. Without a budget the mapping is
# powerful enough to manufacture a plate out of noise: "QQQQQQQQQQ" becomes the
# perfectly well-formed "QQ00QQ0000" in six substitutions, which the format gate
# would then accept as a real registration. Two keeps this to what it is for —
# repairing a glyph or two in a read that is otherwise right — and makes
# rewriting a string into a plate impossible.
_MAX_SUBSTITUTIONS = 2


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
    # `raw` deliberately stays the literal OCR output for the audit trail, while
    # the normalized form gets the grammar-based character-class repair — so a
    # record always shows both what OCR actually said and what it was resolved
    # to. See disambiguate_plate for why this is a constrained correction and
    # not a guess. Confidence is NOT adjusted: the repair does not make the
    # engine more certain than it was, and inflating it would be dishonest.
    normalized = disambiguate_plate(normalize_plate(raw))
    return raw, normalized, float(confidence)


def looks_like_plate(normalized: str) -> bool:
    return bool(PLATE_RE.match(normalized))


def disambiguate_plate(normalized: str) -> str:
    """Repair character-class confusions using the plate grammar.

    Returns a corrected plate when the input is one character-class confusion
    away from a valid registration, and the input UNCHANGED otherwise. It never
    invents or drops characters, never alters a read that already parses, and
    only ever swaps a glyph for its visual twin in the other class.

    The candidate segmentations are enumerated rather than assumed, because the
    RTO code, series and number are all variable length — "GJ05AB1234" and
    "GJ5ABC123" are both valid and split differently.
    """
    if not normalized or looks_like_plate(normalized):
        return normalized
    length = len(normalized)
    # 2 state letters + rto + series + number, so the remainder must be split
    # three ways within each segment's real length limits.
    remaining = length - 2
    if not 5 <= remaining <= 9:
        return normalized
    for rto_len in (2, 1):
        for series_len in (2, 3, 1):
            number_len = remaining - rto_len - series_len
            if number_len not in (4, 3):
                continue
            cursor = 0
            parts = []
            for segment_len, table in (
                (2, _TO_LETTER), (rto_len, _TO_DIGIT), (series_len, _TO_LETTER), (number_len, _TO_DIGIT),
            ):
                parts.append(normalized[cursor:cursor + segment_len].translate(table))
                cursor += segment_len
            candidate = "".join(parts)
            substitutions = sum(1 for before, after in zip(normalized, candidate) if before != after)
            if substitutions <= _MAX_SUBSTITUTIONS and looks_like_plate(candidate):
                return candidate
    return normalized


def passes_anpr_gate(normalized: str, confidence: float) -> bool:
    """The single quality gate (P0-C): a normalized OCR read only becomes a
    Vehicle/Plate correlation record when it looks like a plate AND clears
    the configured confidence floor. Extracted as its own function so it's
    directly unit-testable without a real OCR/frame pipeline."""
    return bool(normalized) and looks_like_plate(normalized) and confidence >= settings.plate_min_confidence
