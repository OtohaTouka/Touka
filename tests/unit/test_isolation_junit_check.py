"""``scripts/ci/isolation_junit_check.py``：Landlock CI job 的 junit 核對（ADR-0272）.

- 兩個 pass：failed／error 一律不准；skip 一定帶允許的標籤；subreaper 案例必須 passed；
  pytest 不能是 pid 1。
- pass A：網路設定不安全時才允許 ``[skip:net]``，而且不安全分支的案例必須 passed。
- pass B：層級測試必須 passed；ABI≥6 時不准 skip，ABI 4／5 時只准 ``[skip:abi6]``。
- 網路設定的判定和啟動器同一套規則；標籤與案例名稱和 Linux 模組對得上（靜態核對，Windows 也跑）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

import pytest
from src.sandbox_kernel import landlock, launcher
from src.utils.isolation_policy import LANDLOCK_LAYER_MIN_ABI

ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = ROOT / "scripts" / "ci" / "isolation_junit_check.py"
LINUX_MODULE = ROOT / "tests" / "integration" / "test_isolation_linux.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("isolation_junit_check_fixture", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 要從 sys.modules 找到自己的模組
    spec.loader.exec_module(module)
    return module


ck = _load()

NET_SKIP = (
    "[skip:net] 網路設定不安全（net_tcp_fastopen），landlock 層不可用，"
    "且沒設 TOUKA_REQUIRE_LANDLOCK"
)
ABI6_SKIP = "[skip:abi6] signal／abstract unix 的 scope 要 ABI 6"


def _junit(
    tmp_path: Path,
    cases: dict[str, tuple[str, str]],
    *,
    pid: int | None = 4242,
    suite_errors: int | None = None,
) -> Path:
    """pytest（xunit2）形狀的 junit：``{name: (outcome, message)}``，outcome 是 passed／skipped／
    failed／error；``record_testsuite_property`` 寫在 testsuite 的 properties."""
    body = []
    for name, (outcome, message) in cases.items():
        attrs = (
            f'classname="tests.integration.test_isolation_linux" name={quoteattr(name)} time="0.1"'
        )
        child = {
            "passed": "",
            "skipped": f'<skipped type="pytest.skip" message={quoteattr(message)}>x</skipped>',
            "failed": f"<failure message={quoteattr(message)}>trace</failure>",
            "error": f"<error message={quoteattr(message)}>trace</error>",
        }[outcome]
        body.append(f"<testcase {attrs}>{child}</testcase>")
    counts = {
        o: sum(1 for x, _ in cases.values() if x == o) for o in ("skipped", "failed", "error")
    }
    errors = counts["error"] if suite_errors is None else suite_errors
    props = (
        ""
        if pid is None
        else f'<properties><property name="pytest_pid" value="{pid}" /></properties>'
    )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest" errors="{errors}" failures="{counts["failed"]}"'
        f' skipped="{counts["skipped"]}" tests="{len(cases)}" time="1.0" hostname="h">'
        f"{props}{''.join(body)}</testsuite></testsuites>"
    )
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def _facts(tmp_path: Path, *, abi: int = 7, net: str | None = "net_tcp_fastopen") -> Path:
    path = tmp_path / "facts.json"
    path.write_text(json.dumps({"abi": abi, "net": net}), encoding="utf-8")
    return path


PASSED = ("passed", "")


def _clean(stage: str, *, net: bool = True) -> dict[str, tuple[str, str]]:
    """這個 pass 的乾淨結果：必須 passed 的都 passed，pass A 另有 17 個網路設定的 skip."""
    cases = dict.fromkeys(ck.SUBREAPER_CASES, PASSED)
    cases["test_self_test_passes_in_pin_mode"] = PASSED
    if stage == "a":
        cases.update(dict.fromkeys(ck.UNSAFE_NET_CASES, PASSED))
        if net:
            cases.update(
                {f"test_needs_landlock_level_{i}": ("skipped", NET_SKIP) for i in range(17)}
            )
            cases[ck.LEVEL_CASE] = ("skipped", NET_SKIP)
        else:
            cases[ck.LEVEL_CASE] = PASSED
    else:
        cases[ck.LEVEL_CASE] = PASSED
    return cases


def _problems(tmp_path, stage, cases, *, abi=7, net="net_tcp_fastopen", **junit) -> list[str]:
    report = ck.read_junit(_junit(tmp_path, cases, **junit))
    facts = ck.read_facts(_facts(tmp_path, abi=abi, net=net))
    return ck.check(report, facts, stage)


# ── 乾淨的結果 ─────────────────────────────────────────────────────────────────


def test_clean_pass_a_with_unsafe_net_and_clean_pass_b_have_no_problems(tmp_path):
    assert _problems(tmp_path, "a", _clean("a")) == []
    assert _problems(tmp_path, "b", _clean("b")) == []


def test_pass_a_on_a_host_whose_net_is_already_safe_has_no_net_skips(tmp_path):
    assert _problems(tmp_path, "a", _clean("a", net=False), net=None) == []


@pytest.mark.parametrize("stage", ["a", "b"])
@pytest.mark.parametrize(("abi", "allowed"), [(4, True), (5, True), (6, False), (7, False)])
def test_abi6_skips_are_allowed_only_below_abi_6(tmp_path, stage, abi, allowed):
    cases = {
        **_clean(stage),
        "test_signals_and_abstract_sockets_stay_inside_the_domain": ("skipped", ABI6_SKIP),
    }
    problems = _problems(tmp_path, stage, cases, abi=abi)
    assert (problems == []) is allowed, problems


# ── 失敗、錯誤、空的 junit ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["a", "b"])
@pytest.mark.parametrize("outcome", ["failed", "error"])
def test_any_failure_or_error_is_a_problem(tmp_path, stage, outcome):
    cases = {**_clean(stage), "test_other": (outcome, "boom")}
    problems = _problems(tmp_path, stage, cases)
    assert any("test_other" in p for p in problems), problems


def test_suite_level_errors_without_a_testcase_are_a_problem(tmp_path):
    """收集錯誤只記在 testsuite 的 errors 上時也不放過。"""
    problems = _problems(tmp_path, "b", _clean("b"), suite_errors=1)
    assert any("errors" in p for p in problems), problems


def test_a_junit_without_cases_is_a_problem(tmp_path):
    problems = _problems(tmp_path, "b", {})
    assert any("no test cases" in p for p in problems), problems


# ── skip 標籤 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "網路設定不安全（net_tcp_fastopen），landlock 層不可用，且沒設 TOUKA_REQUIRE_LANDLOCK",
        "[skip:whatever] 不明的標籤",
        "說明在前 [skip:net] 標籤在後",
        "[skip:landlock] Landlock ABI 0 < 3，且沒設 TOUKA_REQUIRE_LANDLOCK",
        "[skip:launcher] 還沒有 sandbox_kernel 啟動器，且沒設 TOUKA_REQUIRE_LANDLOCK",
        "[skip:level] landlock 層不可用（probe_pending），且沒設 TOUKA_REQUIRE_LANDLOCK",
        "[skip:root] root 不受權限位元限制",
        "[skip:platform] Landlock isolation tests run only on Linux",
    ],
    ids=[
        "untagged",
        "unknown",
        "not-at-start",
        "landlock",
        "launcher",
        "level",
        "root",
        "platform",
    ],
)
@pytest.mark.parametrize("stage", ["a", "b"])
def test_untagged_unknown_or_never_allowed_skips_are_problems(tmp_path, stage, message):
    cases = {**_clean(stage), "test_other": ("skipped", message)}
    assert _problems(tmp_path, stage, cases, abi=5) != []


def test_net_skips_are_not_allowed_in_pass_b(tmp_path):
    cases = {**_clean("b"), "test_needs_landlock_level": ("skipped", NET_SKIP)}
    assert _problems(tmp_path, "b", cases) != []


def test_net_skips_are_not_allowed_in_pass_a_when_the_host_net_is_safe(tmp_path):
    assert _problems(tmp_path, "a", _clean("a"), net=None) != []


def test_abi4_skips_are_allowed_in_pass_a_only_below_abi_4(tmp_path):
    cases = {
        **_clean("a", net=False),
        "test_blocked_ports": ("skipped", "[skip:abi4] TCP 規則要 ABI 4"),
    }
    assert _problems(tmp_path, "a", cases, abi=3, net=None) == []
    assert _problems(tmp_path, "a", cases, abi=4, net=None) != []


# ── 必須 passed 的案例 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("how", ["missing", "skipped"])
def test_unsafe_branch_cases_must_pass_in_pass_a_when_the_net_is_unsafe(tmp_path, how):
    for name in ck.UNSAFE_NET_CASES:
        cases = _clean("a")
        if how == "missing":
            del cases[name]
        else:
            cases[name] = ("skipped", NET_SKIP)  # 帶了允許的標籤也不行
        problems = _problems(tmp_path, "a", cases)
        assert any(name in p for p in problems), (name, problems)


# U2e：網路設定不安全時執行器判 none，人核准後降級到執行器（features 不含 tcp）
EXECUTOR_FALLBACK_CASES = (
    "test_terminal_command_runs_inside_the_executor",
    "test_executor_timeout_kills_the_remote_command",
    "test_verification_command_runs_inside_the_executor",
    "test_evolution_verify_runs_inside_the_executor",
)


def test_executor_fallback_cases_are_part_of_the_unsafe_branch():
    assert set(EXECUTOR_FALLBACK_CASES) <= set(ck.UNSAFE_NET_CASES)


def test_unsafe_branch_cases_are_not_required_when_the_net_is_safe(tmp_path):
    cases = _clean("a", net=False)
    for name in ck.UNSAFE_NET_CASES:
        del cases[name]
    assert _problems(tmp_path, "a", cases, net=None) == []


@pytest.mark.parametrize("how", ["missing", "skipped"])
def test_the_level_case_must_pass_in_pass_b(tmp_path, how):
    cases = _clean("b")
    if how == "missing":
        del cases[ck.LEVEL_CASE]
    else:
        cases[ck.LEVEL_CASE] = ("skipped", ABI6_SKIP)
    problems = _problems(tmp_path, "b", cases, abi=5)
    assert any(ck.LEVEL_CASE in p for p in problems), problems


@pytest.mark.parametrize("stage", ["a", "b"])
def test_subreaper_cases_must_pass_in_both_passes(tmp_path, stage):
    for name in ck.SUBREAPER_CASES:
        cases = _clean(stage)
        del cases[name]
        problems = _problems(tmp_path, stage, cases)
        assert any(name in p for p in problems), (name, problems)


def test_pass_b_needs_a_host_that_reaches_the_landlock_level(tmp_path):
    problems = _problems(tmp_path, "b", _clean("b"), abi=3)
    assert any("ABI" in p for p in problems), problems


# ── pytest 不是 pid 1 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["a", "b"])
@pytest.mark.parametrize("pid", [1, None])
def test_pytest_as_pid_1_or_an_unrecorded_pid_is_a_problem(tmp_path, stage, pid):
    problems = _problems(tmp_path, stage, _clean(stage), pid=pid)
    assert any("pid" in p for p in problems), problems


# ── 指令列 ─────────────────────────────────────────────────────────────────────


def test_check_exit_codes_and_summary(tmp_path, capsys):
    facts = _facts(tmp_path)
    good = _junit(tmp_path, _clean("b"))
    assert ck.main(["check", "--pass", "b", "--junit", str(good), "--facts", str(facts)]) == 0
    assert "pass b" in capsys.readouterr().out
    bad = _junit(tmp_path, {**_clean("b"), "test_other": ("failed", "boom")})
    assert ck.main(["check", "--pass", "b", "--junit", str(bad), "--facts", str(facts)]) == 1
    assert "test_other" in capsys.readouterr().out


def test_facts_command_writes_the_judgement_and_the_raw_values(tmp_path, monkeypatch, capsys):
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"kernel": "6.8.0", "abi": 9, "abi_effective": 7}), "utf-8")
    host = ck.HostFacts(
        kernel="6.8.0-azure", lsm="lockdown,capability,landlock", fastopen="1\n", mptcp="1\n"
    )
    monkeypatch.setattr(ck, "read_host", lambda: host)
    out = tmp_path / "facts.json"
    assert ck.main(["facts", "--probe", str(probe), "--out", str(out)]) == 0
    facts = json.loads(out.read_text("utf-8"))
    assert (facts["abi"], facts["net"]) == (7, "net_tcp_fastopen")
    assert facts["lsm"] == ["lockdown", "capability", "landlock"]
    assert (facts["tcp_fastopen"], facts["mptcp_enabled"]) == ("1", "1")
    assert facts["probe"]["abi"] == 9
    assert json.loads(capsys.readouterr().out) == facts
    assert ck.read_facts(out) == ck.Facts(abi=7, net="net_tcp_fastopen")


@pytest.mark.parametrize(
    ("abi", "expected"), [(3, None), (4, "net_tcp_fastopen"), (7, "net_tcp_fastopen")]
)
def test_facts_judge_the_net_only_from_abi_4_like_the_self_test(abi, expected):
    host = ck.HostFacts(kernel="k", lsm="landlock", fastopen="1\n", mptcp=None)
    assert ck.build_facts({"abi_effective": abi}, host)["net"] == expected


def test_facts_without_a_usable_probe_have_abi_0():
    host = ck.HostFacts(kernel="k", lsm="", fastopen=None, mptcp=None)
    assert ck.build_facts({}, host)["abi"] == 0
    assert ck.build_facts({"abi_effective": "7"}, host)["abi"] == 0


# ── 和啟動器、Linux 模組對得上 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("fastopen", [None, "", "0\n", "1\n", "2\n", "3\n", "x\n", "-1\n"])
@pytest.mark.parametrize("mptcp", [None, "", "0\n", "1\n", "x\n"])
def test_net_judgement_matches_the_launcher(fastopen, mptcp):
    assert ck.net_issue(fastopen, mptcp) == launcher.net_sysctl_issue(fastopen, mptcp)


def test_thresholds_match_the_kernel_and_the_policy():
    assert ck.ABI_TAGS["abi4"] == landlock.MIN_NET_ABI == ck.NET_ABI
    first_scope_abi = next(abi for abi in range(1, 10) if landlock.handled_scopes(abi))
    assert ck.ABI_TAGS["abi6"] == first_scope_abi
    assert ck.LANDLOCK_LEVEL_ABI == LANDLOCK_LAYER_MIN_ABI
    assert ck.NET_SYSCTL_PATHS == launcher.NET_SYSCTL_PATHS


def _linux_tree() -> ast.Module:
    return ast.parse(LINUX_MODULE.read_text(encoding="utf-8"))


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    def called(node: ast.Call) -> str:
        func = node.func
        return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and called(n) == name]


def test_every_skip_in_the_linux_module_starts_with_a_tag():
    for call in _calls(_linux_tree(), "skip"):
        first = call.args[0]
        head = first.values[0] if isinstance(first, ast.JoinedStr) else first
        assert isinstance(head, ast.Constant) and str(head.value).startswith("[skip:"), (
            f"line {call.lineno}: skip message must start with [skip:<tag>]"
        )


def test_the_linux_module_uses_exactly_the_known_tags():
    tree = _linux_tree()
    requires = _calls(tree, "_require")
    assert requires
    keyword_tags = set()
    for call in requires:
        tags = [k.value for k in call.keywords if k.arg == "tag"]
        assert len(tags) == 1, f"line {call.lineno}: _require needs tag="
        constants = {n.value for n in ast.walk(tags[0]) if isinstance(n, ast.Constant)}
        assert constants, f"line {call.lineno}: tag must be literal"
        keyword_tags |= constants
    literal_tags = set(re.findall(r"\[skip:([a-z0-9]+)\]", LINUX_MODULE.read_text("utf-8")))
    assert keyword_tags | literal_tags == ck.KNOWN_TAGS


def test_required_cases_exist_in_the_linux_module():
    functions = {
        node.name: node
        for node in ast.walk(_linux_tree())
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for case in (ck.LEVEL_CASE, *ck.SUBREAPER_CASES, *ck.UNSAFE_NET_CASES):
        name, _, param = case.partition("[")
        assert name in functions, case
        if param:
            ids = {
                n.value
                for decorator in functions[name].decorator_list
                for n in ast.walk(decorator)
                if isinstance(n, ast.Constant)
            }
            assert param.removesuffix("]") in ids, case
