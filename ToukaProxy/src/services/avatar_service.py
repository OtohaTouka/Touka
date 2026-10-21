"""使用者頭像檔案服務 (ADR-0106).

職責：驗證上傳影像（magic-byte + 大小）、以使用者 id 為單一正規檔名儲存於
``<app_data>/avatars/{user_id}.{ext}``、提供尋檔（給公開服務端點）與刪除（清理）。

安全：
- 不信任用戶端的 Content-Type / 檔名；副檔名由 magic-byte 決定（禁 SVG 等一切非白名單）。
- 一切實體 I/O 走 ResourceManager（IOPrison）；路徑以 ``is_relative_to`` 防穿越。
- 單檔/人——重傳前先刪舊（含換格式），杜絕孤兒檔。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.config import AVATAR_MAX_BYTES
from src.core.exceptions import ErrorCode, ToukaException
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from pathlib import Path

# magic-byte → (副檔名, media type)。僅靜態點陣白名單；SVG/其餘一律拒。
_KNOWN: tuple[tuple[str, str], ...] = (
    ("png", "image/png"),
    ("jpg", "image/jpeg"),
    ("webp", "image/webp"),
)


def detect_image(data: bytes) -> tuple[str, str] | None:
    """以 magic-byte 偵測影像格式；回 (ext, media_type) 或 None（不支援）。"""
    if data[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("png", "image/png")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return None


def validate_avatar_bytes(data: bytes) -> tuple[str, str]:
    """驗證大小與格式；回 (ext, media_type)，否則拋對應 ToukaException。"""
    if len(data) > AVATAR_MAX_BYTES:
        raise ToukaException(ErrorCode.AVATAR_TOO_LARGE)
    detected = detect_image(data)
    if detected is None:
        raise ToukaException(ErrorCode.AVATAR_INVALID_FORMAT)
    return detected


def avatars_dir() -> Path:
    """頭像目錄（位於 app data 根，納入備份——見 manage_backup SANCTUARY_FOLDERS）。

    經 resource_manager 取得路徑（PATH_SSOT / TID251），不直接呼叫 get_app_data_dir。
    """
    return resource_manager.get_avatars_dir()


def _safe_candidate(user_id: str, ext: str) -> Path | None:
    """組出 {user_id}.{ext} 路徑並驗證未逃出 avatars 目錄（防惡意 user_id 穿越）。"""
    base = avatars_dir().resolve()
    candidate = (avatars_dir() / f"{user_id}.{ext}").resolve()
    if not candidate.is_relative_to(base):
        return None
    return candidate


def find_avatar(user_id: str) -> tuple[Path, str] | None:
    """尋找該使用者現有頭像檔；回 (path, media_type) 或 None。"""
    for ext, media in _KNOWN:
        p = _safe_candidate(user_id, ext)
        if p is not None and p.exists():
            return (p, media)
    return None


def delete_avatar(user_id: str) -> None:
    """刪除該使用者所有可能格式的頭像檔（清理；缺檔無妨）。"""
    for ext, _media in _KNOWN:
        p = _safe_candidate(user_id, ext)
        if p is not None and p.exists():
            resource_manager.remove_file(p)


def save_avatar(user_id: str, data: bytes) -> str:
    """驗證並儲存頭像；回正規副檔名。

    AV-001：**先寫新檔成功、再刪其他格式舊檔**（不可先刪後寫）——若寫入失敗（磁碟滿/
    權限），舊頭像保持完好，不會因失敗的上傳而永久遺失。換格式時清掉舊格式檔→杜絕孤兒。
    """
    ext, _media = validate_avatar_bytes(data)
    target = _safe_candidate(user_id, ext)
    if target is None:
        raise ToukaException(ErrorCode.AVATAR_INVALID_FORMAT)
    try:
        resource_manager.write_file_binary(target, data)
    except Exception as e:
        raise ToukaException(ErrorCode.AVATAR_WRITE_FAILED) from e
    # 寫入成功後，移除其他格式的舊檔（換格式情境）；保留剛寫入的 target。
    for other_ext, _m in _KNOWN:
        if other_ext == ext:
            continue
        stale = _safe_candidate(user_id, other_ext)
        if stale is not None and stale.exists():
            resource_manager.remove_file(stale)
    return ext
