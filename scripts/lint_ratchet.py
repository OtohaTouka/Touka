# ruff: noqa: T201
"""靜態檢查只減不增（ADR-0278）：收集違規、比對基準、重拍基準都在這裡。

ruff、pyrefly 每條規則的數量必須和基準完全相等。變多要修；變少要在同一個 PR 重拍基準，
讓改善留在差異裡、之後不會被悄悄用掉。vulture 例外，只有多出來才紅（ADR-0297）。
測試（`tests/lint/test_ruff_ratchet.py`、`tests/lint/test_pyrefly_ratchet.py`、
`tests/lint/test_vulture_ratchet.py`）和重拍都用這裡唯一一份收集指令，兩邊不會各算各的。
只用標準函式庫。

用法：
    python scripts/lint_ratchet.py ruff               # 檢查
    python scripts/lint_ratchet.py ruff --update      # 重拍基準
    python scripts/lint_ratchet.py pyrefly [--update] # 型別檢查，範圍在 pyproject.toml
    python scripts/lint_ratchet.py vulture [--update] # 未用的函式、方法、類別
"""

import json
import re
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
VULTURE_BASELINE = PROJECT_ROOT / "tests" / "lint" / "vulture_baseline.json"
VULTURE_WHITELIST = "scripts/vulture_whitelist.py"
# 未用的函式、方法、類別信心值都是 60，門檻要低於等於它才報得出來。
VULTURE_MIN_CONFIDENCE = "60"
# 只排除靠註冊才用到的（路由、MCP 工具、Pydantic、discord 事件）；內建裝飾器不排除。
VULTURE_IGNORE_DECORATORS = (
    "@router.*,@*_router.*,@app.*,@mcp.*,@field_validator,@model_validator,"
    "@computed_field,@self.client.event"
)
VULTURE_KINDS = frozenset({"function", "method", "class", "property"})
# 結束碼 0 是沒有死碼、3 是有死碼；1 是輸入壞掉、2 是參數錯誤。
VULTURE_FINISHED_EXIT_CODES = (0, 3)
VULTURE_UNUSED_LINE = re.compile(r"^(?P<path>.+?):\d+: unused (?P<kind>\w+) '(?P<name>[^']+)' \(")
VULTURE_OTHER_LINE = re.compile(r"^.+?:\d+: (?:unreachable|unsatisfiable|redundant) ")
# 別處新增一個同名的用法就會讓項目消失，和那個 PR 無關，所以這些工具少了不算問題。
ONE_WAY_TOOLS = frozenset({"vulture"})
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


def collect_vulture() -> Counter[str]:
    """跑 vulture，回傳 {相對路徑:種類:名稱: 數量}，路徑一律用 `/`。

    只留函式、方法、類別、屬性。結果裡有看不懂的行就算失敗：升級後格式變了，
    全部比對不到會變成 0 筆，而單向比對不會因此變紅。
    """
    result = _run_tool_module(
        [
            "vulture",
            *PRODUCT_SOURCE_ROOTS,
            VULTURE_WHITELIST,
            "--min-confidence",
            VULTURE_MIN_CONFIDENCE,
            "--ignore-decorators",
            VULTURE_IGNORE_DECORATORS,
        ]
    )
    if result.returncode not in VULTURE_FINISHED_EXIT_CODES:
        message = f"vulture did not finish (exit {result.returncode}): {result.stderr[-1500:]}"
        raise RuntimeError(message)
    found: Counter[str] = Counter()
    for line in result.stdout.splitlines():
        unused = VULTURE_UNUSED_LINE.match(line)
        if unused is None:
            if line.strip() and not VULTURE_OTHER_LINE.match(line):
                raise RuntimeError(f"unexpected vulture output: {line}")
            continue
        if unused["kind"] in VULTURE_KINDS:
            found[f"{unused['path'].replace(chr(92), '/')}:{unused['kind']}:{unused['name']}"] += 1
    return found


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


def compare_additions(current: Counter[str], baseline: dict[str, int]) -> list[str]:
    """只回傳比基準多的項目；少了不算問題。"""
    return [
        f"[變多] {key}：現在 {now} 個，基準 {baseline.get(key, 0)} 個"
        for key, now in sorted(current.items())
        if now > baseline.get(key, 0)
    ]


TOOLS: dict[str, tuple[Callable[[], Counter[str]], Path]] = {
    "ruff": (collect_ruff, RUFF_BASELINE),
    "pyrefly": (collect_pyrefly, PYREFLY_BASELINE),
    "vulture": (collect_vulture, VULTURE_BASELINE),
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
    compare_counts = compare_additions if argv[0] in ONE_WAY_TOOLS else compare
    problems = compare_counts(current, load_baseline(baseline_path))
    for problem in problems:
        print(problem)
    if problems and argv[0] in ONE_WAY_TOOLS:
        print("接上或刪掉；確定要留就加 --update，並在 PR 說明逐項列出。")
    print(f"{argv[0]}: {'FAIL' if problems else 'PASS'} ({sum(current.values())} violations)")
    return 1 if problems else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
