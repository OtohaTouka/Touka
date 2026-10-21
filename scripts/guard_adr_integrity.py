# ruff: noqa: T201
"""ADR Integrity Guard — 驗證架構決策紀錄 (ADR) 的引用、編號與索引完整性。

ADR-0068: 防止「配給 ADR 編號卻未建檔」造成的斷鏈與跳號（如 0059–0063 事件）。
ADR-0173: 補第 4 檢查「修訂雙向一致」，把「單向修訂追蹤」漂移結晶為機器執法。

四項檢查：
1. 引用完整性：程式 / 測試 / 文件中任何 `ADR-NNNN` 引用，必須有對應的
   `docs/architecture/adr/NNNN-*.md` 檔。
2. 編號連續性：ADR 檔編號必須連續（0001..最大號），缺號即報錯（除非登記於
   `WITHDRAWN_ADRS`）。
3. 索引同步：`adr/README.md` 索引表與實體檔案雙向對齊（每檔有列、每列有檔）。
4. 修訂雙向一致：若 ADR-Y 以「反轉性動詞 + ADR-X」正向宣告修訂/取代 X，則 X 的
   狀態區或 README 索引列必須回指 Y（反向註記）。否則報「單向修訂」。

掃描策略仿 `guard_noqa_registry.py`（doc-vs-reality 同步），排除 site/ 等產出目錄。
"""

import re
import stat
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
ADR_DIR = PROJECT_ROOT / "docs" / "architecture" / "adr"
INDEX_PATH = ADR_DIR / "README.md"

# 明確作廢、允許缺號的 ADR 編號（目前無）。未來若有 ADR 被撤回且不保留佔位檔，
# 必須在此登記，否則連續性檢查會報跳號。
WITHDRAWN_ADRS: frozenset[str] = frozenset()

IGNORED_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "env", ".cache", "node_modules",
    "site-packages", "__pycache__", ".pytest_cache", ".git", ".claude", "data",
    "site",  # mkdocs 產出（含舊 ADR 的 HTML 複本），非事實來源
    ".toukadev",  # ToukaDev 本機工作區（lessons／worktrees）；只比對名稱，勿改成路徑逐段比對
})

# 掃描的檔案類型（程式、前端、文件）
_SCAN_SUFFIXES: frozenset[str] = frozenset({".md", ".py", ".ts", ".tsx"})

# ADR 引用偵測：要求 ADR 前綴 + 連字號或空白 + 四位數字（避免把日期/時間誤判）。
# 涵蓋 ADR-0063 / ADR 0008 / （ADR-0063） / [ADR-0063](...) / ADR-0063：等格式。
_ADR_REF_RE = re.compile(r"ADR[-\s](\d{4})(?!\d)", re.IGNORECASE)

# ADR 檔名：NNNN-some-slug.md（README.md 不符此式，自動排除）
_ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")

# 索引表列：| [ADR-NNNN](...) | ...
_INDEX_ROW_RE = re.compile(r"^\|\s*\[ADR-(\d{4})\]", re.MULTILINE)

# ---- 檢查 4（修訂雙向一致）SSOT ----
# 「反轉性」強動詞：宣告 Decision 被反轉/取代者，會使舊 ADR 讀者誤解現況。
# 刻意不含「延伸/延續/建基於/抽取/細分」（擴充或純檔案位移、非決策反轉）——
# 那些反向註記仍鼓勵但屬選配，不納閘（避免大量低價值假陽性）。
_AMEND_VERBS = r"修訂|取代|反轉|汰換|廢棄|supersede[sd]?|amend(?:s|ed)?"

# 正向宣告樣式：動詞在前、ADR 編號緊接其後（編號為受詞）、中間僅容許空白／「了」。
# 三種天然不符本式故不會被誤判為正向 cite（掃全文亦安全）：
#   ①「ADR-Y 動詞…」（編號在前）＝反向註記慣用式；
#   ②「…動詞 by ADR-Y」（被動，中間有 by）；
#   ③「動詞：ADR-Y」（冒號形＝反向註記標籤，如「修訂：[ADR-0118]」）。
# 真正的正向宣告一律「動詞 空白 ADR-X」（取代 ADR-0048／Amends ADR-0035／修訂 ADR-0071）。
_FORWARD_CITE_RE = re.compile(
    rf"(?:{_AMEND_VERBS})\s*(?:了)?\s*\[?ADR[-\s]?(\d{{4}})(?!\d)",
    re.IGNORECASE,
)

