# ruff: noqa: T201
"""靜態檢查只減不增（ADR-0278）：收集違規、比對基準、重拍基準都在這裡。

每條規則的數量必須和基準完全相等。變多要修；變少要在同一個 PR 重拍基準，
讓改善留在差異裡、之後不會被悄悄用掉。測試（`tests/lint/test_ruff_ratchet.py`）和
重拍都用這裡唯一一份收集指令，兩邊不會各算各的。只用標準函式庫。

用法：
    python scripts/lint_ratchet.py ruff            # 檢查
    python scripts/lint_ratchet.py ruff --update   # 重拍基準
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
RUFF_TIMEOUT_SECONDS = 300
SYNTAX_ERROR_KEY = "syntax-error"


def collect_ruff() -> Counter[str]:
    """跑完整 ruff 規則，回傳 {規則代號: 數量}。只讀 JSON 結果，不理會警告。"""
    result = subprocess.run(  # noqa: S603 - fixed ruff module argv, no shell
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            "ruff.toml",
            "--output-format",
            "json",
            "--exit-zero",
            *PRODUCT_SOURCE_ROOTS,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=RUFF_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        message = f"ruff did not finish (exit {result.returncode}): {result.stderr[-1500:]}"
        raise RuntimeError(message)
    return Counter(item["code"] or SYNTAX_ERROR_KEY for item in json.loads(result.stdout))


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
}


def main(argv: list[str]) -> int:
    """命令列入口：第一個參數是工具名稱，加 --update 就重拍基準。"""
    if not argv or argv[0] not in TOOLS:
        print(f"usage: python scripts/lint_ratchet.py {{{'|'.join(TOOLS)}}} [--update]")
        return 2
    collect, baseline_path = TOOLS[argv[0]]
    current = collect()
    if "--update" in argv[1:]:
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
