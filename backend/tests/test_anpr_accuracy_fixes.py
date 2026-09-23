"""Two accuracy defects found by the FIRST real ANPR measurement
(docs/ANPR_ACCURACY.md, 25 labelled Indian plates) — not by inspection.

1. **Two-row plates were scrambled.** OCR fragments were ordered by left-x
   alone, correct only for single-row plates. India uses two-row plates
   widely, and on those a pure x-sort interleaves the rows: the real labelled
   plate `KL07BX7197` was read as `INDBX7197KL07`.

2. **Localization reduced accuracy.** It was documented as "the single largest
   accuracy lever"; measured, it cut exact match from 0.16 to 0.04 and raised
   CER from 0.524 to 0.636, because the localizer sometimes returns a
   sub-region and OCR then reads nothing. Fixed by falling back to the whole
   crop when the localized read fails the gate, keeping whichever is better.
"""
import numpy as np
import pytest

from app.config import settings
from app.pipeline.anpr import better_read, order_fragments, read_plate


def _fragment(text: str, x: float, y: float, width: float = 60.0, height: float = 20.0, conf: float = 0.9):
    """An EasyOCR-shaped result: (4-point bbox, text, confidence)."""
    box = [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
    return (box, text, conf)


class TestTwoRowPlateOrdering:
    def test_a_two_row_plate_is_read_top_row_then_bottom_row(self):
        """The exact measured failure: a two-row plate whose bottom row starts
        further LEFT than the top row. A pure x-sort emits the bottom row
        first; row-aware ordering must not."""
        fragments = [
            _fragment("BX7197", x=10, y=60),   # bottom row, leftmost overall
            _fragment("KL07", x=30, y=10),     # top row
        ]
        ordered = order_fragments(fragments)
        assert [f[1] for f in ordered] == ["KL07", "BX7197"]

    def test_the_measured_ind_marker_case_no_longer_interleaves(self):
        """`KL07BX7197` was read as `INDBX7197KL07`. The country marker and the
        two rows must come out in reading order, so downstream normalization
        has a chance of recovering the plate."""
        fragments = [
            _fragment("BX7197", x=5, y=70),
            _fragment("IND", x=8, y=12, width=30),
            _fragment("KL07", x=45, y=10),
        ]
        assert [f[1] for f in order_fragments(fragments)] == ["IND", "KL07", "BX7197"]

    def test_a_single_row_plate_is_still_plain_left_to_right(self):
        """Regression guard: the fix must not change single-row behavior,
        which was already correct."""
        fragments = [
            _fragment("1234", x=200, y=10),
            _fragment("GJ05", x=10, y=12),
            _fragment("AB", x=110, y=11),
        ]
        assert [f[1] for f in order_fragments(fragments)] == ["GJ05", "AB", "1234"]

    def test_rows_are_split_by_relative_glyph_height_not_a_fixed_pixel_gap(self):
        """Crops arrive at wildly different scales. A large-scale two-row plate
        (tall glyphs, large row gap) must still resolve to two rows."""
        fragments = [
            _fragment("BX7197", x=20, y=600, width=600, height=200),
            _fragment("KL07", x=60, y=100, width=400, height=200),
        ]
        assert [f[1] for f in order_fragments(fragments)] == ["KL07", "BX7197"]

    def test_empty_input_is_handled(self):
        assert order_fragments([]) == []


class TestBetterRead:
    def test_a_gate_passing_read_beats_a_failing_one(self):
        localized = ("", "", 0.0)                      # localization miss -> nothing read
        whole = ("GJ 05 AB 1234", "GJ05AB1234", 0.81)
        assert better_read(localized, whole) is whole

    def test_a_gate_passing_read_is_not_replaced_by_a_more_confident_failure(self):
        """Confidence alone must not win: a high-confidence read that is not
        plate-shaped is exactly the "confident garbage" the quality gate
        exists to reject."""
        good = ("GJ05AB1234", "GJ05AB1234", 0.62)
        confident_junk = ("SUCUN", "SUCUN", 0.97)
        assert better_read(good, confident_junk) is good

    def test_between_two_passing_reads_the_more_confident_wins(self):
        low = ("GJ05AB1234", "GJ05AB1234", 0.55)
        high = ("GJ05AB1234", "GJ05AB1234", 0.88)
        assert better_read(low, high) is high
        assert better_read(high, low) is high

    def test_a_non_empty_read_beats_an_empty_one_even_if_both_fail_the_gate(self):
        """A localization miss must never turn a real (if imperfect) read into
        nothing — that was the measured `GJ01DY6855 -> <empty>` failure."""
        empty = ("", "", 0.0)
        partial = ("GJ00AG855", "GJ00AG855", 0.31)
        assert better_read(empty, partial) is partial

    def test_no_fallback_returns_the_original_unchanged(self):
        original = ("GJ05AB1234", "GJ05AB1234", 0.7)
        assert better_read(original, None) is original

    def test_both_empty_is_stable(self):
        first = ("", "", 0.0)
        assert better_read(first, ("", "", 0.0)) is first


class TestPlateExtraction:
    """`extract_plate` pulls a registration out of a read carrying extra
    characters. Both inputs below are REAL measured OCR outputs."""

    @pytest.mark.parametrize("read,expected", [
        ("INDKL07BX7197", "KL07BX7197"),    # "IND" country marker, measured
        ("SUCUNDL3CD1210", "DL3CD1210"),    # surrounding sticker text, measured
        ("KA01AJ75338E", "KA01AJ7533"),     # trailing noise, measured
    ])
    def test_a_registration_is_recovered_from_surrounding_text(self, read, expected):
        from app.pipeline.anpr import extract_plate
        assert extract_plate(read) == expected

    def test_a_read_that_already_parses_is_returned_untouched(self):
        from app.pipeline.anpr import extract_plate
        assert extract_plate("GJ05AB1234") == "GJ05AB1234"

    @pytest.mark.parametrize("noise", ["QQQQQQQQQQQQ", "SUCUL", "", "12345", "ZZZZ"])
    def test_a_string_with_no_valid_plate_is_never_turned_into_one(self, noise):
        """The critical safety property: this may only ever recover a plate
        that is genuinely present, never manufacture one out of noise — the
        same standard disambiguate_plate is held to."""
        from app.pipeline.anpr import extract_plate
        assert extract_plate(noise) == noise

    def test_the_longest_valid_registration_wins(self):
        """A shorter match is usually a truncation of the real plate."""
        from app.pipeline.anpr import extract_plate
        assert extract_plate("XKL07BX7197") == "KL07BX7197"

    def test_extraction_never_reorders_or_invents_characters(self):
        """Whatever comes back must be a contiguous run of the input."""
        from app.pipeline.anpr import extract_plate
        for read in ("INDKL07BX7197", "SUCUNDL3CD1210", "KA01AJ75338E"):
            assert extract_plate(read) in read


class TestReadPlateStillHandlesDegenerateInput:
    @pytest.mark.parametrize("crop", [None, np.zeros((0, 0, 3), dtype=np.uint8)])
    def test_empty_crops_return_an_honest_empty_read(self, crop):
        assert read_plate(crop) == ("", "", 0.0)
