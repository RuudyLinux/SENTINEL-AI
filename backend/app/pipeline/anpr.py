"""OCR-based ANPR on EasyOCR. Confidence is whatever EasyOCR reports, never
clamped or floored, and low-confidence reads are kept with that confidence so
ANPR quality can actually be measured.
"""
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from ..config import settings

PLATE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4}$")

# Bharat (BH) series, the 2021 all-India registration: YY + "BH" + 4 digits +
# 1-2 letters, e.g. 23BH1234AA. Separate regex so PLATE_RE doesn't have to
# start accepting digits in the state-code slot.
BH_SERIES_RE = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")

# Every registration prefix an Indian state or UT issues.
#
# PLATE_RE alone takes any two letters, so QQ00QQ0000 or XX12AB1234 count as
# plates. That's the dangerous case the benchmark measures: plate-shaped but
# wrong, it clears the gate and becomes a Vehicle row. Checking against codes
# that exist costs nothing.
#
# A set rather than a regex alternation so it's easy to grep and amend.
INDIAN_STATE_CODES = frozenset({
    # States
    "AP",  # Andhra Pradesh
    "AR",  # Arunachal Pradesh
    "AS",  # Assam
    "BR",  # Bihar
    "CG",  # Chhattisgarh
    "GA",  # Goa
    "GJ",  # Gujarat
    "HR",  # Haryana
    "HP",  # Himachal Pradesh
    "JH",  # Jharkhand
    "JK",  # Jammu and Kashmir (UT since 2019; code still issued)
    "KA",  # Karnataka
    "KL",  # Kerala
    "MH",  # Maharashtra
    "ML",  # Meghalaya
    "MN",  # Manipur
    "MP",  # Madhya Pradesh
    "MZ",  # Mizoram
    "NL",  # Nagaland
    "OD",  # Odisha (current)
    "OR",  # Odisha (legacy code, still on the road)
    "PB",  # Punjab
    "RJ",  # Rajasthan
    "SK",  # Sikkim
    "TN",  # Tamil Nadu
    "TG",  # Telangana (current)
    "TS",  # Telangana (legacy code, still on the road)
    "TR",  # Tripura
    "UK",  # Uttarakhand (current)
    "UA",  # Uttarakhand (legacy code, still on the road)
    "UP",  # Uttar Pradesh
    "WB",  # West Bengal
    # Union territories
    "AN",  # Andaman and Nicobar Islands
    "CH",  # Chandigarh
    "DD",  # Daman and Diu / Dadra and Nagar Haveli and Daman and Diu
    "DN",  # Dadra and Nagar Haveli (legacy)
    "DL",  # Delhi
    "LA",  # Ladakh
    "LD",  # Lakshadweep
    "PY",  # Puducherry
})

# Positional character repair. Most common real failure in
# tools/anpr_bench.py is a class confusion in an otherwise right read:
# GJ05AB1234 read as GJO5AB1234, which then fails looks_like_plate and gets
# thrown away over one glyph.
#
# The plate grammar (2 state letters, 1-2 digit RTO, 1-3 letter series, 3-4
# digit number) says which class each position should be, so a swap only
# happens where the grammar wants the other class, and only if the result
# parses. A read that already parses is left alone.
_TO_DIGIT = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})
_TO_LETTER = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A"})

# Without a budget the mapping can make a plate out of noise: QQQQQQQQQQ
# becomes QQ00QQ0000 in six swaps. Two is enough to fix a glyph or two.
_MAX_SUBSTITUTIONS = 2


def cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


# one EasyOCR reader for every camera, calls take turns (not thread-safe on
# a shared GPU model)
_READER_LOCK = threading.Lock()


def _readtext(crop: np.ndarray) -> list:
    reader = get_reader()
    with _READER_LOCK:
        return reader.readtext(crop)


@lru_cache(maxsize=1)
def get_reader():
    import easyocr
    # GPU when torch has CUDA (README "GPU runtime"), so OCR isn't left as the
    # CPU bottleneck behind a GPU detector
    return easyocr.Reader(["en"], gpu=cuda_available(), verbose=False)


