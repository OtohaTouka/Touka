# ruff: noqa: T201
"""靜態檢查只減不增（ADR-0278）：收集違規、比對基準、重拍基準都在這裡。

每條規則的數量必須和基準完全相等。變多要修；變少要在同一個 PR 重拍基準，
讓改善留在差異裡、之後不會被悄悄用掉。測試（`tests/lint/test_ruff_ratchet.py`、
`tests/lint/test_pyrefly_ratchet.py`）和重拍都用這裡唯一一份收集指令，兩邊不會各算各的。
只用標準函式庫。

用法：
    python scripts/lint_ratchet.py ruff               # 檢查
    python scripts/lint_ratchet.py ruff --update      # 重拍基準
    python scripts/lint_ratchet.py pyrefly [--update] # 型別檢查，範圍在 pyproject.toml
"""

import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parent.parent
# 和 CI 的 `ruff check --select F` 步驟相同的四個產品資料夾。
PRODUCT_SOURCE_ROOTS = ("ToukaProxy/src", "ToukaMemory/src", "ToukaHeartbeat", "ToukaDispatch")
RUFF_BASELINE = PROJECT_ROOT / "tests" / "lint" / "ruff_baseline.json"
PYREFLY_BASELINE = PROJECT_ROOT / "tests" / "lint" / "pyrefly_baseline.json"
TOOL_TIMEOUT_SECONDS = 300
SYNTAX_ERROR_KEY = "syntax-error"
# pyrefly 在有型別錯誤時結束碼是 1，可以接受；設定壞掉的結束碼也是 1，只能從輸出分辨。
PYREFLY_FINISHED_EXIT_CODES = (0, 1)
# 正常跑完時 stderr 只有 INFO 行。設定打錯欄位、search-path 不存在都只印 WARN 就照跑，
# 範圍會悄悄變掉；升級後日誌格式也可能改變。所以只要有一行不是 INFO 就算失敗。
PYREFLY_LOG_PREFIX = "INFO "


def _only_info_logs(stderr: str) -> bool:
    """每一行非空白的錯誤輸出都是 INFO 才回 True。"""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return all(line.startswith(PYREFLY_LOG_PREFIX) for line in lines)


def _run_tool_module(args: list[str]) -> subprocess.CompletedProcess[str]:
    """在 repo 根目錄用目前的直譯器跑檢查工具模組，不經 shell。"""
    return subprocess.run(  # noqa: S603 - fixed tool module argv, no shell
        [sys.executable, "-m", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=TOOL_TIMEOUT_SECONDS,
    )


def collect_ruff() -> Counter[str]:
    """跑完整 ruff 規則，回傳 {規則代號: 數量}。只讀 JSON 結果，不理會警告。"""
    result = _run_tool_module(
        [
            "ruff",
            "check",
            "--no-cache",
            "--config",
            "ruff.toml",
            "--output-format",
            "json",
            "--exit-zero",
            *PRODUCT_SOURCE_ROOTS,
        ]
    )
    if result.returncode != 0:
        message = f"ruff did not finish (exit {result.returncode}): {result.stderr[-1500:]}"
        raise RuntimeError(message)
    return Counter(item["code"] or SYNTAX_ERROR_KEY for item in json.loads(result.stdout))


def collect_pyrefly() -> Counter[str]:
    """依 pyproject.toml 的 [tool.pyrefly] 做型別檢查，回傳 {錯誤種類: 數量}。

    直譯器用目前這一個，平台由設定固定成 linux，Windows 本機和 CI 才會算出同一組錯誤。
    只有結束碼正常、stderr 只有 INFO、輸出是完整 JSON，才算跑完。
    """
    result = _run_tool_module(
        [
            "pyrefly",
            "check",
            "--config",
            "pyproject.toml",
            "--python-interpreter-path",
            sys.executable,
            "--output-format",
            "json",
        ]
    )
    failure = f"pyrefly did not finish (exit {result.returncode}): {result.stderr[-1500:]}"
    finished = result.returncode in PYREFLY_FINISHED_EXIT_CODES
    if not finished or not _only_info_logs(result.stderr):
        raise RuntimeError(failure)
    try:
        errors = json.loads(result.stdout)["errors"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(failure) from exc
    return Counter(item["name"] for item in errors)


def load_baseline(path: Path) -> dict[str, int]:
    """讀基準；不存在就是空的，任何違規都會被當成變多。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(path: Path, current: Counter[str]) -> None:
    """依規則代號排序寫出，方便在差異裡審查。"""
    payload = dict(sorted(current.items()))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compare(current: Counter[str], baseline: dict[str, int]) -> list[str]:
    """回傳每條數量和基準不同的規則；空清單代表完全相等。"""
    problems = []
    for rule in sorted(set(current) | set(baseline)):
        now, allowed = current.get(rule, 0), baseline.get(rule, 0)
        if now > allowed:
            problems.append(f"[變多] {rule}：現在 {now} 個，基準 {allowed} 個")
        elif now < allowed:
            problems.append(f"[變少] {rule}：現在 {now} 個，基準 {allowed} 個，請重拍基準")
    return problems


TOOLS: dict[str, tuple[Callable[[], Counter[str]], Path]] = {
    "ruff": (collect_ruff, RUFF_BASELINE),
    "pyrefly": (collect_pyrefly, PYREFLY_BASELINE),
}


def main(argv: list[str]) -> int:
    """命令列入口：第一個參數是工具名稱，加 --update 就重拍基準。"""
    flags = argv[1:]
    if not argv or argv[0] not in TOOLS or any(flag != "--update" for flag in flags):
        print(f"usage: python scripts/lint_ratchet.py {{{'|'.join(TOOLS)}}} [--update]")
        return 2
    collect, baseline_path = TOOLS[argv[0]]
    current = collect()
    if flags:
        write_baseline(baseline_path, current)
        total, rules = sum(current.values()), len(current)
        print(f"{argv[0]}: baseline updated, {total} violations / {rules} rules")
        return 0
    problems = compare(current, load_baseline(baseline_path))
    for problem in problems:
        print(problem)
    print(f"{argv[0]}: {'FAIL' if problems else 'PASS'} ({sum(current.values())} violations)")
    return 1 if problems else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
