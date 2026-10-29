"""聊天上傳文件服務（Phase 2，A1）。

職責：驗證上傳文件（magic-byte 家族 + 大小），存進**當下 agent 自己的 vault**
``<personal_vault>/outputs/uploads/{session}/{name}``，回相對路徑供前端注入「請 AI 用工具讀」提示。

安全：
- 不信任 client Content-Type；以 magic-byte 家族 + 副檔名白名單判定（pdf / office-zip / text）。
- 一切實體 I/O 走 ResourceManager 的區內方法（ADR-0293）；session/filename 為使用者影響值，
  逐段 sanitize 後當成區內路徑的一段——不可逃出該 agent 的 uploads 目錄（→ 永不碰別的 agent
  資料），路上任何一段是連結就拒絕（UPLOAD_FAILED）。
- 路徑由 ``identity`` 衍生（``uploads_dir``）→ 物理上只寫該 agent 自己 vault（無 IDOR）。
- 檔案僅存放、不在伺服器解析；由該 agent 的 bash/python 或 personal_drive read 工具讀取。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server.utils.no_follow import WalkReport
from memory_server.utils.zone_paths import ZoneSegmentError

from src.config import UPLOAD_AGENT_QUOTA_BYTES, UPLOAD_MAX_BYTES
from src.core.exceptions import ErrorCode, ToukaException
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from memory_server.utils.zone_paths import ZonePath

    from src.models.identity import AgentIdentity

# 副檔名 → media type 白名單（zip-based office 以 PK magic + 副檔名雙重判定）。
_OFFICE: dict[str, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_TEXT_EXTS: frozenset[str] = frozenset({
    "txt", "md", "markdown", "csv", "tsv", "log", "json", "xml", "yaml", "yml", "toml", "ini",
    "html", "htm", "css", "js", "jsx", "ts", "tsx", "py", "go", "rs", "java", "c", "h", "cpp",
    "sh", "sql", "vue", "svelte",
})


def _ext_of(filename: str) -> str:
    name = Path(filename).name
    i = name.rfind(".")
    return name[i + 1:].lower() if i > 0 else ""


def detect_document(data: bytes, filename: str) -> tuple[str, str] | None:
    """以 magic-byte 家族 + 副檔名白名單判定文件型別；回 (ext, media_type) 或 None（不支援）。"""
    ext = _ext_of(filename)
    if data[:5] == b"%PDF-":
        return ("pdf", "application/pdf")
    if data[:4] == b"PK\x03\x04":  # zip 容器（docx/xlsx/pptx）
        return (ext, _OFFICE[ext]) if ext in _OFFICE else None
    if ext in _TEXT_EXTS:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return (ext, "text/plain; charset=utf-8")
    return None


def validate_upload_bytes(data: bytes, filename: str) -> tuple[str, str]:
    """驗證大小與型別；回 (ext, media_type)，否則拋對應 ToukaException。"""
    if len(data) > UPLOAD_MAX_BYTES:
        raise ToukaException(ErrorCode.UPLOAD_TOO_LARGE)
    detected = detect_document(data, filename)
    if detected is None:
        raise ToukaException(ErrorCode.UPLOAD_UNSUPPORTED_TYPE)
    return detected


def _assert_safe_agent_segment(seg: str) -> None:
    """agent_id 必須是單一安全路徑段，否則拒（複審 High）。

    agent_id 直接用於組路徑（get_vault_path），``..``/路徑分隔符會讓上傳落點逃出該 agent vault、
    寫進別人資料。縱使上游 auth 目前擋下惡意 X-Agent-ID，此處仍為必要縱深防禦：base 由 agent_id
    衍生，``is_relative_to`` 無法自證。
    """
    name = (seg or "").strip()
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or Path(name).name != name
    ):
        raise ToukaException(ErrorCode.AUTH_UNAUTHORIZED_ACCESS)


def _safe_seg(raw: str) -> str:
    """把使用者影響的 session 字串淨化為單段安全目錄名（無路徑分隔/穿越/隱藏；限長 64）。

    空/未知 session → 固定 ``_misc`` 桶（不再每次 mint uuid，避免無界子目錄；複審 Low）。
    """
    seg = re.sub(r"[^A-Za-z0-9._-]", "_", (raw or "").strip()).lstrip(".")[:64]
    return seg or "_misc"


def _safe_filename(raw: str, ext: str) -> str:
    """取檔名 basename 並淨化；空則以 ext 保底命名（限長 128，複審 Info）。"""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw or "").name).lstrip(".")[:128]
    return base or f"upload.{ext}"


def _uploads_dir_bytes(base: ZonePath) -> int:
    """該 agent uploads 目錄現有總位元組（per-agent 配額用；不進入連結）。"""
    entries = resource_manager.zone_walk(base, WalkReport())
    return sum(entry.info.st_size for entry in entries if entry.kind == "file")


def save_upload(
    identity: AgentIdentity, session_id: str, filename: str, data: bytes
) -> dict[str, str | int]:
    """驗證並儲存上傳檔到當下 agent 自己的 vault；回 ``{filename, relpath, mime, size}``。

    ``relpath`` 為相對於該 agent personal vault 的路徑（``outputs/uploads/{session}/{name}``），
    供前端注入「用 read 工具讀全文 / python 解析」提示——該 agent 的工具即以此相對路徑存取。
    """
    _assert_safe_agent_segment(identity.agent_id)  # ① agent_id 須為安全單段（防跨 agent 穿越）
    ext, media = validate_upload_bytes(data, filename)
    base = resource_manager.uploads_dir(identity)
    if base is None:
        raise ToukaException(ErrorCode.UPLOAD_FAILED)
    seg = _safe_seg(session_id)
    name = _safe_filename(filename, ext)
    try:
        # ③ 區內路徑：每段只能是一段名稱（Windows 的 ``CON.md`` 這類也拒絕）
        target = base.child(seg, name)
        # ② per-agent 配額：現有 + 本次超過上限即拒（防已驗證者塞爆磁碟）。
        if _uploads_dir_bytes(base) + len(data) > UPLOAD_AGENT_QUOTA_BYTES:
            raise ToukaException(ErrorCode.UPLOAD_TOO_LARGE)
        resource_manager.zone_write_binary(target, data)
    except (OSError, ZoneSegmentError) as e:  # 含連結（LinkRefusedError 是 OSError）
        raise ToukaException(ErrorCode.UPLOAD_FAILED) from e
    return {
        "filename": name,
        "relpath": f"outputs/uploads/{seg}/{name}",
        "mime": media,
        "size": len(data),
    }
