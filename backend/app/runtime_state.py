"""Runtime state that must be shared between processes, behind one seam.

Every coordination primitive in this application is currently a module-level
dict: the alert cooldown (`rules_engine._last_alert_at`), the self-heal
duplicate window (`self_heal.engine._last_recovered_at`), the login rate
limiter (`routers.auth._failed_attempts`), the camera worker registry
(`worker.RUNNING`, `worker.CAMERA_STATS`), grid ownership
(`supervisor.AUTO_MANAGED`) and the bulk-action mutex
(`camera_control._IN_PROGRESS`).

That is correct for exactly one process. Start a second worker and the
consequences are not subtle: every alert fires twice, every cooldown is
halved, and an attacker clears a login lockout by being routed to the other
process. Each of those modules already says in a comment that it is
single-process; none of them had anywhere better to put the state.

This module is that somewhere. It holds the two primitives that genuinely
need to be shared, implemented in-process today with the same semantics a
Redis implementation would have, so moving to Redis later is a swap of these
two classes rather than an edit in seven modules.

**Not everything belongs here.** `plate_tracker._TRACKS` (per-track OCR
consensus), `rules_engine._zone_presence` (dwell time), and `clips._RING` /
`clips._SUBSCRIBERS` (pre-event frame buffers) are keyed by a camera or a
track within one camera, and a camera is processed by exactly one worker.
They are per-owner state, not shared state, and moving them to Redis would
add a network round trip per frame to solve a problem that does not exist.
What those DO depend on is that camera ownership is actually exclusive —
which is the camera-lease work, not this.

**Why wall-clock and not `time.monotonic()`.** Both stores this replaces used
`time.monotonic()`. That is a per-process counter measured from an arbitrary
origin: two processes cannot compare theirs, so a monotonic timestamp cannot
be shared at all, and a restart resets it to near zero — which silently
re-fires every alert the cooldown was suppressing. Wall-clock can jump
backwards (NTP, a manual clock change), so every elapsed-time calculation
here clamps a negative delta to "expired now" rather than treating it as a
far-future expiry that would suppress alerts until the clock caught up.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Hashable

logger = logging.getLogger("sentinel.runtime_state")

Clock = Callable[[], float]


def _elapsed(now: float, then: float) -> float:
    """Seconds between two wall-clock readings, never negative.

    A backwards clock step would otherwise produce a negative elapsed time,
    which reads as "no time has passed" and holds a claim open indefinitely.
    Treating it as fully elapsed fails toward firing an alert that might be a
    duplicate, rather than suppressing one that is real.
    """
    delta = now - then
    return delta if delta >= 0 else float("inf")


class ExpiringClaims:
    """Claim a key for a bounded time; report whether it was already claimed.

    This is the shape shared by the alert cooldown and the self-heal duplicate
    window: "have I already acted on this key recently, and if not, record
    that I am about to." Both halves have to happen together — a caller that
    checks and then separately records leaves a gap in which a second caller
    also sees the key free. In Redis this is `SET key NX EX ttl`, which is why
    the claim is expressed as one call returning a bool rather than a
    get/set pair.

    `max_keys` bounds the table. Keys here are derived from camera ids, track
    ids and plate text, so they are not attacker-controlled the way the login
    limiter's are, but an unbounded dict on a long-running process is still a
    leak: a busy camera produces a new track id every few seconds forever.
    Expired entries are dropped on write, and the bound is a backstop for the
    case where writes arrive faster than entries expire.
    """

    #: False for the Redis-backed sibling below. A caller on a hot path (the
    #: camera loop) uses this to decide whether a claim needs to be dispatched
    #: to a thread — an in-process claim is a dict operation and dispatching
    #: it would only add thread-pool overhead for nothing; a Redis claim is a
    #: network round trip and MUST NOT run directly on the event loop.
    is_local = True

    def __init__(self, *, max_keys: int = 10_000, clock: Clock = time.time) -> None:
        self._claimed_at: dict[Hashable, float] = {}
        self._max_keys = max_keys
        self._clock = clock

    def claim(self, key: Hashable, ttl_seconds: float) -> bool:
        """True if the claim was taken, False if `key` is still claimed.

        The caller acts when this returns True. Note the polarity is the
        inverse of the `_on_cooldown` helper it replaces, which returned True
        to mean "suppressed" — stated as a claim, the truthy case is the one
        where something happens, which is the direction that reads correctly
        at the call site.
        """
        now = self._clock()
        previous = self._claimed_at.get(key)
        if previous is not None and _elapsed(now, previous) < ttl_seconds:
            return False
        self._claimed_at[key] = now
        self._evict(now, ttl_seconds)
        return True

    def release(self, key: Hashable) -> None:
        """Drop a claim early. Idempotent."""
        self._claimed_at.pop(key, None)

    def clear(self) -> None:
        self._claimed_at.clear()

    def __len__(self) -> int:
        return len(self._claimed_at)

    def __iter__(self):
        """The keys currently held. Exposed because the bounds on this table
        are a security property, not an implementation detail — a test has to
        be able to assert on key size and count directly."""
        return iter(list(self._claimed_at))

    def _evict(self, now: float, ttl_seconds: float) -> None:
        if len(self._claimed_at) <= self._max_keys:
            return
        # Expired entries first — dropping those changes no behaviour at all.
        for key in [k for k, at in self._claimed_at.items() if _elapsed(now, at) >= ttl_seconds]:
            self._claimed_at.pop(key, None)
        if len(self._claimed_at) <= self._max_keys:
            return
        # Still over: drop the OLDEST live claims, i.e. the ones closest to
        # expiring anyway. Dropping the newest would release the claims most
        # likely to be suppressing an active duplicate right now.
        for key, _ in sorted(self._claimed_at.items(), key=lambda kv: kv[1])[
            : len(self._claimed_at) - self._max_keys
        ]:
            self._claimed_at.pop(key, None)


class SlidingWindow:
    """Count recent events per key within a rolling window.

    Used by the login rate limiter, where the count itself is the decision
    ("five failures in sixty seconds") rather than a yes/no claim. Redis
    models this as a sorted set trimmed by score.

    The eviction policy below is carried over verbatim in intent from
    `routers/auth.py`, because it was written against a real finding: the keys
    are attacker-controlled, so an unbounded table is remote, unauthenticated,
    unbounded memory growth. The bound must never be usable to clear a real
    lockout, which is why an entry already at the limit is never evicted.
    """

    is_local = True  # see ExpiringClaims.is_local

    def __init__(self, *, max_keys: int = 10_000, clock: Clock = time.time) -> None:
        self._events: dict[Hashable, list[float]] = {}
        self._max_keys = max_keys
        self._clock = clock

    def record(self, key: Hashable, window_seconds: float, *, limit: int) -> int:
        """Record one event and return how many are now in the window.

        `limit` is not enforced here — it is passed so eviction can tell an
        entry that is at the limit (and must be kept) from one that is not.
        """
        now = self._clock()
        events = [t for t in self._events.get(key, []) if _elapsed(now, t) < window_seconds]
        events.append(now)
        self._events[key] = events
        self._evict(now, window_seconds, limit)
        return len(events)

    def count(self, key: Hashable, window_seconds: float) -> int:
        """How many events are in the window, without recording one."""
        now = self._clock()
        events = [t for t in self._events.get(key, []) if _elapsed(now, t) < window_seconds]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return len(events)

    def forget(self, key: Hashable) -> None:
        """Clear a key's history — a successful login, for instance."""
        self._events.pop(key, None)

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        """The keys currently held. Exposed because the bounds on this table
        are a security property, not an implementation detail — a test has to
        be able to assert on key size and count directly."""
        return iter(list(self._events))

    def _evict(self, now: float, window_seconds: float, limit: int) -> None:
        if len(self._events) <= self._max_keys:
            return
        for key in [k for k, ts in self._events.items()
                    if not any(_elapsed(now, t) < window_seconds for t in ts)]:
            self._events.pop(key, None)
        if len(self._events) <= self._max_keys:
            return
        # Evict the entries FURTHEST from being limited (fewest events, oldest
        # first) and never one already at the limit, so this cannot be used as
        # a way to clear a lockout that a real attack earned.
        evictable = sorted(
            ((len(ts), min(ts, default=now), k) for k, ts in self._events.items() if len(ts) < limit),
        )
        for _, _, key in evictable[: len(self._events) - self._max_keys]:
            self._events.pop(key, None)


