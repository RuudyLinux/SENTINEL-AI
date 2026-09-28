"""Two accuracy bugs found by the first real ANPR measurement
(docs/ANPR_ACCURACY.md, 25 labelled Indian plates).

1. Two-row plates got scrambled. Fragments were sorted by left x only, which
   interleaves rows: KL07BX7197 came out as INDBX7197KL07.
2. Localization made things worse: exact match 0.16 -> 0.04, CER
   0.524 -> 0.636, because it sometimes returns part of the plate and OCR
   reads nothing. Now a failed localized read falls back to the whole crop
   and the better read wins.
"""
import numpy as np
import pytest

from app.pipeline.anpr import better_read, order_fragments, read_plate


def _fragment(text: str, x: float, y: float, width: float = 60.0, height: float = 20.0, conf: float = 0.9):
    """An EasyOCR-shaped result: (4-point bbox, text, confidence)."""
    box = [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
    return (box, text, conf)


class TestTwoRowPlateOrdering:
    def test_a_two_row_plate_is_read_top_row_then_bottom_row(self):
        """Two-row plate whose bottom row starts further left than the top.
        An x-sort puts the bottom row first."""
        fragments = [
            _fragment("BX7197", x=10, y=60),   # bottom row, leftmost overall
            _fragment("KL07", x=30, y=10),     # top row
        ]
        ordered = order_fragments(fragments)
        assert [f[1] for f in ordered] == ["KL07", "BX7197"]

    def test_the_measured_ind_marker_case_no_longer_interleaves(self):
        """KL07BX7197 read as INDBX7197KL07. Marker and rows have to come out
        in reading order so normalization can recover the plate."""
        fragments = [
            _fragment("BX7197", x=5, y=70),
            _fragment("IND", x=8, y=12, width=30),
            _fragment("KL07", x=45, y=10),
        ]
        assert [f[1] for f in order_fragments(fragments)] == ["IND", "KL07", "BX7197"]

    def test_a_single_row_plate_is_still_plain_left_to_right(self):
        """Single-row behaviour was already right and must stay that way."""
        fragments = [
            _fragment("1234", x=200, y=10),
            _fragment("GJ05", x=10, y=12),
            _fragment("AB", x=110, y=11),
        ]
        assert [f[1] for f in order_fragments(fragments)] == ["GJ05", "AB", "1234"]

    def test_rows_are_split_by_relative_glyph_height_not_a_fixed_pixel_gap(self):
        """Big crop (tall glyphs, big row gap) still resolves to two rows."""
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
        """Confidence alone doesn't win; a confident read that isn't
        plate-shaped is exactly what the gate is for."""
        good = ("GJ05AB1234", "GJ05AB1234", 0.62)
        confident_junk = ("SUCUN", "SUCUN", 0.97)
        assert better_read(good, confident_junk) is good

    def test_between_two_passing_reads_the_more_confident_wins(self):
        low = ("GJ05AB1234", "GJ05AB1234", 0.55)
        high = ("GJ05AB1234", "GJ05AB1234", 0.88)
        assert better_read(low, high) is high
        assert better_read(high, low) is high

    def test_a_non_empty_read_beats_an_empty_one_even_if_both_fail_the_gate(self):
        """A localization miss must never turn a real read into nothing
        (the measured GJ01DY6855 -> <empty>)."""
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
    """extract_plate on real OCR outputs with extra characters."""

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
        """Only recovers a plate that's really there, never makes one up
        from noise (same bar as disambiguate_plate)."""
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
