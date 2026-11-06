"""軌B Batch 5：docgen 成品驗證（artifact_verify）＋engine 成品維度 gating（零-API）。

以 stdlib zipfile 手造最小 OOXML fixture（不需 openpyxl/python-docx），驗證：
合法+含內容→pass、缺內容→fail、缺部件/壞 zip/找不到→fail；路徑擷取（含 Windows 反斜線還原）；
engine.evaluate 在 artifact_kind 情境下以 artifact_valid gate 結構通過。

ADR-0293 PR 2（R15）：產出檔只在 ``temp/docgen_output`` 裡經 RM 不跟隨地讀；在輸出區外、
路上有連結、只出現在非 ``create_<kind>`` 的軌跡項目、名稱不能當一段時都不讀，也不丟例外。
fixture 都放在 ``zone_data_root`` 的輸出區；連結另一端是測試自己建的 ``outside``。
"""
from __future__ import annotations

import json
import sys
import zipfile
from typing import TYPE_CHECKING

import pytest
from src.services.scorecard.artifact_verify import extract_artifact_path, verify_artifact
from src.services.scorecard.engine import evaluate
from src.services.scorecard.models import RunOutcome, Scenario

from tests.support.links import LINK_FOLLOWED, make_dir_link
from tests.support.zone_records import expect_zone_refusal

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def out(zone_data_root) -> Path:
    """這個測試的文件輸出區（``data/temp/docgen_output``）."""
    folder = zone_data_root / "temp" / "docgen_output"
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def outside(tmp_path) -> Path:
    """輸出區外、內容合法的產出檔所在：讀到它就代表跟隨了連結或沒限制位置."""
    folder = tmp_path.resolve() / "outside"
    folder.mkdir()
    _make_xlsx(folder / "report.xlsx")
    return folder


def _make_xlsx(path, *, content="APAC EMEA 120 90", marker=True):
    with zipfile.ZipFile(path, "w") as zf:
        if marker:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/sharedStrings.xml", f"<sst>{content}</sst>")
    return str(path)


def _make_docx(path, *, text="季度報告 APAC"):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", f"<document>{text}</document>")
    return str(path)


# ── verify_artifact ──────────────────────────────────────────────
def test_verify_valid_xlsx_with_content(out):
    p = _make_xlsx(out / "ok.xlsx")
    ok, detail = verify_artifact(p, "xlsx", ("APAC", "EMEA", "120", "90"))
    assert ok is True
    assert "合法 xlsx" in detail


def test_verify_xlsx_missing_content(out):
    p = _make_xlsx(out / "partial.xlsx", content="APAC 120")
    ok, detail = verify_artifact(p, "xlsx", ("APAC", "EMEA", "999"))
    assert ok is False
    assert "缺內容" in detail
    assert "EMEA" in detail and "999" in detail


def test_verify_missing_marker_part(out):
    p = _make_xlsx(out / "nomarker.xlsx", marker=False)
    ok, detail = verify_artifact(p, "xlsx", ("APAC",))
    assert ok is False
    assert "缺必要部件" in detail


def test_verify_not_a_zip(out):
    bad = out / "fake.xlsx"
    bad.write_text("this is plain text, not a zip", encoding="utf-8")
    ok, detail = verify_artifact(str(bad), "xlsx", ("APAC",))
    assert ok is False
    assert "zip" in detail.lower()


def test_verify_file_not_found(out):
    ok, detail = verify_artifact(str(out / "missing.xlsx"), "xlsx", ("APAC",))
    assert ok is False
    assert "不存在" in detail


def test_verify_empty_path():
    ok, detail = verify_artifact("", "xlsx", ("APAC",))
    assert ok is False
    assert "未擷取" in detail


def test_verify_unsupported_kind(out):
    p = _make_xlsx(out / "x.xlsx")
    ok, detail = verify_artifact(p, "pdf", ("APAC",))
    assert ok is False
    assert "不支援" in detail


def test_verify_valid_docx(out):
    p = _make_docx(out / "ok.docx", text="季度報告 APAC 摘要")
    ok, _ = verify_artifact(p, "docx", ("季度報告", "APAC"))
    assert ok is True


def test_verify_in_a_subfolder_of_the_output_zone(out):
    (out / "job").mkdir()
    ok, _ = verify_artifact(_make_xlsx(out / "job" / "r.xlsx"), "xlsx", ("APAC",))
    assert ok is True


def test_the_file_handle_is_closed_after_verifying(out):
    """``ZipFile`` 收到檔案物件時不會幫忙關；Windows 上沒關會鎖住產出檔，刪不掉."""
    p = out / "r.xlsx"
    verify_artifact(_make_xlsx(p), "xlsx", ("APAC",))
    p.unlink()
    assert not p.exists()


# ── 只在輸出區裡、不跟隨連結（ADR-0293 PR 2 Q6） ──────────────────
def test_a_file_outside_the_output_zone_is_not_read(out, outside):
    ok, detail = verify_artifact(str(outside / "report.xlsx"), "xlsx", ("APAC",))
    assert ok is False, LINK_FOLLOWED
    assert detail == "產出檔不在文件輸出區"


def test_a_relative_path_is_not_read(out, outside, monkeypatch):
    monkeypatch.chdir(outside)
    ok, detail = verify_artifact("report.xlsx", "xlsx", ("APAC",))
    assert (ok, detail) == (False, "產出檔不在文件輸出區")


