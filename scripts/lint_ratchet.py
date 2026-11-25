# ruff: noqa: T201
"""靜態檢查只減不增（ADR-0278）：收集違規、比對基準、重拍基準都在這裡。

ruff、pyrefly 每條規則的數量必須和基準完全相等。變多要修；變少要在同一個 PR 重拍基準，
讓改善留在差異裡、之後不會被悄悄用掉。vulture 例外，只有多出來才紅（ADR-0298）。
complexity 記每個超過 15 的函式（ADR-0331），兩個方向都紅：變多要拆，變少要重拍。
測試（`tests/lint/test_ruff_ratchet.py`、`tests/lint/test_pyrefly_ratchet.py`、
`tests/lint/test_vulture_ratchet.py`、`tests/lint/test_complexity_ratchet.py`）和重拍都用
這裡唯一一份收集指令，兩邊不會各算各的。只用標準函式庫。

用法：
    python scripts/lint_ratchet.py ruff               # 檢查
    python scripts/lint_ratchet.py ruff --update      # 重拍基準
    python scripts/lint_ratchet.py pyrefly [--update] # 型別檢查，範圍在 pyproject.toml
    python scripts/lint_ratchet.py vulture [--update] # 未用的函式、方法、類別
    python scripts/lint_ratchet.py complexity [--update]  # 函式複雜度上限 15
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parent.parent
# 和 CI 的 `ruff check --select F` 步驟相同的五個產品資料夾。
PRODUCT_SOURCE_ROOTS = (
    "ToukaProxy/src",
    "ToukaMemory/src",
    "ToukaHeartbeat",
    "ToukaDispatch",
    "ToukaLocal",
)
# 完整 ruff 和 CI F 步驟共用的挑檔參數；挑檔照 ruff.toml，檔案藏不藏得起來由
# tests/lint/test_ruff_sees_every_file.py 比對 git 的清單把關（ADR-0278）。
RUFF_CHECK_ARGS = ("ruff", "check", "--no-cache", "--config", "ruff.toml")
# 這些副檔名都會被當成 Python 執行；比對前先轉小寫，`.PY` 在 Windows 也 import 得到。
PYTHON_SUFFIXES = (".py", ".pyi", ".pyw")
# git 的連結（120000）和子模組（160000）：ruff 不跟進去，裡面的檔就藏起來了。
HIDING_GIT_MODES = frozenset({"120000", "160000"})
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
# 函式複雜度（ruff C901）上限；超過的才記進基準（ADR-0331）。
MAX_COMPLEXITY = 15
COMPLEXITY_BASELINE = PROJECT_ROOT / "tests" / "lint" / "complexity_baseline.json"
# --isolated 不讀 ruff.toml（per-file-ignores 關不掉），--ignore-noqa 讓 noqa 壓不掉；
# 不看 .gitignore、清掉預設排除（dist、venv…）、加掃 .pyw，檔案藏不起來。
COMPLEXITY_ARGS = (
    "ruff", "check", "--isolated", "--ignore-noqa", "--no-cache", "--no-respect-gitignore",
    "--select", "C901", "--config", f"lint.mccabe.max-complexity={MAX_COMPLEXITY}",
    "--config", "exclude=[]", "--config", 'extend-include=["*.pyw"]',
    "--output-format", "json",
)  # fmt: skip
COMPLEXITY_CODE = "C901"
COMPLEXITY_MESSAGE = re.compile(
    r"^`(?P<name>[^`]+)` is too complex \((?P<score>\d+) > (?P<limit>\d+)\)$"
)
# 結束碼 0 是沒有超標、1 是有超標；2 是設定或參數壞掉。
COMPLEXITY_FINISHED_EXIT_CODES = (0, 1)


def _only_info_logs(stderr: str) -> bool:
    """每一行非空白的錯誤輸出都是 INFO 才回 True。"""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return all(line.startswith(PYREFLY_LOG_PREFIX) for line in lines)


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """在 repo 根目錄（或指定資料夾）跑一個固定的指令，不經 shell；不看結束碼。"""
    return subprocess.run(  # noqa: S603 - fixed argv (tool module or git), no shell
        argv,
        cwd=cwd or PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=TOOL_TIMEOUT_SECONDS,
    )


def _run_tool_module(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """用目前的直譯器跑檢查工具模組。"""
    return _run([sys.executable, "-m", *args], cwd)


def _git(args: list[str], cwd: Path) -> str:
    """跑 git，回傳 stdout；只看結束碼，git 把提示和換行警告也寫在 stderr。"""
    git = shutil.which("git")
    if git is None:
        message = "git not found on PATH"
        raise RuntimeError(message)
    result = _run([git, *args], cwd)
    if result.returncode != 0:
        message = f"git {args[0]} failed (exit {result.returncode}): {result.stderr[-1500:]}"
        raise RuntimeError(message)
    return result.stdout


def unchecked_python_files(
    roots: tuple[str, ...] | list[str], root: Path | None = None
) -> list[str]:
    """找出 git 會收、ruff 挑檔時卻沒看到的 Python 檔；回傳 git 給的 `/` 相對路徑。

    「git 會收」是已追蹤的檔加上還沒 add、也沒被忽略的檔；只比對檔名清單，不讀檔案內容。
    任何一步異常都丟錯，不當成「全部看得到」。
    """
    root = root or PROJECT_ROOT
    missing = [name for name in roots if not (root / name).is_dir()]
    if missing:
        message = f"ruff coverage: source roots missing: {missing}"
        raise RuntimeError(message)
    staged = _git(["ls-files", "-z", "-s", "--", *roots], root).split("\0")
    hiding = [entry for entry in staged if entry.split(" ", 1)[0] in HIDING_GIT_MODES]
    if hiding:
        message = f"ruff coverage: links or submodules in scope, ruff skips them: {hiding}"
        raise RuntimeError(message)
    listed = _git(["ls-files", "-z", "-c", "-o", "--exclude-standard", "--", *roots], root)
    expected = {
        (root / name).resolve(): name
        for name in set(listed.split("\0"))
        if Path(name).suffix.lower() in PYTHON_SUFFIXES and (root / name).is_file()
    }
    if not expected:
        message = f"ruff coverage: git listed no Python files under {list(roots)}"
        raise RuntimeError(message)
    result = _run_tool_module([*RUFF_CHECK_ARGS, "--show-files", *roots], cwd=root)
    if result.returncode != 0:
        message = (
            f"ruff --show-files did not finish (exit {result.returncode}): {result.stderr[-1500:]}"
        )
        raise RuntimeError(message)
    seen = {Path(line.strip()).resolve() for line in result.stdout.splitlines() if line.strip()}
    return sorted(name for path, name in expected.items() if path not in seen)


def collect_ruff() -> Counter[str]:
    """跑完整 ruff 規則，回傳 {規則代號: 數量}。只讀 JSON 結果，不理會警告。"""
    result = _run_tool_module(
        [*RUFF_CHECK_ARGS, "--output-format", "json", "--exit-zero", *PRODUCT_SOURCE_ROOTS]
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


def collect_complexity(
    root: Path | None = None, source_roots: tuple[str, ...] = PRODUCT_SOURCE_ROOTS
) -> Counter[str]:
    """跑 ruff C901，回傳 {相對路徑::完整名稱: 複雜度}，只有超過上限的函式。

    先確認每個資料夾都在：ruff 碰到不存在的資料夾只在 stderr 印一行、結束碼照舊。
    """
    root = root or PROJECT_ROOT
    missing = [name for name in source_roots if not (root / name).is_dir()]
    if missing:
        message = f"complexity: source roots missing: {missing}"
        raise RuntimeError(message)
    return parse_complexity(_run_tool_module([*COMPLEXITY_ARGS, *source_roots], cwd=root), root)


def parse_complexity(result: subprocess.CompletedProcess[str], root: Path) -> Counter[str]:
    """讀 ruff 的 JSON 結果；任何一處看不懂就丟錯，不當成 0 筆。"""
    failure = ""
    if result.stderr.strip():
        failure = f"ruff C901 wrote to stderr: {result.stderr[-1500:]}"
    elif result.returncode not in COMPLEXITY_FINISHED_EXIT_CODES:
        failure = f"ruff C901 did not finish (exit {result.returncode})"
    if failure:
        raise RuntimeError(failure)
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        failure = f"ruff C901 output is not JSON: {result.stdout[:300]}"
        raise RuntimeError(failure) from exc
    if bool(items) != (result.returncode == 1):
        failure = f"ruff C901 exit {result.returncode} with {len(items)} results"
        raise RuntimeError(failure)
    found: Counter[str] = Counter()
    names: dict[Path, list[tuple[str, int, int]]] = {}
    for item in sorted(items, key=lambda entry: (entry["filename"], entry["location"]["row"])):
        message = COMPLEXITY_MESSAGE.match(str(item.get("message")))
        if item.get("code") != COMPLEXITY_CODE or message is None:
            failure = f"unexpected ruff C901 result: {item}"
        elif int(message["limit"]) != MAX_COMPLEXITY:
            failure = f"ruff C901 used another limit: {item['message']}"
        if failure:
            raise RuntimeError(failure)
        path = Path(item["filename"])
        if path not in names:
            names[path] = _definitions(path)
        qualname, start = _qualname(names[path], message["name"], item["location"]["row"], path)
        # 同完整名稱的定義（property、if／else 兩邊）照檔內順序編號，互換才會紅。
        serial = sorted(begin for other, begin, _ in names[path] if other == qualname).index(start)
        key = f"{path.resolve().relative_to(root.resolve()).as_posix()}::{qualname}"
        found[f"{key}#{serial + 1}" if serial else key] = int(message["score"])
    return found


def _definitions(path: Path) -> list[tuple[str, int, int]]:
    """檔案裡每個函式的 (完整名稱, def 起始行, 本體第一行)，完整名稱和 __qualname__ 一致。"""
    found: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append((prefix + child.name, child.lineno, child.body[0].lineno))
                visit(child, f"{prefix}{child.name}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(ast.parse(path.read_bytes()), "")
    return found


def _qualname(
    definitions: list[tuple[str, int, int]], name: str, row: int, path: Path
) -> tuple[str, int]:
    """把 ruff 回報的短名稱與行號換成 (完整名稱, def 起始行)；要恰好對到一個 def，否則丟錯。"""
    matches = [
        (qualname, start)
        for qualname, start, body in definitions
        if qualname.rsplit(".", 1)[-1] == name and start <= row <= body
    ]
    if len(matches) != 1:
        message = f"no single definition of {name} at {path}:{row}: {matches}"
        raise RuntimeError(message)
    return matches[0]


def compare_complexity(current: Counter[str], baseline: dict[str, int]) -> list[str]:
    """兩個方向都報：變多要拆，變少或不見了要重拍；空清單代表和基準完全相同。"""
    problems = []
    for key in sorted(set(current) | set(baseline)):
        now, allowed = current.get(key), baseline.get(key)
        if allowed is None:
            problems.append(f"[新超標，要拆] {key}：{now}（上限 {MAX_COMPLEXITY}）")
        elif now is None:
            problems.append(f"[不見了：改名、搬檔或拆掉，請跑 --update] {key}：基準 {allowed}")
        elif now > allowed:
            problems.append(f"[變複雜，要拆] {key}：現在 {now}，基準 {allowed}")
        elif now < allowed:
            problems.append(f"[變簡單，請跑 --update] {key}：現在 {now}，基準 {allowed}")
    return problems


def _complexity_main(current: Counter[str], baseline_path: Path, *, update: bool) -> int:
    """複雜度的檢查與重拍；結果全空而基準不空時兩條路都丟錯。"""
    baseline = load_baseline(baseline_path)
    if baseline and not current:
        message = (
            "complexity: no function over the limit but the baseline is not empty; "
            "if every one was really split, set the baseline to {} by hand and say so in the PR"
        )
        raise RuntimeError(message)
    if update:
        for problem in compare_complexity(current, baseline):
            if "要拆" in problem:  # --update 照單全收，至少讓跑的人看到收了什麼
                print(f"收進基準（只收改名或搬檔、數字沒變大的，PR 說明要列出）：{problem}")
        write_baseline(baseline_path, current)
        print(f"complexity: baseline updated, {len(current)} functions over {MAX_COMPLEXITY}")
        return 0
    problems = compare_complexity(current, baseline)
    for problem in problems:
        print(problem)
    if problems:
        print("要拆的先拆到模組或方法層（不要拆成巢狀函式）；只有改名或搬檔、數字沒變大才重拍。")
    verdict = "FAIL" if problems else "PASS"
    print(f"complexity: {verdict} ({len(current)} functions over {MAX_COMPLEXITY})")
    return 1 if problems else 0


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
    "complexity": (collect_complexity, COMPLEXITY_BASELINE),
}


def main(argv: list[str]) -> int:
    """命令列入口：第一個參數是工具名稱，加 --update 就重拍基準。"""
    flags = argv[1:]
    if not argv or argv[0] not in TOOLS or any(flag != "--update" for flag in flags):
        print(f"usage: python scripts/lint_ratchet.py {{{'|'.join(TOOLS)}}} [--update]")
        return 2
    collect, baseline_path = TOOLS[argv[0]]
    current = collect()
    if argv[0] == "complexity":
        return _complexity_main(current, baseline_path, update=bool(flags))
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
