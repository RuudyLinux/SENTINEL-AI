"""8. Alert dedup (cooldown) — prevents a tracked object re-firing an alert every inference cycle."""
from app.pipeline import rules_engine
from app.runtime_state import ExpiringClaims


class _Clock:
    """The cooldown moved off `time.monotonic()` onto wall-clock, because a
    monotonic reading cannot be compared between processes and resets on
    restart. Driving the store's own clock tests the same behaviour without
    depending on which clock function it reads."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _fixed_clock(monkeypatch, clock):
    monkeypatch.setattr(rules_engine, "_alert_claims", ExpiringClaims(clock=clock))


def test_cooldown_suppresses_immediate_repeat(monkeypatch):
    clock = _Clock()
    _fixed_clock(monkeypatch, clock)

    key = ("cam_1", "zone", "zone_1", "track_9")
    assert rules_engine._on_cooldown(key) is False  # first sighting: not on cooldown, alert fires
    assert rules_engine._on_cooldown(key) is True   # same instant: suppressed


def test_cooldown_expires_after_the_window(monkeypatch):
    clock = _Clock()
    _fixed_clock(monkeypatch, clock)

    key = ("cam_1", "watchlist", "veh_1")
    assert rules_engine._on_cooldown(key) is False
    clock.now += rules_engine.COOLDOWN_SECONDS + 1
    assert rules_engine._on_cooldown(key) is False  # window elapsed: fires again