def test_a_dotdot_path_out_of_the_zone_is_not_read(out, outside):
    """``..`` 先照字面消掉：走出輸出區就不讀."""
    text = str(out / ".." / ".." / ".." / "outside" / "report.xlsx")
    ok, detail = verify_artifact(text, "xlsx", ("APAC",))
    assert (ok, detail) == (False, "產出檔不在文件輸出區")


def test_a_linked_folder_in_the_output_zone_is_not_read(out, outside):
    make_dir_link(out / "job", outside)
    with expect_zone_refusal("ZoneLinkRefusedError"):
        ok, detail = verify_artifact(str(out / "job" / "report.xlsx"), "xlsx", ("APAC",))
        assert ok is False, LINK_FOLLOWED
    assert detail == "產出檔是連結，不讀"


def test_an_output_zone_linked_by_a_person_is_still_read(zone_data_root, outside):
    """輸出區本身是人做的連結（例如掛到別的磁碟）：和其他錨點一樣照常讀.

    代理人只寫得到輸出區裡面，換不掉輸出區本身；區內方法從解析後的錨點開始逐段不跟隨。
    """
    (zone_data_root / "temp").mkdir()
    make_dir_link(zone_data_root / "temp" / "docgen_output", outside)
    text = str(zone_data_root / "temp" / "docgen_output" / "report.xlsx")
    assert verify_artifact(text, "xlsx", ("APAC",))[0] is True


@pytest.mark.skipif(sys.platform == "win32", reason="檔案連結只在 POSIX 測")
def test_a_linked_file_in_the_output_zone_is_not_read(out, outside):
    (out / "report.xlsx").symlink_to(outside / "report.xlsx")
    with expect_zone_refusal("ZoneLinkRefusedError"):
        ok, detail = verify_artifact(str(out / "report.xlsx"), "xlsx", ("APAC",))
        assert ok is False, LINK_FOLLOWED
    assert detail == "產出檔是連結，不讀"


def test_a_name_that_is_not_a_segment_is_not_read(out):
    with expect_zone_refusal("ZoneSegmentError", at_least=0):
        ok, detail = verify_artifact(str(out / "a\x00b.xlsx"), "xlsx", ("APAC",))
    assert (ok, detail) == (False, "產出檔不在文件輸出區")


def test_the_output_zone_itself_is_a_fixed_failure_not_an_exception(out):
    ok, detail = verify_artifact(str(out), "xlsx", ("APAC",))
    assert ok is False
    assert detail == "產出檔開檔失敗"


# ── extract_artifact_path ────────────────────────────────────────
def test_extract_path_posix():
    trace = ({"name": "create_xlsx", "args": "", "result": '{"path": "/data/out/report.xlsx"}'},)
    assert extract_artifact_path(trace, "xlsx") == "/data/out/report.xlsx"


def test_extract_path_backslash_unescape():
    # 模擬 runner 的 json.dumps（反斜線轉義為 \\）→ 擷取後須還原為單反斜線。
    result = json.dumps({"path": r"out\sub\report.xlsx"})
    trace = ({"name": "mcp-docgen__create_xlsx", "args": "", "result": result},)
    assert extract_artifact_path(trace, "xlsx") == r"out\sub\report.xlsx"


def test_extract_path_none_when_absent():
    trace = ({"name": "research", "args": "", "result": "no file here"},)
    assert extract_artifact_path(trace, "xlsx") == ""


def test_extract_path_only_from_create_items():
    """別的工具（例如讀檔、搜尋）提到的 ``*.xlsx`` 不算產出檔（ADR-0293 PR 2 Q6）."""
    trace = (
        {"name": "read", "args": '{"path": "/data/soul/x.xlsx"}', "result": ""},
        {"name": "mcp-docgen__create_xlsx", "args": "", "result": "failed"},
    )
    assert extract_artifact_path(trace, "xlsx") == ""


# ── engine.evaluate：成品維度 gating ─────────────────────────────
def _docgen_scenario():
    return Scenario(
        id="docgen_xlsx", surface="react", expect_tool="create_xlsx",
        artifact_kind="xlsx", artifact_must_contain=("APAC",),
    )


def test_evaluate_fails_when_artifact_invalid():
    out = RunOutcome(
        text="完成", tools_called=("mcp-docgen__create_xlsx",),
        artifact_valid=False, artifact_detail="合法 xlsx 但缺內容：['APAC']",
    )
    r = evaluate(_docgen_scenario(), out)
    assert r.structural_pass is False
    assert "artifact" in r.detail
    assert r.artifact_valid is False


def test_evaluate_passes_when_tool_and_artifact_ok():
    out = RunOutcome(
        text="完成", tools_called=("mcp-docgen__create_xlsx",),
        artifact_valid=True, artifact_detail="合法 xlsx，含全部預期內容（4 項）",
    )
    r = evaluate(_docgen_scenario(), out)
    assert r.structural_pass is True
    assert r.artifact_valid is True


def test_evaluate_non_artifact_scenario_unaffected():
    sc = Scenario(id="chat_basic", surface="chat")
    r = evaluate(sc, RunOutcome(text="hi"))
    assert r.structural_pass is True
    assert r.artifact_valid is None
