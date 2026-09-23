"""Deterministic dataset quality-control checks.

Three severities, and the distinction between them is the whole design:

| severity  | meaning                                              | action |
|-----------|------------------------------------------------------|--------|
| `ERROR`   | structurally invalid or fatal to a valid experiment   | blocks the dataset |
| `WARNING` | suspicious; a human must look                        | flagged for review |
| `INFO`    | notable, not a defect                                 | recorded |

**Unusual plate strings are WARNING, never ERROR.** Auto-rejecting labels that
fail the Indian registration format would delete exactly the non-standard plates
Phase 2 measured as 5 of 10 recognition failures (handwritten, italic,
bolt-obscured), and would quietly restrict the corpus to plates the system
already reads — inflating every subsequent accuracy number. The two known
non-conforming labels in the existing benchmark corpus (`KL34F`, `KL498262`) are
real plates on real vehicles and belong in the data.

Stdlib only: no numpy, no OpenCV, no backend imports. These checks must be
runnable on an annotation workstation that has none of the inference stack.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from schema import (
    ALLOWED_PLATE_CHARS, PlateRecord, VALID_LABEL_CONFIDENCE, VALID_QUALITY,
    VALID_SPLITS, VALID_VISIBILITY, parse_timestamp,
)
from split import (
    SplitConfig, find_identity_leakage, find_image_leakage, find_repeated_plate_text,
)

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"

# Thresholds. Deliberately loose — these are review triggers, not truth.
MIN_PLATE_TEXT_LEN = 4
MAX_PLATE_TEXT_LEN = 13
MIN_GLYPH_PX = 8.0
MIN_ASPECT, MAX_ASPECT = 1.2, 10.0

# A well-formed Indian registration, used ONLY to raise a review flag. This is
# intentionally a copy of the production pattern rather than an import: the
# training tooling must not depend on the backend application, and — more
# importantly — if the production grammar is ever tightened, a corpus must not
# silently start failing QC because of a change made for a different purpose.
_PLAUSIBLE_REGISTRATION = re.compile(r"^([A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4}|\d{2}BH\d{4}[A-Z]{1,2})$")


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    record_id: str = ""

    def __str__(self) -> str:
        where = f" [{self.record_id}]" if self.record_id else ""
        return f"{self.severity}: {self.code}{where}: {self.message}"


def _record_id(record: PlateRecord) -> str:
    return f"{record.image_id}/{record.vehicle_id}"


def check_record(record: PlateRecord) -> list[Finding]:
    """Per-record checks. Structural problems are ERROR; judgement calls are
    WARNING."""
    findings: list[Finding] = []
    rid = _record_id(record)

    def add(severity: str, code: str, message: str) -> None:
        findings.append(Finding(severity, code, message, rid))

    # --- identifiers ---
    if not record.image_id.strip():
        add(ERROR, "missing_image_id", "image_id is empty")
    if not record.vehicle_id.strip():
        add(ERROR, "missing_vehicle_id",
            "vehicle_id is empty — the record cannot be assigned to a split safely")
    if not record.camera_id.strip():
        add(WARNING, "missing_camera_id",
            "camera_id is empty — per-camera evaluation and held-out-camera splits are impossible")

    # --- timestamp ---
    if not record.timestamp.strip():
        add(WARNING, "missing_timestamp", "timestamp is empty")
    elif parse_timestamp(record.timestamp) is None:
        add(ERROR, "invalid_timestamp", f"timestamp is not ISO-8601: {record.timestamp!r}")

    # --- bbox ---
    x1, y1, x2, y2 = record.plate_bbox
    if x2 <= x1 or y2 <= y1:
        add(ERROR, "degenerate_bbox", f"plate_bbox has non-positive size: {record.plate_bbox}")
    else:
        if min(x1, y1) < 0:
            add(ERROR, "negative_bbox", f"plate_bbox has negative coordinates: {record.plate_bbox}")
        if record.image_width is not None and record.image_height is not None:
            if x2 > record.image_width or y2 > record.image_height:
                add(ERROR, "bbox_outside_image",
                    f"plate_bbox {record.plate_bbox} exceeds image "
                    f"{record.image_width}x{record.image_height}")
        aspect = record.aspect_ratio
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            add(WARNING, "extreme_aspect",
                f"plate aspect {aspect:.2f} outside {MIN_ASPECT}-{MAX_ASPECT} — "
                "a mis-drawn box, or a genuinely unusual plate")
        if record.effective_glyph_px() < MIN_GLYPH_PX:
            add(WARNING, "tiny_plate",
                f"estimated glyph height {record.effective_glyph_px():.1f}px is below "
                f"{MIN_GLYPH_PX}px — likely unreadable, keep only if deliberately sampled")

    # --- plate text ---
    text = record.plate_text
    if not ALLOWED_PLATE_CHARS.match(text):
        illegal = sorted({c for c in text if not c.isalnum() or c.islower()})
        add(ERROR, "illegal_characters",
            f"plate_text contains characters outside [A-Z0-9]: {illegal}")
    if not text:
        if record.quality != "unreadable":
            add(WARNING, "empty_plate_text",
                "plate_text is empty but quality is not 'unreadable' — either transcribe it "
                "or mark it unreadable")
        else:
            add(INFO, "unreadable_plate",
                "deliberately unlabelled unreadable plate — trains the detector, "
                "measures honest rejection")
    else:
        if not (MIN_PLATE_TEXT_LEN <= len(text) <= MAX_PLATE_TEXT_LEN):
            add(WARNING, "unusual_length",
                f"plate_text length {len(text)} outside {MIN_PLATE_TEXT_LEN}-{MAX_PLATE_TEXT_LEN}")
        elif not _PLAUSIBLE_REGISTRATION.match(text):
            # WARNING, never ERROR. See the module docstring.
            add(WARNING, "unusual_registration_format",
                f"{text!r} is a valid ANNOTATION but not a well-formed Indian registration — "
                "flag for human review; do NOT auto-correct or drop")

    # --- enumerations ---
    if record.split and record.split not in VALID_SPLITS:
        add(ERROR, "unknown_split", f"split {record.split!r} not in {VALID_SPLITS}")
    if record.quality not in VALID_QUALITY:
        add(ERROR, "unknown_quality", f"quality {record.quality!r} not in {VALID_QUALITY}")
    if record.visibility not in VALID_VISIBILITY:
        add(ERROR, "unknown_visibility", f"visibility {record.visibility!r} not in {VALID_VISIBILITY}")
    if record.label_confidence not in VALID_LABEL_CONFIDENCE:
        add(ERROR, "unknown_label_confidence",
            f"label_confidence {record.label_confidence!r} not in {VALID_LABEL_CONFIDENCE}")

    # --- test-set hygiene ---
    if record.split == "test" and record.label_confidence == "uncertain":
        add(ERROR, "uncertain_label_in_test",
            "an uncertain label cannot be ground truth — allowed in train, never in test")
    if record.split == "test" and record.visibility == "partial":
        add(WARNING, "partial_plate_in_test",
            "a partially visible plate in the test set makes exact match unachievable by "
            "construction; usually belongs in train")

    return findings


def check_dataset(
    records: list[PlateRecord], config: SplitConfig | None = None,
) -> list[Finding]:
    """Corpus-level checks: duplicates, leakage, coverage."""
    config = config or SplitConfig()
    findings: list[Finding] = []

    # --- duplicate identifiers ---
    image_counts = Counter(r.image_id for r in records)
    for image_id, count in sorted(image_counts.items()):
        if count > 1:
            # Not automatically wrong: one frame can hold several vehicles. It
            # IS wrong if the duplicates are the same vehicle.
            vehicles = {r.vehicle_id for r in records if r.image_id == image_id}
            if len(vehicles) == 1:
                findings.append(Finding(
                    ERROR, "duplicate_record",
                    f"image_id {image_id!r} appears {count} times for the same vehicle "
                    f"{vehicles.pop()!r} — duplicate record"))
            else:
                findings.append(Finding(
                    INFO, "multi_vehicle_frame",
                    f"image_id {image_id!r} contains {len(vehicles)} annotated vehicles"))

    exact = Counter((r.image_id, r.vehicle_id, tuple(r.plate_bbox)) for r in records)
    for key, count in sorted(exact.items(), key=lambda kv: str(kv[0])):
        if count > 1:
            findings.append(Finding(
                ERROR, "duplicate_annotation",
                f"{count} identical annotations for image {key[0]!r} vehicle {key[1]!r}"))

    # --- near-duplicate frames within one vehicle ---
    # Without pixels, "near-duplicate" is approximated by identical geometry at
    # the same camera for the same vehicle: the same box, on the same camera, in
    # several records is almost always consecutive frames of a stationary
    # vehicle. Real perceptual hashing runs at crop-export time, where the
    # images exist; this is the manifest-level tripwire.
    geometry: dict[tuple[str, str, tuple], list[str]] = defaultdict(list)
    for record in records:
        geometry[(record.vehicle_id, record.camera_id, tuple(record.plate_bbox))].append(record.image_id)
    for (vehicle, camera, _box), image_ids in sorted(geometry.items(), key=lambda kv: str(kv[0])):
        if len(image_ids) > 1:
            findings.append(Finding(
                WARNING, "suspicious_duplicate_frames",
                f"vehicle {vehicle!r} on camera {camera!r} has {len(image_ids)} records with an "
                "identical bbox — likely near-duplicate frames; cap them so one stationary "
                "vehicle does not dominate training"))

    # --- leakage (the fatal class) ---
    for identity, splits in sorted(find_identity_leakage(records, config).items()):
        findings.append(Finding(
            ERROR, "cross_split_identity_leakage",
            f"vehicle_id={identity} appears in: {', '.join(sorted(splits))}"))

    for image_id, splits in sorted(find_image_leakage(records).items()):
        findings.append(Finding(
            ERROR, "cross_split_image_leakage",
            f"image_id={image_id} appears in: {', '.join(sorted(splits))} — "
            "the same pixels are in more than one split"))

    for text, vehicle_ids in sorted(find_repeated_plate_text(records).items()):
        findings.append(Finding(
            WARNING, "repeated_plate_text",
            f"plate_text {text!r} appears under {len(vehicle_ids)} vehicle ids "
            f"({', '.join(sorted(vehicle_ids))}) — same vehicle labelled twice, or a genuine "
            "coincidence; a human must decide"))

    # --- unassigned ---
    unassigned = [r for r in records if not r.split]
    if unassigned:
        findings.append(Finding(
            WARNING, "unassigned_records",
            f"{len(unassigned)} record(s) have no split — they will not be used"))

    if not records:
        findings.append(Finding(ERROR, "empty_dataset", "dataset contains no records"))

    return findings


def run_all(
    records: list[PlateRecord],
    parse_errors: list[tuple[int, str]] | None = None,
    config: SplitConfig | None = None,
) -> list[Finding]:
    findings: list[Finding] = [
        Finding(ERROR, "malformed_line", message, f"line {number}")
        for number, message in (parse_errors or [])
    ]
    for record in records:
        findings.extend(check_record(record))
    findings.extend(check_dataset(records, config))
    return findings


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == ERROR]


def has_blocking_errors(findings: list[Finding]) -> bool:
    return any(f.severity == ERROR for f in findings)
