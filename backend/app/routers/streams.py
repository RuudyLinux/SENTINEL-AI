import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import models
from ..db import get_db
from sqlalchemy.orm import Session
from ..security import get_current_user
from ..pipeline.worker import LATEST_FRAMES

router = APIRouter(prefix="/api/streams", tags=["streams"])


async def _mjpeg_generator(camera_id: str):
    boundary = b"--frame"
    while True:
        frame = LATEST_FRAMES.get(camera_id)
        if frame is not None:
            yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        await asyncio.sleep(0.1)


@router.get("/{camera_id}/mjpeg")
async def mjpeg_stream(camera_id: str, db: Session = Depends(get_db)):
    camera = db.query(models.Camera).filter(models.Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return StreamingResponse(_mjpeg_generator(camera_id), media_type="multipart/x-mixed-replace; boundary=frame")


@router.get("/{camera_id}/snapshot.jpg")
async def snapshot(camera_id: str):
    from fastapi import Response
    frame = LATEST_FRAMES.get(camera_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="No frame available yet")
    return Response(content=frame, media_type="image/jpeg")
