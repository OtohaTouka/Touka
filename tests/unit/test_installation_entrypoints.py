"""Run shipped developer entrypoints only inside a fake-command filesystem."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PS = shutil.which("pwsh") or shutil.which("powershell")
GIT = shutil.which("git")


def _git_for_windows_bash(git: str | None) -> str | None:
    """Git for Windows keeps bash.exe in Git/bin; never fall back to the WSL bash on PATH."""
    if not git:
        return None
    install = Path(git).resolve().parents[1]
    # Git Bash puts Git/mingw64/bin/git.exe first on PATH: one level deeper than cmd/ or bin/.
    if install.name.casefold() == "mingw64":
        install = install.parent
    bash = install / "bin" / "bash.exe"
    return str(bash) if bash.is_file() else None


BASH = _git_for_windows_bash(GIT) if os.name == "nt" else shutil.which("bash")
# Container build inputs pinned to LF by .gitattributes; permission_init.py is -text (raw bytes).
LF_BUILD_INPUTS = (
    "ToukaProxy/Dockerfile",
    "ToukaHeartbeat/Dockerfile",
    "ToukaChat/Dockerfile.prod",
    "ToukaChat/scripts/ensure-critical-native.mjs",
    "docker-compose.ghcr.yml",
    "tests/docker/Dockerfile.isolation-test",
)
RAW_BUILD_INPUTS = ("ToukaHeartbeat/permission_init.py",)


def _fixture(
    tmp_path: Path, name: str, script_bytes: bytes | None = None
) -> tuple[Path, Path, Path]:
    root = tmp_path / "synthetic repo"
    proxy = root / "ToukaProxy"
    proxy.mkdir(parents=True)
    (proxy / name).write_bytes(
        script_bytes if script_bytes is not None else (ROOT / "ToukaProxy" / name).read_bytes()
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname="synthetic"\nrequires-python="==3.12.*"\n', encoding="utf-8"
    )
    (root / ".python-version").write_text("3.12\n", encoding="utf-8")
    cwd = tmp_path / "unrelated working directory"
    cwd.mkdir()
    # Let the old unsafe setup reach its behaviour without any real configuration.
    (cwd / ".env.example").write_text("# synthetic only\n", encoding="utf-8")
    log = tmp_path / "commands.jsonl"
    return root, cwd, log


def _environment(tmp_path: Path, **values: str) -> dict[str, str]:
    env = {
        key: os.environ[key]  # noqa: ENV_SSOT_VIOLATION — Fixed OS-only child environment, no product config.
        for key in ("SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
        if key in os.environ  # noqa: ENV_SSOT_VIOLATION — Same closed five OS keys; no environment dump.
    }
    env.update(
        PATH="",
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
        TOUKA_BOOTSTRAP_ENABLED="false",
        **values,
    )
    return env


def _powershell(
    tmp_path: Path,
    name: str,
    *,
    uv_exit: int = 0,
    launch_exit: int = 0,
    version: str = "3.12",
    port: str = "8000",
    missing: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, list[dict]]:
    if not PS:
        pytest.skip("PowerShell is unavailable on this host")
    root, cwd, log = _fixture(tmp_path, name)
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake executable: intercepted by local alias")
    if missing and missing != "uv":
        (root / missing).unlink()
    # No inherited PATH/credentials or profiles. Every executable reachable by
    # old/new entrypoints is replaced before the copied script can run.
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        r"""
$ErrorActionPreference = 'Stop'
function Record-Command($command, $arguments) {
    $row = @{command=$command; argv=[string[]]@($arguments); cwd=(Get-Location).Path}
    [IO.File]::AppendAllText($env:CAPTURE, (($row | ConvertTo-Json -Compress) + "`n"))
}
function FakePython {
    Record-Command 'python' $args
    if ($args -contains '--version') { Write-Output ('Python ' + $env:FAKE_VERSION) }
    elseif ($args -contains '-c') { Write-Output $env:FAKE_VERSION }
    $global:LASTEXITCODE = 0
    if ($args -contains 'uvicorn') { $global:LASTEXITCODE=[int]$env:LAUNCH_EXIT }
}
function FakeUv { Record-Command 'uv' $args; $global:LASTEXITCODE=[int]$env:UV_EXIT }
function Forbidden { Record-Command 'FORBIDDEN' $args; $global:LASTEXITCODE=0 }
Set-Alias python FakePython
Set-Alias python3 FakePython
Set-Alias -Name $env:FAKE_PYTHON -Value FakePython
if ($env:MISSING -ne 'uv') { Set-Alias uv FakeUv }
foreach ($name in @('pip','pip3','docker','netstat','findstr')) { Set-Alias $name Forbidden }
$capture = $env:CAPTURE
$env:CAPTURE = $capture + '.preflight'
foreach ($name in @('python','python3','pip','pip3','docker')) {
    if ((Get-Command $name).CommandType -ne 'Alias') { throw 'fake command missing' }
    & $name '__probe__'
}
if ($env:MISSING -ne 'uv') { & uv '__probe__' }
$env:CAPTURE = $capture
$global:LASTEXITCODE = 0
& $env:ENTRYPOINT
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 — Fixed PowerShell fake-command fixture, isolated env/cwd, 15s.
        [
            PS,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            str(harness),
        ],
        cwd=cwd,
        env=_environment(
            tmp_path,
            CAPTURE=str(log),
            FAKE_PYTHON=str(python),
            FAKE_VERSION=version,
            UV_EXIT=str(uv_exit),
            LAUNCH_EXIT=str(launch_exit),
            MISSING=missing,
            PORT=port,
            ENTRYPOINT=str(root / "ToukaProxy" / name),
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    proof = Path(str(log) + ".preflight")
    assert proof.is_file(), result.stdout + result.stderr
    assert len(proof.read_text().splitlines()) == (5 if missing == "uv" else 6)
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, root, cwd, rows


def _bash(
    tmp_path: Path,
    *,
    uv_exit: int = 0,
    version: str = "3.12",
    missing: str = "",
    script_bytes: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, list[dict]]:
    if not BASH or not Path(BASH).is_file():
        pytest.skip("Bash is unavailable on this host（Windows 需要 Git for Windows）")
    root, cwd, log = _fixture(tmp_path, "setup.sh", script_bytes)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for command in ("uv", "python", "python3", "pip", "pip3", "docker"):
        if command == missing:
            continue
        script = fakebin / command
        script.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\t%s' '{command}' \"$PWD\" >> \"$CAPTURE\"\n"
            'printf "\\t%s" "$@" >> "$CAPTURE"; printf "\\n" >> "$CAPTURE"\n'
            'if [ "$1" = "__probe__" ]; then exit 0; fi\n'
            + (
                'exit "$UV_EXIT"\n'
                if command == "uv"
                else 'if [ "$1" = "--version" ]; then printf "Python "; fi\n'
                'printf "%s\\n" "$FAKE_VERSION"\n'
                if command.startswith("python")
                else "exit 91\n"
            ),
            encoding="utf-8",
            newline="\n",
        )
        script.chmod(0o700)
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    shutil.copyfile(fakebin / "python", python)
    python.chmod(0o700)
    if missing and missing != "uv":
        (root / missing).unlink()
    # bash's own coreutils are available, but all dependency/runtime commands
    # are fake. Their only inputs and cwd live under tmp_path.
    env = _environment(
        tmp_path,
        CAPTURE=log.as_posix(),
        FAKE_VERSION=version,
        UV_EXIT=str(uv_exit),
        FAKEBIN=("/" + fakebin.drive[0].lower() + fakebin.as_posix()[2:])
        if os.name == "nt"
        else str(fakebin),
        ENTRYPOINT=(root / "ToukaProxy" / "setup.sh").as_posix(),
    )
    result = subprocess.run(  # noqa: S603 — Fixed Bash fake-command fixture, isolated env/cwd, 15s.
        [
            BASH,
            "--noprofile",
            "--norc",
            "-c",
            'export PATH="$FAKEBIN:/usr/bin:/bin"; '
            "for name in uv python python3 pip pip3 docker; do "
            '[ "$(command -v "$name")" = "$FAKEBIN/$name" ] || exit 97; '
            'CAPTURE="$CAPTURE.preflight" "$name" __probe__ || exit 98; '
            'done; exec bash "$ENTRYPOINT"',
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    proof = Path(str(log) + ".preflight")
    assert proof.is_file(), result.stdout + result.stderr
    assert len(proof.read_text().splitlines()) == 6
    rows = []
    if log.exists():
        for line in log.read_text().splitlines():
            command, command_cwd, *argv = line.split("\t")
            rows.append({"command": command, "cwd": command_cwd, "argv": argv})
    return result, root, cwd, rows


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_setup_only_syncs_locked_workspace_without_user_state(tmp_path, shell):
    result, root, cwd, rows = (
        _powershell(tmp_path, "setup.ps1") if shell == "powershell" else _bash(tmp_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    uv = [row for row in rows if row["command"] == "uv"]
    assert len(uv) == 1
    assert uv[0]["argv"] == ["sync", "--locked", "--all-packages"]
    assert uv[0]["cwd"].replace("\\", "/").endswith(root.name)
    assert all(row["command"] in {"uv", "python"} for row in rows)
    for directory in (cwd, root, root / "ToukaProxy"):
        for relative in (".env", "data", "config"):
            assert not (directory / relative).exists(), relative


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_setup_preserves_dependency_failure(tmp_path, shell):
    result, _, _, _ = (
        _powershell(tmp_path, "setup.ps1", uv_exit=37)
        if shell == "powershell"
        else _bash(tmp_path, uv_exit=37)
    )
    assert result.returncode == 37


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("missing", ["uv.lock", "pyproject.toml", ".python-version"])
def test_setup_rejects_incomplete_workspace_before_sync(tmp_path, shell, missing):
    result, _, _, rows = (
        _powershell(tmp_path, "setup.ps1", missing=missing)
        if shell == "powershell"
        else _bash(tmp_path, missing=missing)
    )
    assert result.returncode != 0
    assert not any(row["command"] == "uv" for row in rows)


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("version", ["3.11", "3.14", "not-a-version"])
def test_setup_rejects_wrong_resulting_venv(tmp_path, shell, version):
    result, _, _, _ = (
        _powershell(tmp_path, "setup.ps1", version=version)
        if shell == "powershell"
        else _bash(tmp_path, version=version)
    )
    assert result.returncode != 0


@pytest.mark.parametrize("missing", ["uv", ".venv/Scripts/python.exe"])
def test_powershell_setup_missing_tool_fails_loud(tmp_path, missing):
    result, _, _, _ = _powershell(tmp_path, "setup.ps1", missing=missing)
    assert result.returncode != 0


def test_launcher_uses_workspace_venv_loopback_and_no_access_log(tmp_path):
    result, root, _, rows = _powershell(tmp_path, "run_proxy.ps1", port="8123")
    assert result.returncode == 0, result.stdout + result.stderr
    launches = [row for row in rows if "uvicorn" in row["argv"]]
    assert len(launches) == 1
    assert launches[0]["cwd"] == str(root / "ToukaProxy")
    assert launches[0]["argv"] == [
        "-B",
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
        "--reload",
        "--no-access-log",
    ]
    assert all(row["command"] == "python" for row in rows)


@pytest.mark.parametrize("port", ["0", "65536", "8000; unsafe", "word"])
def test_launcher_rejects_invalid_port_without_starting(tmp_path, port):
    result, _, _, rows = _powershell(tmp_path, "run_proxy.ps1", port=port)
    assert result.returncode != 0
    assert not any("uvicorn" in row["argv"] for row in rows)


def test_launcher_preserves_server_failure(tmp_path):
    result, _, _, rows = _powershell(tmp_path, "run_proxy.ps1", launch_exit=43)
    assert any("uvicorn" in row["argv"] for row in rows)
    assert result.returncode == 43


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("name", ["setup.ps1", "run_proxy.ps1"])
def test_native_version_command_quotes_are_portable(tmp_path, shell, name):
    """Only the actual version-only argv reaches real Python, isolated and -S."""
    engine = shutil.which(shell)
    if not engine:
        pytest.skip(f"{shell} unavailable")
    line = next(
        line.strip()
        for line in (ROOT / "ToukaProxy" / name).read_text().splitlines()
        if line.strip().startswith("$actualVersion = &")
    )
    assert "-I -S -B -c" in line
    command = (
        "$python = $env:VERSION_PYTHON; "
        + line
        + "; Write-Output $actualVersion; exit $LASTEXITCODE"
    )
    probe = tmp_path / "version-only.ps1"
    probe.write_text(command, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 — Fixed isolated Python version-only probe, 15s; no product import.
        [
            engine,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-File",
            str(probe),
        ],
        cwd=tmp_path,
        env=_environment(tmp_path, VERSION_PYTHON=sys.executable),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    (tmp_path / "native-command.json").write_text(
        json.dumps({"argv": result.args, "script": command, "rc": result.returncode}),
        encoding="utf-8",
    )
    (tmp_path / "native-stdout.txt").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "native-stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_normal_checkout_preserves_executable_bash_source(tmp_path):
    """Use a separate Git index to exercise Windows checkout conversion."""
    if not GIT:
        pytest.skip("Git unavailable")
    checkout = tmp_path / "checkout"
    staged = tmp_path / "git fixture"
    staged.mkdir()
    (staged / "ToukaProxy").mkdir()
    (staged / "ToukaProxy" / "setup.sh").write_bytes(
        (ROOT / "ToukaProxy" / "setup.sh").read_bytes()
    )
    (staged / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    byte_inputs = (*LF_BUILD_INPUTS, *RAW_BUILD_INPUTS)
    for relative in byte_inputs:
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    env = _environment(
        tmp_path,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
    )
    for argv in (
        ["init", "--quiet"],
        ["add", "--", "ToukaProxy/setup.sh", ".gitattributes", *byte_inputs],
        ["checkout-index", "--all", "--prefix=" + checkout.as_posix() + "/"],
    ):
        result = subprocess.run(  # noqa: S603 — Fixed Git argv and owned fixture index, isolated env/cwd, 15s.
            [GIT, "-c", "core.autocrlf=true", *argv],
            cwd=staged,
            env=env,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    for relative in byte_inputs:
        actual = (checkout / relative).read_bytes()
        expected = (ROOT / relative).read_bytes()
        equal = actual == expected
        assert equal, (
            relative,
            len(actual),
            hashlib.sha256(actual).hexdigest(),
            len(expected),
            hashlib.sha256(expected).hexdigest(),
        )
    for relative in LF_BUILD_INPUTS:
        assert b"\r" not in (checkout / relative).read_bytes(), relative
    raw = (checkout / "ToukaProxy" / "setup.sh").read_bytes()
    assert b"\r" not in raw
    result, _, _, rows = _bash(tmp_path / "execution", script_bytes=raw)
    assert result.returncode == 0, result.stdout + result.stderr
    assert next(row for row in rows if row["command"] == "uv")["argv"] == [
        "sync",
        "--locked",
        "--all-packages",
    ]


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake executable")
    return path


@pytest.mark.parametrize("git_dir", ["cmd", "bin", "mingw64/bin", "MINGW64/bin"])
def test_git_for_windows_bash_is_found_from_every_git_location(tmp_path, git_dir):
    """Git Bash puts mingw64/bin/git.exe first on PATH; bash.exe still lives in Git/bin."""
    install = tmp_path / "Program Files" / "Git"
    git = _touch(install / git_dir / "git.exe")
    bash = _touch(install / "bin" / "bash.exe")

    assert _git_for_windows_bash(str(git)) == str(bash.resolve())


@pytest.mark.parametrize("git_dir", ["cmd", "bin"])
def test_git_for_windows_bash_does_not_climb_above_the_install(tmp_path, git_dir):
    """Only mingw64 sits one level deeper; other locations never probe the parent folder."""
    programs = tmp_path / "Program Files"
    git = _touch(programs / "Git" / git_dir / "git.exe")
    _touch(programs / "bin" / "bash.exe")

    assert _git_for_windows_bash(str(git)) is None


def test_missing_git_for_windows_bash_never_falls_back(tmp_path):
    git = _touch(tmp_path / "Git" / "mingw64" / "bin" / "git.exe")

    assert _git_for_windows_bash(str(git)) is None
    assert _git_for_windows_bash(None) is None


def test_missing_bash_skip_reason_names_git_for_windows(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "BASH", None)

    with pytest.raises(pytest.skip.Exception, match="Git for Windows"):
        _bash(tmp_path)
