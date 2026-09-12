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


def _stream_deadline(token: str) -> datetime:
    """The instant this stream must stop, always a real deadline.

    `resource_token_expiry` returns None for any token it cannot decode — and
    an EXPIRED token is one of those, since decoding verifies `exp`. Treating
    None as "no deadline" would fail open on the exact input the bound exists
    for, so an unreadable token falls back to the configured TTL measured from
    now: still bounded, never unlimited.
    """
    return resource_token_expiry(token) or (datetime.utcnow() + timedelta(seconds=settings.stream_token_ttl_seconds))


async def _mjpeg_generator(camera_id: str, deadline: datetime):
    """Frames until the authorizing token expires.

    The token was checked once, when the stream opened, and an MJPEG response
    then stays open indefinitely — so a stream started with a one-hour token
    kept delivering live video for as long as the browser tab was left open,
    days later, and deactivating the user's account did not interrupt it.
    `stream_token_ttl_seconds` bounded nothing at all for the one endpoint
    whose access lasts long enough for a bound to matter.

    Ending the stream is the whole enforcement: the client re-authorizes by
    requesting a new token, which re-runs the full RBAC check.
    """
    boundary = b"--frame"
    while True:
        if datetime.utcnow() >= deadline:
            return
        frame = LATEST_FRAMES.get(camera_id)
        if frame is not None:
            yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        await asyncio.sleep(0.1)


def _authorize_stream(camera_id: str, token: str, require_camera: bool) -> None:
    """Validate the token against a SHORT-LIVED session, then release it.

    Deliberately not `Depends(get_db)`: FastAPI holds a dependency open until
    the response COMPLETES, and these responses are designed not to complete
    for hours. Every viewer therefore pinned one connection out of the pool
    (sized 15 — see db.py) for the lifetime of their stream, so a wall of
    tiles could exhaust the pool and stall every other request in the API
    while doing nothing but reading a dict of JPEG bytes.
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
