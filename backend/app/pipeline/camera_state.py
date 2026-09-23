"""Camera runtime lifecycle state, extracted from worker.py (the per-camera
background task, which now only ORCHESTRATES this rather than also owning
it).

`CAMERA_STATS` is the in-memory, process-local diagnostic/lifecycle record
for every camera a worker has ever touched this process, keyed by camera id.
`grid_state` inside it is the real 9-state connection lifecycle
(DISCOVERING/CONNECTING/CONNECTED/PROCESSING/DEGRADED/RECONNECTING/
DISCONNECTED/AUTH_ERROR/ERROR); `_TRANSITIONS` is which moves between those
are legal, `_set_grid_state` is the one place a camera's state actually
changes, and `_DB_STATUS_FOR_GRID_STATE` is the one place that maps down to
the legacy 3-value `Camera.status` DB column every other write site in
worker.py derives from.

Nothing here touches cv2, torch, a database session, or the event loop —
this is pure in-memory bookkeeping, which is what makes it safe to import
from anywhere (routers, tests, other pipeline modules) without pulling in
the camera loop's heavier dependencies.
"""
import logging
from typing import Any

logger = logging.getLogger("sentinel.worker")

# Phase 4 diagnostics — per-camera runtime counters for the concurrency
# investigation (frame/inference latency, drops, reconnects, last error).
# In-memory, process-local, intentionally lightweight (a temporary
# diagnostic surface per the Phase 4 brief, not a metrics system).
CAMERA_STATS: dict[str, dict[str, Any]] = {}


def _stats(camera_id: str) -> dict[str, Any]:
    return CAMERA_STATS.setdefault(camera_id, {
        "started_at": None,
        "frames_read": 0,
        "frames_processed": 0,
        "read_failures": 0,
        "reconnects": 0,
        "recovered_errors": 0,
        "last_loop_at": None,
        "last_read_ms": None,
        "read_ms_ema": None,
        "last_inference_ms": None,
        "inference_ms_ema": None,
        "loop_gap_ms_ema": None,  # wall-clock time between consecutive loop iterations
        "last_error": None,
        # Richer connection-lifecycle state (final integration task), surfaced
        # via GET /api/cameras/{id}/diagnostics. Deliberately kept separate from
        # Camera.status (DB column, only ever online/offline/degraded — many
        # other call sites already depend on that 3-value contract) rather than
        # migrating it, per "reuse existing, don't redesign."
        #
        # None, not "CONNECTING": this dict is created by `setdefault` the
        # first time ANYTHING asks about a camera, which is not the same
        # moment a worker starts one. A prior version primed this to
        # "CONNECTING", which meant a camera's first REAL transition was
        # checked as if it were "CONNECTING -> whatever" once transition
        # legality started being checked — every fresh camera looked like an
        # illegal transition on its very first move. None means "no lifecycle
        # observed yet", and _set_grid_state treats a None previous state as
        # unconditionally legal, which is the only correct rule for a state
        # that was never really entered.
        "grid_state": None,
    })


# Valid values for CAMERA_STATS[...]["grid_state"].
GRID_STATES = {
    "DISCOVERING", "CONNECTING", "CONNECTED", "PROCESSING", "DEGRADED",
    "RECONNECTING", "DISCONNECTED", "AUTH_ERROR", "ERROR",
}

