import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import models
from ..config import settings
from ..db import SessionLocal, get_db
from sqlalchemy.orm import Session
from ..security import (
    create_resource_token,
    get_current_user,
    get_user_from_resource_token,
    resource_token_expiry,
)
from ..pipeline import worker
from ..pipeline.worker import LATEST_FRAMES

router = APIRouter(prefix="/api/streams", tags=["streams"])


@router.get("/{camera_id}/stream-token")
def get_stream_token(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Issue a signed token for the MJPEG/snapshot endpoints, which browsers load
    via <img src> without a bearer header."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"token": create_resource_token("camera_stream", camera_id, user, settings.stream_token_ttl_seconds)}


def _stream_deadline(token: str) -> datetime:
    """Deadline for this stream. Falls back to the configured TTL when the token
    can't be decoded, so it never fails open.
    """
    return resource_token_expiry(token) or (datetime.utcnow() + timedelta(seconds=settings.stream_token_ttl_seconds))


async def _mjpeg_generator(camera_id: str, deadline: datetime):
    """Stream frames until the authorising token expires. Ending the stream
    forces the client to fetch a new token, which re-runs the RBAC check.
    """
    boundary = b"--frame"
    # counted so the camera loop encodes the preview at full rate only while
    # someone's watching
    worker.VIEWERS[camera_id] = worker.VIEWERS.get(camera_id, 0) + 1
    try:
        while True:
            if datetime.utcnow() >= deadline:
                return
            frame = LATEST_FRAMES.get(camera_id)
            if frame is not None:
                yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            await asyncio.sleep(0.1)
    finally:
        left = worker.VIEWERS.get(camera_id, 1) - 1
        if left > 0:
            worker.VIEWERS[camera_id] = left
        else:
            worker.VIEWERS.pop(camera_id, None)


def _authorize_stream(camera_id: str, token: str, require_camera: bool) -> None:
    """Check the token on a short-lived session and release it. Not
    Depends(get_db): a dependency's session stays open for the whole response,
    and a long stream would pin a pool connection.
    """
    db = SessionLocal()
    try:
        get_user_from_resource_token("camera_stream", camera_id, token, db)
        if require_camera and not db.query(models.Camera).filter(models.Camera.id == camera_id).first():
            raise HTTPException(status_code=404, detail="Camera not found")
    finally:
        db.close()


@router.get("/{camera_id}/mjpeg")
async def mjpeg_stream(camera_id: str, token: str):
    _authorize_stream(camera_id, token, require_camera=True)
    return StreamingResponse(
        _mjpeg_generator(camera_id, _stream_deadline(token)),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/{camera_id}/snapshot.jpg")
async def snapshot(camera_id: str, token: str):
    from fastapi import Response
    _authorize_stream(camera_id, token, require_camera=False)
    frame = LATEST_FRAMES.get(camera_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="No frame available yet")
    return Response(content=frame, media_type="image/jpeg")
