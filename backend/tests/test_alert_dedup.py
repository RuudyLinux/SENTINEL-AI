"""8. Alert dedup (cooldown) — prevents a tracked object re-firing an alert every inference cycle."""
import asyncio

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


def _cooldown(key):
    """`_on_cooldown` is `async def` — it dispatches a Redis-backed claim to a
    thread rather than blocking the camera loop's event loop, and stays a
    plain synchronous dict lookup for the in-process store these tests use
    (`ExpiringClaims.is_local` is True, so no thread dispatch happens here
    either — `asyncio.run` is only what lets a sync test call an async
    function at all)."""
    return asyncio.run(rules_engine._on_cooldown(key))


def test_cooldown_suppresses_immediate_repeat(monkeypatch):
    clock = _Clock()
    _fixed_clock(monkeypatch, clock)

    key = ("cam_1", "zone", "zone_1", "track_9")
    assert _cooldown(key) is False  # first sighting: not on cooldown, alert fires
    assert _cooldown(key) is True   # same instant: suppressed


def test_cooldown_expires_after_the_window(monkeypatch):
    clock = _Clock()
    _fixed_clock(monkeypatch, clock)

    key = ("cam_1", "watchlist", "veh_1")
    assert _cooldown(key) is False
    clock.now += rules_engine.COOLDOWN_SECONDS + 1
    assert _cooldown(key) is False  # window elapsed: fires again
