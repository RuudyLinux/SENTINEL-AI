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


def order_fragments(results: list) -> list:
    """Order OCR fragments the way a human reads a plate: top row first, then
    left-to-right within each row.

    Measured defect this fixes (docs/ANPR_ACCURACY.md): fragments were sorted
    by left-x ALONE, which is correct only for a single-row plate. India uses
    two-row plates widely, and on those a pure x-sort INTERLEAVES the rows —
    a real labelled plate `KL07BX7197` came back as `INDBX7197KL07`, mixing
    the "IND" country marker, the bottom row and the top row into one string
    that no amount of downstream grammar repair can rescue.

    Rows are recovered by clustering fragment centre-y: a fragment starting a
    new band whenever it sits more than half a typical glyph-height below the
    current band's centre. Half the median fragment height is used as the
    threshold rather than a fixed pixel value because crops arrive at wildly
    different scales (a 40px-wide distant plate and a 900px phone photo both
    reach here).

    Single-row plates are unaffected: every fragment lands in one band, and
    the result is exactly the previous left-to-right ordering.
    """
    if not results:
        return results

    def _cy(result) -> float:
        ys = [point[1] for point in result[0]]
        return sum(ys) / len(ys)

    def _height(result) -> float:
        ys = [point[1] for point in result[0]]
        return max(ys) - min(ys)

    heights = sorted(_height(r) for r in results)
    median_height = heights[len(heights) // 2] or 1.0
    tolerance = median_height * 0.5

    bands: list[list] = []
    for result in sorted(results, key=_cy):
        if bands and abs(_cy(result) - _cy(bands[-1][0])) <= tolerance:
            bands[-1].append(result)
        else:
            bands.append([result])

    ordered: list = []
    for band in bands:
        ordered.extend(sorted(band, key=lambda r: r[0][0][0]))  # left-to-right within the row
    return ordered


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
    # Row-aware ordering: top row first, then left-to-right within each row.
    # A pure left-x sort scrambles two-row plates — see order_fragments.
    results = order_fragments(results)
    raw = "".join(r[1] for r in results)
    confidence = sum(r[2] for r in results) / len(results)
    # `raw` deliberately stays the literal OCR output for the audit trail, while
    # the normalized form gets the grammar-based character-class repair — so a
    # record always shows both what OCR actually said and what it was resolved
    # to. See disambiguate_plate for why this is a constrained correction and
    # not a guess. Confidence is NOT adjusted: the repair does not make the
    # engine more certain than it was, and inflating it would be dishonest.
    # Order matters: extract a plate-shaped substring FIRST (removes the "IND"
    # marker and surrounding sticker text that OCR legitimately picks up), then
    # apply the grammar-based character-class repair to whatever survives. Run
    # the other way round, the repair would be trying to fix a string that
    # still has non-plate text attached and could not parse regardless.
    normalized = disambiguate_plate(extract_plate(normalize_plate(raw)))
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


def extract_plate(normalized: str) -> str:
    """Pull a valid registration out of a read that carries extra characters.

    Measured need (docs/ANPR_ACCURACY.md): after the row-ordering fix, the
    remaining errors were dominated by real plate text with NON-plate text
    glued to it — OCR legitimately reads whatever else is on or near the
    plate:

        KL07BX7197  ->  INDKL07BX7197     ("IND" country marker)
        DL3CD1210   ->  SUCUNDL3CD1210    (surrounding sticker/dealer text)

    Both contain the correct registration verbatim. This finds the longest
    substring that is a VALID Indian plate under PLATE_RE and returns it.

    Why this is a constrained extraction and not a guess — the same standard
    `disambiguate_plate` is held to:

    - a read that ALREADY parses is returned untouched, so nothing that works
      today can be changed;
    - characters are never invented, substituted or reordered — only a
      contiguous run of the existing string is selected;
    - the result must itself satisfy `looks_like_plate`, so this can only ever
      turn an unusable read into a well-formed one, never fabricate a plate
      from noise (a string containing no valid registration comes back
      unchanged);
    - LONGEST match wins, because a shorter match is usually a truncation of
      the real plate (e.g. preferring `KL07BX7197` over `KL07BX719`).

    The raw OCR output is still preserved separately on the Plate row
    (`plate_text_raw`), so the extraction is always auditable against what
    the engine literally returned.
    """
    if not normalized or looks_like_plate(normalized):
        return normalized
    # Indian registrations are 6-10 characters (2 state + 1-2 RTO + 1-3
    # series + 3-4 number); nothing outside that range can match PLATE_RE.
    best = ""
    length = len(normalized)
    for start in range(length):
        for end in range(min(start + 10, length), start + 5, -1):
            candidate = normalized[start:end]
            if len(candidate) > len(best) and looks_like_plate(candidate):
                best = candidate
    return best or normalized


def passes_anpr_gate(normalized: str, confidence: float) -> bool:
    """The single quality gate (P0-C): a normalized OCR read only becomes a
    Vehicle/Plate correlation record when it looks like a plate AND clears
    the configured confidence floor. Extracted as its own function so it's
    directly unit-testable without a real OCR/frame pipeline."""
    return bool(normalized) and looks_like_plate(normalized) and confidence >= settings.plate_min_confidence


def better_read(
    first: tuple[str, str, float], second: "tuple[str, str, float] | None",
) -> tuple[str, str, float]:
    """Pick the more trustworthy of two reads of the SAME plate.

    Measured defect this addresses (docs/ANPR_ACCURACY.md): plate localization
    was assumed to be the largest accuracy lever, and on 25 real labelled
    Indian plates it turned out to REDUCE accuracy — exact match 0.16 -> 0.04,
    CER 0.524 -> 0.636 — because `_locate_classical` sometimes returns a
    sub-region of the plate, after which OCR reads nothing at all
    (`GJ01DY6855 -> <empty>`). The localized read is still ~3x cheaper and is
    often right, so the fix is not to abandon localization but to stop
    trusting it blindly.

    Ranking, strongest signal first:
      1. a read that PASSES the quality gate beats one that does not — a
         plate-shaped, sufficiently-confident read is the whole objective;
      2. between two gate-passing (or two failing) reads, higher confidence
         wins;
      3. a non-empty read beats an empty one, so a localization miss can
         never turn a real read into nothing.

    `second` may be None (no fallback was computed), in which case `first` is
    returned unchanged — so a caller that cannot afford the extra OCR pass
    keeps exactly the previous behavior.
    """
    if second is None:
        return first
    first_passes = passes_anpr_gate(first[1], first[2])
    second_passes = passes_anpr_gate(second[1], second[2])
    if first_passes != second_passes:
        return first if first_passes else second
    if bool(first[1]) != bool(second[1]):
        return first if first[1] else second
    return first if first[2] >= second[2] else second


_HUMAN_REVIEW_STATES = {"corrected", "rejected"}


def review_status_for(confidence: float, current: str | None = None) -> str:
    """Human-in-the-loop ANPR review (10/10 roadmap P7): whether a Plate
    sighting needs an operator's eyes.

    A gate-passing read that is nonetheless below
    `plate_review_confidence_floor` is real, stored intelligence — never
    discarded — but flagged `pending_review` rather than treated as settled.
    A read that later climbs above the floor (more corroborating OCR passes)
    is auto-promoted back to `auto_accepted`, EXCEPT once a human has already
    acted on it: `corrected`/`rejected` are terminal states a fresh OCR
    frame must never silently overwrite.
    """
    if current in _HUMAN_REVIEW_STATES:
        return current
    if confidence >= settings.plate_review_confidence_floor:
        return "auto_accepted"
    return "pending_review"
