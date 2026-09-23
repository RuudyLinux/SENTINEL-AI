"""Prometheus scrape endpoint.

Authenticated by default. Metrics here are operationally sensitive — how many
cameras exist and are live, how many plates are being recognized, how many
alerts are firing — so this is never open. Two accepted credentials:

- **A scrape token** (`METRICS_TOKEN`), for Prometheus itself, which cannot
  perform a JWT login. Compared in constant time.
- **An Administrator JWT**, so an operator can read the same data from the app.

With no token configured, only the Administrator JWT path works. There is no
unauthenticated mode.
"""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy.orm import Session

from .. import metrics
from ..config import settings
from ..db import get_db
from ..security import get_user_from_token

router = APIRouter(prefix="/api", tags=["metrics"])


def _token_matches(presented: str) -> bool:
    configured = (settings.metrics_token or "").strip()
    if not configured:
        return False
    # Constant-time compare: a plain `==` on a secret leaks its length and a
    # prefix through timing, and this endpoint is reachable by anyone who can
    # reach the backend.
    return hmac.compare_digest(presented, configured)


def _authorize(authorization: str | None, db: Session) -> None:
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if _token_matches(presented):
        return
    user = get_user_from_token(presented, db) if presented else None
    # Same role check `security.require_roles` performs; done inline because
    # that helper is a FastAPI dependency and this endpoint must first give the
    # scrape token a chance, which the dependency chain cannot express.
    if user is not None and (user.role.name if user.role else "") == "Administrator":
        return
    raise HTTPException(
        status_code=401,
        detail="Metrics require the configured scrape token or an Administrator token.",
    )


@router.get("/metrics")
def prometheus_metrics(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    _authorize(authorization, db)
    return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)
