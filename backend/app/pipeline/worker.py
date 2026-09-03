"""Per-camera background task: read real frames -> real YOLO detection ->
real ANPR -> persist -> evaluate rules -> broadcast. This is the whole
pipeline described in doc §54, running against a webcam or an uploaded
video file rather than a real CCTV/VMS source (see source.py header).
"""
import asyncio
from datetime import datetime

import cv2

from .. import models
from ..db import SessionLocal
from ..config import settings
from ..ws import manager
from .source import CameraSource
from .detector import detect_and_track
from .anpr import read_plate
from .correlate import upsert_vehicle_for_plate
from .rules_engine import evaluate

LATEST_FRAMES: dict[str, bytes] = {}
RUNNING: dict[str, asyncio.Task] = {}


def _draw_boxes(frame, detections):
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        color = (0, 255, 0) if d["cls"] == "person" else (0, 165, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = f'{d["cls"]} {d["confidence"]:.2f}'
        cv2.putText(frame, label, (x1, max(0, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return frame


def _save_snapshot(frame, prefix: str) -> str:
    fname = f"{prefix}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.jpg"
    path = settings.evidence_dir / fname
    cv2.imwrite(str(path), frame)
    return str(path)


async def _camera_loop(camera_id: str):
    db = SessionLocal()
    source = None
    try:
        camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
        if not camera:
            return
        source = CameraSource(camera.source_type, camera.source_uri)
        opened = await asyncio.to_thread(source.open)
        if not opened:
            camera.status = "offline"
            camera.error_count += 1
            db.commit()
            return
        camera.status = "online"
        camera.fps = source.fps() or 15.0
        camera.resolution = source.resolution()
        db.commit()

        frame_idx = 0
        last_detections: list = []
        while True:
            ok, frame = await asyncio.to_thread(source.read)
            if not ok or frame is None:
                camera.status = "degraded"
                camera.error_count += 1
                db.commit()
                await asyncio.sleep(1.0)
                continue

            h, w = frame.shape[:2]
            frame_idx += 1

            if frame_idx % settings.detect_every_n_frames == 0:
                detections = await asyncio.to_thread(
                    detect_and_track, frame, camera.ai_person, camera.ai_vehicle
                )
                last_detections = detections

                for d in detections:
                    snapshot_path = None
                    det_row = models.Detection(
                        camera_id=camera.id, cls=d["cls"], confidence=d["confidence"],
                        bbox=d["bbox"], track_id=(str(d["track_id"]) if d["track_id"] is not None else None),
                        model_version=settings.model_version,
                    )
                    db.add(det_row)
                    db.flush()

                    vehicle = None
                    if camera.ai_anpr and d["cls"] in ("car", "truck", "bus", "motorbike"):
                        x1, y1, x2, y2 = [max(0, int(v)) for v in d["bbox"]]
                        crop = frame[y1:y2, x1:x2]
                        raw, normalized, conf = await asyncio.to_thread(read_plate, crop)
                        if normalized:
                            snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera.camera_code)
                            det_row.snapshot_path = snapshot_path
                            vehicle = upsert_vehicle_for_plate(db, normalized, conf)
                            db.add(models.Plate(
                                vehicle_id=vehicle.id, camera_id=camera.id, detection_id=det_row.id,
                                plate_text_raw=raw, plate_text_normalized=normalized,
                                confidence=conf, snapshot_path=snapshot_path,
                            ))

                    if not snapshot_path and vehicle and vehicle.watchlist_flag:
                        snapshot_path = await asyncio.to_thread(_save_snapshot, frame, camera.camera_code)
                        det_row.snapshot_path = snapshot_path

                    db.commit()
                    await evaluate(db, camera, det_row, w, h, vehicle)
                    await manager.broadcast("detection", {
                        "camera_id": camera.id, "camera_code": camera.camera_code,
                        "cls": d["cls"], "confidence": d["confidence"],
                        "timestamp": det_row.timestamp.isoformat(),
                    })

            annotated = _draw_boxes(frame.copy(), last_detections)
            ok2, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if ok2:
                LATEST_FRAMES[camera.id] = buf.tobytes()

            camera.last_frame_at = datetime.utcnow()
            camera.status = "online"
            db.commit()
            await asyncio.sleep(max(0.01, 1.0 / max(camera.fps, 1.0)))
    except asyncio.CancelledError:
        pass
    finally:
        if source is not None:
            source.release()
        db.close()


def start_worker(camera_id: str):
    existing = RUNNING.get(camera_id)
    if existing and not existing.done():
        return
    RUNNING[camera_id] = asyncio.create_task(_camera_loop(camera_id))


def stop_worker(camera_id: str):
    task = RUNNING.pop(camera_id, None)
    if task:
        task.cancel()
    LATEST_FRAMES.pop(camera_id, None)
