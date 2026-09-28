"""Runtime state that has to be shared between processes.

The alert cooldown and self-heal duplicate window (ExpiringClaims) and the
login rate limiter (SlidingWindow) live here, in-process by default and in
Redis when REDIS_URL is set. As plain module dicts they were right for one
process only: a second worker doubles every alert and an attacker clears a
login lockout by landing on the other process.

Not everything belongs here. plate_tracker._TRACKS, rules_engine._zone_presence
and the clips ring buffers are keyed by camera or track, and one camera is
processed by one worker, so they're per-owner state. Putting them in Redis
would add a round trip per frame for nothing. They do rely on camera
ownership being exclusive, which is the camera-lease work, not this.

Wall clock, not time.monotonic(): monotonic time has a per-process origin, so
it can't be shared, and a restart resets it and re-fires every suppressed
alert. Wall clock can jump backwards (NTP, manual change), so a negative
elapsed time counts as expired instead of suppressing alerts until the clock
catches up.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Hashable

logger = logging.getLogger("sentinel.runtime_state")

Clock = Callable[[], float]


def _elapsed(now: float, then: float) -> float:
    # a backwards clock step would read as "no time passed" and hold a claim
    # open forever. treat it as fully elapsed: better a possible duplicate
    # alert than a swallowed real one
    delta = now - then
    return delta if delta >= 0 else float("inf")


class ExpiringClaims:
    """Claim a key for a while; say whether it was already claimed.

    Check and record have to be one step, or a second caller can see the key
    free in between. In Redis that's SET key NX EX ttl, hence one call
    returning a bool instead of get/set.

    max_keys bounds the table. Keys come from camera ids, track ids and plate
    text, not attacker input, but a busy camera makes a new track id every
    few seconds forever. Expired entries are dropped on write; the bound is
    the backstop.
    """

    # False on the Redis version. Hot paths (camera loop) use it to decide
    # whether a claim needs a thread: local is a dict op, Redis is a round
    # trip that must not run on the event loop.
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
    """Count recent events per key in a rolling window.

    For the login limiter, where the count is the decision ("5 failures in
    60s"). Redis version is a sorted set trimmed by score.

    Keys are attacker-controlled there, so the table must be bounded, and the
    bound must never clear a real lockout: an entry at the limit is never
    evicted.
    """

    is_local = True  # see ExpiringClaims.is_local

    def __init__(self, *, max_keys: int = 10_000, clock: Clock = time.time) -> None:
        self._events: dict[Hashable, list[float]] = {}
        self._max_keys = max_keys
        self._clock = clock

    def record(self, key: Hashable, window_seconds: float, *, limit: int) -> int:
        """Record one event, return how many are in the window now. limit
        isn't enforced here, eviction uses it to keep entries at the limit."""
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


# Redis versions. Same interfaces, a round trip instead of a dict lookup.
# redis is imported lazily in build_*(): most deployments leave REDIS_URL
# empty and this module has to import without the package installed.
#
# Failure policy: log once per process (an outage would flood the log
# otherwise) and fail open. claim() returns True so the alert fires, count()/
# record() return 0 so nobody gets rate-limited. Redis being down must never
# swallow a real alert or lock an operator out.


def _redis_key(prefix: str, key: Hashable) -> str:
    # repr not str: str(("a", "b")) and str(1) vs "1" can collide, which would
    # merge two cooldowns
    return f"{prefix}:{key!r}"


class RedisExpiringClaims:
    """ExpiringClaims shared by every process on the same Redis. It's just
    SET key 1 NX EX ttl; NX makes check-and-claim one atomic call, closing
    the race a GET then SET would have."""

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
    """SlidingWindow shared across processes. Each key is a sorted set scored
    by event time: ZREMRANGEBYSCORE prunes outside the window and ZCARD
    counts, same prune-then-count as the local version but server side.

    Members must be unique, and a bare timestamp isn't on a fast machine, so
    a short random suffix is added so two events in one millisecond both count.
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
    """One ping at startup. Slow counts as absent since this blocks startup;
    later degradation is handled by each store's fail-open."""
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
    """Shared client, or None if REDIS_URL is unset or unreachable. Decided
    once per process; a process that started without Redis stays that way."""
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