# 正向宣告已存在、但反向註記刻意不加（或無法加）的例外對 (Y, X)。
# 每筆須註明理由；新增前請先確認 X 真的不需/無法補反向註記。
AMENDMENT_BACKLINK_GRANDFATHER: frozenset[tuple[str, str]] = frozenset()


def _is_source_path(path: Path) -> bool:
    """Exclude data lexically and reject every link ancestor without following it."""
    absolute = path.absolute()
    if any(part.casefold() == "data" for part in absolute.parts):
        return False
    for component in (*reversed(absolute.parents), absolute):
        metadata = component.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return False
    return True


def get_adr_files(adr_dir: Path) -> dict[str, Path]:
    """掃描 ADR 目錄，回傳 {四位編號: 檔案路徑}。README.md 等非 NNNN- 檔自動排除。"""
    files: dict[str, Path] = {}
    if not _is_source_path(adr_dir) or not adr_dir.is_dir():
        return files
    for item in adr_dir.iterdir():
        if item.name.casefold() == "data" or not _is_source_path(item) or not item.is_file():
            continue
        match = _ADR_FILE_RE.match(item.name)
        if match:
            files[match.group(1)] = item
    return files


def _iter_files(root: Path):
    """遞迴迭代待掃描檔案（.md/.py/.ts/.tsx），跳過 IGNORED_DIRS。"""
    if root.name.casefold() in IGNORED_DIRS or not _is_source_path(root):
        return
    for item in root.iterdir():
        if item.name.casefold() in IGNORED_DIRS or not _is_source_path(item):
            continue
        if item.is_dir():
            yield from _iter_files(item)
        elif item.suffix in _SCAN_SUFFIXES:
            yield item


def scan_references(root: Path) -> dict[str, set[str]]:
    """掃描代碼庫，回傳 {被引用的 ADR 編號: {引用該編號的相對檔案路徑}}。"""
    refs: dict[str, set[str]] = defaultdict(set)
    for path in _iter_files(root):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = {m.group(1) for m in _ADR_REF_RE.finditer(content)}
        if not found:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        for num in found:
            refs[num].add(rel)
    return dict(refs)


def parse_index(index_path: Path) -> set[str]:
    """從 adr/README.md 索引表解析所有列出的 ADR 編號。"""
    if not index_path.is_file():
        return set()
    content = index_path.read_text(encoding="utf-8")
    return set(_INDEX_ROW_RE.findall(content))


def check_references(referenced: dict[str, set[str]], adr_nums: set[str]) -> list[str]:
    """檢查 1：每個被引用的 ADR 編號都必須有對應檔案。"""
    errors: list[str] = []
    for num in sorted(referenced):
        if num not in adr_nums:
            sources = ", ".join(sorted(referenced[num])[:3])
            errors.append(
                f"[斷鏈] ADR-{num} 被引用，但找不到 docs/architecture/adr/{num}-*.md"
                f"（引用處：{sources}）。請建立該 ADR 檔，或修正引用編號。"
            )
    return errors


def check_contiguity(adr_nums: set[str], withdrawn: frozenset[str] = frozenset()) -> list[str]:
    """檢查 2：ADR 編號必須連續（0001..最大號），缺號即報錯（除非已登記作廢）。"""
    if not adr_nums:
        return []
    max_num = max(int(n) for n in adr_nums)
    errors: list[str] = []
    for n in range(1, max_num + 1):
        key = f"{n:04d}"
        if key not in adr_nums and key not in withdrawn:
            errors.append(
                f"[跳號] ADR-{key} 缺號（編號必須連續 0001..{max_num:04d}）。"
                " 請補建該 ADR 檔，或若確認作廢請登記於 WITHDRAWN_ADRS。"
            )
    return errors


def parse_index_rows(index_path: Path) -> dict[str, str]:
    """從 adr/README.md 擷取每個 ADR 的索引列全文，回傳 {四位編號: 該列文字}。"""
    rows: dict[str, str] = {}
    if not index_path.is_file():
        return rows
    for line in index_path.read_text(encoding="utf-8").splitlines():
        match = _INDEX_ROW_RE.match(line)
        if match:
            rows[match.group(1)] = line
    return rows


def _status_region(text: str) -> str:
    """擷取 ADR 檔頂部狀態區（反向註記所在）。

    取至第一個內容區塊標頭（## 背景/上下文/Context/決策/Decision）之前；
    涵蓋不到則退回前 1500 字。
    """
    match = re.search(r"\n##\s*(背景|上下文|Context|決策|Decision|問題)", text)
    if match:
        return text[: match.start()]
    return text[:1500]


