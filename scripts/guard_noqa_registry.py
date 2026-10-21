# ruff: noqa: T201
"""NOQA Registry Sync Guard — 驗證 NOQA_REGISTRY.md 統計與代碼庫實際計數一致性。

ADR-0029: 強制開發者在新增架構型 # noqa 時同步更新 NOQA_REGISTRY.md 統計表。

掃描策略：
- Python (.py)：使用 tokenize 模組，只統計「行內 comment」（同一行有程式碼的 # noqa），
  自動排除字串字面量、docstring 與獨立說明型 comment（如 # 例：# noqa: RULE）。
- TypeScript (.ts/.tsx)：行級掃描，排除以 `//`、`*`、`/*` 開頭的說明行。
"""

import io
import re
import stat
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
REGISTRY_PATH = PROJECT_ROOT / "docs" / "architecture" / "NOQA_REGISTRY.md"

IGNORED_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "env", ".cache", "node_modules",
    "site-packages", "__pycache__", ".pytest_cache", ".git", ".claude", "data",
    "__tests__",   # TS guard 測試目錄：含有 noqa 作為測試資料，非真實豁免
    ".toukadev",   # ToukaDev 本機工作區（lessons／worktrees）；只比對名稱，勿改成路徑逐段比對
})

# 統計表中的具名規則行：| `RULE_ID` | N | description |
# 略過複合行「其他 (`S104`, ...)」（開頭不是單一 backtick rule）
_STATS_ROW_RE = re.compile(
    r"^\|\s*`([A-Z][A-Z0-9_]{2,})`\s*\|\s*(\d+)\s*\|",
    re.MULTILINE,
)

# 架構型 rule ID 判定：全大寫 + 含至少一個底線（排除 B008, E501, S603 等純格式規則）
_ARCH_RULE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

# noqa 規則提取正則（允許規則清單後接說明文字）
_NOQA_EXTRACT_RE = re.compile(
    r"(?:#|//)\s*noqa:\s*([A-Z][A-Z0-9_]*(?:\s*,\s*[A-Z][A-Z0-9_]*)*)"
)

# TypeScript：只有以程式碼開頭的行才計入（排除純 comment / block comment 行）
_TS_COMMENT_ONLY_RE = re.compile(r"^\s*(?://|\*|/\*)")


def _is_arch_rule(rule_id: str) -> bool:
    """判定是否為架構型規則（全大寫 + 至少一個底線）。

    架構型：IO_PRISON_VIOLATION, LOCAL_IMPORT_BAN, FETCH_SSOT ...
    標準 Ruff：B008, E501, S603, TC003 ... （字母+數字，無底線）
    """
    return bool(_ARCH_RULE_RE.match(rule_id))


def parse_registry_stats(registry_path: Path) -> dict[str, int]:
    """從 NOQA_REGISTRY.md 解析全系統統計表，回傳 {rule_id: expected_count}。

    只解析有獨立具名行的規則（regex 限制僅匹配單一 backtick-quoted ALL_CAPS 識別符），
    自動略過「其他 (`S104`, ...)」複合行。
    """
    content = registry_path.read_text(encoding="utf-8")
    return {
        rule_id: int(count)
        for rule_id, count in _STATS_ROW_RE.findall(content)
    }


def _extract_rules_from_noqa(noqa_str: str) -> list[str]:
    """從 noqa 字串提取所有規則 ID（支援逗號分隔多規則）。"""
    return [r.strip() for r in noqa_str.split(",") if r.strip()]


def _scan_py_file(content: str) -> dict[str, int]:
    """使用 tokenize 只從「行內 COMMENT token」中提取 noqa 規則。

    行內 comment = 同一行也有其他程式碼 token（非 COMMENT/NEWLINE/INDENT 等）。
    這確保：
    - 字串字面量中的 noqa（STRING token）→ 排除 ✓
    - Docstring 中的 noqa（STRING token）→ 排除 ✓
    - 純說明型 comment（如 # 例：# noqa: RULE）→ 排除（無程式碼 token）✓
    - 真實 inline noqa（如 `code()  # noqa: RULE`）→ 計入 ✓
    """
    counts: dict[str, int] = defaultdict(int)
    _CODE_TOKEN_TYPES = frozenset({
        tokenize.NAME, tokenize.NUMBER, tokenize.STRING,
        tokenize.OP, tokenize.ERRORTOKEN,
    })
    try:
        all_tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
    except tokenize.TokenError:
        return {}

    # 收集有程式碼 token 的行號
    code_lines: set[int] = set()
    for tok_type, _, (row, _), *_ in all_tokens:
        if tok_type in _CODE_TOKEN_TYPES:
            code_lines.add(row)

    # 只計算行內 COMMENT token（同行有程式碼）中的 noqa
    for tok_type, tok_string, (row, _), *_ in all_tokens:
        if tok_type != tokenize.COMMENT:
            continue
        if row not in code_lines:
            continue  # 純說明型 comment（無程式碼），跳過
        match = _NOQA_EXTRACT_RE.search(tok_string)
        if not match:
            continue
        seen: set[str] = set()
        for rule_id in _extract_rules_from_noqa(match.group(1)):
            if rule_id and rule_id not in seen:
                counts[rule_id] += 1
                seen.add(rule_id)

    return dict(counts)


