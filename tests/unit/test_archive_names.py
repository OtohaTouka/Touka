"""zip 裡的名稱（``utils.archive_names``，ADR-0332 PR B；禁令 18 的純函式）.

- 不能放的原因：git 的資料夾（含 Windows、macOS 當成它的寫法）、會讓資料夾變成 repository 的、
  Windows 不能用的、雙向控制字元、捷徑類檔案、``.env``；其他照常放。
- 大小寫的比對鍵；報告裡的顯示（跳脫、截短）。
"""

from __future__ import annotations

import pytest
from src.utils import archive_names
from src.utils.archive_names import case_key, name_problem, shown, shown_raw

PROBLEMS = {
    archive_names.GIT_FOLDER: [".git/config", "a/.GIT/x", "git~1/x", ".git‌/x", "a/.git. /x"],
    archive_names.VCS_METADATA: [
        "HEAD", "bare/head", "x/HEAD.", ".hg/hgrc", "a/.Svn/x", ".bzr/x", "_darcs/x", ".jj/x",
        ".sl/config", ".pijul/config", ".fslckout", "a/_FOSSIL_", "HG~1/hgrc", "svn~2/x",
        "PIJUL~1/config", "FSLCKO~1",
    ],
    archive_names.BAD_NAME: ["a:b", "CON", "lpt1.txt", "x.", "x ", "a\\b", "c\x01", "", "a/../b"],
    archive_names.BIDI: ["evil‮txt.exe", "a/⁦b"],
    archive_names.SHORTCUT: ["desktop.ini", "x.LNK", "a.themepack", "k/.directory", "app.desktop"],
    archive_names.ENV_FILE: [".env", "a/.env.local"],
}


@pytest.mark.parametrize(
    ("path", "problem"),
    [(path, problem) for problem, paths in PROBLEMS.items() for path in paths],
)
def test_names_that_must_not_reach_a_zip(path, problem):
    assert name_problem(path.split("/")) == problem


@pytest.mark.parametrize(
    "path",
    ["README.md", ".env.example", "src/head.py", "HEADER", "objects/x", "refs/heads/main",
     "config", ".github/workflows/ci.yml", "gitlab/x", "docs/日本語.md", "hg/x", "slides/a",
     "svn_notes.md", "desktop/app.py"],
)
def test_ordinary_names_are_fine(path):
    assert name_problem(path.split("/")) is None


def test_case_keys_match_what_a_case_insensitive_disk_treats_as_one():
    assert case_key("README.md") == case_key("readme.MD")
    assert case_key("é.txt") == case_key("é.txt")  # NFD 和 NFC
    assert case_key("a.txt") != case_key("b.txt")


def test_shown_names_escape_what_could_trick_a_reader():
    assert shown("a\nb‮c\\d") == "a\\u000ab\\u202ec\\\\d"
    assert shown("\U000e0001") == "\\U000e0001"
    assert shown_raw(b"bad\xff.txt") == "bad\\xff.txt"


def test_long_shown_names_are_cut():
    text = "x" * (archive_names.MAX_SHOWN_CHARS + 10)
    assert shown(text) == "x" * archive_names.MAX_SHOWN_CHARS + "..."
    assert shown("x" * archive_names.MAX_SHOWN_CHARS) == "x" * archive_names.MAX_SHOWN_CHARS
