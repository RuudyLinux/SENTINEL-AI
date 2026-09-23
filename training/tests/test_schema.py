"""Dataset schema validation.

The central assertion in this file: **annotation validity is not
vehicle-registration validity.** A label can be unusual without being invalid,
and the schema must not confuse the two.
"""
import json

import pytest

from fixtures import make_record, malformed_records
from schema import (
    PlateRecord, SchemaError, bucket_for, load_jsonl, parse_timestamp,
    record_from_dict, write_jsonl,
)


class TestRequiredFields:
    @pytest.mark.parametrize("case", sorted(malformed_records()))
    def test_structurally_broken_records_are_rejected(self, case):
        with pytest.raises(SchemaError):
            record_from_dict(malformed_records()[case])

    def test_a_well_formed_record_parses(self):
        record = record_from_dict(make_record("vehicle_001").__dict__)
        assert record.vehicle_id == "vehicle_001"
        assert record.plate_text == "GJ05AB1234"

    def test_a_non_object_is_refused(self):
        with pytest.raises(SchemaError):
            record_from_dict(["not", "an", "object"])

    def test_unknown_fields_are_ignored_not_fatal(self):
        """A future annotation tool adding a field must not break older tooling."""
        raw = {**make_record("vehicle_001").__dict__, "annotator_notes": "looks fine"}
        assert record_from_dict(raw).vehicle_id == "vehicle_001"


class TestAnnotationValidityIsNotRegistrationValidity:
    """The distinction this schema exists to enforce."""

    @pytest.mark.parametrize("text", [
        "KL34F",        # a real partial plate from the benchmark corpus
        "KL498262",     # a real handwritten plate that is not a valid registration
        "23BH1234AA",   # BH series — valid, but not the state-code grammar
        "QQ00QQ0000",   # an impossible registration, but a legal ANNOTATION
        "",             # an unreadable plate, deliberately unlabelled
    ])
    def test_unusual_plate_text_still_parses(self, text):
        """These are all things a transcriber could legitimately write down. The
        schema records what was seen; whether it is a valid Indian registration
        is a separate, downstream question."""
        assert record_from_dict(make_record("v", plate_text=text).__dict__).plate_text == text

    def test_the_schema_compiles_exactly_one_regex_the_character_policy(self):
        """Guards against someone 'helpfully' adding a registration-format check
        later: rejecting non-conforming labels would delete the non-standard
        plates Phase 2 measured as 5 of 10 recognition failures.

        Inspected via AST so the assertion is about executable code, not about
        prose — the module docstring legitimately discusses the distinction.
        """
        import ast
        import schema

        tree = ast.parse(open(schema.__file__, encoding="utf-8").read())
        compiled = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
        ]
        assert len(compiled) == 1, "the schema must compile only the character policy"
        assert ast.literal_eval(compiled[0].args[0]) == r"^[A-Z0-9]*$"


class TestBbox:
    def test_bbox_is_coerced_to_floats(self):
        record = record_from_dict(make_record("v", plate_bbox=[1, 2, 3, 4]).__dict__)
        assert record.plate_bbox == [1.0, 2.0, 3.0, 4.0]
        assert all(isinstance(v, float) for v in record.plate_bbox)

    def test_derived_geometry(self):
        record = make_record("v", plate_bbox=[10.0, 20.0, 210.0, 70.0])
        assert record.bbox_width == 200.0
        assert record.bbox_height == 50.0
        assert record.aspect_ratio == 4.0


class TestGlyphHeight:
    def test_explicit_glyph_px_wins(self):
        assert make_record("v", glyph_px=42.0).effective_glyph_px() == 42.0

    def test_single_row_estimate(self):
        record = make_record("v", plate_bbox=[0.0, 0.0, 400.0, 100.0])
        assert record.effective_glyph_px() == pytest.approx(55.0)

    def test_two_row_plates_estimate_half_the_band(self):
        """A double-row plate stacks two character bands in the same box, so the
        glyphs are about half the height a single-row estimate would give.
        Getting this wrong would put motorcycles in the wrong size bucket — the
        bucket that matters most for CCTV."""
        single = make_record("v", plate_bbox=[0.0, 0.0, 400.0, 100.0])
        double = make_record("v", plate_bbox=[0.0, 0.0, 400.0, 100.0], plate_row_layout="double")
        assert double.effective_glyph_px() == pytest.approx(single.effective_glyph_px() / 2)


class TestSizeBuckets:
    @pytest.mark.parametrize("glyph_px,expected", [
        (5.0, "<20px"), (19.9, "<20px"), (20.0, "20-30px"), (29.9, "20-30px"),
        (30.0, "30-50px"), (49.9, "30-50px"), (50.0, "50-75px"), (74.9, "50-75px"),
        (75.0, "75-100px"), (99.9, "75-100px"), (100.0, ">100px"), (600.0, ">100px"),
    ])
    def test_bucket_boundaries(self, glyph_px, expected):
        assert bucket_for(glyph_px) == expected


class TestTimestamps:
    @pytest.mark.parametrize("value", [
        "2026-09-12T08:14:13+05:30", "2026-09-12T08:14:13Z", "2026-09-12T08:14:13.240+05:30",
    ])
    def test_valid_timestamps(self, value):
        assert parse_timestamp(value) is not None

    @pytest.mark.parametrize("value", ["", "not-a-timestamp", "12/09/2026", None])
    def test_invalid_timestamps_return_none_rather_than_raising(self, value):
        assert parse_timestamp(value) is None


class TestJsonlRoundTrip:
    def test_write_then_read_preserves_records(self, tmp_path):
        records = [make_record(f"vehicle_{i:03d}", split="train") for i in range(5)]
        path = tmp_path / "records.jsonl"
        write_jsonl(records, path)
        loaded, errors = load_jsonl(path)
        assert errors == []
        assert [r.vehicle_id for r in loaded] == [r.vehicle_id for r in records]
        assert [r.plate_bbox for r in loaded] == [r.plate_bbox for r in records]

    def test_a_malformed_line_is_reported_not_fatal(self, tmp_path):
        """One bad line must not hide the other 1,999 records' problems."""
        path = tmp_path / "records.jsonl"
        path.write_text(
            make_record("vehicle_001").to_json() + "\n"
            + "{not valid json\n"
            + make_record("vehicle_002").to_json() + "\n",
            encoding="utf-8",
        )
        records, errors = load_jsonl(path)
        assert len(records) == 2
        assert len(errors) == 1 and errors[0][0] == 2

    def test_blank_and_comment_lines_are_skipped(self, tmp_path):
        path = tmp_path / "records.jsonl"
        path.write_text(
            "# a comment\n\n" + make_record("vehicle_001").to_json() + "\n", encoding="utf-8",
        )
        records, errors = load_jsonl(path)
        assert len(records) == 1 and errors == []

    def test_serialization_omits_empty_optional_fields(self):
        """Keeps the manifest readable and diffable, and avoids writing nulls
        that later read as 'recorded as unknown' rather than 'not recorded'."""
        payload = json.loads(make_record("vehicle_001").to_json())
        assert "plate_quad" not in payload
        assert payload["vehicle_id"] == "vehicle_001"


class TestPrivacyOfIdentifiers:
    def test_the_record_carries_no_uri_or_credential_field(self):
        """camera_id is an opaque deployment code by design. A source URI in the
        manifest would put camera credentials into the dataset."""
        fields = set(PlateRecord.__dataclass_fields__)
        for forbidden in ("source_uri", "rtsp_url", "password", "username", "camera_url"):
            assert forbidden not in fields
