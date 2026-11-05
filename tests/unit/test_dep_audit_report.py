"""每日依賴檢查的回報邏輯（ADR-0299）。

結果檔是裝了 dev 依賴的 job 產生的，不可信；這裡用預先存好的輸出，不連網。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 6, tzinfo=UTC)

CLEAN_PYTHON = (
    "[Scan] Python runtime dependency scope\n\n"
    "[OK] No blocking Python dependency vulnerabilities found.\n"
)
CLEAN_NODE = (
    "[Scan] ToukaChat runtime dependency scope\n"
    "  blocking=0 lower-severity=0 registry=x\n"
    "[OK] No HIGH/CRITICAL Node dependency vulnerabilities found.\n"
)
PYTHON_FINDING = (
    "\n[WARN] 1 lower-severity finding(s):\n"
    "  [runtime] [MODERATE] [GHSA-aaaa-bbbb-cccc] lowsev==1.0 fix: 1.1\n"
    "\n[FAIL] 2 blocking or unrated finding(s):\n"
    "  [runtime] [HIGH] [GHSA-2g87-pp6c-29x5] aiohttp==3.14.3 fix: 3.14.5\n"
    "  [build-test] [UNRATED] [CVE-2026-114613] msgpack==1.2.1 fix: 1.2.3\n"
)
NODE_FINDING = (
    "[Scan] ToukaIDE full dependency scope\n"
    "  high @azure/msal-node GHSA-2464-fhr4-w9xw via jsonwebtoken\n"
    "  critical hono GHSA-xcq7-p3pf-p88h\n"
    "  blocking=2 lower-severity=0 registry=x\n"
    "[FAIL] 2 HIGH/CRITICAL finding(s) across audited scopes.\n"
)
DID_NOT_RUN = "[FAIL] Python dependency scan did not run (掃描沒跑成): boom\n"


def _load_report() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dep_audit_report", ROOT / "scripts" / "dep_audit_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load_report()


def _results(
    tmp_path: Path,
    *,
    python: tuple[int, str] | None = (0, CLEAN_PYTHON),
    node: tuple[int, str] | None = (0, CLEAN_NODE),
    exceptions: list[dict[str, object]] | None = None,
    branch: str = "main",
) -> Path:
    root = tmp_path / f"dep-audit-{branch}"
    root.mkdir()
    for name, value in (("python", python), ("node", node)):
        if value is not None:
            (root / f"{name}.exit").write_text(f"{value[0]}\n", encoding="utf-8")
            (root / f"{name}.txt").write_text(value[1], encoding="utf-8")
    (root / "exceptions.json").write_text(json.dumps(exceptions or []), encoding="utf-8")
    return tmp_path


def _classify(tmp_path: Path, **kwargs: object) -> object:
    return report.classify("main", _results(tmp_path, **kwargs), now=NOW)  # type: ignore[arg-type]


def _issue(state: str, digest: str, number: int = 7) -> object:
    return report.ExistingIssue(number=number, state=state, digest=digest)


def test_both_checks_passing_is_clean(tmp_path: Path) -> None:
    result = _classify(tmp_path)
    assert result.state == "clean"
    assert result.findings == ()


def test_only_blocking_python_lines_count_not_the_warnings(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING))
    assert result.state == "attention"
    packages = {finding.package for finding in result.findings}
    assert packages == {"aiohttp", "msgpack"}
    aiohttp = next(item for item in result.findings if item.package == "aiohttp")
    assert aiohttp.version == "3.14.3"
    assert aiohttp.advisories == ("GHSA-2g87-pp6c-29x5",)
    assert aiohttp.severity == "HIGH"


def test_node_findings_keep_the_project_and_scope(tmp_path: Path) -> None:
    result = _classify(tmp_path, node=(1, NODE_FINDING))
    assert result.state == "attention"
    azure = next(item for item in result.findings if item.package == "@azure/msal-node")
    assert azure.advisories == ("GHSA-2464-fhr4-w9xw",)
    assert azure.severity == "HIGH"
    assert "ToukaIDE full" in azure.scope


@pytest.mark.parametrize("output", [DID_NOT_RUN, "", "garbage only\n", "  high \n"])
def test_a_failed_check_with_nothing_to_list_did_not_finish(tmp_path: Path, output: str) -> None:
    result = _classify(tmp_path, python=(1, output))
    assert result.state == "incomplete"
    assert result.incomplete_checks == ("python",)


@pytest.mark.parametrize("missing", ["python", "node"])
def test_a_missing_result_did_not_finish(tmp_path: Path, missing: str) -> None:
    result = _classify(tmp_path, **{missing: None})
    assert result.state == "incomplete"
    assert result.incomplete_checks == (missing,)


def test_no_result_folder_did_not_finish(tmp_path: Path) -> None:
    result = report.classify("dev", tmp_path, now=NOW)
    assert result.state == "incomplete"
    assert result.incomplete_checks == ("python", "node", "exceptions")


@pytest.mark.parametrize("exit_text", ["", "nope", "0 1"])
def test_an_unreadable_exit_code_did_not_finish(tmp_path: Path, exit_text: str) -> None:
    results = _results(tmp_path)
    (results / "dep-audit-main" / "python.exit").write_text(exit_text, encoding="utf-8")
    assert report.classify("main", results, now=NOW).incomplete_checks == ("python",)


def test_a_finding_wins_over_the_other_check_not_finishing(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING), node=(1, "garbage\n"))
    assert result.state == "attention"
    assert result.incomplete_checks == ("node",)
    assert "`node`" in report.render(result)


def test_node_stopping_halfway_lists_what_it_found_and_says_it_did_not_finish(
    tmp_path: Path,
) -> None:
    output = (
        "[Scan] ToukaChat full dependency scope\n"
        "  high hono GHSA-2g87-pp6c-29x5\n"
        "  blocking=1 lower-severity=0 registry=x\n"
        "[FAIL] Node dependency audit evidence invalid: boom\n"
    )
    result = _classify(tmp_path, node=(1, output))
    assert result.state == "attention"
    assert [item.package for item in result.findings] == ["hono"]
    assert result.incomplete_checks == ("node",)


def test_python_coverage_gap_lists_what_it_found_and_says_it_did_not_finish(
    tmp_path: Path,
) -> None:
    output = PYTHON_FINDING + (
        "\n[INCOMPLETE] 1 dependency coverage gap(s):\n"
        "  [runtime] [SKIPPED] torch: not on PyPI\n"
        "[INCOMPLETE] Audit stopped; no later dependency scope was started.\n"
    )
    result = _classify(tmp_path, python=(1, output))
    assert result.state == "attention"
    assert len(result.findings) == 2
    assert result.incomplete_checks == ("python",)


@pytest.mark.parametrize(
    "check,output",
    [
        ("python", PYTHON_FINDING.replace("[FAIL] 2 blocking", "[FAIL] 3 blocking")),
        ("node", NODE_FINDING.replace("[FAIL] 2 HIGH", "[FAIL] 3 HIGH")),
    ],
)
def test_a_line_that_cannot_be_read_does_not_vanish_silently(
    tmp_path: Path, check: str, output: str
) -> None:
    result = _classify(tmp_path, **{check: (1, output)})
    assert result.state == "attention"
    assert result.incomplete_checks == (check,)


def test_complete_output_is_not_marked_as_not_finished(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING), node=(1, NODE_FINDING))
    assert result.incomplete_checks == ()


def test_expired_exception_needs_attention_even_when_the_scan_is_clean(tmp_path: Path) -> None:
    exceptions = [{"package": "pkg-old", "expires": "2026-11-05T00:00:00+00:00"}]
    result = _classify(tmp_path, exceptions=exceptions)
    assert result.state == "attention"
    assert result.expired == ("pkg-old",)
    assert "`pkg-old`" in report.render(result)


def test_exception_inside_the_cooldown_is_not_listed(tmp_path: Path) -> None:
    exceptions = [{"package": "pkg-new", "expires": "2026-11-07T00:00:00+00:00"}]
    assert _classify(tmp_path, exceptions=exceptions).state == "clean"


def test_exception_without_a_date_is_always_listed(tmp_path: Path) -> None:
    exceptions = [{"package": "pkg-free", "expires": None}]
    assert _classify(tmp_path, exceptions=exceptions).expired == ("pkg-free",)


def test_exception_with_an_unsafe_name_is_not_echoed(tmp_path: Path) -> None:
    exceptions = [{"package": "@evil ping", "expires": None}]
    result = _classify(tmp_path, exceptions=exceptions)
    assert result.state == "incomplete"
    assert "evil" not in report.render(result)


@pytest.mark.parametrize(
    "line",
    [
        "  [runtime] [HIGH] [GHSA-2g87-pp6c-29x5] @someone==1.0 fix: 1.1",
        "  [runtime] [HIGH] [@someone] pkg==1.0 fix: 1.1",
        "  [runtime] [HIGH] [GHSA-2g87-pp6c-29x5] pkg==1.0`[x](http://e.test) fix: 1.1",
        "  [runtime] [HIGH] [GHSA-2g87-pp6c-29x5] pkg==1.0 extra @someone",
    ],
)
def test_python_lines_that_do_not_match_exactly_are_dropped(tmp_path: Path, line: str) -> None:
    output = f"[FAIL] 1 blocking or unrated finding(s):\n{line}\nplease ping @someone\n"
    result = _classify(tmp_path, python=(1, output))
    assert result.findings == ()
    assert result.state == "incomplete"
    assert "someone" not in report.render(result)


@pytest.mark.parametrize(
    "line",
    [
        "  high hono GHSA-2g87-pp6c-29x5 @someone",
        "  high <unprintable> GHSA-2g87-pp6c-29x5",
        "  high hono`[x](http://e.test)",
        "  high hono via @someone`",
    ],
)
def test_node_lines_that_do_not_match_exactly_are_dropped(tmp_path: Path, line: str) -> None:
    output = f"[Scan] ToukaChat full dependency scope\n{line}\n"
    result = _classify(tmp_path, node=(1, output))
    assert result.findings == ()
    assert "someone" not in report.render(result)


def test_every_field_is_in_backticks_so_a_scoped_name_is_not_a_mention(tmp_path: Path) -> None:
    body = report.render(_classify(tmp_path, node=(1, NODE_FINDING)))
    assert "`@azure/msal-node`" in body
    assert "@azure/msal-node" not in body.replace("`@azure/msal-node`", "")


def test_a_long_list_is_cut_and_says_so(tmp_path: Path) -> None:
    lines = "".join(f"  high pkg-{index} GHSA-2g87-pp6c-29x5\n" for index in range(200))
    output = f"[Scan] ToukaChat full dependency scope\n{lines}"
    body = report.render(_classify(tmp_path, node=(1, output)))
    assert len(body) < 12_000
    assert f"還有 {200 - report.MAX_ROWS} 筆" in body


def test_oversized_output_is_thrown_away_and_counts_as_not_finished(tmp_path: Path) -> None:
    filler = "x" * (report.MAX_RESULT_BYTES + 10)
    result = _classify(tmp_path, python=(1, filler + "\n" + PYTHON_FINDING))
    assert result.state == "incomplete"


def test_same_content_has_the_same_digest_and_new_content_does_not(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = _classify(tmp_path / "a", python=(1, PYTHON_FINDING))
    again = _classify(tmp_path / "b", python=(1, PYTHON_FINDING))
    other = report.classify(
        "main", _results(tmp_path, python=(1, PYTHON_FINDING), node=(1, NODE_FINDING)), now=NOW
    )
    assert report.digest(first) == report.digest(again)
    assert report.digest(first) != report.digest(other)


def test_clean_with_no_issue_does_nothing(tmp_path: Path) -> None:
    assert report.decide(_classify(tmp_path), None).kind == "none"


def test_clean_closes_the_issue(tmp_path: Path) -> None:
    action = report.decide(_classify(tmp_path), _issue("attention", "old"))
    assert (action.kind, action.number) == ("close", 7)


def test_a_finding_opens_an_issue(tmp_path: Path) -> None:
    assert report.decide(_classify(tmp_path, python=(1, PYTHON_FINDING)), None).kind == "create"


def test_unchanged_finding_does_not_notify_again(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING))
    existing = _issue("attention", report.digest(result))
    assert report.decide(result, existing).kind == "none"


def test_changed_finding_updates_the_issue(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING))
    action = report.decide(result, _issue("attention", "old"))
    assert (action.kind, action.number) == ("update", 7)


def test_not_finishing_opens_an_issue_when_there_is_none(tmp_path: Path) -> None:
    assert report.decide(_classify(tmp_path, python=(1, DID_NOT_RUN)), None).kind == "create"


@pytest.mark.parametrize("state", ["attention", "incomplete"])
def test_not_finishing_leaves_the_open_issue_alone(tmp_path: Path, state: str) -> None:
    result = _classify(tmp_path, python=(1, DID_NOT_RUN))
    assert report.decide(result, _issue(state, "old")).kind == "none"


def test_finding_replaces_a_did_not_finish_issue(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING))
    assert report.decide(result, _issue("incomplete", "old")).kind == "update"


def test_each_branch_has_its_own_issue_title() -> None:
    assert report.issue_title("main") != report.issue_title("dev")


def test_the_marker_round_trips_and_ignores_other_bodies(tmp_path: Path) -> None:
    result = _classify(tmp_path, python=(1, PYTHON_FINDING))
    parsed = report.parse_existing(7, report.render(result))
    assert (parsed.number, parsed.state, parsed.digest) == (7, "attention", report.digest(result))
    assert report.parse_existing(7, "hand edited").digest == ""


def _open_issue(title: str, body: str, login: str = report.WORKFLOW_AUTHOR) -> dict[str, object]:
    return {"number": 9, "title": title, "body": body, "author": {"login": login}}


class _FakeGh:
    """記下送出的 gh 指令，開著的 issue 由測試給。"""

    def __init__(self, open_issues: list[dict[str, object]] | None = None) -> None:
        self.open_issues = open_issues or []
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, stdin: str | None = None) -> str:
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.open_issues)
        return ""

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if call[1] != "list"]


def _run(tmp_path: Path, gh: _FakeGh, *, simulate: bool = False) -> int:
    return report.run_report(
        results=tmp_path,
        branches=("main", "dev"),
        repo="o/r",
        assignee="owner",
        run_url="https://example.test/run/1",
        now=NOW,
        gh=gh,
        simulate=simulate,
    )


def test_main_clean_does_not_close_the_dev_issue(tmp_path: Path) -> None:
    _results(tmp_path, branch="main")
    _results(tmp_path, branch="dev", python=(1, PYTHON_FINDING))
    result = report.classify("dev", tmp_path, now=NOW)
    gh = _FakeGh([_open_issue(report.issue_title("dev"), report.render(result))])
    assert _run(tmp_path, gh) == 0
    assert gh.verbs() == []


def test_an_issue_someone_else_opened_with_the_same_title_does_not_count(tmp_path: Path) -> None:
    _results(tmp_path, branch="main")
    _results(tmp_path, branch="dev")
    gh = _FakeGh([_open_issue(report.issue_title("main"), "hand written", login="someone")])
    assert _run(tmp_path, gh) == 0
    assert gh.verbs() == []


def test_new_issue_is_assigned_to_the_owner(tmp_path: Path) -> None:
    _results(tmp_path, branch="main", python=(1, PYTHON_FINDING))
    _results(tmp_path, branch="dev")
    gh = _FakeGh()
    assert _run(tmp_path, gh) == 0
    created = next(call for call in gh.calls if call[1] == "create")
    assert created[created.index("--assignee") + 1] == "owner"
    assert created[created.index("--title") + 1] == report.issue_title("main")


def test_simulate_only_touches_its_own_issue(tmp_path: Path) -> None:
    gh = _FakeGh()
    assert _run(tmp_path, gh, simulate=True) == 0
    created = [call for call in gh.calls if call[1] == "create"]
    assert len(created) == 1
    title = created[0][created[0].index("--title") + 1]
    assert title == report.issue_title(report.SIMULATED_BRANCH)


def test_a_failed_gh_call_still_handles_the_other_branch_and_fails_the_step(
    tmp_path: Path,
) -> None:
    _results(tmp_path, branch="main", python=(1, PYTHON_FINDING))
    _results(tmp_path, branch="dev", python=(1, PYTHON_FINDING))
    gh = _FakeGh()
    real_call = gh.__call__

    def failing(args: list[str], *, stdin: str | None = None) -> str:
        if args[1] == "create" and report.issue_title("main") in args:
            raise report.GhError("boom")
        return real_call(args, stdin=stdin)

    assert (
        report.run_report(
            results=tmp_path,
            branches=("main", "dev"),
            repo="o/r",
            assignee="owner",
            run_url="https://example.test/run/1",
            now=NOW,
            gh=failing,
        )
        == 1
    )
    assert report.issue_title("dev") in [arg for call in gh.calls for arg in call]


def test_collect_exceptions_reads_the_cutoff_and_the_cooldown(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.uv]\n"
        'exclude-newer = "3 days"\n'
        "[tool.uv.exclude-newer-package]\n"
        'pkg-dated = "2026-11-01T00:00:00Z"\n'
        "pkg-free = false\n"
        'pkg-odd = "soon"\n',
        encoding="utf-8",
    )
    assert report.collect_exceptions(pyproject.read_text(encoding="utf-8")) == [
        {"package": "pkg-dated", "expires": "2026-11-04T00:00:00+00:00"},
        {"package": "pkg-free", "expires": None},
        {"package": "pkg-odd", "expires": None},
    ]


def test_collect_exceptions_writes_the_file_the_report_reads(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.uv]\nexclude-newer = "3 days"\n'
        '[tool.uv.exclude-newer-package]\npkg-a = "2026-11-05T00:00:00Z"\n',
        encoding="utf-8",
    )
    folder = tmp_path / "dep-audit-main"
    folder.mkdir()
    out = folder / "exceptions.json"
    argv = ["collect-exceptions", "--pyproject", str(pyproject), "--out", str(out)]
    assert report.main(argv) == 0
    assert report.classify("main", tmp_path, now=NOW).expired == ()
    later = datetime(2026, 11, 8, tzinfo=UTC)
    assert report.classify("main", tmp_path, now=later).expired == ("pkg-a",)


@pytest.mark.parametrize("now", ["2026-11-06T00:00:00", "soon"])
def test_the_report_refuses_a_time_without_a_timezone(tmp_path: Path, now: str) -> None:
    argv = [
        "report", "--results", str(tmp_path), "--branches", "main", "--repo", "o/r",
        "--assignee", "owner", "--run-url", "https://example.test", "--now", now,
    ]  # fmt: skip
    with pytest.raises(SystemExit):
        report.main(argv)


def test_collect_exceptions_with_none_is_empty() -> None:
    assert report.collect_exceptions('[tool.uv]\nexclude-newer = "3 days"\n') == []
