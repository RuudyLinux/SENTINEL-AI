"""Canonical ANPR training-dataset record, and its validation.

One record per annotated PLATE OBSERVATION (not per frame, not per vehicle).
Serialized as JSON Lines: one JSON object per line, streamable, greppable, and
a single corrupt line invalidates one record rather than the file.

The one distinction this module exists to enforce
-------------------------------------------------
**Annotation validity is not vehicle-registration validity.**

`GJ05AB1234` and `KL498262` are both perfectly valid ANNOTATIONS — a transcriber
wrote down what was visibly on the plate. Only the second fails
`anpr.looks_like_plate`, because it is not a well-formed Indian registration.

This schema validates the first and deliberately says nothing about the second.
Rejecting labels that fail the production regex would delete exactly the
non-standard plates that Phase 2 measured as 5 of 10 recognition failures, and
would train a recogniser only on plates the system already handles — making
every subsequent accuracy figure circular. Registration plausibility is reported
by the QC layer as an INFO/WARNING signal for human review, never as a schema
error.

Deliberately stdlib-only. This tooling must run without torch, without OpenCV
and without the backend application, so that dataset work is never gated on the
inference stack.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

# The dataset's character policy. Transcriptions are uppercase alphanumerics
# only; separators, spaces and punctuation are stripped at annotation time so
# that "GJ 05 AB 1234", "GJ-05-AB-1234" and "GJ05AB1234" cannot become three
# different labels for one plate.
ALLOWED_PLATE_CHARS = re.compile(r"^[A-Z0-9]*$")

VALID_SPLITS = ("train", "val", "test")
VALID_QUALITY = ("clear", "marginal", "unreadable")
VALID_VISIBILITY = ("full", "partial", "occluded")
VALID_LABEL_CONFIDENCE = ("certain", "uncertain")

# Plate-size buckets, keyed on CHARACTER height in pixels rather than plate
# width: glyph height is what determines legibility and is what a recogniser's
# input resize is defined against. Defaults from docs/ANPR_M0_DATA_ACQUISITION.md
# §6; `bucket_for` accepts an override so measured data can move them later
# without a code change.
DEFAULT_SIZE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    # `<20px` is split out from `20-30` deliberately: below the recogniser's
    # input height a correct answer may be information-theoretically
    # impossible, and a model should be allowed to decline there rather than be
    # scored as though it failed.
    ("<20px", 0.0, 20.0),
    ("20-30px", 20.0, 30.0),
    ("30-50px", 30.0, 50.0),
    ("50-75px", 50.0, 75.0),
    ("75-100px", 75.0, 100.0),
    (">100px", 100.0, float("inf")),
)


class SchemaError(Exception):
    """A record could not be parsed at all (malformed JSON, wrong type)."""


@dataclass
class PlateRecord:
    """One annotated plate observation.

    Identifier hygiene, which is a privacy requirement rather than a style
    preference (docs/ANPR_M0_DATA_ACQUISITION.md §3):

    - `vehicle_id` is an opaque DATASET-LOCAL id, never the registration and
      never an operational track id. It is the split key.
    - `camera_id` is an opaque deployment code (`C-014`), never a URI and never
      a credential.
    - the registration appears exactly once, in `plate_text`.
    """

    image_id: str
    camera_id: str
    timestamp: str
    vehicle_id: str
    plate_bbox: list[float]
    plate_text: str
    split: str = ""
    quality: str = "clear"
    visibility: str = "full"
    label_confidence: str = "certain"

    # Optional context. Absent rather than guessed when it was not recorded.
    image_width: int | None = None
    image_height: int | None = None
    glyph_px: float | None = None
    plate_quad: list[list[float]] | None = None
    vehicle_category: str = ""
    plate_style: str = ""
    plate_face: str = ""
    plate_row_layout: str = ""
    conditions: dict[str, Any] = field(default_factory=dict)
    measured: dict[str, Any] = field(default_factory=dict)

    # ---- derived helpers -------------------------------------------------

    @property
    def bbox_width(self) -> float:
        return self.plate_bbox[2] - self.plate_bbox[0]

    @property
    def bbox_height(self) -> float:
        return self.plate_bbox[3] - self.plate_bbox[1]

    @property
    def aspect_ratio(self) -> float:
        return self.bbox_width / self.bbox_height if self.bbox_height else 0.0

    def effective_glyph_px(self) -> float:
        """Character height in pixels — measured when annotated, else estimated."""
        if self.glyph_px is not None:
            return float(self.glyph_px)
        return estimate_glyph_px(self.bbox_height, self.plate_row_layout)

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v not in ("", None, {}, [])},
                          separators=(",", ":"), sort_keys=True)


# Fraction of a plate's box height occupied by one character band.
#
# A single-row Indian plate is ~500x120mm with ~65mm characters, so the glyph
# band is roughly 55% of the plate height. A two-row plate stacks TWO character
# bands into the same box, so each band is about half that.
#
# Getting the two-row case wrong is not cosmetic: motorcycles are almost always
# two-row and are the smallest plates on the road, so treating them as
# single-row would place the worst-case class one or two size buckets too high
# and make CCTV legibility look better than it is.
GLYPH_HEIGHT_FRACTION_SINGLE_ROW = 0.55
GLYPH_HEIGHT_FRACTION_DOUBLE_ROW = 0.275


def estimate_glyph_px(bbox_height: float, row_layout: str = "single") -> float:
    """Estimate character height from a plate box height.

    An ESTIMATE, and labelled as one wherever it is reported. A real
    character-band measurement from an annotation always wins over this.
    """
    fraction = (
        GLYPH_HEIGHT_FRACTION_DOUBLE_ROW if row_layout == "double"
        else GLYPH_HEIGHT_FRACTION_SINGLE_ROW
    )
    return float(bbox_height) * fraction


def bucket_for(glyph_px: float, buckets=DEFAULT_SIZE_BUCKETS) -> str:
    for name, low, high in buckets:
        if low <= glyph_px < high:
            return name
    return buckets[-1][0]


# ---- parsing ------------------------------------------------------------

_REQUIRED = ("image_id", "camera_id", "timestamp", "vehicle_id", "plate_bbox", "plate_text")


def record_from_dict(raw: dict[str, Any]) -> PlateRecord:
    """Build a record, raising SchemaError on anything structurally unusable.

    Only STRUCTURAL problems raise here — a missing field, a bbox that is not
    four numbers, a non-string label. Semantic problems (an impossible bbox, an
    unusual plate string, a bad split value) are reported by `qc.checks`, which
    can grade them by severity and route them to a human. Raising on those would
    abort a whole corpus load over one reviewable record.
    """
    if not isinstance(raw, dict):
        raise SchemaError(f"record is {type(raw).__name__}, expected object")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise SchemaError(f"missing required field(s): {', '.join(missing)}")

    bbox = raw["plate_bbox"]
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise SchemaError("plate_bbox must be [x1, y1, x2, y2]")
    try:
        bbox = [float(v) for v in bbox]
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"plate_bbox contains a non-numeric value: {exc}") from exc

    for key in ("image_id", "camera_id", "vehicle_id", "timestamp", "plate_text"):
        if not isinstance(raw[key], str):
            raise SchemaError(f"{key} must be a string, got {type(raw[key]).__name__}")

    known = {f for f in PlateRecord.__dataclass_fields__}
    return PlateRecord(**{k: v for k, v in {**raw, "plate_bbox": bbox}.items() if k in known})


def load_jsonl(path: str | Path) -> tuple[list[PlateRecord], list[tuple[int, str]]]:
    """Read a JSONL dataset.

    Returns `(records, parse_errors)` where each error is `(line_number,
    message)`. A malformed line never aborts the load: the QC report is the
    place a corpus is judged, and a single bad line should be reportable
    alongside everything else rather than hiding the other 1,999 problems.
    """
    records: list[PlateRecord] = []
    errors: list[tuple[int, str]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            records.append(record_from_dict(json.loads(line)))
        except json.JSONDecodeError as exc:
            errors.append((number, f"malformed JSON: {exc.msg}"))
        except SchemaError as exc:
            errors.append((number, str(exc)))
    return records, errors


def write_jsonl(records: list[PlateRecord], path: str | Path) -> None:
    Path(path).write_text(
        "".join(record.to_json() + "\n" for record in records), encoding="utf-8",
    )


def iter_jsonl(path: str | Path) -> Iterator[PlateRecord]:
    """Streaming read for corpora too large to hold in memory. Skips
    unparseable lines silently — use `load_jsonl` when errors matter."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                yield record_from_dict(json.loads(line))
            except (json.JSONDecodeError, SchemaError):
                continue


def parse_timestamp(value: str) -> datetime | None:
    """ISO-8601, tolerating a trailing `Z`. Returns None when unparseable so
    the caller can report it rather than crash."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
