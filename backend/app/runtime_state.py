"""Runtime state shared between backend processes.

Alert cooldowns and the self-heal duplicate window (ExpiringClaims) and the
login rate limiter (SlidingWindow). In-process by default, Redis-backed when
REDIS_URL is set, so a second process can't double alerts or bypass a lockout.

Per-camera state (plate tracks, zone presence, clip buffers) stays in process:
each camera is processed by exactly one worker.

Uses wall-clock time so state survives restarts and is comparable across
processes. A backwards clock step counts as expired rather than extending a
claim.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Hashable

logger = logging.getLogger("sentinel.runtime_state")

Clock = Callable[[], float]


def _elapsed(now: float, then: float) -> float:
    # A backwards clock step counts as fully elapsed: a possible duplicate alert
    # is better than a swallowed one.
    delta = now - then
    return delta if delta >= 0 else float("inf")


class ExpiringClaims:
    """Claim a key for `ttl` seconds; return whether it was already claimed.

    Check and record are one atomic step (SET NX EX in Redis). max_keys bounds
    the in-process table; expired entries are dropped on write.
    """

    # False for Redis. Hot paths use it to decide whether a claim needs a thread.
    is_local = True

    def __init__(self, *, max_keys: int = 10_000, clock: Clock = time.time) -> None:
        self._claimed_at: dict[Hashable, float] = {}
        self._max_keys = max_keys
        self._clock = clock

    def claim(self, key: Hashable, ttl_seconds: float) -> bool:
        """True if the claim was taken (caller acts), False if still held."""
        now = self._clock()
        previous = self._claimed_at.get(key)
        if previous is not None and _elapsed(now, previous) < ttl_seconds:
            return False
        self._claimed_at[key] = now
        self._evict(now, ttl_seconds)
        return True

    def release(self, key: Hashable) -> None:
        self._claimed_at.pop(key, None)

    def clear(self) -> None:
        self._claimed_at.clear()

    def __len__(self) -> int:
        return len(self._claimed_at)

    def __iter__(self):
        # tests assert on key count/size, the bound is a security property
        return iter(list(self._claimed_at))

    def _evict(self, now: float, ttl_seconds: float) -> None:
        if len(self._claimed_at) <= self._max_keys:
            return
        # expired ones first, that changes nothing
        for key in [k for k, at in self._claimed_at.items() if _elapsed(now, at) >= ttl_seconds]:
            self._claimed_at.pop(key, None)
        if len(self._claimed_at) <= self._max_keys:
            return
        # Still over: drop the oldest live claims, closest to expiring anyway.
        # The newest are the likeliest to be holding back a duplicate right now.
        for key, _ in sorted(self._claimed_at.items(), key=lambda kv: kv[1])[
            : len(self._claimed_at) - self._max_keys
        ]:
            self._claimed_at.pop(key, None)


class SlidingWindow:
    """Count recent events per key in a rolling window (login limiter).

    Keys are attacker-controlled, so the table is bounded, but an entry at the
    limit is never evicted, so eviction can't clear a real lockout.
    """

    is_local = True  # see ExpiringClaims.is_local

    def __init__(self, *, max_keys: int = 10_000, clock: Clock = time.time) -> None:
        self._events: dict[Hashable, list[float]] = {}
        self._max_keys = max_keys
        self._clock = clock

    def record(self, key: Hashable, window_seconds: float, *, limit: int) -> int:
        """Record one event and return the count in the window. `limit` is used
        only to protect entries at the limit from eviction."""
        now = self._clock()
        events = [t for t in self._events.get(key, []) if _elapsed(now, t) < window_seconds]
        events.append(now)
        self._events[key] = events
        self._evict(now, window_seconds, limit)
        return len(events)

    def count(self, key: Hashable, window_seconds: float) -> int:
        now = self._clock()
        events = [t for t in self._events.get(key, []) if _elapsed(now, t) < window_seconds]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return len(events)

    def forget(self, key: Hashable) -> None:
        """Clear a key's history, e.g. after a successful login."""
        self._events.pop(key, None)

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        # tests assert on key count/size, the bound is a security property
        return iter(list(self._events))

    def _evict(self, now: float, window_seconds: float, limit: int) -> None:
        if len(self._events) <= self._max_keys:
            return
        for key in [k for k, ts in self._events.items()
                    if not any(_elapsed(now, t) < window_seconds for t in ts)]:
            self._events.pop(key, None)
        if len(self._events) <= self._max_keys:
            return
        # evict the ones furthest from the limit (fewest events, oldest
        # first), never one at the limit, so this can't clear an earned lockout
        evictable = sorted(
            ((len(ts), min(ts, default=now), k) for k, ts in self._events.items() if len(ts) < limit),
        )
        for _, _, key in evictable[: len(self._events) - self._max_keys]:
            self._events.pop(key, None)


# Redis implementations. redis is imported lazily so the module works without
# the package. Failures are logged once per process and fail open: claim()
# returns True (the alert fires) and count()/record() return 0 (nobody is
# rate-limited). A Redis outage must never swallow an alert or lock users out.


def _redis_key(prefix: str, key: Hashable) -> str:
    # repr not str: str(("a", "b")) and str(1) vs "1" can collide, which would
    # merge two cooldowns
    return f"{prefix}:{key!r}"


