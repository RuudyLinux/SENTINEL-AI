"""Alert cooldown: a tracked object doesn't re-fire every inference cycle."""
import asyncio

from app.pipeline import rules_engine
from app.runtime_state import ExpiringClaims


class _Clock:
    """The cooldown uses wall clock now (monotonic can't be shared between
    processes and resets on restart). Driving the store's clock tests the
    same thing without caring which clock it reads."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _fixed_clock(monkeypatch, clock):
    monkeypatch.setattr(rules_engine, "_alert_claims", ExpiringClaims(clock=clock))


def _cooldown(key):
    """_on_cooldown is async for the Redis store; with the local store used
    here it's a plain dict lookup, asyncio.run just lets a sync test call it."""
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


def test_cooldown_boundary_is_exact(monkeypatch):
    """A repeat just inside the cooldown is suppressed; one at exactly the
    cooldown length is allowed (the store compares elapsed < ttl)."""
    clock = _Clock()
    _fixed_clock(monkeypatch, clock)
    key = ("cam-boundary", "zone", "z", "trk")
    assert _cooldown(key) is False
    clock.now += rules_engine.COOLDOWN_SECONDS - 0.001
    assert _cooldown(key) is True
    clock.now += 0.001
    assert _cooldown(key) is False
