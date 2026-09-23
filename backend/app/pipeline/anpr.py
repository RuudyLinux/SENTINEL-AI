"""Real OCR-based ANPR. Accuracy is whatever EasyOCR actually achieves on the
crop — per the doc's "AI honesty rule" we do not fabricate or floor-clamp
confidence. Garbage reads are kept with their real (low) confidence rather
than silently discarded, so ANPR quality can be measured honestly.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from ..config import settings

PLATE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4}$")

# Bharat (BH) series — the 2021 all-India registration for transferable
# vehicles. It does NOT follow the state-code grammar above: it is
# YY + "BH" + 4 digits + 1-2 letters, e.g. "23BH1234AA". Matched separately
# rather than by loosening PLATE_RE, which would also start accepting
# digit-leading garbage in the state-code position.
BH_SERIES_RE = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")

# Every registration prefix issued by an Indian state or union territory.
#
# Why this matters more than it looks: without it, PLATE_RE accepts any two
# letters, so "QQ00QQ0000", "XX12AB1234" and "ZZ99ZZ9999" are all "valid
# plates". That is exactly the dangerous failure class the benchmark measures —
# a read that is plate-SHAPED but wrong clears the quality gate and becomes a
# real Vehicle row. Constraining the first two characters to codes that actually
# exist is a check against reality, not a heuristic, and it costs nothing.
#
# Kept as data rather than a regex alternation so it is greppable, testable and
# amendable when a new UT code is issued.
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


@dataclass(frozen=True)
class OcrCandidate:
    """One preprocessing variant's reading of the same plate crop."""
    variant: str
    raw: str
    normalized: str
    confidence: float
    # (text, confidence) per OCR fragment, in reading order. The character-level
    # detail EasyOCR actually exposes — kept for the audit trail so a read can be
    # inspected without re-running OCR.
    fragments: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class OcrRead:
    """The structured result of reading one plate crop.

    Every field is a SEPARATE, independently-meaningful signal. They are
    deliberately not combined into a single score:

    - `confidence` is what the OCR engine reported, and nothing else. It is
      never adjusted upward for agreement, never floored, never blended.
    - `variants_agreeing` / `variant_count` describe corroboration ACROSS
      preprocessing variants. Measured on the labelled corpus, this separates
      correct from incorrect reads far more cleanly than `confidence` does
      (<=2 of 7 agreeing: 0 of 13 correct; >=5 of 7: 4 of 4 correct), which is
      exactly why it is reported rather than folded in.

    A read of `0.91` confidence with 1 of 7 variants agreeing and a read of
    `0.57` with 5 of 7 agreeing are different kinds of evidence. The gate
    (`passes_anpr_gate`) reasons over both explicitly; this object refuses to
    pre-digest them into one number that would hide the difference.
    """
    raw: str
    normalized: str
    confidence: float
    variant: str = ""
    variants_agreeing: int = 1
    variant_count: int = 1
    candidates: tuple[OcrCandidate, ...] = field(default=())

    def as_tuple(self) -> tuple[str, str, float]:
        """The legacy 3-tuple contract `read_plate` has always returned."""
        return self.raw, self.normalized, self.confidence