# Which transitions this lifecycle actually makes. The previous guard checked
# that the NEW state was a known name, which is the weaker half of the
# question — "PROCESSING" is a valid name and a nonsense destination from
# DISCONNECTED, and nothing said so. It was also an `assert`, and asserts are
# stripped under `python -O`, so the one check there was could vanish in an
# optimised run.
#
# Self-transitions are listed because the loop re-asserts its state on most
# iterations; leaving them out would make the common case the noisy one.
# Every state can reach DISCONNECTED (an operator can stop a camera at any
# point) and the two failure states (a source can fail at any point), so those
# are added to every row rather than repeated by hand.
_ALWAYS_REACHABLE = {"DISCONNECTED", "AUTH_ERROR", "ERROR"}
_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERING": {"DISCOVERING", "CONNECTING"},
    "CONNECTING": {"CONNECTING", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "CONNECTED": {"CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING"},
    "PROCESSING": {"PROCESSING", "CONNECTED", "DEGRADED", "RECONNECTING"},
    "DEGRADED": {"DEGRADED", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "RECONNECTING": {"RECONNECTING", "CONNECTED", "PROCESSING", "DEGRADED"},
    # A stopped or failed camera comes back only by being started again --
    # AND, real sequence caught live by this table's own instrumentation
    # (not a hypothetical): a fresh worker's very first connect attempt can
    # fail fast enough that `_open_with_timeout` marks DISCONNECTED before
    # its caller's own fallback retries via `_reopen_with_backoff` in the
    # SAME call, which marks RECONNECTING immediately after. That two-line
    # fallback (`if not opened: opened = await _reopen_with_backoff(...)`)
    # is intended behaviour, not a bug the table should be flagging.
    "DISCONNECTED": {"DISCONNECTED", "DISCOVERING", "CONNECTING", "RECONNECTING"},
    "AUTH_ERROR": {"AUTH_ERROR", "DISCOVERING", "CONNECTING"},
    # ERROR is set by the loop's catch-all; the comment at that call site says
    # the next successful iteration flips it straight back, so it reaches the
    # running states directly rather than via CONNECTING.
    "ERROR": {"ERROR", "DISCOVERING", "CONNECTING", "RECONNECTING",
              "CONNECTED", "PROCESSING", "DEGRADED"},
}
for _from, _to in _TRANSITIONS.items():
    _to |= _ALWAYS_REACHABLE

#: Illegal transitions observed at runtime, keyed by (from, to). Read by the
#: diagnostics endpoint and by tests. A count here is a bug report about this
#: table or about the lifecycle, and it is deliberately a COUNT rather than an
#: exception — see _set_grid_state.
ILLEGAL_TRANSITIONS: dict[tuple[str, str], int] = {}


def _set_grid_state(camera_id: str, state: str) -> None:
    """Move a camera to `state`, recording the move if it is not a legal one.

    An illegal transition is applied, not refused. Refusing would leave
    CAMERA_STATS asserting something the camera is no longer doing, which is
    the exact class of bug this lifecycle exists to remove — and raising here
    would kill a live camera worker over a gap in the table above. So the
    transition happens, the violation is counted where a test and the
    diagnostics endpoint can both see it, and the log line names the pair so
    it can be fixed. `test_camera_state_machine.py` asserts the counter is
    empty after driving the real lifecycle, which is what turns this from a
    log nobody reads into a failing build.
    """
    if state not in GRID_STATES:
        raise ValueError(f"unknown grid_state: {state}")
    stats = _stats(camera_id)
    previous = stats.get("grid_state")
    if previous is not None and state not in _TRANSITIONS.get(previous, set()):
        ILLEGAL_TRANSITIONS[(previous, state)] = ILLEGAL_TRANSITIONS.get((previous, state), 0) + 1
        logger.warning(
            "camera %s: illegal state transition %s -> %s (applied anyway)",
            camera_id, previous, state,
        )
    stats["grid_state"] = state


#: Which DB `Camera.status` (the legacy 3-value online/offline/degraded
#: column — see the comment on `grid_state` above) a grid_state write should
#: also produce, wherever that write is one of the 8 sites in this file that
#: sets `camera.status`. A grid_state absent here (CONNECTING, DISCOVERING,
#: ERROR) means status is left untouched at that transition — matching, not
#: changing, the historical behaviour at every one of those 8 sites (verified
#: by reading each before this table existed; ERROR in particular is the
#: generic per-iteration exception handler, a genuine hot path where forcing
#: a DB write on every transient blip would be a real, unjustified cost).
#:
#: Used to derive `camera.status` from the SAME local variable a call site
#: passes to `_set_grid_state`, so the two literals a prior version of this
#: file wrote independently — "degraded" here, "DEGRADED" three lines away —
#: cannot drift apart the way they could when each was its own string.
_DB_STATUS_FOR_GRID_STATE: dict[str, str] = {
    "CONNECTED": "online",
    "PROCESSING": "online",
    "DEGRADED": "degraded",
    "RECONNECTING": "degraded",
    "DISCONNECTED": "offline",
    "AUTH_ERROR": "offline",
}


def _ema(prev: float | None, sample: float, alpha: float = 0.2) -> float:
    return sample if prev is None else (alpha * sample + (1 - alpha) * prev)
