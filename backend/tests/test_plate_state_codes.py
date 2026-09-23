"""Indian registration validation: state/UT codes and the Bharat series.

Why this matters more than a format tweak: the format regex alone accepts any
two letters, so OCR noise landing in the right SHAPE — "QQ00QQ0000",
"XX12AB1234" — was a "valid plate", cleared the quality gate and became a real
Vehicle row. On the labelled corpus, plate-shaped-but-wrong reads outnumbered
correct ones, so this is the cheapest false-positive defence available.
"""
import pytest

from app.pipeline.anpr import (
    INDIAN_STATE_CODES, PLATE_RE, disambiguate_plate, looks_like_plate, normalize_plate,
)


class TestRealRegistrationsAreAccepted:
    @pytest.mark.parametrize("plate", [
        "GJ05AB1234",   # Gujarat, full-length
        "GJ5ABC123",    # single-digit RTO, three-letter series
        "MH12DE1433",   # Maharashtra
        "DL3CD1210",    # Delhi
        "KL07BX7197",   # Kerala
        "KA01AJ7533",   # Karnataka
        "UP32AB1234",   # Uttar Pradesh
        "TN10K1234",    # Tamil Nadu, one-letter series
        "WB06F5544",    # West Bengal
        "RJ14CA1234",   # Rajasthan
    ])
    def test_valid_state_plates(self, plate):
        assert looks_like_plate(plate)

    @pytest.mark.parametrize("plate", ["OD02AB1234", "OR02AB1234", "TG07XY9999", "TS07XY9999",
                                       "UK07AB1234", "UA07AB1234"])
    def test_both_current_and_legacy_codes_are_accepted(self, plate):
        """Odisha, Telangana and Uttarakhand each changed prefix. Vehicles
        carrying the old code are still on the road, and rejecting them would
        blind the system to real traffic."""
        assert looks_like_plate(plate)

    @pytest.mark.parametrize("plate", ["CH01AB1234", "PY01AB1234", "AN01A1234",
                                       "LD01A1234", "LA01AB1234", "DL1CA1234"])
    def test_union_territory_codes_are_accepted(self, plate):
        assert looks_like_plate(plate)


class TestBharatSeries:
    @pytest.mark.parametrize("plate", ["23BH1234AA", "21BH5678A", "24BH0001XY"])
    def test_bh_series_is_accepted(self, plate):
        """The 2021 all-India series for transferable vehicles. It does NOT
        follow the state-code grammar — it starts with the registration year."""
        assert looks_like_plate(plate)

    def test_bh_series_does_not_match_the_state_format(self, plate="23BH1234AA"):
        """Pinned so a future 'simplification' does not merge the two grammars
        and thereby start accepting digit-leading garbage as a state plate."""
        assert not PLATE_RE.match(plate)

    @pytest.mark.parametrize("junk", ["23XX1234AA", "2BH1234AA", "23BH12AA", "23BH1234ABC"])
    def test_near_miss_bh_strings_are_rejected(self, junk):
        assert not looks_like_plate(junk)


class TestUnknownStateCodesAreRejected:
    @pytest.mark.parametrize("junk", [
        "QQ00QQ0000",   # the exact string the disambiguator could once manufacture
        "XX12AB1234",
        "ZZ99ZZ9999",
        "AA01AB1234",
        "IN07BX7197",   # "IND" country marker fragment, a real OCR artefact
    ])
    def test_plate_shaped_but_impossible_reads_are_refused(self, junk):
        """These all satisfy the format regex. Only the state-code check stops
        them becoming vehicle records."""
        assert PLATE_RE.match(junk), "precondition: this IS plate-shaped"
        assert looks_like_plate(junk) is False

    def test_every_accepted_prefix_is_in_the_published_set(self):
        assert looks_like_plate("GJ05AB1234")
        assert "GJ" in INDIAN_STATE_CODES
        assert "QQ" not in INDIAN_STATE_CODES

    def test_the_code_set_is_not_accidentally_empty_or_tiny(self):
        """A truncated set would silently reject most of the country's traffic —
        a failure that looks like 'ANPR got worse' rather than a config bug."""
        assert len(INDIAN_STATE_CODES) >= 35


class TestInteractionWithRepairAndNormalization:
    def test_a_repair_that_would_produce_an_unknown_state_is_not_applied(self):
        """The disambiguator only returns a candidate that `looks_like_plate`
        accepts, so it can no longer repair noise into an impossible state
        code."""
        assert disambiguate_plate("QQQQQQQQQQ") == "QQQQQQQQQQ"

    def test_a_repair_producing_a_real_state_code_still_works(self):
        assert disambiguate_plate(normalize_plate("GJO5AB1234")) == "GJ05AB1234"

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_input_is_not_a_plate(self, empty):
        assert looks_like_plate(empty or "") is False
