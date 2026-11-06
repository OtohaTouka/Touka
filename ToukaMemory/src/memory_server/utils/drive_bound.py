r"""解析路徑之前，先用字面比對目標和根的磁碟或網路分享（sec-9 PR 1b）.

Windows 上 ``resolve()``、``exists()`` 一個 ``\\主機\分享`` 路徑時，系統會先連到那台主機，
可能把登入資訊送出去。這裡只看字串，不碰檔案系統：目標拼在根底下之後，磁碟代號或分享開頭
（``drive``）和根不同就不收。Linux 的 ``drive`` 恆為空字串，所以結果恆為 True。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import PurePath


def same_drive(target: str, root: str, flavour: type[PurePath] = Path) -> bool:
    """``target``（相對時接在 ``root`` 底下）和 ``root`` 是否在同一個磁碟或分享上（不分大小寫）."""
    root_path = flavour(root)
    literal = root_path / flavour(target)
    return literal.drive.casefold() == root_path.drive.casefold()
