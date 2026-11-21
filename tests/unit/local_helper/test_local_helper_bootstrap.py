"""一行指令安裝的釘住值與安全形狀（ADR-0329）。

README 那一行由 install.ps1 的 sha256 鎖住；install.ps1 再用 sha256 鎖住小幫手檔案與 Python。
這裡核對三者一致、README 只指向這個 repo 歷史裡的 commit，以及 install.ps1 的安全檢查沒被拿掉。
對不上時跑 ``python scripts/local_helper_pins.py``（加 ``--commit <C>`` 會印出 README 那一行）。
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import install as installer
import pytest

from tests.unit.test_uv_dependency_cooldown import COOLDOWN_DAYS

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]
LOCAL = ROOT / "ToukaLocal"
BOOTSTRAP = LOCAL / "install.ps1"
README = LOCAL / "README.md"
FIX_HINT = "跑 python scripts/local_helper_pins.py 取得該有的值"


def _load_pins() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "local_helper_pins", ROOT / "scripts" / "local_helper_pins.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pins = _load_pins()


def _bootstrap_text() -> str:
    return BOOTSTRAP.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def _helper_hashes() -> dict[str, str]:
    block = re.search(r"^\$HelperFiles = \[ordered\]@\{\n(.*?)^\}", _bootstrap_text(), re.S | re.M)
    assert block, "install.ps1 找不到 $HelperFiles"
    pairs = re.findall(r"^    '([^']+)' = '([0-9A-F]{64})'$", block.group(1), re.M)
    assert len(pairs) == len(block.group(1).strip().splitlines()), "清單有看不懂的列"
    return dict(pairs)


def _constant(name: str) -> str:
    found = re.search(rf"^\${name} = '([^']*)'$", _bootstrap_text(), re.M)
    assert found, f"install.ps1 找不到 ${name}"
    return found.group(1)


def _readme_line() -> re.Match[str]:
    pattern = re.escape(pins.ONE_LINE_TEMPLATE)
    pattern = pattern.replace(re.escape("<C>"), "(?P<c>[0-9a-f]{40})", 1)
    pattern = pattern.replace(re.escape("<H>"), "(?P<h>[0-9A-F]{64})", 1)
    pattern = pattern.replace(re.escape("<C>"), "(?P=c)")
    matches = [
        m
        for line in README.read_text(encoding="utf-8").splitlines()
        if (m := re.fullmatch(pattern, line.strip()))
    ]
    assert len(matches) == 1, f"README 要剛好有一行符合樣板的指令；{FIX_HINT}"
    return matches[0]


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=60)


def _full_history() -> None:
    shallow = _git("rev-parse", "--is-shallow-repository")
    assert shallow.returncode == 0, "要在 git repo 裡跑"
    if shallow.stdout.strip() == b"true":
        pytest.skip("淺層 clone 沒有完整歷史；CI 的 contracts job 用 fetch-depth: 0")


# --- 清單與雜湊 ---


def test_bootstrap_downloads_exactly_the_installed_files() -> None:
    expected = (*installer.ALLOWED_FILES, "install.py")
    assert tuple(_helper_hashes()) == expected, FIX_HINT
    assert pins.bootstrap_files() == expected


def test_bootstrap_hashes_match_the_files() -> None:
    for relative, sha256 in _helper_hashes().items():
        assert pins.file_sha256(relative) == sha256, f"{relative} 的雜湊過時了；{FIX_HINT}"


def test_lf_hash_ignores_windows_line_endings() -> None:
    assert pins.lf_sha256(b"a\r\nb\n") == hashlib.sha256(b"a\nb\n").hexdigest().upper()


# --- README 那一行 ---


def test_readme_command_pins_the_current_bootstrap() -> None:
    assert _readme_line()["h"] == pins.file_sha256("install.ps1"), FIX_HINT


def test_readme_commit_is_in_this_repository_history() -> None:
    commit = _readme_line()["c"]
    _full_history()
    assert _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0, "README 的 commit 不存在"
    assert _git("merge-base", "--is-ancestor", commit, "HEAD").returncode == 0, (
        "README 的 commit 要在目前分支的歷史裡（不能是 fork 或被 squash 掉的 commit）"
    )


def test_readme_commit_serves_the_pinned_files() -> None:
    line = _readme_line()
    _full_history()
    at_commit = _git("show", f"{line['c']}:ToukaLocal/install.ps1")
    assert at_commit.returncode == 0
    assert hashlib.sha256(at_commit.stdout).hexdigest().upper() == line["h"]
    for relative, sha256 in _helper_hashes().items():
        blob = _git("show", f"{line['c']}:ToukaLocal/{relative}")
        assert blob.returncode == 0, f"README 的 commit 上沒有 {relative}"
        assert hashlib.sha256(blob.stdout).hexdigest().upper() == sha256, (
            f"README 的 commit 上 {relative} 和清單不同"
        )


def test_one_line_is_written_in_only_one_place() -> None:
    signature = "Get-FileHash $f -Algorithm SHA256"
    skip = {
        "ToukaLocal/README.md",
        "scripts/local_helper_pins.py",
        "tests/unit/local_helper/test_local_helper_bootstrap.py",
    }
    listed = _git("ls-files", "-z")
    assert listed.returncode == 0
    for name in listed.stdout.decode("utf-8").split("\0"):
        if not name or name in skip:
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        assert signature not in text, f"{name} 也有安裝指令；只放在 ToukaLocal/README.md"


def test_one_line_template_has_the_safe_shape() -> None:
    template = pins.ONE_LINE_TEMPLATE
    assert template.count("<C>") == 2
    assert template.count("<H>") == 1
    assert "-UseBasicParsing" in template
    assert "(Get-FileHash $f -Algorithm SHA256).Hash -eq '<H>') { powershell" in template
    assert "-ExecutionPolicy Bypass -File $f" in template
    assert "-bor 3072" in template
    hosts = re.findall(r"https://[^/'\s]+", template)
    assert hosts == ["https://raw.githubusercontent.com"]
    for banned in ("iex", "Invoke-Expression", "Set-ExecutionPolicy", "DownloadString"):
        assert banned.casefold() not in template.casefold()


# --- install.ps1 的形狀 ---


def test_bootstrap_is_utf8_with_bom_and_lf() -> None:
    data = BOOTSTRAP.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), "Windows PowerShell 5.1 沒有 BOM 會用系統字碼頁讀"
    data.decode("utf-8")
    assert b"\r" not in data.replace(b"\r\n", b"\n"), "只能有 LF（或 checkout 換成的 CRLF）"


def test_bootstrap_only_talks_to_two_hosts() -> None:
    urls = re.findall(r"https?://[^\s'\"]+", _bootstrap_text())
    allowed = ("https://www.python.org/ftp/python/", "https://raw.githubusercontent.com/OtohaTouka/Touka")
    assert urls
    for url in urls:
        assert url.startswith(allowed), url


@pytest.mark.parametrize(
    "banned",
    [
        "iex",
        "Invoke-Expression",
        "Set-ExecutionPolicy",
        "DownloadString",
        "RunAs",
        "2>&1",
        ".Content",
        "&&",
        "||",
        "??",
        "?.",
    ],
)
def test_bootstrap_avoids_unsafe_or_newer_syntax(banned: str) -> None:
    code = "\n".join(
        line for line in _bootstrap_text().splitlines() if not line.lstrip().startswith("#")
    )
    if banned.isalpha():
        assert not re.search(rf"(?<![\w-]){re.escape(banned)}(?![\w-])", code, re.I)
    else:
        assert banned not in code


def test_bootstrap_sets_up_before_the_first_download() -> None:
    text = _bootstrap_text()
    first_download = text.index("Invoke-WebRequest")
    language = text.index("if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {")
    for setup in (
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor 3072",
        "Add-Type -AssemblyName System.IO.Compression.FileSystem",
    ):
        assert language < text.index(setup) < first_download, setup


@pytest.mark.parametrize(
    "line",
    [
        # commit 與暫存資料夾
        "if ($Commit -cnotmatch '^[0-9a-f]{40}$') { Stop-Install 'commit 的格式不對' }",
        "    if (Test-Link $item) { Stop-Install '暫存資料夾不能是連結' }",
        "    if (-not (Test-SamePath (Get-FullPath $item.Parent.FullName) $script:Temp)) "
        "{ Stop-Install '暫存資料夾不在 TEMP 裡' }",
        "    if ($item.Name -cnotmatch '^touka-helper-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        "[0-9a-f]{4}-[0-9a-f]{12}$') { Stop-Install '暫存資料夾的名稱不對' }",
        "    if (-not (Test-SamePath (Get-FullPath $PSCommandPath) (Get-FullPath "
        "(Join-Path $full 'install.ps1')))) { Stop-Install '安裝腳本不在暫存資料夾裡' }",
        "    if ($names.Count -ne 1 -or $names[0] -ne 'install.ps1') "
        "{ Stop-Install '暫存資料夾裡有別的檔案' }",
        "    return (Get-Item -LiteralPath $Path -Force).FullName.TrimEnd('\\')",
        "    return [string]::Equals($A, $B, [StringComparison]::OrdinalIgnoreCase)",
        # 刪除只刪這次建的
        "    if (Test-Link $item) { return }",
        "    if (-not (Test-SamePath (Get-FullPath $item.Parent.FullName) $Parent)) { return }",
        "    [IO.Directory]::Delete($item.FullName, $true)",
        # 雜湊比對
        "    $actual = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash",
        "    if ($actual -ne $Sha256) {",
        "        Get-Verified \"$RawBase/$Commit/ToukaLocal/$relative\" $target "
        "$HelperFiles[$relative]",
        "    Get-Verified $PythonUrl $zip $PythonSha256",
        # 不用系統管理員、只支援 x64、小幫手不能開著
        "if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) "
        "{ Stop-Install '請用一般的 PowerShell 執行，不要用系統管理員' }",
        "if ($arch -ne 'AMD64') { Stop-Install '小幫手目前只支援 64 位元的 x64 電腦' }",
        "if (Test-HelperRunning) { Stop-Install '小幫手正在執行，請先關掉小幫手再貼一次' }",
        "$MutexName = 'Local\\ToukaLocalHelper'",
        # Python 自我檢查要看結束碼
        "    if ($LASTEXITCODE -ne 0) "
        "{ Stop-Install 'Python 的自我檢查沒有通過（視窗元件載入不了）' }",
    ],
)
def test_bootstrap_keeps_its_safety_checks(line: str) -> None:
    assert line in _bootstrap_text().splitlines()


def test_mutex_name_matches_the_helper() -> None:
    winapi = (LOCAL / "touka_local" / "winapi.py").read_text(encoding="utf-8")
    assert '_MUTEX_NAME: Final = "Local\\\\ToukaLocalHelper"' in winapi


def test_pinned_python_url_is_the_official_zip() -> None:
    version = _constant("PythonVersion")
    assert re.fullmatch(r"3\.\d+\.\d+", version)
    assert _constant("PythonUrl") == (
        f"https://www.python.org/ftp/python/{version}/python-{version}-amd64.zip"
    )
    assert re.fullmatch(r"[0-9A-F]{64}", _constant("PythonSha256"))


def test_pinned_python_waited_out_the_cooldown() -> None:
    released = datetime.date.fromisoformat(_constant("PythonReleased"))
    pinned = datetime.date.fromisoformat(_constant("PythonPinned"))
    assert (pinned - released).days >= COOLDOWN_DAYS


def _powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(_powershell() is None, reason="這台電腦沒有 PowerShell")
def test_bootstrap_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    # 根 conftest 的網路封鎖管不到子程序：關掉 pwsh 啟動時的遙測
    monkeypatch.setenv("POWERSHELL_TELEMETRY_OPTOUT", "1")
    script = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{BOOTSTRAP}', [ref]$null, [ref]$errors); "
        "$errors.Count"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", result.stdout
