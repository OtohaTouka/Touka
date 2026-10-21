"""聊天上傳服務（Phase 2）：magic-byte 型別/大小驗證、存進當下 agent 自己 vault、
穿越防護、只碰 DebugAgent（真實 agent 不受影響）。零-API、沙盒、用 DebugAgent 身分。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from src.core.exceptions import ToukaException
from src.models.identity import AgentIdentity
from src.services import upload_service as U
from src.services.resource_manager import resource_manager

DEBUG = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
JIN = AgentIdentity.for_role(agent_id="Jin", role="Owner")  # 真實 agent，必須不受影響

_PDF = b"%PDF-1.4\n%binary\n"
_DOCX = b"PK\x03\x04" + b"\x00" * 32  # zip 容器（docx/xlsx/pptx 同 magic）
_TEXT = "hello, 燈火\n".encode()


def test_detect_document_pdf():
    assert U.detect_document(_PDF, "report.pdf") == ("pdf", "application/pdf")


def test_detect_document_office_requires_matching_ext():
    assert U.detect_document(_DOCX, "a.docx")[0] == "docx"
    assert U.detect_document(_DOCX, "a.xlsx")[0] == "xlsx"
    # PK 容器但副檔名不在白名單 → 拒（防把任意 zip 當 office）
    assert U.detect_document(_DOCX, "a.zip") is None


def test_detect_document_text_needs_valid_utf8():
    assert U.detect_document(_TEXT, "notes.md") == ("md", "text/plain; charset=utf-8")
    assert U.detect_document(b"\xff\xfe\x00rubbish", "notes.md") is None  # 非 utf-8 → 拒


def test_detect_document_unknown_type():
    assert U.detect_document(b"\x00\x01\x02", "mystery.bin") is None


def test_validate_rejects_oversized(monkeypatch):
    monkeypatch.setattr(U, "UPLOAD_MAX_BYTES", 4)
    with pytest.raises(ToukaException):
        U.validate_upload_bytes(_PDF, "big.pdf")


def test_validate_rejects_unsupported():
    with pytest.raises(ToukaException):
        U.validate_upload_bytes(b"\x00\x01\x02", "x.exe")


def test_save_upload_lands_in_debugagent_vault():
    res = U.save_upload(DEBUG, "sess-1", "notes.md", _TEXT)
    assert res["filename"] == "notes.md"
    assert res["relpath"] == "outputs/uploads/sess-1/notes.md"
    base = Path(resource_manager.get_uploads_path(DEBUG))
    saved = base / "sess-1" / "notes.md"
    assert saved.exists()
    assert saved.read_bytes() == _TEXT
    # DebugAgent 的 uploads base 與 Jin 的不同 → 不可能寫到 Jin
    assert Path(resource_manager.get_uploads_path(JIN)) != base


def test_save_upload_sanitizes_traversal_filename_and_session():
    res = U.save_upload(DEBUG, "../../evil", "../../../etc/passwd.pdf", _PDF)
    base = Path(resource_manager.get_uploads_path(DEBUG)).resolve()
    # 不論 session/filename 多惡意，落點都被夾在 DebugAgent 的 uploads base 內（穿越防護）
    saved = (base / Path(res["relpath"]).relative_to("outputs/uploads")).resolve()
    assert saved.is_relative_to(base)
    assert ".." not in Path(res["relpath"]).parts  # 無父層段
    assert res["filename"] == "passwd.pdf"  # 取 basename


def test_safe_seg_neutralizes_dot_dot():
    assert U._safe_seg("..") == "_misc"  # 純 .. → 退為固定桶（不再 mint uuid）
    assert "/" not in U._safe_seg("a/b/c")


def test_blank_session_buckets_to_misc():
    res = U.save_upload(DEBUG, "", "a.md", _TEXT)
    assert res["relpath"].startswith("outputs/uploads/_misc/")


# ── 複審加固（HIGH：agent_id 穿越 / MEDIUM：配額）──────────────────────
def test_assert_safe_agent_segment_rejects_traversal():
    for bad in ["..", ".", "a/b", "a\\b", "../x", "", "x\x00y"]:
        with pytest.raises(ToukaException):
            U._assert_safe_agent_segment(bad)
    U._assert_safe_agent_segment("DebugAgent")  # 合法單段不拋


def test_save_upload_rejects_cross_agent_via_unsafe_agent_id():
    """agent_id 含 ../ → save_upload 立即拒（不可逃出自己 vault 寫進別人資料）。"""
    evil = AgentIdentity.for_role(agent_id="../personal_drive/Jin", role="Base")
    with pytest.raises(ToukaException):
        U.save_upload(evil, "s1", "notes.md", _TEXT)


def test_save_upload_enforces_per_agent_quota(monkeypatch):
    monkeypatch.setattr(U, "UPLOAD_AGENT_QUOTA_BYTES", 5)
    with pytest.raises(ToukaException):
        U.save_upload(DEBUG, "s1", "notes.md", b"way over five bytes")
