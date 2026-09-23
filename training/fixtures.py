"""Synthetic dataset fixtures for testing the tooling.

**No real plate images, no real registrations, no network, no model weights.**
Every record here is invented. Plate strings use the Indian *format* because the
tooling's behaviour depends on the shape, but the registrations themselves are
fabricated and correspond to no vehicle.

These fixtures exist so the entire M1 pipeline — schema, split, QC, leakage
gate, reports, metrics — is testable before any authorized data exists, which is
the whole point of doing M1 while M0 is blocked.
"""
from __future__ import annotations

from schema import PlateRecord

# Fabricated. Deliberately mixes well-formed registrations with legitimately
# unusual ones, so tests can assert that unusual != invalid.
_STATES = ("GJ", "MH", "KA", "KL", "TN", "UP", "RJ", "WB", "DL", "MP")


def make_record(
    vehicle: str,
    frame: int = 0,
    plate_text: str = "GJ05AB1234",
    camera_id: str = "C-001",
    split: str = "",
    **overrides,
) -> PlateRecord:
    """One synthetic record with sane defaults; override any field."""
    fields = dict(
        image_id=f"{camera_id}_{vehicle}_f{frame:03d}",
        camera_id=camera_id,
        timestamp=f"2026-09-12T08:{frame % 60:02d}:00+05:30",
        vehicle_id=vehicle,
        plate_bbox=[100.0, 200.0, 320.0, 260.0],   # 220x60 -> aspect 3.67, glyph ~33px
        plate_text=plate_text,
        split=split,
        image_width=1920,
        image_height=1080,
    )
    fields.update(overrides)
    return PlateRecord(**fields)


def clean_dataset(vehicles: int = 30, frames_per_vehicle: int = 3) -> list[PlateRecord]:
    """A well-formed corpus: several frames per vehicle, several cameras.

    Each frame of a vehicle gets a slightly different bbox, mirroring a real
    tracked vehicle moving through frame — and avoiding the identical-geometry
    near-duplicate warning, which is itself tested separately.
    """
    records: list[PlateRecord] = []
    for index in range(vehicles):
        vehicle = f"vehicle_{index:04d}"
        state = _STATES[index % len(_STATES)]
        plate_text = f"{state}{index % 90 + 10:02d}AB{index % 9000 + 1000:04d}"
        camera = f"C-{index % 4 + 1:03d}"
        for frame in range(frames_per_vehicle):
            records.append(make_record(
                vehicle, frame, plate_text, camera,
                plate_bbox=[100.0 + frame * 12, 200.0 + frame * 4,
                            320.0 + frame * 12, 260.0 + frame * 4],
            ))
    return records


def dataset_with_leakage() -> list[PlateRecord]:
    """One vehicle deliberately placed in both train and test — the fatal case
    the CI gate must catch."""
    return [
        make_record("vehicle_001", 0, "GJ05AB1234", split="train"),
        make_record("vehicle_001", 1, "GJ05AB1234", split="test"),
        make_record("vehicle_002", 0, "MH12CD5678", split="test"),
    ]


def dataset_without_leakage() -> list[PlateRecord]:
    """The control case: distinct vehicles in distinct splits. Must pass."""
    return [
        make_record("vehicle_001", 0, "GJ05AB1234", split="train"),
        make_record("vehicle_001", 1, "GJ05AB1234", split="train"),
        make_record("vehicle_002", 0, "MH12CD5678", split="test"),
    ]


def malformed_records() -> dict[str, dict]:
    """Raw dicts that must fail SCHEMA parsing (structurally unusable)."""
    base = make_record("vehicle_001").__dict__
    return {
        "missing_required_field": {k: v for k, v in base.items() if k != "plate_text"},
        "bbox_wrong_length": {**base, "plate_bbox": [1.0, 2.0, 3.0]},
        "bbox_non_numeric": {**base, "plate_bbox": [1.0, 2.0, "x", 4.0]},
        "non_string_text": {**base, "plate_text": 1234},
        "non_string_vehicle": {**base, "vehicle_id": None},
    }


def qc_problem_records() -> dict[str, PlateRecord]:
    """Records that PARSE but that QC must flag, keyed by the expected code.

    These are the semantic problems — a record can be structurally fine and
    still be unusable, or fine and merely unusual. Keeping the two apart is the
    design this exercises.
    """
    return {
        "degenerate_bbox": make_record("v_deg", plate_bbox=[100.0, 200.0, 100.0, 200.0]),
        "negative_bbox": make_record("v_neg", plate_bbox=[-10.0, 200.0, 320.0, 260.0]),
        "bbox_outside_image": make_record("v_out", plate_bbox=[100.0, 200.0, 5000.0, 260.0]),
        "extreme_aspect": make_record("v_asp", plate_bbox=[0.0, 0.0, 1000.0, 20.0]),
        "tiny_plate": make_record("v_tiny", plate_bbox=[0.0, 0.0, 30.0, 8.0]),
        "illegal_characters": make_record("v_ill", plate_text="GJ05-AB 1234"),
        "empty_plate_text": make_record("v_empty", plate_text="", quality="clear"),
        "unusual_length": make_record("v_len", plate_text="GJ0"),
        # Legitimately unusual: a real partial plate. Must be WARNING, not ERROR.
        "unusual_registration_format": make_record("v_odd", plate_text="KL34F"),
        "invalid_timestamp": make_record("v_ts", timestamp="not-a-timestamp"),
        "unknown_split": make_record("v_split", split="holdout"),
        "unknown_quality": make_record("v_q", quality="pristine"),
        "missing_vehicle_id": make_record("", plate_text="GJ05AB1234"),
        "uncertain_label_in_test": make_record(
            "v_unc", split="test", label_confidence="uncertain"),
    }


def rare_character_dataset() -> list[PlateRecord]:
    """A corpus whose plates deliberately omit `I O Q V Z` — reproducing the
    exact gap measured in the current n=25 benchmark corpus, so the coverage
    report can be asserted to surface it."""
    return [
        make_record(f"vehicle_{i:03d}", 0, text, split="train")
        for i, text in enumerate(("GJ05AB1234", "MH12CD5678", "KA03EF9012", "TN07GH3456"))
    ]


def size_bucket_dataset() -> list[PlateRecord]:
    """One record per plate-size bucket, for testing the size report.

    Heights are chosen so that `glyph_px = height * 0.55` lands inside each
    bucket: 30 -> 16.5px, 90 -> 49.5px... and so on.
    """
    heights = {"<20px": 30, "20-30px": 50, "30-50px": 80, "50-75px": 120,
               "75-100px": 170, ">100px": 400}
    return [
        make_record(f"vehicle_sz_{name}", 0, "GJ05AB1234", split="train",
                    plate_bbox=[0.0, 0.0, height * 4.0, float(height)])
        for name, height in heights.items()
    ]
