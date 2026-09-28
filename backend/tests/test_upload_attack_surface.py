"""Trying to break POST /api/cameras/upload-video.

Most cases already held; the 0-byte upload was a real bug and is pinned below.

Content is only checked against a deny-list of clearly non-video types, not
fully sniffed. That's fine here: uploads_dir is never served over HTTP (no
StaticFiles, no FileResponse; routers/system.py only checks it's writable),
so a stored blob is inert and can only be handed to cv2.VideoCapture.
"""
import pytest

from app.config import settings


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _upload(client, auth, filename: str, content: bytes):
    return client.post(
        "/api/cameras/upload-video",
        files={"file": (filename, content, "video/mp4")},
        headers=auth,
    )


class TestPathTraversal:
    """A client filename never affects the path on disk."""

    @pytest.mark.parametrize("evil_name", [
        "../../../../etc/passwd.mp4",
        r"..\..\..\windows\system32\evil.mp4",
        "/etc/cron.d/evil.mp4",
        r"C:\Windows\Temp\evil.mp4",
        "видео\u202e.mp4",  # unicode + right-to-left override
        "sub/dir/nested.mp4",
    ])
    def test_client_filename_never_escapes_the_uploads_directory(self, client, auth, evil_name):
        before = {p.name for p in settings.uploads_dir.iterdir()}
        resp = _upload(client, auth, evil_name, b"\x00\x01fake video bytes")
        assert resp.status_code == 200

        saved = resp.json()["filename"]
        # Stored name is a generated UUID + allow-listed extension, never the
        # client's string, and the file really is inside uploads_dir.
        assert saved.endswith(".mp4")
        assert "/" not in saved and "\\" not in saved and ".." not in saved
        stored = settings.uploads_dir / saved
        assert stored.resolve().parent == settings.uploads_dir.resolve()

        new_files = {p.name for p in settings.uploads_dir.iterdir()} - before
        assert new_files == {saved}, "upload created a file under an unexpected name"


class TestExtensionAllowList:
    @pytest.mark.parametrize("name", [
        "evil.mp4.exe",      # real extension is .exe
        "noextension",
        ".mp4",              # leading-dot name has no suffix at all
        "script.sh",
        "payload.php",
    ])
    def test_disallowed_extensions_are_rejected(self, client, auth, name):
        assert _upload(client, auth, name, b"\x00\x01").status_code == 400


class TestEmptyUpload:
    def test_a_zero_byte_upload_is_rejected_and_leaves_no_file(self, client, auth):
        """A 0-byte upload got a 200 and stayed on disk as a camera source
        that could never open."""
        before = {p.name for p in settings.uploads_dir.iterdir()}
        resp = _upload(client, auth, "empty.mp4", b"")
        assert resp.status_code == 400, "a 0-byte file is not a video and must be rejected"

        after = {p.name for p in settings.uploads_dir.iterdir()}
        assert after == before, "the rejected empty upload left an orphaned file behind"


class TestContentValidation:
    """The extension check alone stored an exe and a zip renamed .mp4;
    clearly non-video content is refused now."""

    @pytest.mark.parametrize("label,payload", [
        ("windows executable", b"MZ\x90\x00\x03\x00\x00\x00"),
        ("elf executable", b"\x7fELF\x02\x01\x01\x00"),
        ("zip / office doc", b"PK\x03\x04\x14\x00\x00\x00"),
        ("gzip archive", b"\x1f\x8b\x08\x00\x00\x00\x00\x00"),
        ("pdf", b"%PDF-1.7\n"),
        ("shell script", b"#!/bin/sh\nrm -rf /\n"),
        ("html", b"<!DOCTYPE html><html><body>x</body></html>"),
        ("png", b"\x89PNG\r\n\x1a\n"),
        ("jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
    ])
    def test_definitively_non_video_content_is_rejected(self, client, auth, label, payload):
        before = {p.name for p in settings.uploads_dir.iterdir()}
        resp = _upload(client, auth, "disguised.mp4", payload)
        assert resp.status_code == 400, f"{label} was accepted as a video"
        assert {p.name for p in settings.uploads_dir.iterdir()} == before, f"{label} left a file on disk"

    def test_a_real_mp4_header_is_accepted(self, client, auth):
        """The demo video's real first bytes still pass."""
        real_mp4_head = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41"
        assert _upload(client, auth, "real.mp4", real_mp4_head + b"\x00" * 64).status_code == 200

    @pytest.mark.parametrize("payload", [
        b"RIFF\x00\x00\x00\x00AVI LIST",                 # AVI
        b"\x1aE\xdf\xa3\x01\x00\x00\x00",                 # Matroska / WebM
        b"\x00\x00\x01\xba\x21\x00\x01\x00",              # MPEG program stream
        b"\x00\x01\x02\x03unrecognised but plausible",    # unknown -> allowed on purpose
    ])
    def test_video_and_unrecognised_content_is_still_allowed(self, client, auth, payload):
        """Deny-list: an unknown container isn't rejected just for being unknown."""
        assert _upload(client, auth, "clip.mp4", payload).status_code == 200


class TestSizeCap:
    def test_an_over_cap_upload_is_rejected_and_cleaned_up(self, client, auth, monkeypatch):
        """The size cap rejects AND deletes the partial file."""
        monkeypatch.setattr(settings, "max_upload_mb", 1)
        before = {p.name for p in settings.uploads_dir.iterdir()}

        resp = _upload(client, auth, "toobig.mp4", b"\x00" * (2 * 1024 * 1024))
        assert resp.status_code == 413

        after = {p.name for p in settings.uploads_dir.iterdir()}
        assert after == before, "an over-cap upload left its partial file on disk"


class TestAuthorization:
    def test_upload_requires_a_credential(self, client):
        resp = client.post(
            "/api/cameras/upload-video",
            files={"file": ("clip.mp4", b"\x00\x01", "video/mp4")},
        )
        assert resp.status_code == 401


class TestConcurrentUploads:
    def test_simultaneous_uploads_never_collide_on_a_filename(self, client, auth):
        """UUID names, so two people uploading clip.mp4 don't overwrite each other."""
        names = set()
        for _ in range(8):
            resp = _upload(client, auth, "same-name.mp4", b"\x00\x01payload")
            assert resp.status_code == 200
            names.add(resp.json()["filename"])
        assert len(names) == 8, "concurrent uploads of the same client filename collided on disk"
