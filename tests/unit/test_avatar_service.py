"""ADR-0106 avatar_service unit tests (TDD).

Covers magic-byte detection, size/format validation, atomic-ish save with
format-change cleanup, find (for serving), delete (cleanup), and path-traversal
hardening — all without HTTP, by pointing the avatars dir at a tmp_path.
"""

from __future__ import annotations

import pytest

from src.core.exceptions import ErrorCode, ToukaException
from src.services import avatar_service

# Minimal valid magic-byte payloads
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture()
def avatars_tmp(tmp_path, monkeypatch):
    """Point avatar_service's avatars dir at a tmp path."""
    target = tmp_path / "avatars"
    monkeypatch.setattr(avatar_service, "avatars_dir", lambda: target)
    return tmp_path


# ── detect_image ───────────────────────────────────────────────────────────

def test_detect_png_jpeg_webp():
    assert avatar_service.detect_image(PNG) == ("png", "image/png")
    assert avatar_service.detect_image(JPEG) == ("jpg", "image/jpeg")
    assert avatar_service.detect_image(WEBP) == ("webp", "image/webp")


def test_detect_rejects_svg_text_empty():
    assert avatar_service.detect_image(SVG) is None
    assert avatar_service.detect_image(b"hello world") is None
    assert avatar_service.detect_image(b"") is None


# ── validate_avatar_bytes ──────────────────────────────────────────────────

def test_validate_accepts_valid_image():
    assert avatar_service.validate_avatar_bytes(PNG) == ("png", "image/png")


def test_validate_rejects_bad_format():
    with pytest.raises(ToukaException) as ei:
        avatar_service.validate_avatar_bytes(SVG)
    assert ei.value.code == ErrorCode.AVATAR_INVALID_FORMAT


def test_validate_rejects_too_large(monkeypatch):
    monkeypatch.setattr(avatar_service, "AVATAR_MAX_BYTES", 10)
    with pytest.raises(ToukaException) as ei:
        avatar_service.validate_avatar_bytes(PNG)  # 24 bytes > 10
    assert ei.value.code == ErrorCode.AVATAR_TOO_LARGE


# ── save / find / delete ───────────────────────────────────────────────────

def test_save_writes_file_and_returns_ext(avatars_tmp):
    ext = avatar_service.save_avatar("user1", PNG)
    assert ext == "png"
    assert (avatars_tmp / "avatars" / "user1.png").exists()


def test_save_format_change_removes_old_no_orphan(avatars_tmp):
    avatar_service.save_avatar("user1", PNG)
    avatar_service.save_avatar("user1", JPEG)  # switch format
    adir = avatars_tmp / "avatars"
    assert not (adir / "user1.png").exists()  # old format removed
    assert (adir / "user1.jpg").exists()       # only new remains


def test_save_write_failure_preserves_existing_avatar(avatars_tmp, monkeypatch):
    """AV-001: if the new write fails, the existing avatar must NOT be lost."""
    avatar_service.save_avatar("user1", PNG)  # existing avatar
    adir = avatars_tmp / "avatars"
    assert (adir / "user1.png").exists()

    def _boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(avatar_service.resource_manager, "write_file_binary", _boom)

    with pytest.raises(ToukaException) as ei:
        avatar_service.save_avatar("user1", JPEG)  # different format, write fails
    assert ei.value.code == ErrorCode.AVATAR_WRITE_FAILED
    # old avatar still intact (not deleted by the failed save)
    assert (adir / "user1.png").exists()


def test_find_avatar_returns_path_and_media(avatars_tmp):
    avatar_service.save_avatar("user1", WEBP)
    found = avatar_service.find_avatar("user1")
    assert found is not None
    path, media = found
    assert path.name == "user1.webp"
    assert media == "image/webp"


def test_find_avatar_none_when_absent(avatars_tmp):
    assert avatar_service.find_avatar("ghost") is None


def test_delete_avatar_removes_file(avatars_tmp):
    avatar_service.save_avatar("user1", PNG)
    avatar_service.delete_avatar("user1")
    assert avatar_service.find_avatar("user1") is None


def test_save_rejects_path_traversal_user_id(avatars_tmp):
    with pytest.raises(ToukaException) as ei:
        avatar_service.save_avatar("../../evil", PNG)
    assert ei.value.code == ErrorCode.AVATAR_INVALID_FORMAT
    # nothing escaped the avatars dir
    assert not (avatars_tmp.parent / "evil.png").exists()


def test_find_path_traversal_user_id_returns_none(avatars_tmp):
    assert avatar_service.find_avatar("../../etc/passwd") is None
