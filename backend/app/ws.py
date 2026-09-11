"""Live event fan-out to connected dashboards.

V2 gives the stream a real event vocabulary and a throttle.

**Vocabulary.** Events were previously named ad hoc at each call site
("detection", "alert", "self_heal_event"), so a consumer had to know every
producer to know what could arrive. Canonical `domain.action` names are now
declared in `EventType` and used everywhere.

**Compatibility.** The legacy names are still emitted alongside the canonical
ones for the low-frequency event types (see `LEGACY_ALIASES`), so a consumer
that was not migrated keeps working. This costs one extra small frame per alert
— not per detection — which is why it is affordable here and deliberately NOT
done for the high-frequency stream.

**Throttling.** Detections are the only genuinely high-frequency event: N
cameras x their inference rate, each previously its own WebSocket frame, every
one of them a React state update in every open dashboard. They are now
coalesced into one `detection.batch` frame per flush interval. Alerts,
incidents and camera-state changes are never batched — an operator waiting even
250ms extra for a CRITICAL watchlist hit is the wrong trade.
"""
import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from .config import settings

logger = logging.getLogger("sentinel.ws")


class EventType:
    """Canonical live-event names. `domain.action`, stable, machine-filterable."""
    DETECTION_CREATED = "detection.created"
    DETECTION_BATCH = "detection.batch"
    PLATE_DETECTED = "plate.detected"
    PLATE_UPDATED = "plate.updated"
    VEHICLE_SIGHTING = "vehicle.sighting"
    VEHICLE_ROUTE_UPDATED = "vehicle.route.updated"
    ALERT_CREATED = "alert.created"
    INCIDENT_CREATED = "incident.created"
    CAMERA_STATUS = "camera.status"
    CAMERA_HEALTH = "camera.health"
    SELF_HEAL_RECOVERY = "self_heal.recovery"
    BULK_PROGRESS = "bulk_progress"
    BULK_COMPLETE = "bulk_complete"


# Canonical name -> the pre-V2 name still emitted beside it. Only low-frequency
# events are aliased; duplicating the detection stream would defeat the batching.
LEGACY_ALIASES = {
    EventType.ALERT_CREATED: "alert",
    EventType.SELF_HEAL_RECOVERY: "self_heal_event",
}

# Canonical name -> the batch envelope its events are coalesced into.
BATCHED_INTO = {EventType.DETECTION_CREATED: EventType.DETECTION_BATCH}


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._flush_task: "asyncio.Task[None] | None" = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, event_type: str, payload: dict[str, Any]):
        """Send one event to every connected client, immediately.

        Kept as the low-level primitive (and as the pre-V2 entry point, so
        existing call sites and tests behave exactly as before). Prefer
        `publish`, which additionally applies aliasing and batching policy.

        BUG-2 fix (10/10 debugging pass, 2026-09-11): iterates a SNAPSHOT
        (`list(self.active)`), not `self.active` itself. `await ws.send_text`
        is a real yield point, and `main.py::websocket_endpoint` runs each
        connected client's receive-loop as its own concurrent task — the
        instant ANY client disconnects, that task calls
        `manager.disconnect(ws)`, mutating this SAME list. Iterating the live
        list directly meant a disconnect landing between two `send_text`
        calls could shift a still-connected, still-live client out of the
        iterator's reach for that one broadcast — silently skipping it,
        including a CRITICAL `alert.created` event. See
        tests/test_ws_broadcast_race.py for the deterministic reproduction.
        `main.py:209` (`for camera_id in list(RUNNING.keys())`) already uses
        this exact snapshot idiom elsewhere in this codebase for the
        identical reason; this was the one place it had been missed.
        """
        message = json.dumps({"type": event_type, "data": payload}, default=str)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit an event under the V2 policy: batched if it is high-frequency,
        otherwise sent immediately, plus its legacy alias if it has one."""
        batch_type = BATCHED_INTO.get(event_type)
        if batch_type is not None:
            self._buffer(event_type, batch_type, payload)
            return
        await self.broadcast(event_type, payload)
        alias = LEGACY_ALIASES.get(event_type)
        if alias is not None:
            await self.broadcast(alias, payload)

    def _buffer(self, event_type: str, batch_type: str, payload: dict[str, Any]) -> None:
        buffer = self._buffers.setdefault(batch_type, [])
        # Hard cap. If flushing ever stalls (no event loop scheduling the task,
        # a client blocking the send), the buffer must not grow without bound
        # and turn a delivery hiccup into a memory problem. The OLDEST entries
        # are dropped: on a live operations feed the newest events are the ones
        # that matter, and the batch says how many were dropped rather than
        # silently pretending the stream was complete.
        buffer.append({"type": event_type, **payload})
        overflow = len(buffer) - settings.ws_batch_max_events
        if overflow > 0:
            del buffer[:overflow]
        self._ensure_flush_task()

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.create_task(self._flush_loop())
        except RuntimeError:
            # No running event loop (e.g. a synchronous unit test calling into
            # a producer). Buffered events will flush on the next publish that
            # does have a loop; nothing is lost and nothing raises.
            self._flush_task = None

    async def _flush_loop(self) -> None:
        """Drain the batch buffers on a fixed interval.

        Runs only while there is something to send: it exits once the buffers
        are empty, and `_buffer` restarts it on the next event. That keeps an
        idle process genuinely idle instead of holding a permanent timer.
        """
        while True:
            await asyncio.sleep(settings.ws_batch_interval_seconds)
            if not await self._flush():
                return

    async def _flush(self) -> bool:
        """Send every non-empty buffer. Returns whether anything was sent."""
        sent = False
        for batch_type, buffer in list(self._buffers.items()):
            if not buffer:
                continue
            events, self._buffers[batch_type] = buffer, []
            await self.broadcast(batch_type, {"events": events, "count": len(events)})
            sent = True
        return sent

    async def shutdown(self) -> None:
        """Stop the flush task and deliver whatever is still buffered.

        Same deterministic-cleanup contract as the camera workers (see main.py's
        _on_shutdown): `task.cancel()` alone only REQUESTS cancellation, so the
        task is awaited rather than abandoned.

        The final flush is done HERE, explicitly, rather than in a
        `except CancelledError` handler inside the task. A task cancelled before
        the event loop ever scheduled it never enters its own body at all, so
        its cancellation handler never runs — which meant an event buffered
        immediately before shutdown was silently dropped. Flushing from the
        caller covers that case and every other one. `_flush` empties the
        buffers, so this is safe even if the task did already flush.
        """
        task, self._flush_task = self._flush_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("websocket flush task failed during shutdown")
        try:
            await self._flush()
        except Exception:
            logger.exception("final websocket batch flush failed during shutdown")


manager = ConnectionManager()
