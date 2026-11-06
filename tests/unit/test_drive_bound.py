"""檔案工具解析路徑之前，先用字面比對目標和根的磁碟或網路分享（sec-9 PR 1b）。

Windows 上 ``resolve()`` 一個 ``\\\\主機\\分享`` 路徑會先連出去；``same_drive`` 不碰檔案系統，
在解析之前就擋掉不同磁碟或分享的目標。用 ``PureWindowsPath`` 參數化，Linux CI 也測得到；
Linux 的 ``drive`` 恆為空字串，行為不變。純函式，不碰任何檔案。
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest
from memory_server.utils.drive_bound import same_drive

WIN_ROOT = "C:\\proj"  # noqa: PATH_LITERAL_SSOT - 純字串比對的測試資料，不碰檔案系統
SHARE_ROOT = "\\\\fileserver\\team\\proj"


@pytest.mark.parametrize(
    "target",
    [
        "\\\\192.0.2.1\\share\\x",
        "//192.0.2.1/share/x",
        "\\/192.0.2.1/share/x",
        "\\\\?\\C:\\x",
        "D:\\x",  # noqa: PATH_LITERAL_SSOT - 純字串比對的測試資料
        "D:x",
    ],
)
def test_other_drives_and_shares_are_refused(target):
    assert same_drive(target, WIN_ROOT, flavour=PureWindowsPath) is False


@pytest.mark.parametrize(
    "target",
    [
        "\\??\\UNC\\192.0.2.1\\share\\x",
        "/??/UNC/192.0.2.1/share/x",
        "/??\\UNC\\192.0.2.1\\share\\x",
        "\\??\\D:\\x",
        "\\??\\C:\\proj\\x",
        "\\??\\PIPE\\x",
    ],
)
def test_nt_paths_are_refused_even_on_the_same_drive(target):
    """``\\??\\`` 開頭會原樣交給系統，看不出會到哪個磁碟或分享，一律不收."""
    assert same_drive(target, WIN_ROOT, flavour=PureWindowsPath) is False


@pytest.mark.parametrize(
    "target",
    [
        "sub\\x",
        "C:x",
        "c:\\proj\\x",  # noqa: PATH_LITERAL_SSOT - 純字串比對的測試資料
        "\\rooted\\x",
        "..\\..\\x",
    ],
)
def test_same_drive_is_accepted(target):
    assert same_drive(target, WIN_ROOT, flavour=PureWindowsPath) is True


def test_a_project_on_a_share_accepts_the_same_share_only():
    assert same_drive("\\\\FileServer\\Team\\proj\\x", SHARE_ROOT, flavour=PureWindowsPath)
    assert same_drive("sub\\x", SHARE_ROOT, flavour=PureWindowsPath)
    assert not same_drive("\\\\fileserver\\other\\x", SHARE_ROOT, flavour=PureWindowsPath)
    assert not same_drive(WIN_ROOT, SHARE_ROOT, flavour=PureWindowsPath)


@pytest.mark.parametrize("target", ["/abs/x", "rel/x", "\\\\not-a-share-on-linux"])
def test_posix_paths_have_no_drive(target):
    assert same_drive(target, "/srv/proj", flavour=PurePosixPath) is True
