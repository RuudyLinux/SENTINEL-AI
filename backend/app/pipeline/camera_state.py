"""Camera runtime lifecycle state.

CAMERA_STATS holds per-camera in-memory diagnostics, including grid_state, the
connection lifecycle (DISCOVERING, CONNECTING, CONNECTED, PROCESSING, DEGRADED,
RECONNECTING, DISCONNECTED, AUTH_ERROR, ERROR). _set_grid_state is the only
place it changes; _DB_STATUS_FOR_GRID_STATE maps it to Camera.status.

Has no cv2, torch, DB or event-loop dependencies, so anything can import it.
"""
import logging
from typing import Any

logger = logging.getLogger("sentinel.worker")

# Per-camera runtime counters, in memory and per process.
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
        # Lifecycle state for the diagnostics endpoint, separate from
        # Camera.status. Starts as None (not CONNECTING) because the entry is
        # created on first lookup, not when a worker starts.
        "grid_state": None,
    })


# Valid values for CAMERA_STATS[...]["grid_state"].
GRID_STATES = {
    "DISCOVERING", "CONNECTING", "CONNECTED", "PROCESSING", "DEGRADED",
    "RECONNECTING", "DISCONNECTED", "AUTH_ERROR", "ERROR",
}

# Legal transitions. Self-transitions are listed because the loop re-asserts
# its state; every state can reach DISCONNECTED and both failure states.
_ALWAYS_REACHABLE = {"DISCONNECTED", "AUTH_ERROR", "ERROR"}
_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERING": {"DISCOVERING", "CONNECTING"},
    "CONNECTING": {"CONNECTING", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "CONNECTED": {"CONNECTED", "PROCESSING", "DEGRADED", "RECONNECTING"},
    "PROCESSING": {"PROCESSING", "CONNECTED", "DEGRADED", "RECONNECTING"},
    "DEGRADED": {"DEGRADED", "CONNECTED", "PROCESSING", "RECONNECTING"},
    "RECONNECTING": {"RECONNECTING", "CONNECTED", "PROCESSING", "DEGRADED"},
    # A stopped or failed camera only comes back by starting again. A fast
    # first-connect failure can go DISCONNECTED -> RECONNECTING in one call.
    "DISCONNECTED": {"DISCONNECTED", "DISCOVERING", "CONNECTING", "RECONNECTING"},
    "AUTH_ERROR": {"AUTH_ERROR", "DISCOVERING", "CONNECTING"},
    # ERROR comes from the loop's catch-all and the next good iteration goes
    # straight back to a running state, not via CONNECTING
    "ERROR": {"ERROR", "DISCOVERING", "CONNECTING", "RECONNECTING",
              "CONNECTED", "PROCESSING", "DEGRADED"},
}
for _from, _to in _TRANSITIONS.items():
    _to |= _ALWAYS_REACHABLE

# Illegal transitions seen at runtime, (from, to) -> count; read by the
# diagnostics endpoint and tests.
ILLEGAL_TRANSITIONS: dict[tuple[str, str], int] = {}


def _set_grid_state(camera_id: str, state: str) -> None:
    """Move a camera to `state`, counting the move if it isn't legal.

    Illegal moves are still applied, so CAMERA_STATS never misreports the
    camera and a gap in the table can't kill a live worker. The counter should
    stay empty (test_camera_state_machine.py).
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


# Camera.status set alongside each grid_state. States not listed leave status
# unchanged; ERROR is a per-iteration catch-all and not worth a DB write.
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
