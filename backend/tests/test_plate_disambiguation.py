"""Grammar-based character-class repair for OCR reads.

With real EasyOCR output (tools/anpr_bench.py) the main failure is a class
mix-up in an otherwise right read (GJ05AB1234 -> GJO5AB1234), which then
failed the format gate and got dropped.

Just as important: never invent a plate, never touch a read that already parses.
"""
import pytest

from app.pipeline.anpr import disambiguate_plate, looks_like_plate, normalize_plate, passes_anpr_gate


class TestRepairsRealConfusions:
    @pytest.mark.parametrize("misread,expected", [
        ("GJO5AB1234", "GJ05AB1234"),   # O in the RTO digits, the measured case
        ("6J05AB1234", "GJ05AB1234"),   # 6 in the state letters, also measured
        ("GJ05AB123O", "GJ05AB1230"),   # letter O in the number
        ("GJO1XY7788", "GJ01XY7788"),
        ("GJ05A81234", "GJ05AB1234"),   # digit 8 for letter B in the series
        ("GJ05AB12I4", "GJ05AB1214"),   # letter I for digit 1
    ])
    def test_a_single_class_confusion_is_repaired(self, misread, expected):
        assert disambiguate_plate(misread) == expected
        assert looks_like_plate(disambiguate_plate(misread))

    def test_an_ambiguous_read_that_already_parses_is_left_alone(self):
        """"GJ0SAB1234" could be GJ05AB1234 with S for 5, but it's also a valid
        plate itself (GJ / 0 / SAB / 1234). The repair can't tell, so it
        doesn't pick; turning one valid plate into another would corrupt a
        correct read."""
        assert disambiguate_plate("GJ0SAB1234") == "GJ0SAB1234"
        assert looks_like_plate("GJ0SAB1234")


class TestSafety:
    @pytest.mark.parametrize("valid", ["GJ05AB1234", "GJ01XY7788", "MH12DE1433", "GJ5ABC123"])
    def test_a_valid_plate_is_never_altered(self, valid):
        """Inert on reads that already parse."""
        assert disambiguate_plate(valid) == valid

    @pytest.mark.parametrize("junk", ["", "XX", "HELLO", "1234567890123", "AB"])
    def test_unrepairable_input_is_returned_unchanged(self, junk):
        """Not a plate stays not a plate; the gate rejects it."""
        assert disambiguate_plate(junk) == junk

    def test_never_changes_the_length_of_a_read(self):
        """Swaps only. Adding or dropping characters would be making things up."""
        for candidate in ["GJO5AB1234", "HELLO", "GJ05AB123O", "ZZZZZZZZZ"]:
            assert len(disambiguate_plate(candidate)) == len(candidate)

    def test_a_repaired_read_still_has_to_clear_the_confidence_gate(self):
        """Only fixes format. A low-confidence read stays low confidence."""
        repaired = disambiguate_plate("GJO5AB1234")
        assert looks_like_plate(repaired)
        assert passes_anpr_gate(repaired, 0.10) is False
        assert passes_anpr_gate(repaired, 0.90) is True

    @pytest.mark.parametrize("noise", ["QQQQQQQQQQ", "OOOOOOOOOO", "IIIIIIIIII", "SSSSSSSSSS"])
    def test_noise_of_plate_length_is_not_manufactured_into_a_plate(self, noise):
        """This test found it: with no budget "QQQQQQQQQQ" became "QQ00QQ0000"
        in six swaps and passed the format gate. A glyph or two, never a
        rewrite."""
        assert disambiguate_plate(noise) == noise

    def test_at_most_two_characters_are_ever_changed(self):
        for candidate in ["QQQQQQQQQQ", "OOOOOOOOOO", "GJO5AB1234", "6J05AB1234", "ZZZZZZZZZ"]:
            repaired = disambiguate_plate(candidate)
            changed = sum(1 for a, b in zip(candidate, repaired) if a != b)
            assert changed <= 2, f"{candidate} -> {repaired} changed {changed} characters"


def test_the_end_to_end_normalization_path_applies_the_repair():
    """read_plate normalizes then repairs; pinned so a refactor can't drop
    the repair."""
    from app.pipeline.anpr import disambiguate_plate as repair

    assert repair(normalize_plate("GJ O5 AB 1234")) == "GJ05AB1234"
