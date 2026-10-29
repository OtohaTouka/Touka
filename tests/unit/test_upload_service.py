"""聊天上傳服務（Phase 2）：magic-byte 型別/大小驗證、存進當下 agent 自己 vault、
穿越防護、只碰 DebugAgent（真實 agent 不受影響）。零-API、沙盒、用 DebugAgent 身分。

ADR-0293：上傳資料夾或路上的一段被換成連結時拒絕（UPLOAD_FAILED），不寫到連結另一端。
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services import upload_service as U
from src.services.resource_manager import resource_manager

from tests.support.links import LINK_FOLLOWED, make_dir_link

if TYPE_CHECKING:
    from pathlib import Path

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
    base = resource_manager.uploads_dir(DEBUG)
    assert resource_manager.zone_read_binary(base.child("sess-1", "notes.md")) == _TEXT
    # DebugAgent 的 uploads base 與 Jin 的不同 → 不可能寫到 Jin
    assert resource_manager.uploads_dir(JIN) != base


def test_save_upload_sanitizes_traversal_filename_and_session():
    res = U.save_upload(DEBUG, "../../evil", "../../../etc/passwd.pdf", _PDF)
    base = resource_manager.uploads_dir(DEBUG)
    # 不論 session/filename 多惡意，落點都被夾在 DebugAgent 的 uploads base 內（穿越防護）
    saved = resource_manager.outputs_dir(DEBUG).join_rel(res["relpath"].removeprefix("outputs/"))
    assert saved.is_under(base)
    assert resource_manager.zone_kind(saved) == "file"
    assert ".." not in res["relpath"].split("/")  # 無父層段
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


# ── ADR-0293：連結與不能當一段的名稱 ──────────────────────
ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")


@pytest.mark.parametrize("swapped", ["drive", "outputs", "uploads", "session"])
def test_save_upload_refuses_a_linked_folder(zone_data_root: Path, tmp_path: Path, swapped: str):
    """個人空間、outputs、uploads 或 session 資料夾被換成連結：UPLOAD_FAILED，另一端沒被寫。"""
    drive = zone_data_root / "personal_drive" / "Alice"
    folders = {
        "drive": drive,
        "outputs": drive / "outputs",
        "uploads": drive / "outputs" / "uploads",
        "session": drive / "outputs" / "uploads" / "s1",
    }
    folders[swapped].parent.mkdir(parents=True, exist_ok=True)
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    (fake / "hooks.json").write_text("{}", encoding="utf-8")
    make_dir_link(folders[swapped], fake)
    with pytest.raises(ToukaException) as exc:
        U.save_upload(ALICE, "s1", "notes.md", _TEXT)
    assert exc.value.code is ErrorCode.UPLOAD_FAILED
    assert sorted(p.name for p in fake.rglob("*")) == ["hooks.json"], LINK_FOLLOWED


def test_save_upload_name_that_cannot_be_a_segment_is_upload_failed():
    """名稱在這個系統不能當一段（Windows 的 ``CON.md``）：既有的 UPLOAD_FAILED，不是 500。"""
    if sys.platform != "win32":
        assert U.save_upload(DEBUG, "s1", "CON.md", _TEXT)["filename"] == "CON.md"
        return
    with pytest.raises(ToukaException) as exc:
        U.save_upload(DEBUG, "s1", "CON.md", _TEXT)
    assert exc.value.code is ErrorCode.UPLOAD_FAILED
