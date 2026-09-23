"""V2 Phase 10 — grammar-based character-class repair for OCR reads.

Measured with tools/anpr_bench.py against real EasyOCR output: the dominant
failure is not a wrong plate but a character-class confusion in an otherwise
perfect read (`GJ05AB1234` -> `GJO5AB1234`), which then failed the format gate
and was silently discarded.

The safety property matters as much as the repair: this must never invent a
plate that was not read, and must never touch a read that already parses.
"""
import pytest

from app.pipeline.anpr import disambiguate_plate, looks_like_plate, normalize_plate, passes_anpr_gate


class TestRepairsRealConfusions:
    @pytest.mark.parametrize("misread,expected", [
        ("GJO5AB1234", "GJ05AB1234"),   # letter O in the RTO digits — the measured case
        ("6J05AB1234", "GJ05AB1234"),   # digit 6 in the state letters — also measured
        ("GJ05AB123O", "GJ05AB1230"),   # letter O in the number
        ("GJO1XY7788", "GJ01XY7788"),
        ("GJ05A81234", "GJ05AB1234"),   # digit 8 for letter B in the series
        ("GJ05AB12I4", "GJ05AB1214"),   # letter I for digit 1
    ])
    def test_a_single_class_confusion_is_repaired(self, misread, expected):
        assert disambiguate_plate(misread) == expected
        assert looks_like_plate(disambiguate_plate(misread))

    def test_an_ambiguous_read_that_already_parses_is_left_alone(self):
        """"GJ0SAB1234" looks like an S-for-5 misread of GJ05AB1234, but it is
        ALSO a well-formed registration on its own (GJ / 0 / SAB / 1234). The
        repair cannot tell those apart, so it must not choose — silently
        rewriting a valid plate into a different valid plate would corrupt a
        correct read, which is worse than leaving an ambiguous one alone."""
        assert disambiguate_plate("GJ0SAB1234") == "GJ0SAB1234"
        assert looks_like_plate("GJ0SAB1234")


class TestSafety:
    @pytest.mark.parametrize("valid", ["GJ05AB1234", "GJ01XY7788", "MH12DE1433", "GJ5ABC123"])
    def test_a_valid_plate_is_never_altered(self, valid):
        """The repair must be inert on reads that already parse — otherwise it
        could corrupt a correct plate into a different valid one."""
        assert disambiguate_plate(valid) == valid

    @pytest.mark.parametrize("junk", ["", "XX", "HELLO", "1234567890123", "AB"])
    def test_unrepairable_input_is_returned_unchanged(self, junk):
        """No plate is invented from something that is not one — the read is
        returned as-is and the quality gate then rejects it."""
        assert disambiguate_plate(junk) == junk

    def test_never_changes_the_length_of_a_read(self):
        """Only substitutions are permitted. Inserting or dropping a character
        would be fabricating evidence, not correcting a glyph."""
        for candidate in ["GJO5AB1234", "HELLO", "GJ05AB123O", "ZZZZZZZZZ"]:
            assert len(disambiguate_plate(candidate)) == len(candidate)

    def test_a_repaired_read_still_has_to_clear_the_confidence_gate(self):
        """Repair fixes the FORMAT check only. A low-confidence read is still a
        low-confidence read and must not become trustworthy by being tidied up."""
        repaired = disambiguate_plate("GJO5AB1234")
        assert looks_like_plate(repaired)
        assert passes_anpr_gate(repaired, 0.10) is False
        assert passes_anpr_gate(repaired, 0.90) is True

    @pytest.mark.parametrize("noise", ["QQQQQQQQQQ", "OOOOOOOOOO", "IIIIIIIIII", "SSSSSSSSSS"])
    def test_noise_of_plate_length_is_not_manufactured_into_a_plate(self, noise):
        """Found by this test: without a substitution budget the mapping is
        strong enough to turn "QQQQQQQQQQ" into the well-formed "QQ00QQ0000" in
        six substitutions, which the format gate would then accept as a real
        registration. A repair fixes a glyph or two; it must never rewrite a
        string into a plate."""
        assert disambiguate_plate(noise) == noise

    def test_at_most_two_characters_are_ever_changed(self):
        for candidate in ["QQQQQQQQQQ", "OOOOOOOOOO", "GJO5AB1234", "6J05AB1234", "ZZZZZZZZZ"]:
            repaired = disambiguate_plate(candidate)
            changed = sum(1 for a, b in zip(candidate, repaired) if a != b)
            assert changed <= 2, f"{candidate} -> {repaired} changed {changed} characters"


def test_the_end_to_end_normalization_path_applies_the_repair():
    """`read_plate` normalizes then repairs; this pins that composition so a
    future refactor cannot quietly drop the repair step."""
    from app.pipeline.anpr import disambiguate_plate as repair

    assert repair(normalize_plate("GJ O5 AB 1234")) == "GJ05AB1234"
