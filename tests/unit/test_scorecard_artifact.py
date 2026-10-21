"""軌B Batch 5：docgen 成品驗證（artifact_verify）＋engine 成品維度 gating（零-API）。

以 stdlib zipfile 手造最小 OOXML fixture（不需 openpyxl/python-docx），驗證：
合法+含內容→pass、缺內容→fail、缺部件/壞 zip/找不到→fail；路徑擷取（含 Windows 反斜線還原）；
engine.evaluate 在 artifact_kind 情境下以 artifact_valid gate 結構通過。
"""
from __future__ import annotations

import json
import zipfile

from src.services.scorecard.artifact_verify import extract_artifact_path, verify_artifact
from src.services.scorecard.engine import evaluate
from src.services.scorecard.models import RunOutcome, Scenario


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
def test_verify_valid_xlsx_with_content(tmp_path):
    p = _make_xlsx(tmp_path / "ok.xlsx")
    ok, detail = verify_artifact(p, "xlsx", ("APAC", "EMEA", "120", "90"))
    assert ok is True
    assert "合法 xlsx" in detail


def test_verify_xlsx_missing_content(tmp_path):
    p = _make_xlsx(tmp_path / "partial.xlsx", content="APAC 120")
    ok, detail = verify_artifact(p, "xlsx", ("APAC", "EMEA", "999"))
    assert ok is False
    assert "缺內容" in detail
    assert "EMEA" in detail and "999" in detail


def test_verify_missing_marker_part(tmp_path):
    p = _make_xlsx(tmp_path / "nomarker.xlsx", marker=False)
    ok, detail = verify_artifact(p, "xlsx", ("APAC",))
    assert ok is False
    assert "缺必要部件" in detail


def test_verify_not_a_zip(tmp_path):
    bad = tmp_path / "fake.xlsx"
    bad.write_text("this is plain text, not a zip", encoding="utf-8")
    ok, detail = verify_artifact(str(bad), "xlsx", ("APAC",))
    assert ok is False
    assert "zip" in detail.lower()


def test_verify_file_not_found(tmp_path):
    ok, detail = verify_artifact(str(tmp_path / "missing.xlsx"), "xlsx", ("APAC",))
    assert ok is False
    assert "不存在" in detail


def test_verify_empty_path():
    ok, detail = verify_artifact("", "xlsx", ("APAC",))
    assert ok is False
    assert "未擷取" in detail


def test_verify_unsupported_kind(tmp_path):
    p = _make_xlsx(tmp_path / "x.xlsx")
    ok, detail = verify_artifact(p, "pdf", ("APAC",))
    assert ok is False
    assert "不支援" in detail


def test_verify_valid_docx(tmp_path):
    p = _make_docx(tmp_path / "ok.docx", text="季度報告 APAC 摘要")
    ok, _ = verify_artifact(p, "docx", ("季度報告", "APAC"))
    assert ok is True


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
