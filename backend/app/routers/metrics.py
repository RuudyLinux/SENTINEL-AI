"""Prometheus scrape endpoint.

Always authenticated, the numbers (live cameras, plate reads, alerts) are
operationally sensitive. Either the METRICS_TOKEN scrape token (Prometheus
can't do a JWT login; compared in constant time) or an Administrator JWT.
No token configured = admin JWT only. No anonymous mode.
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
    # constant time, == leaks length/prefix through timing
    return hmac.compare_digest(presented, configured)


def _authorize(authorization: str | None, db: Session) -> None:
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if _token_matches(presented):
        return
    user = get_user_from_token(presented, db) if presented else None
    # same check as security.require_roles, inline because the scrape token
    # has to get a chance first and a dependency can't express that
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
