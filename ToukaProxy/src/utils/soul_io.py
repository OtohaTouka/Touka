"""靈魂檔案 (Soul) IO 看守者。

負責代理人靈魂核心檔案 (soul.md, config.json 等) 的讀取、寫入、備份與刪除。
強制執行身份主權邊界，防止目錄穿越。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from memory_server.utils.zone_paths import (
    ALL_ANCHORS,
    contains_zone,
    current_roots,
    is_zone_segment,
    zone_of,
)

from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager
from src.utils.path_security import validate_no_symlink_escape

logger = logging.getLogger(__name__)

# 靈魂實體儲存的三個 base（create 建立／刪除清理的完整集合）。
_SOUL_STORAGE_BASES = ("soul", "legacy", "personal_drive")


def _safe_target(base: Path, filename: str) -> Path | None:
    """Resolve ``filename`` under ``base`` and confirm it stays in the sandbox.

    #7-class fix: the prior ``str(target).startswith(str(base))`` check left ``base``
    un-resolved and was defeated by a sibling-prefix dir (``/soul/AgentX-evil`` starts
    with ``/soul/AgentX``). ``validate_no_symlink_escape`` resolves BOTH sides and uses
    ``is_relative_to``, also blocking symlink/``..`` escapes. Returns None if outside.

    ADR-0293 PR 2：``filename`` 來自模型，只收單層名稱（擋 ``sub/x``）；``base/filename`` 也不能
    在代理人資料夾裡或是它的根（擋自訂模型靈魂根 ``custom/<id>`` 底下的 ``drive``）。靈魂根的
    區外部分只有 Touka 寫得到，單層名稱與它的 ``.tmp``、``.bak`` 都在區外。
    """
    if not is_zone_segment(filename):
        logger.warning("🚫 Soul file name is not a single level: %r", filename)
        return None
    target = base / filename
    roots = current_roots()
    in_zone = zone_of(target, roots, anchors=ALL_ANCHORS) is not None
    if in_zone or contains_zone(target, roots, anchors=ALL_ANCHORS):
        logger.warning("🚫 Soul file is in an agent folder: %s", filename)
        return None
    if not validate_no_symlink_escape(target, base):
        logger.warning("🚫 Soul sandbox violation: %s", filename)
        return None
    return target.resolve()


def _get_soul_base(identity: AgentIdentity) -> Path | None:
    """獲取代理人的靈魂根目錄。"""
    path_str = resource_manager.get_vault_path("soul", identity)
    if not path_str:
        # 如果是 Level 2 模型，可能會在 custom_soul
        path_str = resource_manager.get_vault_path("custom_soul", identity)

    return Path(path_str) if path_str else None


def list_soul_files(identity: AgentIdentity) -> list[str]:
    """列出靈魂目錄下的所有檔案。"""
    base = _get_soul_base(identity)
    if not base or not base.exists():
        return []
    return [f.name for f in base.iterdir() if f.is_file()]


def read_soul_file(identity: AgentIdentity, filename: str) -> str | None:
    """讀取指定的靈魂檔案內容。"""
    base = _get_soul_base(identity)
    if not base:
        return None

    target = _safe_target(base, filename)
    if target is None or not target.exists():
        return None

    try:
        return target.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read soul file %s", filename)
        return None


def write_soul_file(identity: AgentIdentity, filename: str, content: str) -> bool:
    """安全地寫入靈魂檔案（包含自動備份與原子寫入）。"""
    base = _get_soul_base(identity)
    if not base:
        return False

    target = _safe_target(base, filename)
    if target is None:
        return False

    try:
        base.mkdir(parents=True, exist_ok=True)
        # 如果檔案已存在，先備份
        if target.exists():
            backup_path = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(str(target), str(backup_path))

        # 原子寫入
        temp_file = target.with_suffix(target.suffix + ".tmp")
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(target)
    except Exception:
        logger.exception("Failed to write soul file %s", filename)
        return False
    else:
        return True


def write_soul_json(identity: AgentIdentity, filename: str, data: Any) -> bool:
    """以 JSON 序列化寫入靈魂檔案（DRY：dumps → write_soul_file 同一受認可通道）。"""
    content = json.dumps(data, indent=4, ensure_ascii=False)
    return write_soul_file(identity, filename, content)


def remove_soul_storage_dir(base_name: str, agent_id: str) -> str:
    """刪除單一儲存 base（soul／legacy／personal_drive）下的 <agent_id> 目錄。

    Soul Protection（resource_manager.is_protected_path）對 soul/legacy 下的刪除**無差別**
    raise——本函式是「人類發起的合法管理操作」（create 失敗回滾／admin 刪除靈魂）的特權
    通道，僅存在於本看守者：目標逐 base 過 validate_no_symlink_escape 監獄（agent_id 含
    ``../`` 等逃逸、或解析等於 base 本身〔空字串／"."〕一律拒），best-effort 不拋。

    Returns:
        "removed"（已刪）／"absent"（本就不存在）／"violation"（監獄拒絕）／"error"（IO 失敗）。
    """
    if base_name not in _SOUL_STORAGE_BASES:
        return "violation"
    if base_name == "personal_drive":
        return _remove_soul_drive(agent_id)
    base = Path(resource_manager.get_base_path()) / base_name
    target = base / agent_id
    if not validate_no_symlink_escape(target, base) or target.resolve() == base.resolve():
        logger.warning("🚫 Soul purge sandbox violation: %s/%s", base_name, agent_id)
        return "violation"
    if not target.exists():
        return "absent"
    try:
        shutil.rmtree(target)
    except Exception:
        logger.exception("Failed to purge %s/%s", base_name, agent_id)
        return "error"
    else:
        return "removed"


def _remove_soul_drive(agent_id: str) -> str:
    """個人空間走區內方法（ADR-0293）：本身是連結只刪連結，樹裡的連結也只刪本身."""
    drive = resource_manager.soul_drive(AgentIdentity(agent_id=agent_id))
    if drive is None:
        logger.warning("🚫 Soul purge sandbox violation: personal_drive/%s", agent_id)
        return "violation"
    try:
        kind = resource_manager.zone_kind(drive)
        if kind != "missing":
            resource_manager.zone_remove_dir(drive)
    except Exception:
        logger.exception("Failed to purge personal_drive/%s", agent_id)
        return "error"
    return "absent" if kind == "missing" else "removed"


def purge_soul_storage(agent_id: str) -> dict[str, str]:
    """清除某 agent 的全部實體儲存 base（soul／legacy／personal_drive）。

    回滾（create 失敗）與 admin 刪除靈魂（ADR-0142 D4）共用。每 base 各自過監獄、
    best-effort。Returns: ``{base: status}``（status ∈ removed／absent／violation／error）。
    """
    return {base: remove_soul_storage_dir(base, agent_id) for base in _SOUL_STORAGE_BASES}


def backup_soul_file(identity: AgentIdentity, filename: str) -> bool:
    """手動觸發檔案備份。"""
    base = _get_soul_base(identity)
    if not base:
        return False
    target = _safe_target(base, filename)
    if target is None or not target.exists():
        return False
    try:
        backup_path = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(str(target), str(backup_path))
    except Exception:
        logger.exception("Failed to backup soul file %s", filename)
        return False
    else:
        return True


def undo_soul_edit(identity: AgentIdentity, filename: str) -> bool:
    """回滾最後一次編輯。"""
    base = _get_soul_base(identity)
    if not base:
        return False
    target = _safe_target(base, filename)
    if target is None:
        return False
    bak_path = target.with_suffix(target.suffix + ".bak")
    if not bak_path.exists():
        return False
    try:
        bak_path.replace(target)
    except Exception:
        logger.exception("Failed to undo soul edit for %s", filename)
        return False
    else:
        return True


def delete_soul_file(identity: AgentIdentity, filename: str) -> bool:
    """刪除指定的靈魂檔案。"""
    base = _get_soul_base(identity)
    if not base:
        return False
    target = _safe_target(base, filename)
    if target is None or not target.exists():
        return False
    try:
        target.unlink()
    except Exception:
        logger.exception("Failed to delete soul file %s", filename)
        return False
    else:
        return True


def soul_file_exists(identity: AgentIdentity, filename: str) -> bool:
    """檢查靈魂檔案是否存在。"""
    base = _get_soul_base(identity)
    if not base:
        return False
    target = _safe_target(base, filename)
    return target is not None and target.exists()


def list_soul_ids() -> list[str]:
    """列出所有已存在的靈魂 ID (Warden SSOT)。"""
    base = Path(resource_manager.get_base_path()) / "soul"
    if not base.exists():
        return []
    return [d.name for d in base.iterdir() if d.is_dir()]
