"""路徑與檔名規則（ADR-0311）：每條拒絕規則一個案例，用 Windows 的判斷方式在任何系統都測得到。"""

from __future__ import annotations

import pytest
from touka_local.errors import Refused
from touka_local.paths import (
    is_hidden_character,
    is_misleading_character,
    is_too_long,
    split_request_path,
)
from touka_local.rules import (
    check_created_file_name,
    check_move_names,
    check_new_segments,
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


_VARIATION_SELECTORS = {0xFE00, 0xFE0F, 0xE0100}


@pytest.mark.parametrize(
    "code_point",
    [
        0x00AD,
        0x034F,
        0x061C,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        0x180B,
        0x180E,
        0x180F,
        0x200B,
        0x200D,
        0x202E,
        0x2060,
        0x2065,
        0x206F,
        0x3164,
        0xFE00,
        0xFE0F,
        0xFEFF,
        0xFFA0,
        0xFFF0,
        0xFFF8,
        0x1BCA0,
        0x1BCA3,
        0x1D173,
        0x1D17A,
        0xE0000,
        0xE0100,
        0xE0FFF,
    ],
)
def test_default_ignorable_characters_are_hidden(code_point: int) -> None:
    assert is_hidden_character(chr(code_point)) is True
    if code_point in _VARIATION_SELECTORS:
        return  # 既有檔名要能用，見下一條
    with pytest.raises(Refused) as caught:
        split_request_path(f"Open/a{chr(code_point)}b.txt")
    assert caught.value.code == "bad_path"


@pytest.mark.parametrize("code_point", sorted(_VARIATION_SELECTORS))
def test_existing_names_can_carry_variation_selectors(code_point: int) -> None:
    name = f"\u2764{chr(code_point)}.txt"
    assert split_request_path(f"Open/{name}") == ("Open", (name,))


@pytest.mark.parametrize(
    "character", ["a", "字", "ア", "한", "-", "_", "(", "😀", "\u034e", "\ufe10"]
)
def test_ordinary_characters_are_not_hidden(character: str) -> None:
    assert is_hidden_character(character) is False


@pytest.mark.parametrize(
    "character",
    [
        "\uff0f",
        "\uff3c",
        "\u2044",
        "\u2215",
        "\u29f8",
        "\u29f9",
        "\u2216",
        "\ufe68",
        "\uff5c",
        "\u2223",
        "\u01c0",
        "\u2758",
        "\uffe8",
        "\u2571",
        "\u2572",
        "\u00a0",
        "\u2003",
        "\u3000",
        "\u202f",
    ],
)
def test_lookalike_characters_are_misleading(character: str) -> None:
    assert is_misleading_character(character) is True


@pytest.mark.parametrize("character", [" ", "a", "字", "-", "_", ".", "(", "、"])
def test_ordinary_characters_are_not_misleading(character: str) -> None:
    assert is_misleading_character(character) is False


@pytest.mark.parametrize("name", ["a.{645FF040-5081-101B-9F08-00AA002F954E}", "a.{x}", "A.{}"])
def test_shell_folder_suffix_is_refused(name: str) -> None:
    with pytest.raises(Refused) as caught:
        split_request_path(f"Open/{name}")
    assert caught.value.code == "bad_path"


@pytest.mark.parametrize("name", ["a{b}.txt", "{x}", "a.{b}.txt"])
def test_braces_elsewhere_are_fine(name: str) -> None:
    assert split_request_path(f"Open/{name}") == ("Open", (name,))


@pytest.mark.parametrize(
    ("text", "too_long"),
    [
        ("a" * 254, False),
        ("a" * 255, True),
        ("😀" * 127, False),
        ("😀" * 128, True),
    ],
)
def test_length_counts_utf16_units_and_leaves_room_for_the_git_probe(
    text: str, too_long: bool
) -> None:
    assert is_too_long(text) is too_long


@pytest.mark.parametrize(
    "parts",
    [
        (".cursor",),
        ("a", ".vscode", "b.txt"),
        (".hidden.txt",),
        ("a\uff0fb.txt",),
        ("a\u3000b.txt",),
        ("x", "y\u2215z"),
        ("\u2764\ufe0f.txt",),
        ("a\u200bb.txt",),
    ],
)
def test_new_paths_refuse_dot_names_and_lookalikes(parts: tuple[str, ...]) -> None:
    with pytest.raises(Refused) as caught:
        check_new_segments(parts)
    assert caught.value.code == "file_name"


@pytest.mark.parametrize(
    "parts",
    [
        ("a",),
        ("a", "b.txt"),
        ("a.b", "c d.md"),
        ("報告", "一.txt"),
        ("報告\u30002024", "x.txt"),
        ("影片\uff5c頻道", "x.txt"),
    ],
)
def test_new_paths_allow_ordinary_names(parts: tuple[str, ...]) -> None:
    check_new_segments(parts)


@pytest.mark.parametrize("name", ["AGENTS.override.md", "agent.md", "CMakeLists.txt"])
def test_more_files_tools_read_automatically_are_refused(name: str) -> None:
    with pytest.raises(Refused) as caught:
        check_created_file_name(name)
    assert caught.value.code == "file_name"
    with pytest.raises(Refused) as caught:
        check_move_names(f"x{name}", name)
    assert caught.value.code == "file_name"


@pytest.mark.parametrize(
    "name",
    ["_netrc", ".envrc", ".htpasswd", "home.ovpn", "prod.env", ".ENV", "PROD.ENV", "a.Ovpn"],
)
def test_more_secret_names(name: str) -> None:
    assert is_secret_name(name) is True


@pytest.mark.parametrize("name", [".kube", ".docker", ".KUBE"])
def test_more_protected_folders(name: str) -> None:
    assert is_protected_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "a.py",
        "a.pyw",
        "a.url",
        "a.html",
        "a.htm",
        "a.chm",
        "a.msc",
        "a.inf",
        "a.docm",
        "a.xlsm",
        "a.ocx",
        "a.sys",
        "a.appinstaller",
        "a.library-ms",
        ".exe",
        ".BAT",
    ],
)
def test_more_unusual_files(name: str) -> None:
    assert is_unusual_file(name) is True


@pytest.mark.parametrize("name", ["影片\uff5c頻道.mp4", "a\u3000b.txt", "\u2764\ufe0f.txt"])
def test_a_name_that_is_kept_is_not_new(name: str) -> None:
    check_new_segments(("box", name), kept_name=name)
    with pytest.raises(Refused):
        check_new_segments(("box", name), kept_name="other.txt")


def test_a_kept_name_still_cannot_start_with_a_dot() -> None:
    with pytest.raises(Refused):
        check_new_segments((".a.txt",), kept_name=".a.txt")
