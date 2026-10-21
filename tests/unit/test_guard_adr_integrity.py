"""ADR Integrity Guard 測試 (TDD) — 見 ADR-0068。

驗證 guard_adr_integrity.py 的三項檢查：引用完整性、編號連續性、索引同步。

注意：本測試檔本身會被真守衛掃描，故所有「假」ADR 編號一律以
`f"ADR-{n:04d}"` 動態組字串（見 `_ref`），檔內不得出現靜態的假 `ADR-NNNN`
字面，否則真守衛會把它當成引用了不存在的 ADR。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from guard_adr_integrity import (  # noqa: E402
    ADR_DIR,
    INDEX_PATH,
    check_amendment_backlinks,
    check_contiguity,
    check_index_sync,
    check_references,
    detect_forward_amendments,
    get_adr_files,
    parse_index,
    parse_index_rows,
    scan_references,
)
from guard_adr_integrity import PROJECT_ROOT as GUARD_ROOT  # noqa: E402


def _ref(n: int) -> str:
    """動態組 ADR 引用字串，避免在測試檔留下靜態假編號。"""
    return f"ADR-{n:04d}"


def _num(n: int) -> str:
    """動態組四位編號字串。"""
    return f"{n:04d}"


# ---------------------------------------------------------------------------
# get_adr_files
# ---------------------------------------------------------------------------


def test_get_adr_files_extracts_numbers_and_skips_readme(tmp_path: Path) -> None:
    """只回傳符合 NNNN- 命名的 ADR 檔，README.md 等被排除。"""
    (tmp_path / "0001-alpha.md").write_text("x", encoding="utf-8")
    (tmp_path / "0002-beta.md").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("index", encoding="utf-8")
    assert set(get_adr_files(tmp_path)) == {"0001", "0002"}


# ---------------------------------------------------------------------------
# scan_references
# ---------------------------------------------------------------------------


def test_scan_references_detects_multiple_formats(tmp_path: Path) -> None:
    """ADR-NNNN / （ADR-NNNN） / [ADR-NNNN](...) / ADR-NNNN： 等格式皆能偵測。"""
    (tmp_path / "doc.md").write_text(
        f"see {_ref(1)} and （{_ref(2)}） and [{_ref(3)}](x.md) and {_ref(4)}：ok",
        encoding="utf-8",
    )
    assert set(scan_references(tmp_path)) == {"0001", "0002", "0003", "0004"}


def test_scan_references_ignores_non_adr_numbers(tmp_path: Path) -> None:
    """無 ADR 前綴的四位數字（時間、日期、雜湊）不得誤判。"""
    (tmp_path / "doc.md").write_text(
        "timeout 3000ms, date 2026-05-31, value 1234 here.", encoding="utf-8"
    )
    assert scan_references(tmp_path) == {}


def test_scan_references_skips_ignored_dirs(tmp_path: Path) -> None:
    """node_modules 等忽略目錄中的引用不掃描。"""
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "x.md").write_text(_ref(9), encoding="utf-8")
    assert scan_references(tmp_path) == {}


# ---------------------------------------------------------------------------
# check_references
# ---------------------------------------------------------------------------


def test_check_references_flags_missing() -> None:
    """被引用但無對應檔案 → 報斷鏈，訊息含缺號與引用處。"""
    errors = check_references({_num(2): {"some/file.py"}}, {_num(1)})
    assert len(errors) == 1
    assert _num(2) in errors[0]
    assert "some/file.py" in errors[0]


def test_check_references_passes_when_all_exist() -> None:
    """所有被引用編號都有檔案 → 無錯。"""
    assert check_references({_num(1): {"f.md"}}, {_num(1), _num(2)}) == []


# ---------------------------------------------------------------------------
# check_contiguity
# ---------------------------------------------------------------------------


def test_check_contiguity_detects_gap() -> None:
    """0001,0002,0004 → 偵測到缺 0003。"""
    errors = check_contiguity({_num(1), _num(2), _num(4)})
    assert len(errors) == 1
    assert _num(3) in errors[0]


def test_check_contiguity_passes_when_contiguous() -> None:
    """連續編號 → 無錯。"""
    assert check_contiguity({_num(1), _num(2), _num(3)}) == []


def test_check_contiguity_respects_withdrawn() -> None:
    """登記於 WITHDRAWN 的缺號不報錯；未登記則報錯。"""
    assert check_contiguity({_num(1), _num(3)}, frozenset({_num(2)})) == []
    assert check_contiguity({_num(1), _num(3)}) != []


# ---------------------------------------------------------------------------
# check_index_sync
# ---------------------------------------------------------------------------


def test_check_index_sync_file_without_row() -> None:
    """有檔但索引缺列 → 報錯。"""
    errors = check_index_sync({_num(1), _num(2)}, {_num(1)})
    assert len(errors) == 1
    assert _num(2) in errors[0]


def test_check_index_sync_row_without_file() -> None:
    """索引有列但無檔 → 報錯。"""
    errors = check_index_sync({_num(1)}, {_num(1), _num(2)})
    assert len(errors) == 1
    assert _num(2) in errors[0]


def test_check_index_sync_matched() -> None:
    """索引與檔案完全對齊 → 無錯。"""
    assert check_index_sync({_num(1), _num(2)}, {_num(1), _num(2)}) == []


# ---------------------------------------------------------------------------
# parse_index
# ---------------------------------------------------------------------------


def test_parse_index_extracts_rows(tmp_path: Path) -> None:
    """從索引表列擷取 ADR 編號。"""
    idx = tmp_path / "README.md"
    idx.write_text(
        "| ID | 標題 |\n"
        f"| [{_ref(1)}](0001-a.md) | [t](0001-a.md) | ✅ |\n"
        f"| [{_ref(2)}](0002-b.md) | [t](0002-b.md) | ✅ |\n",
        encoding="utf-8",
    )
    assert parse_index(idx) == {"0001", "0002"}


# ---------------------------------------------------------------------------
# parse_index_rows
# ---------------------------------------------------------------------------


def test_parse_index_rows_maps_number_to_full_row(tmp_path: Path) -> None:
    """每個 ADR 索引列擷取為 {編號: 該列全文}。"""
    idx = tmp_path / "README.md"
    idx.write_text(
        "| ID | 標題 | 狀態 |\n"
        f"| [{_ref(1)}](0001-a.md) | [t](0001-a.md) | ✅ 已接受 |\n"
        f"| [{_ref(2)}](0002-b.md) | [t](0002-b.md) | ✅ 已接受（{_ref(1)} 修訂） |\n",
        encoding="utf-8",
    )
    rows = parse_index_rows(idx)
    assert set(rows) == {"0001", "0002"}
    assert _ref(1) in rows["0002"]  # 反向註記文字保留於列


# ---------------------------------------------------------------------------
# detect_forward_amendments — 方向判別（只認動詞在前的正向宣告）
# ---------------------------------------------------------------------------


def _adr(tmp_path: Path, n: int, body: str) -> None:
    """在 tmp ADR 目錄寫一個最小 ADR 檔。"""
    (tmp_path / f"{n:04d}-x.md").write_text(body, encoding="utf-8")


def test_detect_forward_amendment_verb_first_is_matched(tmp_path: Path) -> None:
    """『動詞 ADR-X』（動詞在前）＝正向宣告，被偵測為 (Y, X)。"""
    _adr(tmp_path, 2, f"# ADR-0002\n本 ADR 取代 {_ref(1)} 的決策。")
    _adr(tmp_path, 1, "# ADR-0001\n舊決策。")
    pairs = detect_forward_amendments(get_adr_files(tmp_path), {})
    assert (_num(2), _num(1)) in pairs


def test_detect_forward_amendment_number_first_not_matched(tmp_path: Path) -> None:
    """『ADR-Y 修訂…』（編號在前）＝反向註記慣用式，不被誤判為正向 cite。"""
    _adr(tmp_path, 1, f"# ADR-0001\n✅ 已接受（{_ref(2)} 修訂：某機制改寫）。")
    pairs = detect_forward_amendments(get_adr_files(tmp_path), {})
    assert (_num(1), _num(2)) not in pairs


def test_detect_forward_amendment_passive_by_not_matched(tmp_path: Path) -> None:
    """『… by ADR-Y』（被動、中間有 by）不被誤判為正向 cite。"""
    _adr(tmp_path, 1, f"# ADR-0001\n狀態：部分修訂 by {_ref(2)}。")
    pairs = detect_forward_amendments(get_adr_files(tmp_path), {})
    assert (_num(1), _num(2)) not in pairs


def test_detect_forward_amendment_colon_label_not_matched(tmp_path: Path) -> None:
    """『修訂：ADR-Y』（冒號標籤形）不被誤判為正向 cite。"""
    _adr(tmp_path, 1, f"# ADR-0001\n狀態：✅ 已接受（延伸／修訂：{_ref(2)}）。")
    pairs = detect_forward_amendments(get_adr_files(tmp_path), {})
    assert (_num(1), _num(2)) not in pairs


def test_detect_forward_amendment_weak_verb_not_enforced(tmp_path: Path) -> None:
    """『延伸/抽取』等非反轉性動詞不納入正向對（範圍刻意收斂）。"""
    _adr(tmp_path, 2, f"# ADR-0002\n本 ADR 延伸 {_ref(1)}，並抽取 {_ref(1)} 的邏輯。")
    _adr(tmp_path, 1, "# ADR-0001\n舊決策。")
    pairs = detect_forward_amendments(get_adr_files(tmp_path), {})
    assert (_num(2), _num(1)) not in pairs


def test_detect_forward_amendment_from_index_row(tmp_path: Path) -> None:
    """README 索引列的正向宣告（列＝該列 ADR）也被偵測。"""
    _adr(tmp_path, 2, "# ADR-0002\n無內文 cite。")
    _adr(tmp_path, 1, "# ADR-0001\n舊決策。")
    rows = {_num(2): f"| [{_ref(2)}](x.md) | t | ✅ 已接受（Amends {_ref(1)}） |"}
    pairs = detect_forward_amendments(get_adr_files(tmp_path), rows)
    assert (_num(2), _num(1)) in pairs


# ---------------------------------------------------------------------------
# check_amendment_backlinks — 紅→綠
# ---------------------------------------------------------------------------


def test_amendment_backlink_missing_reverse_note_fails(tmp_path: Path) -> None:
    """Y 宣告修訂 X，但 X 無反向註記 → 報單向修訂（紅）。"""
    _adr(tmp_path, 2, f"# ADR-0002\n本 ADR 取代 {_ref(1)}。")
    _adr(tmp_path, 1, "# ADR-0001\n✅ 已接受\n\n## 背景\n舊決策，無反向註記。")
    errors = check_amendment_backlinks(get_adr_files(tmp_path), {})
    assert len(errors) == 1
    assert _num(1) in errors[0] and _num(2) in errors[0]


def test_amendment_backlink_reverse_in_status_passes(tmp_path: Path) -> None:
    """X 狀態區回指 Y → 綠。"""
    _adr(tmp_path, 2, f"# ADR-0002\n本 ADR 取代 {_ref(1)}。")
    _adr(
        tmp_path,
        1,
        f"# ADR-0001\n✅ 已接受（{_ref(2)} 取代：機制改寫）\n\n## 背景\n舊決策。",
    )
    assert check_amendment_backlinks(get_adr_files(tmp_path), {}) == []


def test_amendment_backlink_reverse_in_index_row_passes(tmp_path: Path) -> None:
    """X 的 README 索引列回指 Y（狀態區沒有）→ 綠。"""
    _adr(tmp_path, 2, f"# ADR-0002\n本 ADR 取代 {_ref(1)}。")
    _adr(tmp_path, 1, "# ADR-0001\n✅ 已接受\n\n## 背景\n舊決策。")
    rows = {_num(1): f"| [{_ref(1)}](x.md) | t | ✅ 已接受（{_ref(2)} 取代） |"}
    assert check_amendment_backlinks(get_adr_files(tmp_path), rows) == []


def test_amendment_backlink_grandfather_suppresses(tmp_path: Path) -> None:
    """登記於 grandfather 的正向對即使無反向註記也不報錯。"""
    _adr(tmp_path, 2, f"# ADR-0002\n本 ADR 取代 {_ref(1)}。")
    _adr(tmp_path, 1, "# ADR-0001\n✅ 已接受\n\n## 背景\n無反向註記。")
    gf = frozenset({(_num(2), _num(1))})
    assert check_amendment_backlinks(get_adr_files(tmp_path), {}, gf) == []


# ---------------------------------------------------------------------------
# 真實 repo 整合驗證
# ---------------------------------------------------------------------------


def test_real_repo_passes_all_checks() -> None:
    """真實 repo 目前應全部通過（連續、無斷鏈、索引齊全、修訂雙向一致）。"""
    adr_nums = set(get_adr_files(ADR_DIR))
    assert adr_nums, "should discover real ADR files"
    referenced = scan_references(GUARD_ROOT)
    index_nums = parse_index(INDEX_PATH)
    index_rows = parse_index_rows(INDEX_PATH)
    adr_files = get_adr_files(ADR_DIR)
    assert check_references(referenced, adr_nums) == []
    assert check_contiguity(adr_nums, frozenset()) == []
    assert check_index_sync(adr_nums, index_nums) == []
    assert check_amendment_backlinks(adr_files, index_rows, frozenset()) == []
