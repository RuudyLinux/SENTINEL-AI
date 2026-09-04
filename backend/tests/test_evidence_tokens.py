"""5. Evidence authorization  6. Evidence token scope — P0-E."""
import pytest
from fastapi import HTTPException

from app.security import create_resource_token, get_user_from_resource_token


def test_valid_token_for_the_right_resource_authorizes(db_session, admin_user):
    token = create_resource_token("evidence_file", "evd_abc123", admin_user, ttl_seconds=60)
    user = get_user_from_resource_token("evidence_file", "evd_abc123", token, db_session)
    assert user.id == admin_user.id


def test_token_rejected_for_a_different_resource_id(db_session, admin_user):
    token = create_resource_token("evidence_file", "evd_abc123", admin_user, ttl_seconds=60)
    with pytest.raises(HTTPException) as exc:
        get_user_from_resource_token("evidence_file", "evd_DIFFERENT", token, db_session)
    assert exc.value.status_code == 401


def test_token_rejected_for_a_different_resource_type(db_session, admin_user):
    # same resource id, but minted for a different resource kind (e.g. a
    # stream token must not double as an evidence-file token)
    token = create_resource_token("camera_stream", "evd_abc123", admin_user, ttl_seconds=60)
    with pytest.raises(HTTPException) as exc:
        get_user_from_resource_token("evidence_file", "evd_abc123", token, db_session)
    assert exc.value.status_code == 401


def test_garbage_token_rejected(db_session):
    with pytest.raises(HTTPException) as exc:
        get_user_from_resource_token("evidence_file", "evd_abc123", "not-a-real-jwt", db_session)
    assert exc.value.status_code == 401