def read_plate(crop: np.ndarray) -> tuple[str, str, float]:
    """Returns (raw_text, normalized_text, confidence). Confidence is the
    real mean OCR confidence over detected text fragments; 0.0 if nothing read.

    Unchanged contract. `read_plate_structured` is the richer interface; this
    stays because the worker's legacy path, the benchmark and the existing tests
    are written against the tuple.
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


def read_candidate(crop: np.ndarray, variant: str) -> OcrCandidate:
    """Read ONE preprocessing variant, keeping the per-fragment detail.

    Same OCR work `read_plate` does — the fragments are simply not thrown away,
    so a stored read can be audited without re-running the engine.
    """
    if crop is None or crop.size == 0:
        return OcrCandidate(variant=variant, raw="", normalized="", confidence=0.0)
    results = get_reader().readtext(crop)
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
    """Choose between several variants' readings of the SAME plate crop.

    Selection is by AGREEMENT, not by confidence. Measured on the 25-plate
    labelled corpus (docs/ANPR_ACCURACY.md), picking the highest-confidence of
    seven variant reads gave exact 0.24 / CER 0.3160 and pushed false positives
    UP (0.28 -> 0.40); grouping by text and picking the most-agreed gave exact
    0.28 / CER 0.2814 with false positives at 0.32, and on the cheaper
    `original+sharpen+adaptive` set held false positives at the baseline 0.28
    while improving the wrong-rate among accepted reads from 0.58 to 0.50.

    Highest-confidence selection is also a biased estimator: the maximum of N
    samples is systematically larger than any one of them, so reporting it would
    inflate the recorded confidence of every read and silently loosen the
    downstream gates. This deliberately does not do that.

    Reported confidence is the MEAN over the reads that agreed on the winning
    text — a statement about what the OCR engine said, nothing more. It is not
    raised because several variants agreed; the agreement is reported separately
    as `variants_agreeing` so the gate can weigh it explicitly.

    Ties (equal agreement) break toward a gate-passing read, then toward summed
    confidence. An empty read never beats a non-empty one.
    """
    candidates = tuple(candidates)
    if not candidates:
        return OcrRead(raw="", normalized="", confidence=0.0, variant="", variants_agreeing=0, variant_count=0)

    groups: dict[str, list[OcrCandidate]] = {}
    for candidate in candidates:
        if candidate.normalized:
            groups.setdefault(candidate.normalized, []).append(candidate)

    if not groups:
        # Every variant read nothing usable. The honest answer is the empty read,
        # reported with a real variant name so the record says which image was
        # tried rather than implying none was.
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
    # The representative read is the agreeing member whose own confidence is
    # highest — it is only used for `raw`/`variant` provenance, NOT to set the
    # reported confidence, which stays the mean over all agreeing members.
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
    """Read every supplied `(variant_name, image)` and select between them.

    With the default single-variant configuration this runs exactly one OCR pass
    and returns that read verbatim — the same work, the same confidence and the
    same cost as before this function existed.
    """
    if not variants:
        return OcrRead(raw="", normalized="", confidence=0.0, variant="", variants_agreeing=0, variant_count=0)
    return select_candidate([read_candidate(image, name) for name, image in variants])


def looks_like_plate(normalized: str) -> bool:
    """Whether a normalized read is a well-formed Indian registration.

    Two accepted grammars — the state-coded form (`GJ05AB1234`) and the Bharat
    series (`23BH1234AA`) — and, for the state-coded form, the prefix must be a
    code an Indian state or UT actually issues.

    The state-code check is the cheapest false-positive defence available here.
    The format regex alone accepts `QQ00QQ0000`, so OCR noise that happens to
    land in the right shape becomes a "valid plate", clears the quality gate and
    creates a real Vehicle row. Requiring a prefix that exists tests the read
    against reality rather than against a pattern.
    """
    if not normalized:
        return False
    if BH_SERIES_RE.match(normalized):
        return True
    return bool(PLATE_RE.match(normalized)) and normalized[:2] in INDIAN_STATE_CODES


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


def passes_format_and_confidence(normalized: str, confidence: float) -> bool:
    """Format gate + confidence floor. The two signals that have always gated a
    read; kept as its own function because `select_candidate` needs to ask the
    question without the agreement rule below applying recursively."""
    return bool(normalized) and looks_like_plate(normalized) and confidence >= settings.plate_min_confidence


def passes_anpr_gate(normalized: str, confidence: float) -> bool:
    """The single quality gate (P0-C): a normalized OCR read only becomes a
    Vehicle/Plate correlation record when it looks like a plate AND clears
    the configured confidence floor. Extracted as its own function so it's
    directly unit-testable without a real OCR/frame pipeline.

    Unchanged signature and unchanged behavior. `passes_read_gate` is the
    variant-aware form for callers holding a full `OcrRead`.
    """
    return passes_format_and_confidence(normalized, confidence)


def passes_read_gate(read: OcrRead) -> bool:
    """The quality gate for a structured read, reasoning over BOTH signals.

    Format and confidence must pass exactly as before. When more than one
    preprocessing variant was actually read, the winning text must ALSO have
    been produced by at least `plate_min_variants_agreeing` of them.

    This direction is deliberate and load-bearing: multi-variant reading can
    only ever make the gate STRICTER, never looser. Selecting among several
    reads shifts the reported confidence distribution upward (the agreeing
    subset skews toward easier crops), and without this rule that drift alone
    would push borderline reads over `plate_min_confidence` and over
    `plate_review_confidence_floor` — quietly auto-accepting reads that a
    single-variant pipeline would have sent to a human. A read with high
    confidence but only one variant agreeing is NOT corroborated evidence, and
    is not treated as such.

    With one variant configured (the default) `variant_count` is 1, the
    agreement rule cannot fire, and this is identical to `passes_anpr_gate`.
    """
    if not passes_format_and_confidence(read.normalized, read.confidence):
        return False
    if read.variant_count <= 1:
        return True
    return read.variants_agreeing >= settings.plate_min_variants_agreeing


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


def review_status_for(
    confidence: float, current: str | None = None, corroborated: bool = True,
) -> str:
    """Human-in-the-loop ANPR review (10/10 roadmap P7): whether a Plate
    sighting needs an operator's eyes.

    A gate-passing read that is nonetheless below
    `plate_review_confidence_floor` is real, stored intelligence — never
    discarded — but flagged `pending_review` rather than treated as settled.
    A read that later climbs above the floor (more corroborating OCR passes)
    is auto-promoted back to `auto_accepted`, EXCEPT once a human has already
    acted on it: `corrected`/`rejected` are terminal states a fresh OCR
    frame must never silently overwrite.

    `corroborated=False` means the temporal layer never got enough agreeing
    observations of this plate (see `plate_tracker.has_consensus`). Such a read
    is ALWAYS flagged for review, however confident that single frame was — a
    high-confidence read observed once is not corroborated evidence, and
    treating it as settled is exactly the "one lucky frame becomes a vehicle
    identity" failure this gate exists to prevent. Confidence and corroboration
    are separate signals and neither substitutes for the other.
    """
    if current in _HUMAN_REVIEW_STATES:
        return current
    if not corroborated:
        return "pending_review"
    if confidence >= settings.plate_review_confidence_floor:
        return "auto_accepted"
    return "pending_review"
