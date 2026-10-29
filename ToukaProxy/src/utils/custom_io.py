"""自定義模型物理看守者 (CustomModelIO).

強制執行身分主權，確保 Level 2 自定義模型 (data/custom/) 的實體檔案操作安全。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server.utils.zone_paths import Anchor, ZoneSegmentError

from src.config import get_app_data_dir  # noqa: TID251
from src.services.resource_manager import resource_manager
from src.utils.path_security import validate_no_symlink_escape

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


def _safe_target(target_dir: Path, filename: str) -> Path | None:
    """Resolve ``filename`` under ``target_dir`` and confirm it stays in the sandbox.

    #7-class fix: ``write_custom_file`` used ``str(target.resolve()).startswith(...)``
    (sibling-prefix bypass), while ``read_custom_file`` / ``custom_file_exists`` had NO
    boundary check at all. All three now resolve both sides via
    ``validate_no_symlink_escape`` (``is_relative_to``). Returns None if outside.
    """
    target = target_dir / filename
    if not validate_no_symlink_escape(target, target_dir):
        logger.warning("🚫 Custom-model sandbox violation: %s", filename)
        return None
    return target.resolve()


def is_plain_custom_name(name: str) -> bool:
    """自訂模型 ID 只能是單層名稱：不含斜線、反斜線或 ``..``（擋路徑穿越，ADR-0245 修補）。"""
    return name not in {"", "."} and not any(part in name for part in ("/", "\\", ".."))


def _get_base_dir() -> Path:
    """獲取自定義模型的基礎物理目錄。"""
    return Path(get_app_data_dir()) / "custom"


def get_custom_dir(identity: AgentIdentity) -> Path | None:
    """獲取該身分對應的合法物理目錄。"""
    path_str = resource_manager.get_vault_path("custom_soul", identity)
    return Path(path_str) if path_str else None


def write_custom_file(identity: AgentIdentity, filename: str, content: str) -> bool:
    """寫入自定義模型檔案。"""
    target_dir = get_custom_dir(identity)
    if not target_dir:
        return False

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # 安全檢查：確保檔案位於 target_dir 內（is_relative_to，擋 sibling-prefix 繞過）
        target = _safe_target(target_dir, filename)
        if target is None:
            return False

        target.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception(
            "Failed to write custom file %s for %s", filename, identity.agent_id
        )
        return False
    else:
        return True


def read_custom_file(identity: AgentIdentity, filename: str) -> str | None:
    """讀取自定義模型檔案。"""
    target_dir = get_custom_dir(identity)
    if not target_dir:
        return None

    target = _safe_target(target_dir, filename)
    if target is None or not target.exists():
        return None

    try:
        return target.read_text(encoding="utf-8")
    except Exception:
        logger.exception(
            "Failed to read custom file %s for %s", filename, identity.agent_id
        )
        return None


def delete_custom_dir(identity: AgentIdentity) -> bool:
    """刪除整個自定義模型資料夾（不跟隨連結，ADR-0293 R5）；沒有或刪不掉回 False。"""
    try:
        return resource_manager.remove_custom_model_dir(identity)
    except Exception:
        logger.exception("Failed to delete custom dir for %s", identity.agent_id)
        return False


def list_custom_ids() -> list[str]:
    """列出所有已存在的自定義模型 ID。"""
    base = _get_base_dir()
    if not base.exists():
        return []
    return [d.name for d in base.iterdir() if d.is_dir()]


def move_custom_dir(old_id: str, new_id: str) -> bool:
    """改名自定義模型資料夾：只改名、不複製，舊名是連結就不改（ADR-0293 R10）。

    兩個名字都必須是單層名稱（擋 ``../`` 路徑穿越）；新名已被占用、舊名不在或出錯回 False。
    只差大小寫（Windows 不分大小寫）照常改名。
    """
    try:
        resource_manager.rename_agent_container(Anchor.CUSTOM_DRIVE, old_id, new_id)
    except ZoneSegmentError:
        logger.warning(
            "🚫 Custom-model sandbox violation on move: %s -> %s", old_id, new_id
        )
        return False
    except Exception:
        logger.exception("Failed to move custom model %s to %s", old_id, new_id)
        return False
    return True


def custom_path_exists(model_id: str) -> bool:
    """custom/ 底下這個名字有沒有東西（資料夾或檔案）；逃出沙箱的名字一律當沒有。"""
    base = _get_base_dir()
    path = base / model_id
    return validate_no_symlink_escape(path, base) and path.exists()


def custom_file_exists(identity: AgentIdentity, filename: str) -> bool:
    """檢查自定義模型檔案是否存在。"""
    target_dir = get_custom_dir(identity)
    if not target_dir:
        return False
    target = _safe_target(target_dir, filename)
    return target is not None and target.exists()
