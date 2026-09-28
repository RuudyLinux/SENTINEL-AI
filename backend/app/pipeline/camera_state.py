"""Camera runtime lifecycle state (split out of worker.py).

CAMERA_STATS is the in-memory per-camera diagnostic record, keyed by camera
id. Its grid_state is the 9-state connection lifecycle (DISCOVERING,
CONNECTING, CONNECTED, PROCESSING, DEGRADED, RECONNECTING, DISCONNECTED,
AUTH_ERROR, ERROR). _TRANSITIONS says which moves are legal,
_set_grid_state is the only place state changes, and
_DB_STATUS_FOR_GRID_STATE maps it to the 3-value Camera.status column.

No cv2, torch, DB session or event loop here, so anything can import it.
"""
import logging
from typing import Any

logger = logging.getLogger("sentinel.worker")

# per-camera runtime counters (latency, drops, reconnects, last error).
# in-memory, per-process, deliberately lightweight
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
        # Lifecycle state for GET /api/cameras/{id}/diagnostics, separate from
        # Camera.status which lots of code expects to be online/offline/degraded.
        #
        # None, not "CONNECTING": setdefault creates this the first time
        # anything asks about a camera, not when a worker starts it. Priming
        # it to CONNECTING made every fresh camera's first real move look
        # illegal. _set_grid_state treats None as "anything goes".
        "grid_state": None,
    })


# Valid values for CAMERA_STATS[...]["grid_state"].
GRID_STATES = {
    "DISCOVERING", "CONNECTING", "CONNECTED", "PROCESSING", "DEGRADED",
    "RECONNECTING", "DISCONNECTED", "AUTH_ERROR", "ERROR",
}

# Legal transitions. Only checking that the new state was a known name let
# DISCONNECTED -> PROCESSING through, and it was an assert, gone under -O.
#
# Self-transitions are listed since the loop re-asserts its state most
# iterations. Every state can reach DISCONNECTED (operator stop) and both
# failure states, so those get added to every row below.
_ALWAYS_REACHABLE = {"DISCONNECTED", "AUTH_ERROR", "ERROR"}
_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERING": {"DISCOVERING", "CONNECTING"},
    "CONNECTING": {"CONNECTING", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "CONNECTED": {"CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING"},
    "PROCESSING": {"PROCESSING", "CONNECTED", "DEGRADED", "RECONNECTING"},
    "DEGRADED": {"DEGRADED", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "RECONNECTING": {"RECONNECTING", "CONNECTED", "PROCESSING", "DEGRADED"},
    # A stopped or failed camera only comes back by starting again. Also seen
    # live: the first connect can fail fast enough that _open_with_timeout
    # marks DISCONNECTED and then _reopen_with_backoff, in the same call,
    # marks RECONNECTING. That fallback is intended.
    "DISCONNECTED": {"DISCONNECTED", "DISCOVERING", "CONNECTING", "RECONNECTING"},
    "AUTH_ERROR": {"AUTH_ERROR", "DISCOVERING", "CONNECTING"},
    # ERROR comes from the loop's catch-all and the next good iteration goes
    # straight back to a running state, not via CONNECTING
    "ERROR": {"ERROR", "DISCOVERING", "CONNECTING", "RECONNECTING",
              "CONNECTED", "PROCESSING", "DEGRADED"},
}
for _from, _to in _TRANSITIONS.items():
    _to |= _ALWAYS_REACHABLE

# Illegal transitions seen at runtime, (from, to) -> count. Read by the
# diagnostics endpoint and tests. A count, not an exception, see
# _set_grid_state.
ILLEGAL_TRANSITIONS: dict[tuple[str, str], int] = {}


def _set_grid_state(camera_id: str, state: str) -> None:
    """Move a camera to `state`, counting it if the move isn't legal.

    Illegal moves are still applied: refusing would leave CAMERA_STATS
    claiming something the camera isn't doing, and raising would kill a live
    worker over a gap in the table. The pair is counted and logged instead,
    and test_camera_state_machine.py asserts the counter stays empty after
    driving the real lifecycle.
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


# DB Camera.status a grid_state write also sets. States missing here
# (CONNECTING, DISCOVERING, ERROR) leave status alone, same as before; ERROR
# is the per-iteration catch-all and a DB write on every blip would cost.
#
# Call sites derive status from the same variable they pass to
# _set_grid_state, so "degraded" and "DEGRADED" can't drift apart again.
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