class RedisExpiringClaims:
    """ExpiringClaims shared through Redis: SET key 1 NX EX ttl, atomic
    check-and-claim."""

    is_local = False

    def __init__(self, client, prefix: str) -> None:
        self._client = client
        self._prefix = prefix
        self._warned = False

    def _warn_once(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            logger.warning(
                "runtime_state: Redis unavailable for %s (%s) -- failing open "
                "until it recovers; further errors this process are not "
                "logged individually",
                self._prefix, exc,
            )

    def claim(self, key: Hashable, ttl_seconds: float) -> bool:
        import math
        try:
            granted = self._client.set(
                _redis_key(self._prefix, key), "1",
                nx=True, ex=max(1, math.ceil(ttl_seconds)),
            )
            return bool(granted)
        except Exception as exc:  # redis.RedisError and friends
            self._warn_once(exc)
            return True  # fail open: the action proceeds

    def release(self, key: Hashable) -> None:
        try:
            self._client.delete(_redis_key(self._prefix, key))
        except Exception as exc:
            self._warn_once(exc)

    def clear(self) -> None:
        # tests/ops only. scan_iter not KEYS, KEYS blocks the whole server
        try:
            for k in self._client.scan_iter(match=f"{self._prefix}:*", count=500):
                self._client.delete(k)
        except Exception as exc:
            self._warn_once(exc)

    def __len__(self) -> int:
        try:
            return sum(1 for _ in self._client.scan_iter(match=f"{self._prefix}:*", count=500))
        except Exception as exc:
            self._warn_once(exc)
            return 0


class RedisSlidingWindow:
    """SlidingWindow shared through Redis: one sorted set per key scored by
    event time, pruned with ZREMRANGEBYSCORE and counted with ZCARD. Members
    get a random suffix so events in the same millisecond all count.
    """

    is_local = False

    def __init__(self, client, prefix: str) -> None:
        self._client = client
        self._prefix = prefix
        self._warned = False

    def _warn_once(self, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            logger.warning(
                "runtime_state: Redis unavailable for %s (%s) -- failing open "
                "until it recovers; further errors this process are not "
                "logged individually",
                self._prefix, exc,
            )

    def record(self, key: Hashable, window_seconds: float, *, limit: int) -> int:
        import math
        redis_key = _redis_key(self._prefix, key)
        try:
            now = time.time()
            member = f"{now!r}:{uuid.uuid4().hex[:8]}"
            pipe = self._client.pipeline()
            pipe.zremrangebyscore(redis_key, "-inf", now - window_seconds)
            pipe.zadd(redis_key, {member: now})
            # backstop TTL so an abandoned key doesn't sit in Redis forever.
            # individual events expire via ZREMRANGEBYSCORE above
            pipe.expire(redis_key, max(1, math.ceil(window_seconds * 2)))
            pipe.zcard(redis_key)
            results = pipe.execute()
            return int(results[-1])
        except Exception as exc:
            self._warn_once(exc)
            return 0  # fail open: nothing is rate-limited

    def count(self, key: Hashable, window_seconds: float) -> int:
        redis_key = _redis_key(self._prefix, key)
        try:
            now = time.time()
            pipe = self._client.pipeline()
            pipe.zremrangebyscore(redis_key, "-inf", now - window_seconds)
            pipe.zcard(redis_key)
            results = pipe.execute()
            return int(results[-1])
        except Exception as exc:
            self._warn_once(exc)
            return 0

    def forget(self, key: Hashable) -> None:
        try:
            self._client.delete(_redis_key(self._prefix, key))
        except Exception as exc:
            self._warn_once(exc)

    def clear(self) -> None:
        try:
            for k in self._client.scan_iter(match=f"{self._prefix}:*", count=500):
                self._client.delete(k)
        except Exception as exc:
            self._warn_once(exc)

    def __len__(self) -> int:
        try:
            return sum(1 for _ in self._client.scan_iter(match=f"{self._prefix}:*", count=500))
        except Exception as exc:
            self._warn_once(exc)
            return 0


def _try_connect(redis_url: str, connect_timeout: float):
    """One ping at startup; a slow Redis counts as absent. Later outages are
    handled by each store failing open."""
    try:
        import redis as redis_module
    except ImportError:
        logger.warning(
            "runtime_state: REDIS_URL is set but the `redis` package is not "
            "installed -- falling back to in-process state"
        )
        return None
    try:
        client = redis_module.Redis.from_url(
            redis_url, socket_connect_timeout=connect_timeout, socket_timeout=connect_timeout,
        )
        client.ping()
        return client
    except Exception as exc:
        logger.warning(
            "runtime_state: could not reach Redis at startup (%s) -- falling "
            "back to in-process state for this process's lifetime", exc,
        )
        return None


# set by the first build_* call; one connection pool shared by every store
_redis_client = "unattempted"


def get_redis_client(settings):
    """Shared client, or None if REDIS_URL is unset or unreachable. Decided once
    per process."""
    global _redis_client
    if _redis_client == "unattempted":
        if settings.redis_url:
            _redis_client = _try_connect(settings.redis_url, settings.redis_connect_timeout_seconds)
        else:
            _redis_client = None
    return _redis_client


def build_claims_store(name: str, settings, *, max_keys: int = 10_000):
    """In-process claims, or a Redis table shared under `name` if REDIS_URL
    is reachable. max_keys only bounds the in-process store."""
    client = get_redis_client(settings)
    if client is None:
        return ExpiringClaims(max_keys=max_keys)
    return RedisExpiringClaims(client, prefix=f"sentinel:claims:{name}")


def build_sliding_window(name: str, settings, *, max_keys: int = 10_000):
    """Same as build_claims_store for window counters."""
    client = get_redis_client(settings)
    if client is None:
        return SlidingWindow(max_keys=max_keys)
    return RedisSlidingWindow(client, prefix=f"sentinel:window:{name}")
