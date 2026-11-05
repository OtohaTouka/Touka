# ruff: noqa: T201, S603, S607, TRY003
"""每日依賴檢查的結果彙整與 issue 回報（ADR-0299、禁令 69）。

兩個子命令，都只用標準函式庫（回報 job 不裝依賴，以 ``python3 -I`` 執行）：

- ``collect-exceptions``：掃描 job 用，讀分支上的 ``pyproject.toml``，算出 Python 例外的到期日。
- ``report``：回報 job 用，讀掃描 job 存下的結果，每條分支一張 issue。

結果檔是裝了 dev 依賴的 job 產生的，一律當成不可信的資料：只篩出格式固定的欄位，
其他文字都不進 issue。檔案規格：``dep-audit-<分支>/`` 底下的 ``python.txt``、``python.exit``、
``node.txt``、``node.exit``、``exceptions.json``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    GhRunner = Callable[..., str]

CHECKS = ("python", "node")
EXCEPTIONS = "exceptions"
MAX_RESULT_BYTES = 1_000_000
MAX_ROWS = 50
MAX_ADVISORIES_PER_ROW = 5
GH_TIMEOUT_SECONDS = 60
SIMULATED_BRANCH = "模擬（請忽略）"

_ADVISORY = r"(?:GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}|PYSEC-\d{4}-\d{1,7}|CVE-\d{4}-\d{4,7})"
_PYPI_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._~-]{0,99}/)?[a-z0-9][a-z0-9._~-]{0,99}"
_PYTHON_BLOCKING_HEADER = re.compile(r"\[FAIL\] \d+ blocking or unrated finding\(s\):")
_PYTHON_FINDING = re.compile(
    r"  \[(?P<scope>runtime|build-test)\] \[(?P<severity>[A-Z]{1,10})\] "
    rf"\[(?P<advisory>{_ADVISORY})\] (?P<package>{_PYPI_NAME})=="
    r"(?P<version>[0-9A-Za-z][0-9A-Za-z.!+_-]{0,49}) fix: [0-9A-Za-z., !+_-]{0,200}"
)
_NODE_SCOPE = re.compile(
    r"\[Scan\] (?P<scope>(?:ToukaChat|ToukaIDE) (?:runtime|full)) dependency scope"
)
_NODE_FINDING = re.compile(
    rf"  (?P<severity>high|critical) (?P<package>{_NPM_NAME})"
    rf"(?P<advisories>(?: {_ADVISORY})*)(?: via {_NPM_NAME})*"
)
_SAFE_PACKAGE = re.compile(_PYPI_NAME)
_EXIT_CODE = re.compile(r"\d{1,3}")
_COOLDOWN = re.compile(r"(\d{1,3}) days?")
_MARKER = re.compile(r"<!-- dep-audit state=(attention|incomplete) digest=([0-9a-f]{64}) -->")


class GhError(RuntimeError):
    """gh 指令失敗。"""


@dataclass(frozen=True)
class Finding:
    """一則要通知的弱點，欄位都通過格式檢查。"""

    package: str
    version: str  # Node 的檢查結果沒有版本
    advisories: tuple[str, ...]
    severity: str
    scope: str


@dataclass(frozen=True)
class BranchResult:
    """一條分支這次的檢查結果。"""

    branch: str
    state: str  # clean、attention、incomplete
    findings: tuple[Finding, ...] = ()
    expired: tuple[str, ...] = ()
    incomplete_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExistingIssue:
    """開著的 issue 與它記下的狀態。"""

    number: int
    state: str
    digest: str


@dataclass(frozen=True)
class Action:
    """對 issue 要做的事。"""

    kind: str  # none、create、update、close
    number: int | None = None


def _read_limited(path: Path) -> str | None:
    """超過上限或讀不到都當成沒有結果，不解析半份輸出。"""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RESULT_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_RESULT_BYTES:
        return None
    return raw.decode("utf-8", errors="replace")


def _read_exit_code(path: Path) -> int | None:
    text = _read_limited(path)
    if text is None or not _EXIT_CODE.fullmatch(text.strip()):
        return None
    return int(text.strip())


def _parse_python(output: str) -> list[Finding]:
    findings: list[Finding] = []
    blocking = False
    for line in output.splitlines():
        if _PYTHON_BLOCKING_HEADER.fullmatch(line):
            blocking = True
        elif line.startswith("[") or not line.strip():
            blocking = False
        elif blocking and (match := _PYTHON_FINDING.fullmatch(line)):
            findings.append(
                Finding(
                    package=match["package"],
                    version=match["version"],
                    advisories=(match["advisory"],),
                    severity=match["severity"],
                    scope=f"Python {match['scope']}",
                )
            )
    return findings


def _parse_node(output: str) -> list[Finding]:
    findings: list[Finding] = []
    scope: str | None = None
    for line in output.splitlines():
        if scope_match := _NODE_SCOPE.fullmatch(line):
            scope = scope_match["scope"]
        elif scope is not None and (match := _NODE_FINDING.fullmatch(line)):
            findings.append(
                Finding(
                    package=match["package"],
                    version="",
                    advisories=tuple(match["advisories"].split()),
                    severity=match["severity"].upper(),
                    scope=scope,
                )
            )
    return findings


_PARSERS = {"python": _parse_python, "node": _parse_node}


def _merge_scopes(findings: list[Finding]) -> tuple[Finding, ...]:
    """同一則問題出現在好幾個範圍時併成一列，順序固定，內容沒變摘要就不變。"""
    scopes: dict[tuple[str, str, tuple[str, ...], str], set[str]] = {}
    for item in findings:
        key = (item.package, item.version, item.advisories, item.severity)
        scopes.setdefault(key, set()).add(item.scope)
    return tuple(
        Finding(package, version, advisories, severity, "、".join(sorted(names)))
        for (package, version, advisories, severity), names in sorted(scopes.items())
    )


def _read_expired(path: Path, now: datetime) -> tuple[str, ...] | None:
    """回傳過期的例外；內容不合格式回 None，不把裡面的文字帶出去。"""
    text = _read_limited(path)
    if text is None:
        return None
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list):
        return None
    expired: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        package, expires = row.get("package"), row.get("expires")
        if not isinstance(package, str) or not _SAFE_PACKAGE.fullmatch(package):
            return None
        if expires is None:
            expired.append(package)
            continue
        if not isinstance(expires, str):
            return None
        try:
            deadline = datetime.fromisoformat(expires)
        except ValueError:
            return None
        if deadline.tzinfo is None:
            return None
        if now >= deadline:
            expired.append(package)
    return tuple(sorted(expired))


def classify(branch: str, results: Path, *, now: datetime) -> BranchResult:
    """兩道檢查都回 0 才算乾淨；失敗又列不出任何一筆就算沒跑完。"""
    folder = results / f"dep-audit-{branch}"
    findings: list[Finding] = []
    incomplete: list[str] = []
    for check in CHECKS:
        exit_code = _read_exit_code(folder / f"{check}.exit")
        if exit_code == 0:
            continue
        output = _read_limited(folder / f"{check}.txt")
        parsed = _PARSERS[check](output) if exit_code is not None and output is not None else []
        if not parsed:
            incomplete.append(check)
        findings.extend(parsed)
    expired = _read_expired(folder / "exceptions.json", now)
    if expired is None:
        incomplete.append(EXCEPTIONS)
        expired = ()
    state = "attention" if findings or expired else "incomplete" if incomplete else "clean"
    return BranchResult(
        branch=branch,
        state=state,
        findings=_merge_scopes(findings),
        expired=expired,
        incomplete_checks=tuple(incomplete),
    )


def digest(result: BranchResult) -> str:
    """內容摘要；不含執行連結，內容沒變就不重複通知。"""
    canonical = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _code(text: str) -> str:
    """欄位都放反引號裡：`@scope/name` 才不會變成提及。"""
    return f"`{text}`"


def _finding_row(item: Finding) -> str:
    shown = item.advisories[:MAX_ADVISORIES_PER_ROW]
    advisories = "、".join(_code(advisory) for advisory in shown) or "—"
    if len(item.advisories) > len(shown):
        advisories += f" 等 {len(item.advisories)} 則"
    version = _code(item.version) if item.version else "—"
    return (
        f"| {_code(item.package)} | {version} | {advisories} | "
        f"{_code(item.severity)} | {_code(item.scope)} |"
    )


def render(result: BranchResult, run_url: str | None = None) -> str:
    """組出 issue 內文。第一行記狀態與摘要，下次才知道內容有沒有變。"""
    lines = [f"<!-- dep-audit state={result.state} digest={digest(result)} -->", ""]
    if result.findings:
        lines += [
            f"`{result.branch}` 有依賴弱點。沒收到這張 issue 不等於安全，每日檢查的限制見 "
            "`docs/rules/deploy.md` 禁令 69。",
            "",
            "| 套件 | 版本 | 公告 | 嚴重度 | 範圍 |",
            "| --- | --- | --- | --- | --- |",
            *(_finding_row(item) for item in result.findings[:MAX_ROWS]),
        ]
        hidden = len(result.findings) - MAX_ROWS
        if hidden > 0:
            lines += ["", f"還有 {hidden} 筆，見這次的執行紀錄。"]
    if result.expired:
        lines += [
            "",
            "這些 Python 冷卻期例外已經到期，要從 `pyproject.toml` 拿掉："
            + "、".join(_code(package) for package in result.expired),
        ]
    if result.incomplete_checks:
        lines += [
            "",
            f"`{result.branch}` 這幾項沒跑完，沒有結果不代表沒有問題："
            + "、".join(_code(check) for check in result.incomplete_checks),
        ]
    if run_url:
        lines += ["", f"這次的執行：{run_url}"]
    return "\n".join(lines) + "\n"


def issue_title(branch: str) -> str:
    """每條分支一張 issue：main 乾淨不會關掉 dev 那張。"""
    return f"每日依賴檢查：{branch}"


def parse_existing(number: int, body: str) -> ExistingIssue:
    """讀回內文記下的狀態；被人改過就當成內容有變。"""
    match = _MARKER.search(body)
    if match is None:
        return ExistingIssue(number=number, state="", digest="")
    return ExistingIssue(number=number, state=match[1], digest=match[2])


def decide(result: BranchResult, existing: ExistingIssue | None) -> Action:
    """有問題開或更新，內容沒變不重複通知，乾淨才關。"""
    if result.state == "clean":
        return Action("close", existing.number) if existing else Action("none")
    if existing is None:
        return Action("create")
    # 這次沒跑完不代表問題解決：開著的 issue 留著，內容也不蓋掉
    if result.state == "incomplete" or existing.digest == digest(result):
        return Action("none")
    return Action("update", existing.number)


def _simulated_result() -> BranchResult:
    return BranchResult(
        branch=SIMULATED_BRANCH,
        state="attention",
        findings=(
            Finding("example-package", "0.0.0", ("這是模擬，沒有真的弱點",), "HIGH", "模擬"),
        ),
    )


def _open_issues(repo: str, gh: GhRunner) -> dict[str, ExistingIssue]:
    """只看這個 workflow 開的 issue：別人開同名的 issue 不算。"""
    output = gh(
        [
            "issue", "list", "--repo", repo, "--state", "open", "--author", "app/github-actions",
            "--limit", "100", "--json", "number,title,body",
        ]
    )  # fmt: skip
    return {
        item["title"]: parse_existing(item["number"], item["body"]) for item in json.loads(output)
    }


def _apply(
    action: Action, result: BranchResult, *, repo: str, assignee: str, run_url: str, gh: GhRunner
) -> None:
    body = render(result, run_url)
    number = str(action.number)
    if action.kind == "create":
        gh(
            [
                "issue", "create", "--repo", repo, "--title", issue_title(result.branch),
                "--assignee", assignee, "--body-file", "-",
            ],
            stdin=body,
        )  # fmt: skip
    elif action.kind == "update":
        gh(["issue", "edit", number, "--repo", repo, "--body-file", "-"], stdin=body)
        note = f"內容有變，這次的執行：{run_url}"
        gh(["issue", "comment", number, "--repo", repo, "--body", note])
    elif action.kind == "close":
        gh(["issue", "close", number, "--repo", repo, "--comment", f"這條線掃乾淨了：{run_url}"])


def run_report(
    *,
    results: Path,
    branches: Sequence[str],
    repo: str,
    assignee: str,
    run_url: str,
    now: datetime,
    gh: GhRunner,
    simulate: bool = False,
) -> int:
    """每條分支各自處理；一條失敗不影響另一條，最後再讓這一步變紅。"""
    targets = (
        [_simulated_result()]
        if simulate
        else [classify(branch, results, now=now) for branch in branches]
    )
    try:
        existing = _open_issues(repo, gh)
    except (GhError, ValueError, KeyError, TypeError) as exc:
        print(f"[FAIL] 讀不到開著的 issue：{exc}")
        return 1
    failed = False
    for result in targets:
        action = decide(result, existing.get(issue_title(result.branch)))
        print(f"{result.branch}: {result.state} -> {action.kind}")
        try:
            _apply(action, result, repo=repo, assignee=assignee, run_url=run_url, gh=gh)
        except GhError as exc:
            print(f"[FAIL] {result.branch}：{exc}")
            failed = True
    return 1 if failed else 0


def _run_gh(args: list[str], *, stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh {' '.join(args[:2])} 沒有跑完") from exc
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} 結束碼 {result.returncode}：{result.stderr[:500]}")
    return result.stdout


def _expiry(cutoff: object, cooldown_days: int | None) -> str | None:
    """放行時間加冷卻期。沒有日期（例如 false）或看不懂就回 None，回報時一律列出來。"""
    if not isinstance(cutoff, str) or cooldown_days is None:
        return None
    try:
        released = datetime.fromisoformat(cutoff)
    except ValueError:
        return None
    if released.tzinfo is None:
        released = released.replace(tzinfo=UTC)
    return (released + timedelta(days=cooldown_days)).isoformat()


def collect_exceptions(pyproject_text: str) -> list[dict[str, str | None]]:
    """列出 Python 冷卻期例外與到期日，天數讀同一份設定，不另寫一處。"""
    uv = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {})
    cooldown = _COOLDOWN.fullmatch(str(uv.get("exclude-newer", "")))
    cooldown_days = int(cooldown[1]) if cooldown else None
    return [
        {"package": package, "expires": _expiry(cutoff, cooldown_days)}
        for package, cutoff in sorted(uv.get("exclude-newer-package", {}).items())
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """命令列入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect-exceptions")
    collect.add_argument("--pyproject", type=Path, required=True)
    collect.add_argument("--out", type=Path, required=True)
    report = commands.add_parser("report")
    report.add_argument("--results", type=Path, required=True)
    report.add_argument("--branches", nargs="+", required=True)
    report.add_argument("--repo", required=True)
    report.add_argument("--assignee", required=True)
    report.add_argument("--run-url", required=True)
    report.add_argument("--simulate", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.command == "collect-exceptions":
        rows = collect_exceptions(arguments.pyproject.read_text(encoding="utf-8"))
        arguments.out.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0
    return run_report(
        results=arguments.results,
        branches=arguments.branches,
        repo=arguments.repo,
        assignee=arguments.assignee,
        run_url=arguments.run_url,
        now=datetime.now(UTC),
        gh=_run_gh,
        simulate=arguments.simulate or os.environ.get("DEP_AUDIT_SIMULATE") == "true",
    )


if __name__ == "__main__":
    sys.exit(main())