# --- Redis-backed implementations -----------------------------------------
#
# Same two interfaces, same call signatures, a network round trip instead of
# a dict lookup. `redis` is imported lazily inside build_*() rather than at
# module scope: REDIS_URL is empty on most deployments (every one before this
# module existed), and this module must import cleanly whether or not the
# `redis` package happens to be installed at all.
#
# Failure policy, applied uniformly: a Redis error is logged ONCE per process
# (further errors during an outage would otherwise flood the log for as long
# as Redis stays down) and the call then returns the value that FAILS OPEN --
# `claim()` returns True (the action proceeds; a real alert firing when Redis
# hiccups is the same direction this codebase already takes with a missing
# corroboration signal -- see Vehicle.plate_corroborated's NULL handling), and
# `count()`/`record()` return 0 (nothing is rate-limited; the login limiter's
# own comment already says an availability failure is worse than the
# brute-force it defends against). Redis being down must never be the reason
# a real alert is silently swallowed or an operator is locked out.


def _redis_key(prefix: str, key: Hashable) -> str:
    """A key repr is used rather than str() -- repr distinguishes ("a", "b")
    from "a", "b" and 1 from "1", which str() does not, and two logically
    different in-process keys colliding into one Redis key would silently
    merge their cooldowns."""
    return f"{prefix}:{key!r}"


