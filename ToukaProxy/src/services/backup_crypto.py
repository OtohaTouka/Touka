"""ADR-0108：備份下載時即時加密。

把伺服器端的**明文**備份 zip 重新封裝成 WinZip-AES-256 加密 zip（pyzipper），供管理員下載到
異地保存——可被 7-Zip / WinRAR 直接以密碼開啟，無需專屬工具。明文 zip 永不以明文離開伺服器；
還原仍走伺服器端明文檔（Phase 1 流程不變）。

IOPrison：本模組僅用 `tempfile.mkstemp` / `os.close` / `zipfile.ZipFile` / `pyzipper.AESZipFile`
等（非 IOPrison 禁令清單中的 open/mkdir/unlink），暫存檔清理走 ResourceManager（SSOT）。
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

import pyzipper

from src.services.resource_manager import resource_manager


def encrypt_backup_zip(plain_zip: Path, passphrase: str) -> Path:
    """把 ``plain_zip`` 的每個條目重新寫入一個**新的** AES-256 加密 zip（flat 結構）。

    回傳暫存加密檔的路徑；呼叫端負責於串流結束後刪除（建議用 FileResponse 的
    ``background=resource_manager.remove_file``）。**加密中途若拋例外，本函式會先清掉半成品
    暫存再上拋**，避免孤兒檔/磁碟洩漏（外層 FileResponse 的背景清理在失敗路徑不會執行）。

    逐條目讀取（非整檔載入），記憶體上限為最大單一條目。flat re-zip（非巢狀）→ 開啟即直接見
    ``.env`` / ``data/`` / ``config/``，無多餘巢狀層。
    """
    fd, name = tempfile.mkstemp(prefix="touka_enc_", suffix=".zip")
    os.close(fd)  # pyzipper 以路徑自行開啟；先關閉 fd，Windows 上失敗時才能 unlink。
    out_path = Path(name)
    try:
        with zipfile.ZipFile(plain_zip, "r") as src, pyzipper.AESZipFile(
            out_path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as enc:
            enc.setpassword(passphrase.encode("utf-8"))
            enc.setencryption(pyzipper.WZ_AES, nbits=256)
            for info in src.infolist():
                if info.is_dir():
                    continue
                enc.writestr(info.filename, src.read(info.filename))
    except Exception:
        resource_manager.remove_file(out_path)  # 半成品暫存清除（SSOT，IOPrison 合規）
        raise
    return out_path
