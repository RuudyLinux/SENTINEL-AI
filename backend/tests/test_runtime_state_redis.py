"""Redis-backed runtime state, against a real Redis server.

Everything test_runtime_state.py checks is the SHAPE of the interface using
an in-process fake. This file exists because the shape was never the risky
part — a dict with a clock is easy to get right. What actually needed
proving is the property no in-process test can demonstrate at all: that two
SEPARATE store instances, each with its own connection, pointed at the same
Redis, genuinely share one claim table. That is simulated here with two
Python-level instances (`store_a`, `store_b`) standing in for two OS
processes — from Redis's point of view that is exactly what two processes
talking to it look like, since nothing about `SET NX` cares which process
issued the command.

Skipped, not failed, when no Redis is reachable: Redis is optional
infrastructure (REDIS_URL empty is a fully supported deployment), so a
checkout with no Redis running must still pass the ordinary suite. Run
locally against a throwaway container:

    docker run -d --rm -p 6379:6379 redis:7-alpine
    pytest tests/test_runtime_state_redis.py -q
"""
import uuid

import pytest

redis = pytest.importorskip("redis")

from app.runtime_state import RedisExpiringClaims, RedisSlidingWindow  # noqa: E402

REDIS_URL = "redis://localhost:6379/0"


def _reachable() -> bool:
    try:
        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1.0)
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no Redis reachable at {REDIS_URL}")


@pytest.fixture
def prefix():
    """A fresh, unique key prefix per test — tests can run in any order or in
    parallel against the same Redis without one test's keys colliding with
    another's, and nothing needs cleaning up afterward beyond what TTLs
    already handle."""
    return f"test:{uuid.uuid4().hex[:12]}"


@pytest.fixture
def client():
    c = redis.Redis.from_url(REDIS_URL)
    yield c
    c.close()


class TestClaimsAcrossTwoStoreInstances:
    """Two instances standing in for two processes. If these disagree about
    who holds a claim, a second worker would double-fire every alert — the
    exact failure the in-process dict could not prevent."""

    def test_a_claim_made_by_one_instance_is_visible_to_the_other(self, client, prefix):
        store_a = RedisExpiringClaims(client, prefix)
        store_b = RedisExpiringClaims(redis.Redis.from_url(REDIS_URL), prefix)
        try:
            assert store_a.claim("cam_1:track_9", 30.0) is True
            assert store_b.claim("cam_1:track_9", 30.0) is False, (
                "a second process claimed a key the first is still holding — "
                "this is the exact bug the in-process dict had"
            )
        finally:
            store_a.clear()

    def test_the_claim_expires_for_both(self, client, prefix):
        import time
        store_a = RedisExpiringClaims(client, prefix)
        store_b = RedisExpiringClaims(redis.Redis.from_url(REDIS_URL), prefix)
        try:
            store_a.claim("k", 1.0)
            time.sleep(1.3)
            assert store_b.claim("k", 1.0) is True
        finally:
            store_a.clear()

    def test_distinct_keys_do_not_interfere(self, client, prefix):
        store = RedisExpiringClaims(client, prefix)
        try:
            assert store.claim("cam_1", 30.0) is True
            assert store.claim("cam_2", 30.0) is True
        finally:
            store.clear()

    def test_release_frees_the_key_for_every_instance(self, client, prefix):
        store_a = RedisExpiringClaims(client, prefix)
        store_b = RedisExpiringClaims(redis.Redis.from_url(REDIS_URL), prefix)
        try:
            store_a.claim("k", 30.0)
            store_a.release("k")
            assert store_b.claim("k", 30.0) is True
        finally:
            store_a.clear()

    def test_two_different_prefixes_never_collide(self, client, prefix):
        """The alert cooldown and the self-heal dedup window share one Redis
        in a real deployment; their prefixes are what keeps a key in one from
        ever being read by the other."""
        store_alerts = RedisExpiringClaims(client, f"{prefix}:alerts")
        store_selfheal = RedisExpiringClaims(client, f"{prefix}:selfheal")
        try:
            store_alerts.claim("cam_1", 30.0)
            assert store_selfheal.claim("cam_1", 30.0) is True, (
                "the same logical key in a different namespace must be independent"
            )
        finally:
            store_alerts.clear()
            store_selfheal.clear()

    def test_len_reflects_only_this_prefix(self, client, prefix):
        store = RedisExpiringClaims(client, prefix)
        other = RedisExpiringClaims(client, f"{prefix}-other")
        try:
            store.claim("a", 30.0)
            store.claim("b", 30.0)
            other.claim("z", 30.0)
            assert len(store) == 2
        finally:
            store.clear()
            other.clear()