class RedisExpiringClaims:
    """ExpiringClaims, shared across every process pointed at the same
    Redis. SET key 1 NX EX ttl is the whole implementation: NX makes the set
    conditional on the key not existing, so "check, then claim" is one atomic
    round trip rather than two -- the same race a separate GET-then-SET would
    have between two processes is exactly what NX exists to close.
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
        """Test/ops use only -- a real deployment never clears this wholesale.
        scan_iter rather than KEYS: KEYS blocks the whole Redis server on a
        large keyspace, which nothing here needs even for a test."""
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
    """SlidingWindow, shared across every process. Each key is a Redis sorted
    set: the score is the event's wall-clock time, so
    ZREMRANGEBYSCORE key -inf (now-window) prunes everything outside the
    window in one call and ZCARD counts what is left -- the same two-step
    "prune, then count" the in-process version does, just server-side instead
    of by rebuilding a Python list.

    The member has to be unique per event (a Redis set cannot hold the same
    member twice), which a bare timestamp is not on a fast machine -- a short
    random suffix is appended so two events in the same millisecond both
    count.
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
            # Safety TTL well past the window: an abandoned key (an attacker
            # who stops, an account nobody logs into again) is not left in
            # Redis forever. Not the mechanism that expires individual
            # events -- ZREMRANGEBYSCORE above is -- only a backstop for the
            # key itself.
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
    """One ping at startup, not a connection pool health check on every call.

    A Redis that is merely SLOW is treated the same as one that is absent --
    this blocks application startup, and a slow dependency is not a distinct
    case worth a separate policy from a missing one. If it accepts real
    connections but degrades later, that is what each store's own per-call
    fail-open handles; this function only decides which implementation to
    hand back at boot.
    """
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


#: Set once at startup by the first build_claims_store/build_sliding_window
#: call (see get_redis_client). Reused so every store shares one connection
#: pool rather than opening a new one per cooldown/limiter.
_redis_client = "unattempted"


def get_redis_client(settings):
    """The shared client, or None if REDIS_URL is unset or unreachable.

    Connects at most once per process -- later calls, even after a transient
    failure, reuse that decision rather than re-probing Redis on every store
    construction. A process that started without Redis stays that way; that
    matches every other "discover once at boot" pattern in this codebase
    (the camera catalogue, the self-heal supervisor)."""
    global _redis_client
    if _redis_client == "unattempted":
        if settings.redis_url:
            _redis_client = _try_connect(settings.redis_url, settings.redis_connect_timeout_seconds)
        else:
            _redis_client = None
    return _redis_client


def build_claims_store(name: str, settings, *, max_keys: int = 10_000):
    """The store a module should hold at import time -- in-process unless
    REDIS_URL resolves to a reachable Redis, in which case every process
    pointed at the same URL shares one claim table under this `name`.

    `max_keys` only bounds the in-process fallback (Redis manages its own
    memory and eviction as a separate service, and has no equivalent
    per-store cap in this client) -- but it is accepted unconditionally
    rather than only in the fallback branch, so a caller's choice of bound
    does not silently stop applying the moment Redis becomes reachable.
    """
    client = get_redis_client(settings)
    if client is None:
        return ExpiringClaims(max_keys=max_keys)
    return RedisExpiringClaims(client, prefix=f"sentinel:claims:{name}")


def build_sliding_window(name: str, settings, *, max_keys: int = 10_000):
    """The window-counter equivalent of build_claims_store. Same `max_keys`
    caveat: it bounds the in-process fallback only."""
    client = get_redis_client(settings)
    if client is None:
        return SlidingWindow(max_keys=max_keys)
    return RedisSlidingWindow(client, prefix=f"sentinel:window:{name}")
