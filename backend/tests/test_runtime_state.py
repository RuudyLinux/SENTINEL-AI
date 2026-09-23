"""The two primitives that have to survive a second process.

These replace `time.monotonic()`-based dicts in rules_engine, self_heal and
auth. The behaviours pinned here are the ones that made those dicts wrong
outside a single process:

- a claim and its recording are one operation, so two callers cannot both see
  a key as free;
- time is wall-clock, because a monotonic reading cannot be compared between
  processes and resets on restart;
- a backwards clock step expires a claim rather than freezing it;
- the tables are bounded, and the bound can never release a claim or a
  lockout that is currently doing its job.
"""
import pytest

from app.runtime_state import ExpiringClaims, SlidingWindow


class FakeClock:
    """Wall-clock the test drives directly, including backwards."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestExpiringClaims:
    def test_a_first_claim_is_granted(self):
        claims = ExpiringClaims(clock=FakeClock())
        assert claims.claim("cam_1:track_9", 45.0) is True

    def test_a_repeat_within_the_window_is_refused(self):
        claims = ExpiringClaims(clock=FakeClock())
        claims.claim("cam_1:track_9", 45.0)
        assert claims.claim("cam_1:track_9", 45.0) is False

    def test_the_claim_is_granted_again_once_the_window_passes(self):
        clock = FakeClock()
        claims = ExpiringClaims(clock=clock)
        claims.claim("cam_1:track_9", 45.0)
        clock.advance(45.1)
        assert claims.claim("cam_1:track_9", 45.0) is True

    def test_the_boundary_is_exclusive(self):
        """Exactly `ttl` seconds later, the claim has expired — matching the
        `now - last < COOLDOWN_SECONDS` comparison this replaces."""
        clock = FakeClock()
        claims = ExpiringClaims(clock=clock)
        claims.claim("k", 45.0)
        clock.advance(45.0)
        assert claims.claim("k", 45.0) is True

    def test_keys_do_not_interfere(self):
        claims = ExpiringClaims(clock=FakeClock())
        assert claims.claim("cam_1:track_9", 45.0) is True
        assert claims.claim("cam_2:track_9", 45.0) is True

    def test_a_refused_claim_does_not_extend_the_window(self):
        """A busy camera re-checks every inference cycle. If each refusal
        pushed the expiry out, the alert would never fire again."""
        clock = FakeClock()
        claims = ExpiringClaims(clock=clock)
        claims.claim("k", 45.0)
        for _ in range(10):
            clock.advance(4.0)
            claims.claim("k", 45.0)
        clock.advance(6.0)  # 46s total since the granted claim
        assert claims.claim("k", 45.0) is True

    def test_release_frees_the_key_immediately(self):
        claims = ExpiringClaims(clock=FakeClock())
        claims.claim("k", 45.0)
        claims.release("k")
        assert claims.claim("k", 45.0) is True

    def test_release_of_an_unknown_key_is_harmless(self):
        ExpiringClaims(clock=FakeClock()).release("never-claimed")

    def test_a_backwards_clock_expires_the_claim(self):
        """NTP correcting the clock backwards must not freeze a claim. Failing
        this way fires a possible duplicate; failing the other way suppresses
        real alerts until the clock catches up."""
        clock = FakeClock()
        claims = ExpiringClaims(clock=clock)
        claims.claim("k", 45.0)
        clock.advance(-3600.0)
        assert claims.claim("k", 45.0) is True

    def test_the_table_is_bounded(self):
        claims = ExpiringClaims(max_keys=50, clock=FakeClock())
        for i in range(500):
            claims.claim(f"track_{i}", 45.0)
        assert len(claims) <= 50

    def test_eviction_prefers_expired_entries(self):
        """Dropping an expired claim changes no behaviour; dropping a live one
        releases a suppression that is still wanted."""
        clock = FakeClock()
        claims = ExpiringClaims(max_keys=10, clock=clock)
        for i in range(10):
            claims.claim(f"old_{i}", 5.0)
        clock.advance(10.0)  # every old_* claim is now expired
        for i in range(10):
            claims.claim(f"live_{i}", 45.0)
        assert claims.claim("live_5", 45.0) is False, "a live claim was evicted before an expired one"

    def test_clear_empties_the_table(self):
        claims = ExpiringClaims(clock=FakeClock())
        claims.claim("k", 45.0)
        claims.clear()
        assert len(claims) == 0
        assert claims.claim("k", 45.0) is True


class TestSlidingWindow:
    def test_it_counts_events_in_the_window(self):
        window = SlidingWindow(clock=FakeClock())
        assert window.record("bob", 60.0, limit=5) == 1
        assert window.record("bob", 60.0, limit=5) == 2

    def test_events_leave_the_window(self):
        clock = FakeClock()
        window = SlidingWindow(clock=clock)
        window.record("bob", 60.0, limit=5)
        clock.advance(61.0)
        assert window.record("bob", 60.0, limit=5) == 1

    def test_it_slides_rather_than_resetting(self):
        """The window must roll forward continuously: four failures spread
        over ninety seconds are never five in sixty."""
        clock = FakeClock()
        window = SlidingWindow(clock=clock)
        for _ in range(4):
            window.record("bob", 60.0, limit=5)
            clock.advance(30.0)
        assert window.count("bob", 60.0) == 1

    def test_count_does_not_record(self):
        window = SlidingWindow(clock=FakeClock())
        window.record("bob", 60.0, limit=5)
        assert window.count("bob", 60.0) == 1
        assert window.count("bob", 60.0) == 1

    def test_forget_clears_one_key(self):
        """A successful login clears that account's failures, and only that
        account's."""
        window = SlidingWindow(clock=FakeClock())
        window.record("bob", 60.0, limit=5)
        window.record("eve", 60.0, limit=5)
        window.forget("bob")
        assert window.count("bob", 60.0) == 0
        assert window.count("eve", 60.0) == 1

    def test_a_backwards_clock_expires_events(self):
        clock = FakeClock()
        window = SlidingWindow(clock=clock)
        window.record("bob", 60.0, limit=5)
        clock.advance(-3600.0)
        assert window.count("bob", 60.0) == 0

    def test_the_table_is_bounded_against_attacker_supplied_keys(self):
        """The login limiter is keyed by a username an unauthenticated caller
        chooses. Unbounded, that is remote memory growth with no credential."""
        window = SlidingWindow(max_keys=50, clock=FakeClock())
        for i in range(500):
            window.record(f"attacker-{i}", 60.0, limit=5)
        assert len(window) <= 50

    def test_eviction_never_clears_an_entry_at_the_limit(self):
        """Otherwise flooding novel usernames becomes a way to unlock the
        account you are actually attacking."""
        window = SlidingWindow(max_keys=20, clock=FakeClock())
        for _ in range(5):
            window.record("victim", 60.0, limit=5)
        for i in range(500):
            window.record(f"noise-{i}", 60.0, limit=5)
        assert window.count("victim", 60.0) >= 5, "the real lockout was evicted by noise"

    def test_clear_empties_the_table(self):
        window = SlidingWindow(clock=FakeClock())
        window.record("bob", 60.0, limit=5)
        window.clear()
        assert len(window) == 0


def test_the_default_clock_is_wall_clock_not_monotonic():
    """Both stores this replaces used `time.monotonic()`, which is measured
    from a per-process origin: two processes cannot compare readings, and a
    restart resets it — silently re-firing every suppressed alert. The value
    has to be one a second process and a later run would recognise."""
    import time as _time

    claims = ExpiringClaims()
    claims.claim("k", 45.0)
    recorded = next(iter(claims._claimed_at.values()))
    assert abs(recorded - _time.time()) < 5.0
    assert recorded > 1_600_000_000.0, "looks like a monotonic counter, not an epoch timestamp"
