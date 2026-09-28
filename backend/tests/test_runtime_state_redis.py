"""Redis-backed runtime state against a real Redis server.

test_runtime_state.py covers the interface in-process. What needs a real
server is that two separate store instances with their own connections share
one claim table. store_a and store_b stand in for two processes; to Redis
that's the same thing, SET NX doesn't care who sent it.

Skipped, not failed, without Redis (REDIS_URL empty is a supported setup).
Locally:

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
    """Unique key prefix per test, so order and parallel runs don't collide
    and TTLs handle cleanup."""
    return f"test:{uuid.uuid4().hex[:12]}"


@pytest.fixture
def client():
    c = redis.Redis.from_url(REDIS_URL)
    yield c
    c.close()


class TestClaimsAcrossTwoStoreInstances:
    """Two instances as two processes. If they disagree on who holds a claim,
    a second worker double-fires every alert."""

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
        """Cooldown and self-heal dedup share one Redis; prefixes keep them apart."""
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
    """Login limiter: both instances agree on the count, or an attacker on
    another process never hits the limit."""

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
    """Pointed at a port with nothing listening, i.e. Redis down mid-run.
    No mocking; a real connection really fails."""

    @pytest.fixture
    def unreachable_client(self):
        # real refused/timed-out connect, not a mock
        return redis.Redis.from_url(
            "redis://localhost:1/0", socket_connect_timeout=0.3, socket_timeout=0.3,
        )

    def test_claim_fails_open_rather_than_raising(self, unreachable_client, prefix):
        store = RedisExpiringClaims(unreachable_client, prefix)
        assert store.claim("k", 30.0) is True, (
            "a Redis outage must not silently swallow a real alert"
        )

    def test_a_repeated_claim_still_fails_open(self, unreachable_client, prefix):
        """Still no raising after the first warning; the warning is one-shot,
        failing open isn't."""
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
    """get_redis_client / build_claims_store against the real server."""

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
