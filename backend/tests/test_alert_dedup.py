"""8. Alert dedup (cooldown) — prevents a tracked object re-firing an alert every inference cycle."""
from app.pipeline import rules_engine


def test_cooldown_suppresses_immediate_repeat(monkeypatch):
    rules_engine._last_alert_at.clear()
    t = [1000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    key = ("cam_1", "zone", "zone_1", "track_9")
    assert rules_engine._on_cooldown(key) is False  # first sighting: not on cooldown, alert fires
    assert rules_engine._on_cooldown(key) is True   # same instant: suppressed


def test_cooldown_expires_after_the_window(monkeypatch):
    rules_engine._last_alert_at.clear()
    t = [1000.0]
    monkeypatch.setattr(rules_engine.time, "monotonic", lambda: t[0])

    key = ("cam_1", "watchlist", "veh_1")
    assert rules_engine._on_cooldown(key) is False
    t[0] += rules_engine.COOLDOWN_SECONDS + 1
    assert rules_engine._on_cooldown(key) is False  # window elapsed: fires again