def normalize_plate(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def order_fragments(results: list) -> list:
    """Order OCR fragments top row first, then left to right in each row.

    Sorting by left x alone interleaves two-row plates (common in India):
    KL07BX7197 came back as INDBX7197KL07 and no grammar repair can fix that.
    Rows are found by clustering fragment centre-y, starting a new band when a
    fragment is more than half the median fragment height below the current
    one. Relative to height because crops range from 40px to 900px wide.
    Single-row plates end up in one band, same as a plain x sort.
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


@dataclass(frozen=True)
class OcrCandidate:
    """One preprocessing variant's read of a plate crop."""
    variant: str
    raw: str
    normalized: str
    confidence: float
    # (text, confidence) per fragment in reading order, kept so a read can be
    # audited without re-running OCR
    fragments: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class OcrRead:
    """Result of reading one plate crop.

    confidence is exactly what the OCR engine reported, never adjusted for
    agreement. variants_agreeing/variant_count are corroboration across
    preprocessing variants, and on the labelled corpus they separate right
    from wrong reads much better than confidence (<=2 of 7 agreeing: 0 of 13
    correct; >=5 of 7: 4 of 4). So they're kept separate instead of blended
    into one score; the gate looks at both.
    """
    raw: str
    normalized: str
    confidence: float
    variant: str = ""
    variants_agreeing: int = 1
    variant_count: int = 1
    candidates: tuple[OcrCandidate, ...] = field(default=())

    def as_tuple(self) -> tuple[str, str, float]:
        return self.raw, self.normalized, self.confidence


def read_plate(crop: np.ndarray) -> tuple[str, str, float]:
    """(raw_text, normalized_text, confidence). Confidence is the mean OCR
    confidence over fragments, 0.0 if nothing was read. The legacy worker path,
    the benchmark and tests use this tuple form; read_plate_structured is the
    richer one.
    """
    if crop is None or crop.size == 0:
        return "", "", 0.0
    results = _readtext(crop)
    if not results:
        return "", "", 0.0
    results = order_fragments(results)  # row-aware, see order_fragments
    raw = "".join(r[1] for r in results)
    confidence = sum(r[2] for r in results) / len(results)
    # raw stays the literal OCR output for the audit trail; normalized gets
    # the repair. Confidence isn't touched, the repair doesn't make OCR more
    # sure of itself.
    # Extract first (drops "IND" and sticker text), then repair. The other
    # way round the repair works on a string that can't parse anyway.
    normalized = disambiguate_plate(extract_plate(normalize_plate(raw)))
    return raw, normalized, float(confidence)


def read_candidate(crop: np.ndarray, variant: str) -> OcrCandidate:
    """Read one preprocessing variant, keeping per-fragment detail."""
    if crop is None or crop.size == 0:
        return OcrCandidate(variant=variant, raw="", normalized="", confidence=0.0)
    results = _readtext(crop)
    if not results:
        return OcrCandidate(variant=variant, raw="", normalized="", confidence=0.0)
    results = order_fragments(results)
    raw = "".join(result[1] for result in results)
    confidence = sum(result[2] for result in results) / len(results)
    fragments = tuple((str(result[1]), float(result[2])) for result in results)
    normalized = disambiguate_plate(extract_plate(normalize_plate(raw)))
    return OcrCandidate(
        variant=variant, raw=raw, normalized=normalized,
        confidence=float(confidence), fragments=fragments,
    )


def select_candidate(candidates: "list[OcrCandidate] | tuple[OcrCandidate, ...]") -> OcrRead:
    """Pick between several variants' reads of the same crop, by agreement.

    On the 25-plate corpus (docs/ANPR_ACCURACY.md), taking the highest
    confidence of 7 variants gave exact 0.24 / CER 0.3160 and raised false
    positives 0.28 -> 0.40. Most-agreed text gave 0.28 / 0.2814 with FP 0.32,
    and on original+sharpen+adaptive held FP at 0.28 while the wrong rate among
    accepted reads fell 0.58 -> 0.50.

    Max-of-N confidence is also biased upward and would quietly loosen every
    gate downstream. Reported confidence is the mean over the reads that
    agreed on the winner; agreement goes in variants_agreeing.

    Ties go to a gate-passing read, then summed confidence. Empty never beats
    non-empty.
    """
    candidates = tuple(candidates)
    if not candidates:
        return OcrRead(raw="", normalized="", confidence=0.0, variant="", variants_agreeing=0, variant_count=0)

    groups: dict[str, list[OcrCandidate]] = {}
    for candidate in candidates:
        if candidate.normalized:
            groups.setdefault(candidate.normalized, []).append(candidate)

    if not groups:
        # nothing usable from any variant. return the empty read but with a
        # real variant name so the record shows what was tried
        return OcrRead(
            raw=candidates[0].raw, normalized="", confidence=candidates[0].confidence,
            variant=candidates[0].variant, variants_agreeing=0,
            variant_count=len(candidates), candidates=candidates,
        )

    def group_rank(text: str) -> tuple:
        members = groups[text]
        gate_passes = any(passes_format_and_confidence(m.normalized, m.confidence) for m in members)
        return (len(members), gate_passes, sum(m.confidence for m in members))

    winner_text = max(groups, key=group_rank)
    members = groups[winner_text]
    # representative is only for raw/variant provenance; confidence stays the mean
    representative = max(members, key=lambda m: m.confidence)
    return OcrRead(
        raw=representative.raw,
        normalized=winner_text,
        confidence=sum(m.confidence for m in members) / len(members),
        variant=representative.variant,
        variants_agreeing=len(members),
        variant_count=len(candidates),
        candidates=candidates,
    )


def read_plate_structured(
    variants: "list[tuple[str, np.ndarray]]",
) -> OcrRead:
    """Read every (variant_name, image) and select between them. With the
    default single variant that's one OCR pass returned as is."""
    if not variants:
        return OcrRead(raw="", normalized="", confidence=0.0, variant="", variants_agreeing=0, variant_count=0)
    return select_candidate([read_candidate(image, name) for name, image in variants])


def looks_like_plate(normalized: str) -> bool:
    """Well-formed Indian registration: state-coded (GJ05AB1234) with a real
    state/UT prefix, or Bharat series (23BH1234AA). The prefix check is the
    cheapest false-positive defence here, the regex alone takes QQ00QQ0000."""
    if not normalized:
        return False
    if BH_SERIES_RE.match(normalized):
        return True
    return bool(PLATE_RE.match(normalized)) and normalized[:2] in INDIAN_STATE_CODES


def disambiguate_plate(normalized: str) -> str:
    """Fix character-class confusions using the plate grammar.

    Returns the corrected plate if the input is within _MAX_SUBSTITUTIONS
    glyph swaps of a valid one, otherwise the input unchanged. Never adds or
    drops characters and never touches a read that already parses.
    Segmentations are tried rather than assumed since RTO, series and number
    all vary in length (GJ05AB1234 and GJ5ABC123 are both valid).
    """
    if not normalized or looks_like_plate(normalized):
        return normalized
    length = len(normalized)
    # after the 2 state letters the rest splits into rto + series + number
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
    """Pull a valid registration out of a read with extra text glued on.

    After the row-ordering fix most remaining errors were real plate text plus
    whatever else OCR saw on or near the plate:

        KL07BX7197  ->  INDKL07BX7197     ("IND" country marker)
        DL3CD1210   ->  SUCUNDL3CD1210    (sticker/dealer text)

    Returns the longest substring that passes looks_like_plate. A read that
    already parses is returned as is, characters are never changed or
    reordered, and a string with no valid plate in it comes back unchanged.
    Longest wins since a shorter match is usually a truncation (KL07BX719).
    plate_text_raw keeps the literal OCR output for auditing.
    """
    if not normalized or looks_like_plate(normalized):
        return normalized
    # plates are 6-10 chars, nothing else can match PLATE_RE
    best = ""
    length = len(normalized)
    for start in range(length):
        for end in range(min(start + 10, length), start + 5, -1):
            candidate = normalized[start:end]
            if len(candidate) > len(best) and looks_like_plate(candidate):
                best = candidate
    return best or normalized


def passes_format_and_confidence(normalized: str, confidence: float) -> bool:
    # separate from passes_read_gate so select_candidate can ask without the
    # agreement rule applying recursively
    return bool(normalized) and looks_like_plate(normalized) and confidence >= settings.plate_min_confidence


def passes_anpr_gate(normalized: str, confidence: float) -> bool:
    """A read only becomes a Vehicle/Plate record if it looks like a plate and
    clears the confidence floor. passes_read_gate is the variant-aware form."""
    return passes_format_and_confidence(normalized, confidence)


def passes_read_gate(read: OcrRead) -> bool:
    """Gate for a structured read.

    Format and confidence as in passes_anpr_gate, and when more than one
    variant was read the winner must also have plate_min_variants_agreeing
    of them behind it.

    This can only make the gate stricter. Selecting among variants pushes
    reported confidence up (the agreeing subset skews to easy crops), which
    would otherwise nudge borderline reads past plate_min_confidence and the
    review floor. High confidence from one variant isn't corroboration.
    With one variant (default) this is the same as passes_anpr_gate.
    """
    if not passes_format_and_confidence(read.normalized, read.confidence):
        return False
    if read.variant_count <= 1:
        return True
    return read.variants_agreeing >= settings.plate_min_variants_agreeing


def better_read(
    first: tuple[str, str, float], second: "tuple[str, str, float] | None",
) -> tuple[str, str, float]:
    """The more trustworthy of two reads of the same plate.

    Localization turned out to cut accuracy on 25 real plates (exact match
    0.16 -> 0.04, CER 0.524 -> 0.636) because _locate_classical sometimes
    returns part of the plate and OCR then reads nothing. It's still ~3x
    cheaper and often right, so it's kept but not trusted blindly.

    Order: a gate-passing read wins, then non-empty beats empty, then higher
    confidence. second=None (no fallback computed) returns first.
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


def review_status_for(
    confidence: float, current: str | None = None, corroborated: bool = True,
) -> str:
    """Whether a plate sighting needs an operator to look at it.

    A gate-passing read below plate_review_confidence_floor is kept but marked
    pending_review. If later reads push it over the floor it goes back to
    auto_accepted, unless a human already corrected or rejected it; those are
    final and new OCR frames never overwrite them.

    corroborated=False (plate_tracker.has_consensus never saw enough agreeing
    reads) always means pending_review however confident the frame was. One
    confident frame isn't corroborated evidence.
    """
    if current in _HUMAN_REVIEW_STATES:
        return current
    if not corroborated:
        return "pending_review"
    if confidence >= settings.plate_review_confidence_floor:
        return "auto_accepted"
    return "pending_review"
