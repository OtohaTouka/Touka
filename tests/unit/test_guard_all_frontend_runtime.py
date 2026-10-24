"""Guard aggregator frontend runtime preflight regression tests."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from dependency_audit_evidence import ProcessEvidence, create_evidence_root

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
GUARD_ALL_PATH = ROOT / "scripts" / "guard_all.py"
CHAT_ROOT = ROOT / "ToukaChat"
# Guard #42 (Release Workflow Gate) and #43 (ghcr Compose Pull-Purity) are retired;
# guard numbers are stable identifiers and are not renumbered.
RETIRED_GUARD_IDS = {42, 43}
GUARD_IDS = set(range(1, 46)) - RETIRED_GUARD_IDS
AUDIT_GUARD_IDS = {22, 23}


def _load_guard_all() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_all_under_test", GUARD_ALL_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_preflight_reports_expected_and_actual_versions() -> None:
    guard_all = _load_guard_all()
    calls: list[str] = []

    def fake_runner(command: str, **_: Any) -> tuple[bool, str]:
        calls.append(command)
        outputs = {
            "node --version": "v26.2.0\n",
            "npm --version": "11.13.0\n",
        }
        return True, outputs[command]

    success, detail = guard_all.run_frontend_runtime_preflight(
        CHAT_ROOT,
        command_runner=fake_runner,
    )

    assert success is False
    assert calls == ["node --version", "npm --version"]
    assert "Node expected=24.22.1 actual=26.2.0" in detail
    assert "npm expected=11.19.1 actual=11.13.0" in detail


def test_runtime_preflight_accepts_exact_project_versions() -> None:
    guard_all = _load_guard_all()

    def fake_runner(command: str, **_: Any) -> tuple[bool, str]:
        outputs = {
            "node --version": "v24.22.1\n",
            "npm --version": "11.19.1\n",
        }
        if command.startswith("node -e "):
            return True, ""
        return True, outputs[command]

    success, detail = guard_all.run_frontend_runtime_preflight(
        CHAT_ROOT,
        command_runner=fake_runner,
    )

    assert success is True
    assert detail == "Node 24.22.1 / npm 11.19.1"


def test_runtime_preflight_rejects_missing_windows_user_profile_capability() -> None:
    guard_all = _load_guard_all()
    calls: list[str] = []

    def fake_runner(command: str, **_: Any) -> tuple[bool, str]:
        calls.append(command)
        outputs = {
            "node --version": (True, "v24.22.1\n"),
            "npm --version": (True, "11.19.1\n"),
        }
        if command.startswith("node -e "):
            return False, "uv_os_get_passwd returned ENOMEM"
        return outputs[command]

    success, detail = guard_all.run_frontend_runtime_preflight(
        CHAT_ROOT,
        command_runner=fake_runner,
    )

    assert success is False
    assert len(calls) == 3
    assert calls[:2] == ["node --version", "npm --version"]
    assert calls[2].startswith("node -e ")
    assert detail == ("Node user-profile capability failed: uv_os_get_passwd returned ENOMEM")


def test_main_stops_before_guard_tasks_when_runtime_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_all = _load_guard_all()
    task_calls: list[str] = []

    monkeypatch.setattr(
        guard_all,
        "run_frontend_runtime_preflight",
        lambda _frontend_dir: (False, "runtime drift"),
    )

    def unexpected_task(command: str, **_: Any) -> tuple[bool, str]:
        task_calls.append(command)
        return True, ""

    monkeypatch.setattr(guard_all, "run_command", unexpected_task)

    with pytest.raises(SystemExit) as exc_info:
        guard_all.main()

    assert exc_info.value.code == 1
    assert task_calls == []


def test_dependency_audit_guards_use_dedicated_fail_closed_scripts() -> None:
    source = GUARD_ALL_PATH.read_text(encoding="utf-8")

    commands = {
        value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
        and key.value == "command"
        and isinstance(value, ast.Constant)
        and isinstance(value.value, str)
    }

    assert "python scripts/guard_python_cve.py" in commands
    assert "python scripts/guard_node_cve.py" in commands
    assert not any("--profile" in command for command in commands)
    assert "npm run guard:audit" not in commands


def test_main_keeps_43_guards_and_registers_both_audit_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_all = _load_guard_all()
    calls: list[tuple[str, Path, str]] = []
    monkeypatch.setattr(
        guard_all,
        "run_frontend_runtime_preflight",
        lambda _frontend_dir: (True, "exact runtime"),
    )

    def fake_run(command: str, *, cwd: Path, name: str, **_: Any) -> tuple[bool, str]:
        calls.append((command, cwd, name))
        return True, ""

    monkeypatch.setattr(guard_all, "run_command", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        guard_all.main()

    assert exc_info.value.code == 0
    assert len(calls) == len(GUARD_IDS) == 43
    assert calls[21][0] == "python scripts/guard_python_cve.py"
    assert "[Guard #22]" in calls[21][2]
    assert calls[22][0] == "python scripts/guard_node_cve.py"
    assert "[Guard #23]" in calls[22][2]
    assert calls[42][0] == "python -B scripts/guard_native_tool_contracts.py"
    assert "[Guard #45]" in calls[42][2]
    assert not any(
        f"[Guard #{number}]" in name for _, _, name in calls for number in RETIRED_GUARD_IDS
    )


@pytest.mark.parametrize(
    ("number", "arguments"),
    [
        (17, ["tests/integration/test_api_schema_sync.py"]),
        (34, ["tests/unit/test_config_examples.py", "-q"]),
    ],
)
def test_pytest_guards_use_current_interpreter_native_argv(
    monkeypatch: pytest.MonkeyPatch, number: int, arguments: list[str]
) -> None:
    guard_all = _load_guard_all()
    calls: dict[str, tuple[object, Path]] = {}
    monkeypatch.setattr(guard_all, "run_frontend_runtime_preflight", lambda _: (True, "exact"))

    def fake_run(command: object, *, cwd: Path, name: str, **_: Any) -> tuple[bool, str]:
        calls[name] = (command, cwd)
        return True, ""

    monkeypatch.setattr(guard_all, "run_command", fake_run)
    with pytest.raises(SystemExit) as result:
        guard_all.main(["--non-audit"])
    assert result.value.code == 0
    matches = [row for name, row in calls.items() if f"[Guard #{number}]" in name]
    assert matches == [([guard_all.sys.executable, "-B", "-m", "pytest", *arguments], ROOT)]


@pytest.mark.parametrize("failed_guard", [None, "[Guard #27]"])
def test_non_audit_runs_exactly_41_and_reports_incomplete_full_suite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_guard: str | None,
) -> None:
    guard_all = _load_guard_all()
    calls: list[str] = []
    monkeypatch.setattr(
        guard_all,
        "run_frontend_runtime_preflight",
        lambda _frontend_dir: (True, "exact runtime"),
    )

    def fake_run(_command: str, *, name: str, **_: Any) -> tuple[bool, str]:
        calls.append(name)
        success = failed_guard is None or failed_guard not in name
        return success, "" if success else "selected guard failed"

    monkeypatch.setattr(guard_all, "run_command", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        guard_all.main(["--non-audit"])

    expected_ids = GUARD_IDS - AUDIT_GUARD_IDS
    assert len(calls) == len(expected_ids)
    assert {int(name.rsplit("#", 1)[1].removesuffix("]")) for name in calls} == expected_ids
    assert exc_info.value.code == (1 if failed_guard else 0)
    output = capsys.readouterr().out
    assert "[NOT RUN] | Python CVE Guard (pip-audit) [Guard #22]" in output
    assert "[NOT RUN] | Node CVE Guard (Chat + IDE, runtime + full) [Guard #23]" in output
    assert "Full 43-guard suite: INCOMPLETE" in output
    assert "All architectural checks PASSED" not in output
    if failed_guard:
        assert "selected guard failed" in output
        assert "Selected guards PASSED" not in output
    else:
        assert "Selected guards PASSED (41/41)" in output


def test_unknown_guard_option_fails_before_any_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_all = _load_guard_all()

    def unexpected_preflight(_frontend_dir: Path) -> tuple[bool, str]:
        pytest.fail("Invalid options must not start runtime preflight")

    monkeypatch.setattr(guard_all, "run_frontend_runtime_preflight", unexpected_preflight)
    with pytest.raises(SystemExit) as exc_info:
        guard_all.main(["--skip-audits"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("non_audit", [False, True])
def test_dependency_cache_only_changes_only_dependency_guard_command(
    monkeypatch: pytest.MonkeyPatch,
    non_audit: bool,
) -> None:
    guard_all = _load_guard_all()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        guard_all,
        "run_frontend_runtime_preflight",
        lambda _frontend_dir: (True, "exact runtime"),
    )

    def fake_run(command: str, *, name: str, **_: Any) -> tuple[bool, str]:
        calls.append((command, name))
        return True, ""

    monkeypatch.setattr(guard_all, "run_command", fake_run)
    with pytest.raises(SystemExit) as baseline:
        guard_all.main()
    assert baseline.value.code == 0
    expected = [
        (command + " --cache-only" if "[Guard #16]" in name else command, name)
        for command, name in calls
        if not non_audit or not any(f"[Guard #{number}]" in name for number in (22, 23))
    ]
    calls.clear()
    argv = ["--dependency-cache-only"] + (["--non-audit"] if non_audit else [])
    with pytest.raises(SystemExit) as actual:
        guard_all.main(argv)
    assert actual.value.code == 0
    assert calls == expected


@pytest.mark.parametrize("failed_audit", [None, "python", "node"])
def test_retained_audits_use_safe_argv_and_keep_separate_raw_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failed_audit: str | None,
) -> None:
    guard_all = _load_guard_all()
    evidence_root = tmp_path / "audit & ! % ; $(literal)"
    calls: list[tuple[str | Sequence[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        guard_all, "run_frontend_runtime_preflight", lambda _: (True, "exact runtime")
    )

    def fake_popen(command: str | Sequence[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        exit_code = 0
        if not isinstance(command, str) and list(command[:4]) == [
            sys.executable,
            "-B",
            "-m",
            "pytest",
        ]:
            assert list(command[4:]) in (
                ["tests/integration/test_api_schema_sync.py"],
                ["tests/unit/test_config_examples.py", "-q"],
            )
            assert kwargs["shell"] is False
            assert kwargs["cwd"] == ROOT
        elif not isinstance(command, str):
            child = Path(command[-1])
            assert child.parent == evidence_root
            assert child.name in {"python", "node"}
            assert list(command) == [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / f"guard_{child.name}_cve.py"),
                "--evidence-dir",
                str(child),
            ]
            assert kwargs["shell"] is False
            assert kwargs["cwd"] == ROOT
            # Only the simulated child owns creation of its evidence directory.
            assert not child.exists()
            retained = create_evidence_root(child, ROOT)
            capture = ProcessEvidence(retained, "fixture-audit", list(command), ROOT, {}, 300)
            exit_code = int(child.name == failed_audit)
            capture.finish(
                stdout=b"fixture-only raw output\xff",
                stderr=b"fixture-only error\xfe" if exit_code else b"",
                exit_code=exit_code,
            )
        else:
            assert kwargs["shell"] is True
        return SimpleNamespace(
            returncode=exit_code,
            communicate=lambda timeout: ("fixture stdout", "fixture failure" if exit_code else ""),
        )

    monkeypatch.setattr(
        guard_all,
        "subprocess",
        SimpleNamespace(
            Popen=fake_popen,
            PIPE=subprocess.PIPE,
            TimeoutExpired=subprocess.TimeoutExpired,
            CREATE_NEW_PROCESS_GROUP=512,
        ),
    )
    with pytest.raises(SystemExit) as actual:
        guard_all.main(["--audit-evidence-root", str(evidence_root)])

    assert actual.value.code == int(failed_audit is not None)
    assert len(calls) == 43
    assert [index for index, (command, _) in enumerate(calls) if not isinstance(command, str)] == [
        16,
        21,
        22,
        33,
    ]
    for audit in ("python", "node"):
        retained = evidence_root / audit / "fixture-audit"
        assert (retained / "stdout.bin").read_bytes() == b"fixture-only raw output\xff"
        expected_error = b"fixture-only error\xfe" if audit == failed_audit else b""
        assert (retained / "stderr.bin").read_bytes() == expected_error
        receipt = json.loads((retained / "receipt.json").read_text(encoding="utf-8"))
        assert receipt["exit_code"] == int(audit == failed_audit)
    output = capsys.readouterr().out
    assert ("Full 43-guard suite: INCOMPLETE" in output) == (failed_audit is not None)
    assert ("All architectural checks PASSED" in output) == (failed_audit is None)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_argv_execution_never_uses_shell_and_keeps_platform_process_groups(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    guard_all = _load_guard_all()
    calls: list[tuple[Sequence[str], dict[str, Any]]] = []
    command = [sys.executable, "audit script.py", "--evidence-dir", "audit & ! % ; $(literal)"]

    def fake_popen(argv: Sequence[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, communicate=lambda timeout: ("done", ""))

    monkeypatch.setattr(guard_all, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        guard_all,
        "subprocess",
        SimpleNamespace(Popen=fake_popen, PIPE=subprocess.PIPE, CREATE_NEW_PROCESS_GROUP=512),
    )
    assert guard_all.run_command(command, cwd=ROOT, timeout=17) == (True, "done")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == command
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == ROOT
    if platform == "win32":
        assert kwargs["creationflags"] == 512
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs


def test_non_audit_retention_option_never_creates_root_or_launches_audits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard_all = _load_guard_all()
    evidence_root = tmp_path / "unused evidence"
    calls: list[str] = []
    monkeypatch.setattr(
        guard_all, "run_frontend_runtime_preflight", lambda _: (True, "exact runtime")
    )

    def unexpected_root(*_: Any) -> Path:
        pytest.fail("--non-audit must not create any evidence directory")

    def fake_run(_command: str, *, name: str, **_: Any) -> tuple[bool, str]:
        calls.append(name)
        return True, ""

    monkeypatch.setattr(guard_all, "create_evidence_root", unexpected_root, raising=False)
    monkeypatch.setattr(guard_all, "run_command", fake_run)
    with pytest.raises(SystemExit) as actual:
        guard_all.main(["--non-audit", "--audit-evidence-root", str(evidence_root)])

    assert actual.value.code == 0
    assert {int(name.rsplit("#", 1)[1].removesuffix("]")) for name in calls} == (
        GUARD_IDS - AUDIT_GUARD_IDS
    )
    assert len(calls) == 41
    assert not evidence_root.exists()
    output = capsys.readouterr().out
    assert "[NOT RUN] | Python CVE Guard (pip-audit) [Guard #22]" in output
    assert "[NOT RUN] | Node CVE Guard (Chat + IDE, runtime + full) [Guard #23]" in output
    assert "Full 43-guard suite: INCOMPLETE" in output
    assert "Selected guards PASSED (41/41)" in output
    assert "All architectural checks PASSED" not in output


@pytest.mark.parametrize(
    "invalid_kind", ["existing", "relative", "repository", "protected", "missing-parent"]
)
def test_invalid_evidence_root_refuses_all_guards_and_preserves_existing_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    invalid_kind: str,
) -> None:
    guard_all = _load_guard_all()
    previous = tmp_path / "previous evidence"
    previous.mkdir()
    marker = previous / "previous-failure.bin"
    marker.write_bytes(b"original failure\xff")
    evidence_root = {
        "existing": previous,
        "relative": Path("relative-evidence"),
        "repository": ROOT / "rejected-evidence",
        "protected": tmp_path / "data" / "rejected-evidence",
        "missing-parent": tmp_path / "missing-parent" / "rejected-evidence",
    }[invalid_kind]
    monkeypatch.setattr(
        guard_all, "run_frontend_runtime_preflight", lambda _: (True, "exact runtime")
    )

    def unexpected_guard(*_: Any, **__: Any) -> tuple[bool, str]:
        pytest.fail("Invalid evidence root must not start any guard or temporary fallback")

    monkeypatch.setattr(guard_all, "run_command", unexpected_guard)
    with pytest.raises(SystemExit) as actual:
        guard_all.main(["--audit-evidence-root", str(evidence_root)])
    assert actual.value.code == 1
    assert marker.read_bytes() == b"original failure\xff"
    output = capsys.readouterr().out
    assert "[FAIL] Audit evidence root" in output
    assert "Full 43-guard suite: INCOMPLETE" in output
    assert "All architectural checks PASSED" not in output


def test_retention_root_is_not_created_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    guard_all = _load_guard_all()
    evidence_root = tmp_path / "unused evidence"
    monkeypatch.setattr(
        guard_all, "run_frontend_runtime_preflight", lambda _: (False, "runtime drift")
    )

    def unexpected_action(*_: Any, **__: Any) -> None:
        pytest.fail("Failed runtime preflight must precede evidence creation and guard execution")

    monkeypatch.setattr(guard_all, "create_evidence_root", unexpected_action, raising=False)
    monkeypatch.setattr(guard_all, "run_command", unexpected_action)
    with pytest.raises(SystemExit) as actual:
        guard_all.main(["--audit-evidence-root", str(evidence_root)])
    assert actual.value.code == 1
    assert not evidence_root.exists()


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [("SSOT rule and source line\n", ""), ("SSOT rule and source line\n", "child error\n")],
)
def test_nonzero_guard_preserves_both_output_streams(
    monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str
) -> None:
    guard_all = _load_guard_all()
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_popen(command: str, **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, communicate=lambda timeout: (stdout, stderr))

    monkeypatch.setattr(
        guard_all,
        "subprocess",
        SimpleNamespace(Popen=fake_popen, PIPE=subprocess.PIPE, CREATE_NEW_PROCESS_GROUP=512),
    )
    success, output = guard_all.run_command(
        "python scripts/lint_ssot.py", cwd=ROOT, name="SSOT fixture", timeout=17
    )

    assert success is False
    assert stdout in output
    if stderr:
        assert stderr in output
    assert len(calls) == 1
    assert calls[0][0] == "python scripts/lint_ssot.py"
    assert calls[0][1]["cwd"] == ROOT
    assert calls[0][1]["shell"] is True
