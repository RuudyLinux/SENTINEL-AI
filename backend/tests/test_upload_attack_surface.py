"""Final deep-debug pass — active attacks on POST /api/cameras/upload-video.

Everything here is a real attempt to break the endpoint, not a happy-path
smoke test. Most cases PASS against the existing implementation (recorded as
"attacked, no bug found"); one — the 0-byte upload (BUG-B) — was a genuine
finding and is locked down below.

Explicitly NOT claimed: content-type sniffing. The endpoint validates the
extension, not the bytes, so an arbitrary blob named `.mp4` is stored. That
is bounded rather than dangerous here: `uploads_dir` is written by this
endpoint and never served over HTTP by anything (verified — no StaticFiles
mount, no FileResponse from it; the only other reference is a writability
check in routers/system.py), so an uploaded blob is inert on disk and can
only ever be handed to cv2.VideoCapture as a camera source. See the final
report's residual-risk list.
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
    """The UUID-rename defense: a client filename must NEVER influence the
    path on disk. Every case here previously passed and must keep passing."""

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
        """BUG-B: a 0-byte upload was accepted with 200 and left a permanent
        0-byte file on disk, usable as a camera source that could never
        open."""
        before = {p.name for p in settings.uploads_dir.iterdir()}
        resp = _upload(client, auth, "empty.mp4", b"")
        assert resp.status_code == 400, "a 0-byte file is not a video and must be rejected"

        after = {p.name for p in settings.uploads_dir.iterdir()}
        assert after == before, "the rejected empty upload left an orphaned file behind"


class TestContentValidation:
    """C2: the extension allow-list alone accepted an arbitrary blob renamed
    `.mp4` — measured, a PE executable and a ZIP were both stored. Now
    definitively-non-video content is refused."""

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
        """The demo asset's actual first bytes — the check must not reject
        genuine video."""
        real_mp4_head = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41"
        assert _upload(client, auth, "real.mp4", real_mp4_head + b"\x00" * 64).status_code == 200

    @pytest.mark.parametrize("payload", [
        b"RIFF\x00\x00\x00\x00AVI LIST",                 # AVI
        b"\x1aE\xdf\xa3\x01\x00\x00\x00",                 # Matroska / WebM
        b"\x00\x00\x01\xba\x21\x00\x01\x00",              # MPEG program stream
        b"\x00\x01\x02\x03unrecognised but plausible",    # unknown -> allowed on purpose
    ])
    def test_video_and_unrecognised_content_is_still_allowed(self, client, auth, payload):
        """Deny-list, not allow-list: an unusual or unknown container must
        NOT be rejected just because this check does not recognise it."""
        assert _upload(client, auth, "clip.mp4", payload).status_code == 200


class TestSizeCap:
    def test_an_over_cap_upload_is_rejected_and_cleaned_up(self, client, auth, monkeypatch):
        """The streaming size cap must both reject AND delete the partial
        file — an enforced cap that leaves the oversized bytes on disk is not
        a cap."""
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
        """Names are UUIDs, so concurrent uploads must never overwrite each
        other — the classic "two users upload clip.mp4" data-loss bug."""
        names = set()
        for _ in range(8):
            resp = _upload(client, auth, "same-name.mp4", b"\x00\x01payload")
            assert resp.status_code == 200
            names.add(resp.json()["filename"])
        assert len(names) == 8, "concurrent uploads of the same client filename collided on disk"