def _scan_ts_file(content: str) -> dict[str, int]:
    """行級掃描 TypeScript/TSX 檔案中的 // noqa: RULE 指令。

    排除純 comment 行（以 //、*、/* 為行首的行），
    只計算出現在程式碼之後的 // noqa: RULE 指令。
    """
    counts: dict[str, int] = defaultdict(int)
    for line in content.splitlines():
        # 排除純 comment 行（說明文字或 JSDoc block comment 行）
        if _TS_COMMENT_ONLY_RE.match(line):
            continue
        match = _NOQA_EXTRACT_RE.search(line)
        if not match:
            continue
        seen: set[str] = set()
        for rule_id in _extract_rules_from_noqa(match.group(1)):
            if rule_id and rule_id not in seen:
                counts[rule_id] += 1
                seen.add(rule_id)
    return dict(counts)


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


def _iter_files(root: Path):
    """遞迴迭代 .py 和 .ts/.tsx 檔案，跳過 IGNORED_DIRS。"""
    if root.name.casefold() in IGNORED_DIRS or not _is_source_path(root):
        return
    for item in root.iterdir():
        if item.name.casefold() in IGNORED_DIRS or not _is_source_path(item):
            continue
        if item.is_dir():
            yield from _iter_files(item)
        elif item.suffix == ".py":
            yield item, "py"
        elif item.suffix in {".ts", ".tsx"}:
            yield item, "ts"


def scan_codebase(root: Path) -> dict[str, int]:
    """掃描代碼庫，回傳 {rule_id: actual_count}。

    Python：tokenize-based 行內 comment（排除字串/docstring/說明型 comment）
    TypeScript：行級掃描，排除純 comment 行
    """
    total: dict[str, int] = defaultdict(int)
    for path, lang in _iter_files(root):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanner = _scan_py_file if lang == "py" else _scan_ts_file
        for rule_id, count in scanner(content).items():
            total[rule_id] += count
    return dict(total)


def compare(expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    """比對預期與實際計數，回傳錯誤訊息列表。

    規則：
    1. registry 中具名的規則：actual[rule] 必須等於 expected[rule]
    2. actual 中出現但 registry 未追蹤的架構型規則（含底線）→ 報錯
    3. 純 Ruff 格式規則（字母+數字，無底線）→ 不報錯（不是追蹤對象）
    """
    errors: list[str] = []

    for rule_id, expected_count in expected.items():
        actual_count = actual.get(rule_id, 0)
        if actual_count != expected_count:
            errors.append(
                f"[{rule_id}] 登記數 {expected_count}，實際掃描 {actual_count}。"
                " 請更新 NOQA_REGISTRY.md 統計表。"
            )

    for rule_id, actual_count in actual.items():
        if rule_id not in expected and actual_count > 0 and _is_arch_rule(rule_id):
            errors.append(
                f"[{rule_id}] 發現 {actual_count} 個架構型 noqa，但未登記於 NOQA_REGISTRY.md。"
                " 請新增至統計表並說明豁免理由。"
            )

    return errors


def main() -> None:
    """Guard 執行入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("🔍 NOQA Registry Sync Guard 正在啟動...")

    if not REGISTRY_PATH.exists():
        print(f"❌ 找不到 NOQA_REGISTRY.md：{REGISTRY_PATH}")
        sys.exit(1)

    expected = parse_registry_stats(REGISTRY_PATH)
    actual = scan_codebase(PROJECT_ROOT)
    errors = compare(expected, actual)

    if errors:
        print(f"🚨 發現 {len(errors)} 處 NOQA Registry 不同步：\n")
        for error in errors:
            print(f"  {error}")
        print("\n❌ 請更新 NOQA_REGISTRY.md 後重新執行。")
        sys.exit(1)

    print(f"✅ NOQA Registry 同步正常（共追蹤 {len(expected)} 條具名規則）。")
    sys.exit(0)


if __name__ == "__main__":
    main()
