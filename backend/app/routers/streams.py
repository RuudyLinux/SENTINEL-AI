import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import models
from ..config import settings
from ..db import get_db
from sqlalchemy.orm import Session
from ..security import get_current_user, create_resource_token, get_user_from_resource_token
from ..pipeline.worker import LATEST_FRAMES

router = APIRouter(prefix="/api/streams", tags=["streams"])


@router.get("/{camera_id}/stream-token")
def get_stream_token(camera_id: str, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """RBAC-checked step handing out a signed token for the mjpeg/snapshot
    endpoints below, which browsers hit via plain <img src> and can't attach
    a bearer header to (P0-E — same pattern as evidence file/package)."""
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"token": create_resource_token("camera_stream", camera_id, user, settings.stream_token_ttl_seconds)}


async def _mjpeg_generator(camera_id: str):
    boundary = b"--frame"
    while True:
        frame = LATEST_FRAMES.get(camera_id)
        if frame is not None:
            yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        await asyncio.sleep(0.1)


@router.get("/{camera_id}/mjpeg")
async def mjpeg_stream(camera_id: str, token: str, db: Session = Depends(get_db)):
    get_user_from_resource_token("camera_stream", camera_id, token, db)
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return StreamingResponse(_mjpeg_generator(camera_id), media_type="multipart/x-mixed-replace; boundary=frame")


@router.get("/{camera_id}/snapshot.jpg")
async def snapshot(camera_id: str, token: str, db: Session = Depends(get_db)):
    from fastapi import Response
    get_user_from_resource_token("camera_stream", camera_id, token, db)
    frame = LATEST_FRAMES.get(camera_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="No frame available yet")
    return Response(content=frame, media_type="image/jpeg")
