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

import time
from typing import Callable, Hashable

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
