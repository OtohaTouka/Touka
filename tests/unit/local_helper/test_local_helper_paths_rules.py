"""路徑與檔名規則（ADR-0311）：每條拒絕規則一個案例，用 Windows 的判斷方式在任何系統都測得到。"""

from __future__ import annotations

import pytest
from touka_local.errors import Refused
from touka_local.paths import split_request_path
from touka_local.rules import (
    check_created_file_name,
    check_move_names,
    check_segment_allowed,
    is_protected_name,
    is_secret_name,
    is_unusual_file,
)


@pytest.mark.parametrize(
    ("raw", "root", "parts"),
    [
        ("下載", "下載", ()),
        ("下載/a", "下載", ("a",)),
        ("下載/子資料夾/報告 2026.txt", "下載", ("子資料夾", "報告 2026.txt")),
        ("Work/.hidden", "Work", (".hidden",)),
        ("Work/a.tar.gz", "Work", ("a.tar.gz",)),
        ("Work/console.txt", "Work", ("console.txt",)),
        ("Work/CONTRACT.pdf", "Work", ("CONTRACT.pdf",)),
    ],
)
def test_accepts(raw: str, root: str, parts: tuple[str, ...]) -> None:
    assert split_request_path(raw) == (root, parts)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/",
        "/foo",
        "foo/",
        "foo//bar",
        "foo/.",
        "foo/./bar",
        "foo/..",
        "foo/../bar",
        "..",
        "C:",
        "C:/Windows",
        "foo/C:",
        "C:foo",
        "foo\\bar",
        "\\foo",
        "\\\\server\\share",
        "\\\\?\\C:\\x",
        "foo/a.txt:stream",
        "foo/a.txt::$DATA",
        "foo/CON",
        "foo/con",
        "foo/CON.txt",
        "foo/PRN",
        "foo/AUX",
        "foo/NUL",
        "foo/nul.tar.gz",
        "foo/COM1",
        "foo/COM9.txt",
        "foo/LPT1",
        "foo/COM¹",
        "foo/COM²",
        "foo/COM³.txt",
        "foo/LPT¹",
        "foo/CONIN$",
        "foo/CONOUT$",
        "foo/a<b",
        "foo/a>b",
        'foo/a"b',
        "foo/a|b",
        "foo/a?b",
        "foo/a*b",
        "foo/a\x01b",
        "foo/a\nb",
        "foo/a\x7fb",
        "foo/a\u202eb.txt",
        "foo/a\u200bb",
        "foo/a.",
        "foo/a ",
        "foo/ ",
        "foo/ a",
        "foo/PROGRA~1",
        "foo/LONGNA~2.TXT",
        "foo/" + "a" * 256,
        "x/" * 600,
    ],
)
def test_refuses(raw: str) -> None:
    with pytest.raises(Refused) as caught:
        split_request_path(raw)
    assert caught.value.code == "bad_path"


@pytest.mark.parametrize("value", [None, 1, ["a"], b"a"])
def test_refuses_non_text(value: object) -> None:
    with pytest.raises(Refused):
        split_request_path(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    [
        ".git",
        ".GIT",
        ".Git",
        ".github",
        ".ssh",
        ".aws",
        ".gnupg",
        ".vscode",
        ".idea",
        ".claude",
        ".codex",
        ".agents",
    ],
)
def test_protected_names(name: str) -> None:
    assert is_protected_name(name) is True
    with pytest.raises(Refused) as caught:
        check_segment_allowed(name)
    assert caught.value.code == "protected"


@pytest.mark.parametrize("name", ["git", ".gitignore", "github", "ssh", "a.git", ".gitattributes"])
def test_not_protected(name: str) -> None:
    assert is_protected_name(name) is False
    check_segment_allowed(name)


@pytest.mark.parametrize("name", ["a.txt", "A.TXT", "b.md", "c.log", "d.tar.txt"])
def test_created_file_allowed(name: str) -> None:
    check_created_file_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "a.exe",
        "a.bat",
        "a.cmd",
        "a.ps1",
        "a.lnk",
        "a.html",
        "a.svg",
        "a.json",
        "a.yml",
        "a.yaml",
        "a.csv",
        "a.txt.exe",
        ".txt.bat",
    ],
)
def test_created_file_refused_by_extension(name: str) -> None:
    with pytest.raises(Refused) as caught:
        check_created_file_name(name)
    assert caught.value.code == "file_type"


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE.md",
        "claude.md",
        "CLAUDE.local.md",
        "AGENTS.md",
        "agents.MD",
        "GEMINI.md",
        "SKILL.md",
    ],
)
def test_created_file_refused_by_name(name: str) -> None:
    with pytest.raises(Refused) as caught:
        check_created_file_name(name)
    assert caught.value.code == "file_name"


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("a.txt", "b.txt"),
        ("a.TXT", "b.txt"),
        ("a.tar.gz", "b.gz"),
        ("noext", "other"),
        ("資料夾", "新資料夾"),
    ],
)
def test_move_names_allowed(source: str, destination: str) -> None:
    check_move_names(source, destination)


@pytest.mark.parametrize(
    ("source", "destination", "code"),
    [
        ("a.txt", "a.exe", "extension_changed"),
        ("a.txt", "a.bat", "extension_changed"),
        ("a", "a.exe", "extension_changed"),
        ("a.txt", "a", "extension_changed"),
        ("a.txt", "CLAUDE.txt.md", "extension_changed"),
        ("notes.md", "CLAUDE.md", "file_name"),
        ("notes.md", "agents.md", "file_name"),
        ("notes.md", "Claude.Local.md", "file_name"),
    ],
)
def test_move_names_refused(source: str, destination: str, code: str) -> None:
    with pytest.raises(Refused) as caught:
        check_move_names(source, destination)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".ENV",
        ".env.local",
        ".env.production",
        "server.pem",
        "server.KEY",
        "cert.pfx",
        "cert.p12",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "credentials",
        "credentials.json",
        "vault.kdbx",
        ".netrc",
        ".npmrc",
        ".pypirc",
    ],
)
def test_secret_names(name: str) -> None:
    assert is_secret_name(name) is True


@pytest.mark.parametrize(
    "name",
    [".env.example", ".env.sample", ".env.template", "environment.txt", "keys.txt", "notes.md"],
)
def test_not_secret_names(name: str) -> None:
    assert is_secret_name(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "a.exe",
        "A.EXE",
        "a.bat",
        "a.cmd",
        "a.ps1",
        "a.vbs",
        "a.js",
        "a.msi",
        "a.lnk",
        "a.scr",
        "a.com",
        "a.jar",
        "a.reg",
        "a.wsf",
    ],
)
def test_unusual_files(name: str) -> None:
    assert is_unusual_file(name) is True


@pytest.mark.parametrize("name", ["a.txt", "a.pdf", "photo.jpg", "noext"])
def test_ordinary_files(name: str) -> None:
    assert is_unusual_file(name) is False
