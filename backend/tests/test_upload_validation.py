"""7. Upload validation — P0-F."""
import io

from app.config import settings


def test_disallowed_extension_rejected(client, admin_token):
    resp = client.post(
        "/api/cameras/upload-video",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("evil.exe", io.BytesIO(b"not really an exe"), "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_allowed_extension_saved_under_generated_uuid_name(client, admin_token):
    resp = client.post(
        "/api/cameras/upload-video",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": ("my clip (final) v2.mp4", io.BytesIO(b"\x00" * 1024), "video/mp4")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["original_filename"] == "my clip (final) v2.mp4"
    assert body["filename"] != "my clip (final) v2.mp4"  # never the raw client filename
    assert body["filename"].endswith(".mp4")
    saved = settings.uploads_dir / body["filename"]
    assert saved.exists()
    saved.unlink()


def test_upload_requires_auth(client):
    resp = client.post(
        "/api/cameras/upload-video",
        files={"file": ("clip.mp4", io.BytesIO(b"\x00" * 10), "video/mp4")},
    )
    assert resp.status_code == 401
