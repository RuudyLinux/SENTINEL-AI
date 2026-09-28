"""Indian registrations: state/UT codes and the Bharat series.

The regex alone takes any two letters, so noise shaped like QQ00QQ0000 or
XX12AB1234 passed the gate and became a Vehicle. On the labelled corpus
plate-shaped wrong reads outnumber correct ones; this is the cheapest defence.
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
        """Odisha, Telangana and Uttarakhand changed prefix; old plates are
        still on the road."""
        assert looks_like_plate(plate)

    @pytest.mark.parametrize("plate", ["CH01AB1234", "PY01AB1234", "AN01A1234",
                                       "LD01A1234", "LA01AB1234", "DL1CA1234"])
    def test_union_territory_codes_are_accepted(self, plate):
        assert looks_like_plate(plate)


class TestBharatSeries:
    @pytest.mark.parametrize("plate", ["23BH1234AA", "21BH5678A", "24BH0001XY"])
    def test_bh_series_is_accepted(self, plate):
        """2021 all-India series, starts with the registration year, not a state."""
        assert looks_like_plate(plate)

    def test_bh_series_does_not_match_the_state_format(self, plate="23BH1234AA"):
        """Kept separate so merging the grammars can't start accepting
        digit-leading junk as a state plate."""
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
        """All pass the regex; only the state check stops them."""
        assert PLATE_RE.match(junk), "precondition: this IS plate-shaped"
        assert looks_like_plate(junk) is False

    def test_every_accepted_prefix_is_in_the_published_set(self):
        assert looks_like_plate("GJ05AB1234")
        assert "GJ" in INDIAN_STATE_CODES
        assert "QQ" not in INDIAN_STATE_CODES

    def test_the_code_set_is_not_accidentally_empty_or_tiny(self):
        """A short list would reject real traffic, and it'd look like ANPR got
        worse rather than a config bug."""
        assert len(INDIAN_STATE_CODES) >= 35


class TestInteractionWithRepairAndNormalization:
    def test_a_repair_that_would_produce_an_unknown_state_is_not_applied(self):
        """The repair only returns something looks_like_plate accepts, so it
        can't produce an impossible state code."""
        assert disambiguate_plate("QQQQQQQQQQ") == "QQQQQQQQQQ"

    def test_a_repair_producing_a_real_state_code_still_works(self):
        assert disambiguate_plate(normalize_plate("GJO5AB1234")) == "GJ05AB1234"

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_input_is_not_a_plate(self, empty):
        assert looks_like_plate(empty or "") is False