def detect_forward_amendments(
    adr_files: dict[str, Path], index_rows: dict[str, str]
) -> set[tuple[str, str]]:
    """收集正向修訂宣告對 (Y, X)：Y 以反轉性動詞宣告修訂/取代 X（X≠Y）。

    掃描 ADR 內文（各檔全文）＋ README 索引列（列＝該列 ADR 的宣告）。
    """
    pairs: set[tuple[str, str]] = set()
    for y_num, path in adr_files.items():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _FORWARD_CITE_RE.finditer(text):
            x_num = m.group(1)
            if x_num != y_num:
                pairs.add((y_num, x_num))
    for y_num, row in index_rows.items():
        for m in _FORWARD_CITE_RE.finditer(row):
            x_num = m.group(1)
            if x_num != y_num:
                pairs.add((y_num, x_num))
    return pairs


def check_amendment_backlinks(
    adr_files: dict[str, Path],
    index_rows: dict[str, str],
    grandfather: frozenset[tuple[str, str]] = frozenset(),
) -> list[str]:
    """檢查 4：每個正向修訂宣告 (Y→X) 都必須在 X 的狀態區或 README 索引列有反向註記（提到 Y）。

    治本「單向修訂追蹤」漂移（本專案 ADR 最大宗）：後續 ADR-Y 宣告修訂 ADR-X，
    但被改的舊 ADR-X 未回頭加反向註記、仍標 ✅ 已接受，直開舊 ADR 的讀者會誤解現況。
    """
    errors: list[str] = []
    status_cache: dict[str, str] = {}
    for y_num, x_num in sorted(detect_forward_amendments(adr_files, index_rows)):
        if (y_num, x_num) in grandfather:
            continue
        if x_num not in adr_files:
            continue  # 斷鏈由檢查 1 處理
        if x_num not in status_cache:
            try:
                status_cache[x_num] = _status_region(
                    adr_files[x_num].read_text(encoding="utf-8", errors="ignore")
                )
            except OSError:
                status_cache[x_num] = ""
        y_ref = re.compile(rf"ADR[-\s]?{y_num}(?!\d)", re.IGNORECASE)
        haystack = status_cache[x_num] + "\n" + index_rows.get(x_num, "")
        if not y_ref.search(haystack):
            errors.append(
                f"[單向修訂] ADR-{y_num} 宣告修訂/取代 ADR-{x_num}，但 ADR-{x_num} "
                f"的狀態區與 README 索引列皆無回指 ADR-{y_num} 的反向註記。請在 "
                f"ADR-{x_num} 補反向狀態註記（如「（… by ADR-{y_num}）」）＋ README "
                f"索引列，或若確認不需，登記於 AMENDMENT_BACKLINK_GRANDFATHER。"
            )
    return errors


def check_index_sync(adr_nums: set[str], index_nums: set[str]) -> list[str]:
    """檢查 3：索引表與實體檔案雙向對齊。"""
    errors: list[str] = []
    for num in sorted(adr_nums - index_nums):
        errors.append(
            f"[索引缺列] ADR-{num} 有實體檔案，但未列入 adr/README.md 索引表。"
        )
    for num in sorted(index_nums - adr_nums):
        errors.append(
            f"[索引多列] adr/README.md 索引列出 ADR-{num}，但找不到對應實體檔案。"
        )
    return errors


def main() -> None:
    """Guard 執行入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("🔍 ADR Integrity Guard 正在啟動...")

    adr_files = get_adr_files(ADR_DIR)
    adr_nums = set(adr_files)
    if not adr_nums:
        print(f"❌ 找不到任何 ADR 檔：{ADR_DIR}")
        sys.exit(1)

    referenced = scan_references(PROJECT_ROOT)
    index_nums = parse_index(INDEX_PATH)
    index_rows = parse_index_rows(INDEX_PATH)

    errors: list[str] = []
    errors += check_references(referenced, adr_nums)
    errors += check_contiguity(adr_nums, WITHDRAWN_ADRS)
    errors += check_index_sync(adr_nums, index_nums)
    errors += check_amendment_backlinks(
        adr_files, index_rows, AMENDMENT_BACKLINK_GRANDFATHER
    )

    if errors:
        print(f"🚨 發現 {len(errors)} 處 ADR 完整性問題：\n")
        for error in errors:
            print(f"  {error}")
        print("\n❌ 請修正後重新執行（見 ADR-0068）。")
        sys.exit(1)

    max_num = max(int(n) for n in adr_nums)
    print(
        f"✅ ADR 完整性正常（{len(adr_nums)} 份，0001..{max_num:04d} 連續、"
        "無斷鏈、索引齊全、修訂雙向一致）。"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
