"""How many cameras may run AI at once on this machine.

Measured on the demo machine (docs/AI_ACCURACY.md): one AI camera with the
selected model holds the CPU at ~92%, and a second takes it to 100% — every
camera's AI rate collapses and the machine stops responding well. Connecting a
camera costs almost nothing; running AI on it is what exhausts the CPU. So the
limit is on AI, not on connections.

A camera holds an AI slot while its worker runs inference. The worker takes the
slot before every inference and simply streams without AI when none is free
(`try_acquire`), which makes the limit hold whichever path turned AI on — API,
bulk action, startup resume, or supervisor. The API checks first (`blocked`) so
an operator gets a clear refusal instead of a camera that silently shows no AI.

Process-local, like the worker registry it guards: slots are held by this
process's workers and vanish with them on restart.
"""
from ..config import settings

_HOLDERS: set[str] = set()

LIMIT_MESSAGE = (
    "AI capacity limit reached ({limit} AI camera(s) on this machine). "
    "Stop AI on another camera first, or run the GPU runtime (see README)."
)


def try_acquire(camera_id: str) -> bool:
    if camera_id in _HOLDERS:
        return True
    if len(_HOLDERS) >= max(0, settings.max_ai_cameras):
        return False
    _HOLDERS.add(camera_id)
    return True


def release(camera_id: str) -> None:
    _HOLDERS.discard(camera_id)


def holders() -> set[str]:
    return set(_HOLDERS)


def blocked(camera_id: str) -> "str | None":
    """The refusal message if `camera_id` cannot start AI now, else None."""
    if camera_id in _HOLDERS or len(_HOLDERS) < max(0, settings.max_ai_cameras):
        return None
    return LIMIT_MESSAGE.format(limit=settings.max_ai_cameras)
