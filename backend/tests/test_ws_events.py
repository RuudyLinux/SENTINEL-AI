"""V2 Phase 4 — live event vocabulary, batching and legacy compatibility.

Two things must both hold: the stream gains canonical `domain.action` names and
a throttle on the one genuinely high-frequency event type, AND every pre-V2
consumer keeps working without being touched.
"""
import asyncio
import json

import pytest

from app.config import settings
from app.ws import ConnectionManager, EventType, LEGACY_ALIASES, BATCHED_INTO


class _FakeSocket:
    """Records what was sent, in order, without a real WebSocket."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]

    def of_type(self, event_type: str) -> list[dict]:
        return [m["data"] for m in self.sent if m["type"] == event_type]


def _manager_with_client():
    manager = ConnectionManager()
    socket = _FakeSocket()
    manager.active.append(socket)  # bypass accept(); this is not a transport test
    return manager, socket


class TestImmediateEvents:
    def test_an_alert_is_sent_immediately_not_batched(self):
        """An operator must never wait on a flush interval for a CRITICAL hit."""
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.ALERT_CREATED, {"id": "alt_1", "severity": "CRITICAL"})
            return socket

        socket = asyncio.run(scenario())
        assert EventType.ALERT_CREATED in socket.types()
        assert socket.of_type(EventType.ALERT_CREATED)[0]["severity"] == "CRITICAL"

    def test_an_incident_is_sent_immediately(self):
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.INCIDENT_CREATED, {"id": "inc_1"})
            return socket

        assert EventType.INCIDENT_CREATED in asyncio.run(scenario()).types()

    def test_a_vehicle_sighting_is_sent_immediately(self):
        """A plate identification is the headline V2 event — it is low
        frequency (one per vehicle per camera, not per frame) and must not be
        delayed behind the detection batch."""
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.VEHICLE_SIGHTING, {"plate_text": "GJ05AB1234"})
            return socket

        assert EventType.VEHICLE_SIGHTING in asyncio.run(scenario()).types()


class TestLegacyCompatibility:
    """The pre-V2 frontend listens for "alert" and "self_heal_event". Those
    consumers must keep working without being migrated."""

    def test_an_alert_also_arrives_under_its_legacy_name(self):
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.ALERT_CREATED, {"id": "alt_1"})
            return socket

        types = asyncio.run(scenario()).types()
        assert "alert" in types
        assert EventType.ALERT_CREATED in types

    def test_a_self_heal_event_also_arrives_under_its_legacy_name(self):
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.SELF_HEAL_RECOVERY, {"id": "sh_1"})
            return socket

        types = asyncio.run(scenario()).types()
        assert "self_heal_event" in types
        assert EventType.SELF_HEAL_RECOVERY in types

    def test_the_high_frequency_stream_is_not_aliased(self):
        """Duplicating every detection under a legacy name would defeat the
        batching it exists to enable."""
        assert EventType.DETECTION_CREATED not in LEGACY_ALIASES

    def test_the_legacy_payload_is_identical_to_the_canonical_one(self):
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.ALERT_CREATED, {"id": "alt_1", "severity": "HIGH"})
            return socket

        socket = asyncio.run(scenario())
        assert socket.of_type("alert") == socket.of_type(EventType.ALERT_CREATED)


class TestDetectionBatching:
    def test_detections_are_coalesced_into_one_frame(self, monkeypatch):
        monkeypatch.setattr(settings, "ws_batch_interval_seconds", 0.01)

        async def scenario():
            manager, socket = _manager_with_client()
            for i in range(5):
                await manager.publish(EventType.DETECTION_CREATED, {"detection_id": f"det_{i}"})
            # Nothing sent yet — the whole point of buffering.
            assert socket.sent == []
            await asyncio.sleep(0.05)
            await manager.shutdown()
            return socket

        socket = asyncio.run(scenario())
        batches = socket.of_type(EventType.DETECTION_BATCH)
        assert len(batches) == 1, "five detections must arrive as one frame, not five"
        assert batches[0]["count"] == 5
        assert [e["detection_id"] for e in batches[0]["events"]] == [f"det_{i}" for i in range(5)]

    def test_each_batched_event_keeps_its_own_type(self):
        """A batch is an envelope, not a type erasure — a consumer must still
        be able to tell what each event inside it was."""

        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.DETECTION_CREATED, {"detection_id": "det_1"})
            await manager.shutdown()  # cancellation triggers the final flush
            return socket

        batch = asyncio.run(scenario()).of_type(EventType.DETECTION_BATCH)[0]
        assert batch["events"][0]["type"] == EventType.DETECTION_CREATED

    def test_buffered_events_are_flushed_at_shutdown_not_dropped(self):
        async def scenario():
            manager, socket = _manager_with_client()
            await manager.publish(EventType.DETECTION_CREATED, {"detection_id": "det_last"})
            await manager.shutdown()
            return socket

        socket = asyncio.run(scenario())
        assert socket.of_type(EventType.DETECTION_BATCH)[0]["count"] == 1

    def test_the_buffer_is_capped_and_drops_the_oldest(self, monkeypatch):
        """If flushing ever stalls, a delivery hiccup must not become a memory
        problem — and on a live feed the newest events are the ones that matter."""
        monkeypatch.setattr(settings, "ws_batch_max_events", 3)

        async def scenario():
            manager, socket = _manager_with_client()
            for i in range(6):
                manager._buffer(
                    EventType.DETECTION_CREATED, EventType.DETECTION_BATCH, {"detection_id": f"det_{i}"}
                )
            await manager._flush()
            await manager.shutdown()
            return socket

        batch = asyncio.run(scenario()).of_type(EventType.DETECTION_BATCH)[0]
        assert batch["count"] == 3
        assert [e["detection_id"] for e in batch["events"]] == ["det_3", "det_4", "det_5"]

    def test_the_flush_task_stops_when_the_stream_goes_quiet(self, monkeypatch):
        """An idle process must be genuinely idle, not holding a permanent timer."""
        monkeypatch.setattr(settings, "ws_batch_interval_seconds", 0.01)

        async def scenario():
            manager, _ = _manager_with_client()
            await manager.publish(EventType.DETECTION_CREATED, {"detection_id": "det_1"})
            await asyncio.sleep(0.06)
            return manager

        manager = asyncio.run(scenario())
        assert manager._flush_task is None or manager._flush_task.done()

    def test_buffering_outside_an_event_loop_does_not_raise(self):
        """A synchronous caller must not crash on a producer call; the events
        flush on the next publish that does have a loop."""
        manager, socket = _manager_with_client()
        manager._buffer(EventType.DETECTION_CREATED, EventType.DETECTION_BATCH, {"detection_id": "det_1"})
        assert manager._flush_task is None
        assert socket.sent == []


class TestTransportResilience:
    def test_a_dead_client_is_dropped_and_does_not_block_the_others(self):
        class _BrokenSocket(_FakeSocket):
            async def send_text(self, message: str) -> None:
                raise ConnectionResetError("client vanished")

        async def scenario():
            manager = ConnectionManager()
            broken, healthy = _BrokenSocket(), _FakeSocket()
            manager.active.extend([broken, healthy])
            await manager.publish(EventType.ALERT_CREATED, {"id": "alt_1"})
            return manager, healthy

        manager, healthy = asyncio.run(scenario())
        assert EventType.ALERT_CREATED in healthy.types()
        assert len(manager.active) == 1


def test_every_batched_type_declares_a_distinct_envelope():
    for source, envelope in BATCHED_INTO.items():
        assert source != envelope, "an event batched into itself would recurse"


@pytest.mark.parametrize("name", [
    "detection.created", "plate.detected", "plate.updated", "vehicle.sighting",
    "vehicle.route.updated", "alert.created", "incident.created",
    "camera.status", "camera.health", "self_heal.recovery",
])
def test_the_declared_event_vocabulary_is_stable(name):
    """These names are a published contract the frontend filters on — a rename
    silently breaks a live dashboard, so changing one must fail here first."""
    declared = {v for k, v in vars(EventType).items() if not k.startswith("_") and isinstance(v, str)}
    assert name in declared
