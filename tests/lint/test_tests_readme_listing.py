"""tests/README.md 列出每個測試檔，但不寫「現在有幾個案例」（ADR-0278）。

案例數在不同平台本來就不同（例如 Linux 專用的隔離測試在 Windows 收集到 0 個），
CI 的結果才是正本，README 只留檔名與說明。

- 版控中的每個 `tests/**/test_*.py` 都要在 README 用反引號列出。沒加反引號的不算；
  有相對路徑就比對相對路徑；只寫檔名時，那個檔名在 tests/ 裡必須唯一。
- 反引號裡的 test_*.py 都要對得到版控中的檔案：改名或刪掉的舊名要拿掉反引號，
  免得之後出現的同名新檔被當成已列；路徑不准用 `..` 或絕對路徑。
- 現在還沒列的檔案凍結在 `tests_readme_unlisted.json`，清單只准變短：
  新測試檔要直接在 README 加一列；已經補列或刪掉的檔案要從清單拿掉。
- README 不寫「現在有幾個案例」。這裡只擋固定格式（數字欄、粗體或標籤裡的數量、
  「N 個測試／TDD／案例」、合計句）；某一批加了幾個的歷史紀錄、HTTP 狀態碼、年份、
  有日期的實跑結果、人工 live 評測的預算不算。說明文字裡其他寫法靠審查。
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
WILDCARD = re.compile(r"[*?{]")
NOT_A_COUNT = r"(?![1-5]\d\d\D|(?:19|20)\d\d\D)"  # HTTP 狀態碼、年份
UNIT = r"(?:cases?|collected|測|案)"
COUNT_PATTERNS = (
    # 數字欄
    re.compile(r"^\|[^|]+\| 案例數 \|"),
    # **41 cases**、**M4：35 collected**、**12 測**、**新增 17 cases**
    re.compile(rf"\*\*[^*|]*?(?<![\d.]){NOT_A_COUNT}\d+(?:[＋+]\d+)*\s*{UNIT}\b"),
    # **ADR-0245（8）**、**B2（8 測）**
    re.compile(rf"（{NOT_A_COUNT}\d+(?:[＋+]\d+)*(?:\s*(?:測|cases?))?）\*\*"),
    # **Guard #16 成員鎖（21，ADR-0240）**
    re.compile(rf"\*\*[^*|]*（{NOT_A_COUNT}\d+[，；＝][^*|]*\*\*"),
    # 說明欄開頭的標籤（3）：
    re.compile(rf"^\| [^|]+ \| [^|（]{{0,60}}?（{NOT_A_COUNT}\d[\d＋+]*）(?:[：；]|——)"),
    # 11 個 TDD、43 個測試（「加 8 個測試」這類增量不算）
    re.compile(r"(?<![\d加增])(?<![加增] )\d+\s*個\s*(?:TDD|測試|案例)"),
    # （29 測）
    re.compile(r"（\d+\s*測）"),
    # 合計 177 cases、共 **168 案**
    re.compile(r"(?:合計|共)\s*\**\d+\s*(?:cases?|案)"),
)


def git_ls_files(*pathspec: str) -> list[str]:
    """回傳版控中的檔案（斜線分隔）。沒有 git 就失敗、不跳過。"""
    git = shutil.which("git")
    assert git is not None, "git is required for this check; it must not be skipped"
    result = subprocess.run(  # noqa: S603 - resolved git, fixed argv, no shell
        [git, "ls-files", "-z", "--", *pathspec],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def tracked_test_files() -> list[str]:
    """回傳 tests/ 底下版控中的 test_*.py，相對於 tests/。"""
    paths = [PurePosixPath(name) for name in git_ls_files("tests")]
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


def dangling_test_names(readme: str, tracked_tests: list[str], tracked_repo: set[str]) -> list[str]:
    """反引號裡對不到版控檔案的 test_*.py，每筆附原因。萬用字元的寫法不算。"""
    names = Counter(PurePosixPath(path).name for path in tracked_tests)
    problems = set()
    for token in BACKTICKED_PY.findall(readme):
        path = PurePosixPath(token)
        if not path.name.startswith("test_") or WILDCARD.search(token):
            continue
        if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", token):
            problems.add(f"{token}：不准用絕對路徑或 ..")
        elif "/" in token:
            if token not in tracked_repo and f"tests/{token}" not in tracked_repo:
                problems.add(f"{token}：路徑對不到版控中的檔案")
        elif names[token] == 0:
            problems.add(f"{token}：對不到任何檔案，改名或刪掉的舊名請拿掉反引號")
        elif names[token] > 1:
            problems.add(f"{token}：tests/ 裡有 {names[token]} 個同名檔，請寫相對路徑")
    return sorted(problems)


def test_every_test_file_is_listed_or_frozen() -> None:
    tracked = tracked_test_files()
    unlisted = set(tracked) - listed_files(README.read_text(encoding="utf-8"), tracked)
    frozen = set(json.loads(UNLISTED.read_text(encoding="utf-8")))
    new = sorted(unlisted - frozen)
    assert not new, f"在 tests/README.md 為這些測試檔加一列（檔名放在反引號裡）：{new}"
    stale = sorted(frozen - unlisted)
    assert not stale, f"這些已經列在 README 或已刪除，請從 {UNLISTED.name} 拿掉：{stale}"


def test_backticked_test_names_point_at_real_files() -> None:
    readme = README.read_text(encoding="utf-8")
    problems = dangling_test_names(readme, tracked_test_files(), set(git_ls_files()))
    assert not problems, "\n".join(problems)


def test_readme_writes_no_current_case_counts() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    hits = [
        f"{number}: {line[:80]}"
        for number, line in enumerate(lines, 1)
        if any(pattern.search(line) for pattern in COUNT_PATTERNS)
    ]
    assert not hits, "tests/README.md 不寫現在有幾個案例，CI 的結果才是正本：\n" + "\n".join(hits)


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


def test_dangling_names_are_reported_with_the_reason() -> None:
    tracked_tests = ["unit/test_alpha.py", "unit/test_dup.py", "lint/test_dup.py"]
    tracked_repo = {"tests/unit/test_alpha.py", "tests/unit/test_dup.py", "tests/lint/test_dup.py"}
    tracked_repo.add("ToukaIDE/scripts/test_tool.py")
    readme = " ".join(
        [
            "`unit/test_alpha.py` `tests/unit/test_alpha.py` `ToukaIDE/scripts/test_tool.py`",
            "`test_alpha.py` `test_*.py` `**/test_*.py` `test_golden_{a,b}.py`",
            "`lint/test_alpha.py` `test_renamed.py` `test_dup.py`",
            "`../outside/test_x.py` `/abs/test_y.py` `C:/x/test_z.py`",
        ]
    )
    problems = dangling_test_names(readme, tracked_tests, tracked_repo)
    assert [problem.split("：")[0] for problem in problems] == sorted(
        [
            "lint/test_alpha.py",
            "test_renamed.py",
            "test_dup.py",
            "../outside/test_x.py",
            "/abs/test_y.py",
            "C:/x/test_z.py",
        ]
    )
    assert any("同名檔" in problem for problem in problems if problem.startswith("test_dup.py"))


def test_count_patterns_catch_current_counts_only() -> None:
    current_counts = [
        "| 檔案 | 案例數 | 說明 |",
        "| `test_x.py` | **41 cases**：說明 |",
        "| `test_x.py` | **3 cases（Linux）**：說明 |",
        "| `test_x.py` | **新增 17 cases**：說明 |",
        "| `test_x.py` | **15 collected**：說明 |",
        "| `test_x.py` | **M4：35 collected**：說明 |",
        "| `test_x.py` | **12 測**：說明 |",
        "| `test_x.py` | **ADR-0245（8）**：說明 |",
        "| `test_x.py` | **B2（8 測）**（ADR-0137）：說明 |",
        "| `test_x.py` | **Guard #16 成員鎖（21，ADR-0240）**：說明 |",
        "| `test_x.py` | **原生聚合器（25＝原23＋2）**：說明 |",
        "| `x.test.tsx` | ADR-0081 重整接回（3）：說明 |",
        "| `test_x.py` | 純函式：11 個 TDD（說明） |",
        "| `test_x.py` | `SlidingWindowService`：43 個測試 |",
        "| `test_x.py` | 37 個案例（說明） |",
        "| `test_x.py` | 見 `test_y.py`（29 測） |",
        "六個新測試檔共 **168 案**，說明",
        "全部離線。合計 177 cases。",
    ]
    for line in current_counts:
        assert any(pattern.search(line) for pattern in COUNT_PATTERNS), line
    not_counts = [
        "| `test_x.py` | 含 `..` 的 ID（422），不改寫 |",
        "| `test_x.py` | 沒登入（401）：說明 |",
        "| `test_x.py` | 欄位錯誤（422）；說明 |",
        "| `test_x.py` | 舊格式（2026）——說明 |",
        "| `test_x.py` | 改名拒絕（409，含 `ToukaSoul`） |",
        "| `test_x.py` | **404**：找不到 |",
        "| `test_x.py` | **2026** 版 |",
        "| `test_x.py` | **找不到（404）**：說明 |",
        "| `test_x.py` | **改名拒絕（409，含 ToukaSoul）**：說明 |",
        "| `x.test.tsx` | ADR-0254 H1（+4）：某一批加的案例 |",
        "| `test_x.py` | （C2-2 +3：某一批加的案例） |",
        "| `test_x.py` | ADR-0263 加 8 個測試：說明 |",
        "| `test_x.py` | 2026-08-14 實跑結果：**24/24 Green** |",
        "| `test_x.py` | 先 **1 failed／1 passed**，再 **2 passed** |",
        "| `test_x.py` | 9 個 bash 測試用 Git for Windows |",
        "| `test_x.py` | 產品數量（4 個） |",
        "| `tool_eval` | 12 cases；samples／turns 硬上限 4，最多 48 requests |",
        "| `test_x.py` | 版本（v2）、（3a／3b）、（P8-B）、（3.12） |",
    ]
    for line in not_counts:
        assert not any(pattern.search(line) for pattern in COUNT_PATTERNS), line
