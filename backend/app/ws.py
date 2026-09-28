"""Live event fan-out to connected dashboards.

Event names are canonical `domain.action` values in EventType. The old ad hoc
names are still sent next to them for low-frequency events (LEGACY_ALIASES)
so unmigrated consumers keep working; that's an extra frame per alert, which
is fine, but not per detection.

Detections are the only high-frequency event (N cameras x inference rate,
each a React update in every dashboard), so they're coalesced into one
detection.batch frame per flush interval. Alerts, incidents and camera state
are never batched; an extra 250ms on a CRITICAL watchlist hit isn't worth it.
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


# canonical -> old name also sent. low-frequency only, aliasing detections
# would undo the batching
LEGACY_ALIASES = {
    EventType.ALERT_CREATED: "alert",
    EventType.SELF_HEAL_RECOVERY: "self_heal_event",
}

# canonical -> the batch envelope it's coalesced into
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
        """Send one event to every client right now. Low-level; publish() adds
        aliasing and batching on top.

        Iterates a copy of self.active: send_text yields, and a client
        disconnecting meanwhile (manager.disconnect from its own receive
        task) mutated the list and could skip a live client for that
        broadcast, CRITICAL alerts included. tests/test_ws_broadcast_race.py
        reproduces it.
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
        """Batch high-frequency events, send the rest now, plus any legacy alias."""
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
        # Hard cap so a stalled flush can't turn into a memory problem. Drop
        # the oldest, the newest matter most on a live feed, and the batch
        # reports how many were dropped.
        buffer.append({"type": event_type, **payload})
        overflow = len(buffer) - settings.ws_batch_max_events
        if overflow > 0:
            del buffer[:overflow]
        self._ensure_flush_task()

    def _ensure_flush_task(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        # Check for a loop before building the coroutine. create_task(
        # self._flush_loop()) inside a try builds the coroutine first, then the
        # RuntimeError drops it: a "never awaited" warning on every sync publish.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # no running loop (sync unit test calling a producer). the buffer
            # flushes on the next publish that has one
            self._flush_task = None
            return
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Flush the batch buffers on an interval. Exits once they're empty and
        _buffer restarts it, so an idle process has no timer running."""
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
        """Stop the flush task and send whatever's still buffered.

        The task is awaited, cancel() only asks. The final flush happens here,
        not in a CancelledError handler inside the task: a task cancelled
        before it ever ran never enters its body, so an event buffered right
        before shutdown was lost. _flush empties the buffers, so a double
        flush is harmless.
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