class TestSlidingWindowAcrossTwoStoreInstances:
    """The login rate limiter, proven the same way: two instances must agree
    on the count, or an attacker routed to a different process never hits
    the limit at all."""

    def test_events_recorded_by_one_instance_are_counted_by_the_other(self, client, prefix):
        window_a = RedisSlidingWindow(client, prefix)
        window_b = RedisSlidingWindow(redis.Redis.from_url(REDIS_URL), prefix)
        try:
            for _ in range(3):
                window_a.record("attacker", 60.0, limit=5)
            assert window_b.count("attacker", 60.0) == 3, (
                "a second process could not see the first's failed attempts — "
                "exactly how a distributed lockout gets bypassed"
            )
        finally:
            window_a.clear()

    def test_the_limit_is_reached_from_combined_attempts(self, client, prefix):
        window_a = RedisSlidingWindow(client, prefix)
        window_b = RedisSlidingWindow(redis.Redis.from_url(REDIS_URL), prefix)
        try:
            for _ in range(3):
                window_a.record("attacker", 60.0, limit=5)
            for _ in range(2):
                window_b.record("attacker", 60.0, limit=5)
            assert window_a.count("attacker", 60.0) >= 5
        finally:
            window_a.clear()

    def test_events_leave_the_window(self, client, prefix):
        import time
        window = RedisSlidingWindow(client, prefix)
        try:
            window.record("bob", 1.0, limit=5)
            time.sleep(1.3)
            assert window.count("bob", 1.0) == 0
        finally:
            window.clear()

    def test_forget_clears_only_that_key(self, client, prefix):
        window = RedisSlidingWindow(client, prefix)
        try:
            window.record("bob", 60.0, limit=5)
            window.record("eve", 60.0, limit=5)
            window.forget("bob")
            assert window.count("bob", 60.0) == 0
            assert window.count("eve", 60.0) == 1
        finally:
            window.clear()


class TestFailOpenOnRedisFailure:
    """A store pointed at a port nothing is listening on — simulating Redis
    being down mid-run, not merely unconfigured. The policy has to hold even
    though nothing here mocks the client: a real connection attempt really
    fails, exactly like production would see."""

    @pytest.fixture
    def unreachable_client(self):
        # A real socket connect that will genuinely refuse/time out, not a
        # mock standing in for one — the whole point is proving what happens
        # against an actual failed connection.
        return redis.Redis.from_url(
            "redis://localhost:1/0", socket_connect_timeout=0.3, socket_timeout=0.3,
        )

    def test_claim_fails_open_rather_than_raising(self, unreachable_client, prefix):
        store = RedisExpiringClaims(unreachable_client, prefix)
        assert store.claim("k", 30.0) is True, (
            "a Redis outage must not silently swallow a real alert"
        )

    def test_a_repeated_claim_still_fails_open(self, unreachable_client, prefix):
        """Nothing here should start raising after the first warning is
        logged — the warning is one-shot, the fail-open behaviour is not."""
        store = RedisExpiringClaims(unreachable_client, prefix)
        for _ in range(5):
            assert store.claim("k", 30.0) is True

    def test_sliding_window_fails_open_to_zero(self, unreachable_client, prefix):
        window = RedisSlidingWindow(unreachable_client, prefix)
        assert window.record("bob", 60.0, limit=5) == 0, (
            "a Redis outage must not lock every operator out at once"
        )
        assert window.count("bob", 60.0) == 0

    def test_release_and_forget_do_not_raise_either(self, unreachable_client, prefix):
        RedisExpiringClaims(unreachable_client, prefix).release("k")
        RedisSlidingWindow(unreachable_client, prefix).forget("k")

    def test_len_fails_open_to_zero_rather_than_raising(self, unreachable_client, prefix):
        assert len(RedisExpiringClaims(unreachable_client, prefix)) == 0


class TestBuildFactoriesAgainstARealServer:
    """get_redis_client / build_claims_store, exercised against this real
    Redis rather than only unit-tested with a fake settings object."""

    def test_build_claims_store_returns_a_redis_backed_instance_when_reachable(self, monkeypatch):
        import app.runtime_state as rs
        monkeypatch.setattr(rs, "_redis_client", "unattempted")

        class FakeSettings:
            redis_url = REDIS_URL
            redis_connect_timeout_seconds = 2.0

        store = rs.build_claims_store("factory_probe", FakeSettings())
        try:
            assert store.is_local is False
            assert isinstance(store, RedisExpiringClaims)
        finally:
            store.clear()
            monkeypatch.setattr(rs, "_redis_client", "unattempted")

    def test_an_unset_redis_url_returns_the_in_process_store(self, monkeypatch):
        import app.runtime_state as rs
        monkeypatch.setattr(rs, "_redis_client", "unattempted")

        class FakeSettings:
            redis_url = ""
            redis_connect_timeout_seconds = 2.0

        store = rs.build_claims_store("factory_probe_2", FakeSettings())
        assert store.is_local is True
        monkeypatch.setattr(rs, "_redis_client", "unattempted")

    def test_an_unreachable_redis_url_falls_back_to_in_process(self, monkeypatch):
        import app.runtime_state as rs
        monkeypatch.setattr(rs, "_redis_client", "unattempted")

        class FakeSettings:
            redis_url = "redis://localhost:1/0"  # nothing listens here
            redis_connect_timeout_seconds = 0.3

        store = rs.build_claims_store("factory_probe_3", FakeSettings())
        assert store.is_local is True, "startup must fail open to in-process, not raise"
        monkeypatch.setattr(rs, "_redis_client", "unattempted")
