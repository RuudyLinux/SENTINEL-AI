"""4. ANPR quality gate — P0-C."""
from app.pipeline.anpr import passes_anpr_gate


def test_passes_on_plausible_plate_and_sufficient_confidence():
    assert passes_anpr_gate("GJ05AB1234", 0.5) is True


def test_rejects_low_confidence_even_if_format_looks_right():
    assert passes_anpr_gate("GJ05AB1234", 0.10) is False


def test_rejects_implausible_format_even_at_high_confidence():
    assert passes_anpr_gate("XX", 0.99) is False


def test_rejects_empty_read():
    assert passes_anpr_gate("", 0.99) is False
