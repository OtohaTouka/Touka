"""測試用的資料夾連結：POSIX 建 symlink，這台 Windows 不能建 symlink 時改建 junction。

只在 pytest 的 ``tmp_path`` 裡用；連結另一端是測試自己建的假設定，不碰 ``data/``。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

if sys.platform == "win32":
    import _winapi

# 連結測試「跟隨了連結」那幾條斷言的失敗訊息（ADR-0293 R15：在舊版上跑時用它確認紅的原因）
LINK_FOLLOWED = "LINK_FOLLOWED"


def make_dir_link(link: Path, target: Path) -> None:
    """在 ``link`` 建一個指向資料夾 ``target`` 的連結（``link`` 不能已存在）。"""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        _winapi.CreateJunction(str(target), str(link))
