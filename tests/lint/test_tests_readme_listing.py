"""tests/README.md 列出每個測試檔，但不寫案例數（ADR-0278）。

案例數在不同平台本來就不同（例如 Linux 專用的隔離測試在 Windows 收集到 0 個），
CI 的結果才是正本，README 只留檔名與說明。

- 版控中的每個 `tests/**/test_*.py` 都要在 README 用反引號列出。只認反引號裡的名稱：
  有相對路徑就比對相對路徑；只寫檔名時，那個檔名在 tests/ 裡必須唯一。
- 現在還沒列的檔案凍結在 `tests_readme_unlisted.json`，清單只准變短：
  新測試檔要直接在 README 加一列；已經補列或刪掉的檔案要從清單拿掉。
- README 不再寫案例數。
"""

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).absolute().parents[2]
README = ROOT / "tests" / "README.md"
UNLISTED = Path(__file__).with_name("tests_readme_unlisted.json")
BACKTICKED_PY = re.compile(r"`([^`\s]+\.py)`")
COUNT_PATTERNS = (
    re.compile(r"^\|[^|]+\| 案例數 \|"),
    re.compile(r"\*\*\d+(?:[＋+]\d+)*\s*cases?\b"),
    re.compile(r"（\d+(?:[＋+]\d+)*(?:\s*(?:測|cases?))?）\*\*"),
    re.compile(r"^\| [^|]+ \| [^|（]{0,60}?（[\d＋+]+）(?:[：；]|——)"),
)


def tracked_test_files() -> list[str]:
    """回傳 tests/ 底下版控中的 test_*.py（相對於 tests/，斜線分隔）。沒有 git 就失敗、不跳過。"""
    git = shutil.which("git")
    assert git is not None, "git is required for this check; it must not be skipped"
    result = subprocess.run(  # noqa: S603 - resolved git, fixed argv, no shell
        [git, "ls-files", "-z", "--", "tests"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    paths = [PurePosixPath(name) for name in result.stdout.decode("utf-8").split("\0") if name]
    return sorted(
        str(path.relative_to("tests"))
        for path in paths
        if path.name.startswith("test_") and path.suffix == ".py"
    )


def listed_files(readme: str, tracked: list[str]) -> set[str]:
    """README 反引號裡列到的測試檔；只寫檔名時，那個檔名在 tests/ 裡必須唯一。"""
    tokens = {token.removeprefix("tests/") for token in BACKTICKED_PY.findall(readme)}
    basename_counts = Counter(PurePosixPath(path).name for path in tracked)
    return {
        path
        for path in tracked
        if path in tokens
        or (PurePosixPath(path).name in tokens and basename_counts[PurePosixPath(path).name] == 1)
    }


def test_every_test_file_is_listed_or_frozen() -> None:
    tracked = tracked_test_files()
    unlisted = set(tracked) - listed_files(README.read_text(encoding="utf-8"), tracked)
    frozen = set(json.loads(UNLISTED.read_text(encoding="utf-8")))
    new = sorted(unlisted - frozen)
    assert not new, f"在 tests/README.md 為這些測試檔加一列（檔名放在反引號裡）：{new}"
    stale = sorted(frozen - unlisted)
    assert not stale, f"這些已經列在 README 或已刪除，請從 {UNLISTED.name} 拿掉：{stale}"


def test_readme_writes_no_case_counts() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    hits = [
        f"{number}: {line[:80]}"
        for number, line in enumerate(lines, 1)
        if any(pattern.search(line) for pattern in COUNT_PATTERNS)
    ]
    assert not hits, "tests/README.md 不寫案例數，CI 的結果才是正本：\n" + "\n".join(hits)


def test_only_backticked_names_count_as_listed() -> None:
    tracked = [
        "unit/test_alpha.py",
        "lint/test_beta.py",
        "unit/test_gamma.py",
        "lint/test_gamma.py",
    ]
    readme = "\n".join(
        [
            "| `unit/test_alpha.py` | 有相對路徑 |",
            "| `test_beta.py` | 只寫檔名、名稱唯一 |",
            "| `test_gamma.py` | 只寫檔名，但兩個資料夾都有 |",
            "說明文字裡提到 unit/test_gamma.py 不算",
        ]
    )
    assert listed_files(readme, tracked) == {"unit/test_alpha.py", "lint/test_beta.py"}


def test_count_patterns_catch_each_old_style() -> None:
    old_styles = [
        "| 檔案 | 案例數 | 說明 |",
        "| `test_x.py` | **41 cases**：說明 |",
        "| `test_x.py` | **ADR-0245（8）**：說明 |",
        "| `test_x.py` | **B2（8 測）**（ADR-0137）：說明 |",
        "| `x.test.tsx` | ADR-0081 重整接回（3）：說明 |",
    ]
    for line in old_styles:
        assert any(pattern.search(line) for pattern in COUNT_PATTERNS), line
    assert not any(
        pattern.search("| `test_x.py` | 含 `..` 的 ID（422），不改寫 |")
        for pattern in COUNT_PATTERNS
    )
