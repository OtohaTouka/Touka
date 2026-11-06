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

from memory_server.utils.no_follow import WalkReport
from memory_server.utils.zone_paths import (
    ALL_ANCHORS,
    Anchor,
    ZoneRootsUnavailableError,
    contains_zone,
    current_roots,
    is_zone_segment,
    zone_of,
)

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager
from src.utils.path_security import validate_no_symlink_escape

logger = logging.getLogger(__name__)

# 靈魂實體儲存的三個 base（create 建立／刪除清理的完整集合）。
_SOUL_STORAGE_BASES = ("soul", "legacy", "personal_drive")


# 每個代理人在這些資料夾底下各有一個同名資料夾（個人空間另外看錨點）：新的 ID 不能是其中
# 任何一個既有資料夾的別名，不然會和別的代理人共用資料夾。
_AGENT_FOLDER_BASES = ("soul", "legacy", "custom", "stats")
# Windows 8.3 短檔名的標記：新的代理人 ID 不收
_SHORT_NAME_MARK = "~"


def check_new_agent_name(identity: AgentIdentity, *, renamed_from: str | None = None) -> None:
    """新的代理人 ID 能不能用：建立靈魂、用編輯建立或轉換靈魂、建立或改名自訂模型（sec-9 PR 1b）.

    - 不收 ``~``：Windows 的 8.3 短檔名都帶 ``~``，是之後才配給新資料夾的；新 ID 先佔下，
      之後建立的代理人的短檔名就會指到它。
    - 代理人資料夾所在的四個位置和個人空間都查，規則見 ``_check_folder_names``。
    - ``renamed_from``：自我改名只差 ASCII 大小寫時，在不分大小寫的檔案系統上指的就是自己的資料夾，
      不查別名。其他 Unicode 的大小寫對應（例如開氏符號 K）不算：檔案系統不把它們當成同一個字。
    """
    if _SHORT_NAME_MARK in identity.agent_id:
        logger.warning("🚫 New agent ID contains the 8.3 short-name mark: %r", identity.agent_id)
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    if renamed_from is not None and _differs_only_by_ascii_case(identity.agent_id, renamed_from):
        _check_folder_names(identity, (), personal=False)
        return
    _check_folder_names(identity, _AGENT_FOLDER_BASES, personal=True)


def _differs_only_by_ascii_case(name: str, other: str) -> bool:
    """兩個名稱只差 ASCII 字母的大小寫（``bytes.lower`` 只動 ASCII）."""
    return name.encode("utf-8").lower() == other.encode("utf-8").lower()


def check_soul_name(identity: AgentIdentity) -> None:
    """刪除靈魂前：會清掉的三個位置（soul、legacy、個人空間）都不能是別名（sec-9 PR 1b）."""
    _check_folder_names(identity, ("soul", "legacy"), personal=True)


def check_soul_folder_name(identity: AgentIdentity) -> None:
    """編輯既有靈魂前：只寫 ``soul/<id>``，只查這一個位置（sec-9 PR 1b）.

    舊版在 Windows 上可能用別的大小寫建出 legacy、個人空間；那在 Windows 上就是同一份資料，
    不擋編輯。用編輯建立或轉換靈魂時另外走 ``check_new_agent_name``。
    """
    _check_folder_names(identity, ("soul",), personal=False)


def _check_folder_names(
    identity: AgentIdentity, bases: tuple[str, ...], *, personal: bool
) -> None:
    r"""名稱是單一段，而且不是 ``bases``（與個人空間）裡既有資料夾的別名.

    1. 不是單一段（``is_zone_segment``）→ ``VALIDATION_ERROR``。這一步不碰檔案系統：Windows 上
       ``\\主機\分享`` 拼上去會變成網路路徑，檢查之後才會碰到本機的 base。
    2. 每個位置各自判斷：名稱逐字出現在目錄列表 → 通過；不在列表、檔案系統卻說它存在 → 別名
       （只差大小寫、8.3 短檔名、Unicode 正規化），``VALIDATION_ERROR``；都不是 → 還沒有，通過。
       Linux 上真的並存的 ``Foo``、``foo`` 各自逐字在列表裡，都通過。
    3. 列不完整或位置解析不到 → ``SOUL_VAULT_ERROR``：不能確認就不動。
    """
    agent_id = identity.agent_id
    if not is_zone_segment(agent_id):
        logger.warning("🚫 Agent name is not a single segment: %r", agent_id)
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    base_path = Path(resource_manager.get_base_path())
    for base_name in bases:
        root = base_path / base_name
        try:
            names = resource_manager.list_child_names(root)
        except OSError as exc:
            logger.warning("🚫 Cannot list %s to check the agent name %r", base_name, agent_id)
            raise ToukaException(ErrorCode.SOUL_VAULT_ERROR) from exc
        if agent_id not in names and (root / agent_id).exists(follow_symlinks=False):
            _refuse_alias(base_name, agent_id)
    if not personal:
        return
    try:
        drive = resource_manager.soul_drive(identity)
        names = _personal_drive_names()
        present = drive is not None and resource_manager.zone_kind(drive) != "missing"
    except (OSError, ZoneRootsUnavailableError) as exc:
        logger.warning("🚫 Cannot list the personal space to check the agent name %r", agent_id)
        raise ToukaException(ErrorCode.SOUL_VAULT_ERROR) from exc
    if agent_id not in names and present:
        _refuse_alias(Anchor.PERSONAL_DRIVE.value, agent_id)


def _refuse_alias(base_name: str, name: str) -> None:
    logger.warning(
        "🚫 %r is not the exact name of the existing %s folder it points to", name, base_name
    )
    raise ToukaException(ErrorCode.VALIDATION_ERROR)


def _personal_drive_names() -> set[str]:
    """個人空間錨點第一層的實際名稱（區內方法，不跟隨連結；列不完整丟 OSError）."""
    report = WalkReport()
    root = resource_manager.anchor_root(Anchor.PERSONAL_DRIVE)
    entries = list(resource_manager.zone_walk(root, report, list_depth=1, show_links=True))
    if report.errors:
        failed = f"Cannot list every entry of the personal space ({report.errors[0][0] or '.'})"
        raise OSError(failed)
    return {entry.parts[0] for entry in entries if len(entry.parts) == 1}


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
