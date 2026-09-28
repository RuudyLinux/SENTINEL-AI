"""How many cameras may run AI at once on this machine, and who gets a turn.

On the demo machine (docs/AI_ACCURACY.md) one AI camera holds the CPU at
~92% and a second pins it at 100%; the 4 GB GPU is tested at 2. Connecting
costs almost nothing, AI is what eats the machine, so the limit is on AI.

The worker asks for a slot before every inference and streams without AI
while it has none (try_acquire), so the limit holds however AI got turned on.

With ai_rotation_seconds > 0 the slots rotate: a camera that has held one
that long hands it over when another camera is waiting, and waiting cameras
get slots in the order they started waiting. So every connected camera gets
AI in turn instead of the first two keeping it. 0 = fixed slots, where the
API refuses AI on a full machine (blocked) instead of queueing.

Per process, like the worker registry; slots go away on restart.
"""
import time

from ..config import settings

# camera_id -> when it got the slot (monotonic)
_HOLDERS: dict[str, float] = {}
# camera_id -> last time a holder asked
_HOLDER_SEEN: dict[str, float] = {}
# camera_id -> (waiting since, last asked)
_WAITING: dict[str, tuple[float, float]] = {}
# Workers ask every frame. One that stops asking (stream stalled, stopped)
# loses its place after this long; 30s covers a slow RTSP handshake.
_STALE_S = 30.0

_clock = time.monotonic

LIMIT_MESSAGE = (
    "AI capacity limit reached ({limit} AI camera(s) on this machine). "
    "Stop AI on another camera first, or run the GPU runtime (see README)."
)


def _limit() -> int:
    return max(0, settings.max_ai_cameras)


def _rotating() -> bool:
    return settings.ai_rotation_seconds > 0


def _prune(now: float) -> None:
    for cid in [c for c, (_, asked) in _WAITING.items() if now - asked > _STALE_S]:
        _WAITING.pop(cid, None)
    if _rotating():
        # a holder that went quiet (stalled stream) mustn't sit on a slot
        for cid in [c for c in _HOLDERS if now - _HOLDER_SEEN.get(c, _HOLDERS[c]) > _STALE_S]:
            _HOLDERS.pop(cid, None)
            _HOLDER_SEEN.pop(cid, None)


def _next_in_line(camera_id: str) -> bool:
    free = _limit() - len(_HOLDERS)
    queue = sorted(_WAITING, key=lambda c: _WAITING[c][0])
    return camera_id in queue[:max(0, free)]


def try_acquire(camera_id: str) -> bool:
    now = _clock()
    _prune(now)
    if camera_id in _HOLDERS:
        _HOLDER_SEEN[camera_id] = now
        held_for = now - _HOLDERS[camera_id]
        if _rotating() and held_for >= settings.ai_rotation_seconds and any(c != camera_id for c in _WAITING):
            # turn's over, back of the queue
            del _HOLDERS[camera_id]
            _HOLDER_SEEN.pop(camera_id, None)
            _WAITING[camera_id] = (now, now)
            return False
        return True
    since = _WAITING[camera_id][0] if camera_id in _WAITING else now
    _WAITING[camera_id] = (since, now)
    if len(_HOLDERS) < _limit() and (not _rotating() or _next_in_line(camera_id)):
        _WAITING.pop(camera_id, None)
        _HOLDERS[camera_id] = now
        _HOLDER_SEEN[camera_id] = now
        return True
    return False


def release(camera_id: str) -> None:
    _HOLDERS.pop(camera_id, None)
    _HOLDER_SEEN.pop(camera_id, None)
    _WAITING.pop(camera_id, None)


def holders() -> set[str]:
    return set(_HOLDERS)


def waiting() -> list[str]:
    """Cameras waiting for a slot, next first."""
    _prune(_clock())
    return sorted(_WAITING, key=lambda c: _WAITING[c][0])


def blocked(camera_id: str) -> "str | None":
    """Refusal message if camera_id can't have AI now, else None. Never
    refuses while rotating; the camera just waits for its turn."""
    if _rotating() or camera_id in _HOLDERS or len(_HOLDERS) < _limit():
        return None
    return LIMIT_MESSAGE.format(limit=settings.max_ai_cameras)


def reset() -> None:
    _HOLDERS.clear()
    _HOLDER_SEEN.clear()
    _WAITING.clear()
