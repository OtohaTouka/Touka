"""清除連備份：作業系統自己放進升級備份資料夾的雜檔（縮圖快取、資料夾設定）.

名字在這份清單上（不分大小寫、只限 ASCII）**而且**內容不是 SQLite、也不含目標 ID 的位元組，
才當成雜檔：現場資料夾的記成資訊、快照裡的照抄。其餘一律照舊 fail closed。快照的獨立驗證只用
這裡的常數自己重算，不呼叫 is_os_metadata_content。（ADR-0270 補記 2026-10-28）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

OS_METADATA_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
SQLITE_HEADER = b"SQLite format 3\x00"
# 目標 ID 的幾種寫法：desktop.ini 常是 UTF-16LE，.DS_Store 的檔名是 UTF-16BE
ID_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
MAX_OS_FILE_BYTES = 4 * 1024 * 1024  # 雜檔都很小；超過就不當雜檔


def is_os_metadata_name(name: str) -> bool:
    """名字是清單上的雜檔名（不分大小寫；非 ASCII 的一律不算，避免折疊後才相同）."""
    return name.isascii() and name.lower() in OS_METADATA_NAMES


def id_needles(identity: AgentIdentity) -> tuple[bytes, ...]:
    """目標 ID 在 ID_ENCODINGS 各種寫法下的位元組."""
    return tuple(identity.agent_id.encode(enc, "surrogatepass") for enc in ID_ENCODINGS)


def is_os_metadata_content(content: bytes, identity: AgentIdentity) -> bool:
    """內容可以當雜檔：不超過上限、不是 SQLite 檔頭、不含目標 ID 的任何一種寫法."""
    if len(content) > MAX_OS_FILE_BYTES or content.startswith(SQLITE_HEADER):
        return False
    return not any(needle in content for needle in id_needles(identity))
