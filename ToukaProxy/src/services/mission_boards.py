"""任務看板跟著代理人走（ADR-0325）.

看板在 ``<data>/missions``，內容只經 ``MissionManager`` 讀寫；這裡負責：

- 升級後第一次啟動把個人空間裡的舊看板搬一次（``import_legacy_boards``，在外部 MCP 伺服器
  開起來之前）。
- 判斷一個逐字編號屬不屬於某個既有代理人；建立新代理人時清掉不屬於任何人的殘留看板。
- 刪除、改名時刪看板、搬看板，失敗時回結果讓路由決定錯誤碼。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from memory_server.services.mission_manager import LegacyImport, MissionManager
from memory_server.utils.zone_paths import ZoneRootsUnavailableError, is_zone_segment

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import SYSTEM_AGENT_IDS
from src.services.agent_service import agent_service
from src.services.auth_service import AuthService
from src.services.identity_service import identity_service
from src.services.resource_manager import resource_manager
from src.utils import soul_io

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

# 列不出名稱、讀不到登記表或使用者清單時丟的例外（RegistryUnreadableError 是 ValueError；
# users.json 壞掉時 AuthService 丟 ToukaException）
_LISTING_ERRORS = (OSError, ValueError, ZoneRootsUnavailableError, ToukaException)


# --------------------------------------------------------------------------- #
# 代理人編號的來源（列不完整一律丟例外：少列一個就等於當成沒有人用）
# --------------------------------------------------------------------------- #


def _soul_ids() -> list[str]:
    return soul_io.list_soul_ids()


def _system_ids() -> list[str]:
    return sorted(SYSTEM_AGENT_IDS)


def _registry_ids() -> list[str]:
    return sorted(agent_service.registered_ids())


def _custom_names() -> list[str]:
    """``custom/`` 第一層的資料夾名稱（不跟隨連結；含沒有 config 的個人空間，例如人類）."""
    base = Path(resource_manager.get_base_path()) / "custom"
    return sorted(resource_manager.list_child_names(base, dirs_only=True))


def _personal_names() -> list[str]:
    return sorted(soul_io.personal_drive_names())


def _human_ids() -> set[str]:
    return {user.id for user in AuthService().get_all_users()}


# --------------------------------------------------------------------------- #
# 升級後搬一次舊看板
# --------------------------------------------------------------------------- #


def legacy_candidates() -> tuple[list[str], bool]:
    """搬舊看板的候選編號（依序、去重、只收單一段）與是否列完整.

    順序：靈魂的正式 ID（舊版在 Windows 上可能用別的大小寫建出個人空間，用正式 ID 才搬給正式
    編號）、系統與登記表的編號（被設成 Owner 時看板在個人空間）、``custom/`` 的名稱，最後才是只
    出現在個人空間的名稱（前面已搬走的，這裡只會看到舊檔不存在）。
    """
    sources = (_soul_ids, _system_ids, _registry_ids, _custom_names, _personal_names)
    ordered: list[str] = []
    seen: set[str] = set()
    complete = True
    for source in sources:
        try:
            names = source()
        except _LISTING_ERRORS:
            logger.exception("❌ Old mission boards: cannot list %s", source.__name__)
            complete = False
            continue
        for name in names:
            if name not in seen and is_zone_segment(name):
                seen.add(name)
                ordered.append(name)
    return ordered, complete


async def import_legacy_boards() -> None:
    """升級後第一次啟動：把個人空間裡的舊看板搬到 ``data/missions``（ADR-0325）.

    記號已在就什麼都不做。個別看板失敗只記 ERROR、照樣寫記號（再試就等於又信任舊位置）；
    候選列不完整時不寫記號，下次啟動再來（已搬的因為新位置已有看板而略過）。
    """
    manager = MissionManager()
    if manager.legacy_import_done():
        return
    ids, complete = legacy_candidates()
    moved = 0
    for agent_id in ids:
        try:
            result = await manager.import_legacy_board(identity_service.get_identity(agent_id))
        except Exception:
            logger.exception("❌ Old mission board of %s not moved", agent_id)
            continue
        moved += result is LegacyImport.IMPORTED
    if not complete:
        logger.error("❌ Old mission boards: listing incomplete; retrying at the next start")
        return
    manager.mark_legacy_import_done()
    logger.info("Old mission boards moved to the system folder: %d", moved)


# --------------------------------------------------------------------------- #
# 建立、刪除、改名
# --------------------------------------------------------------------------- #


def is_unclaimed(identity: AgentIdentity) -> bool:
    """逐字的編號不屬於任何既有代理人.

    不在靈魂清單、不是 ``custom/`` 第一層的資料夾名稱、不是系統編號、不在 agents 登記表、不是
    人類帳號。讀不到資料回 ``AGENT_ID_CHECK_FAILED``：不能確認就不建立，也不能當成「不屬於」去清。
    """
    try:
        claimed = (
            set(_soul_ids())
            | set(_custom_names())
            | set(_system_ids())
            | set(_registry_ids())
            | _human_ids()
        )
    except _LISTING_ERRORS as exc:
        logger.warning("🚫 Cannot tell whether %r belongs to an agent", identity.agent_id)
        raise ToukaException(ErrorCode.AGENT_ID_CHECK_FAILED) from exc
    return identity.agent_id not in claimed


async def new_board_unclaimed(identity: AgentIdentity, *, refuse_claimed: bool) -> bool:
    """新編號的看板能不能清：回編號是否不屬於任何代理人.

    ``refuse_claimed``（建立、改名自訂模型）：編號屬於別的身分而且它有看板 →
    ``MODEL_ALREADY_EXISTS``，不接手別人的看板（以前那個身分寫看板時會建出 ``custom/<id>``，
    撞名檢查就會擋）。
    """
    if is_unclaimed(identity):
        return True
    if refuse_claimed and await MissionManager().has_board(identity):
        logger.warning("🚫 %r belongs to another identity with a mission board", identity.agent_id)
        raise ToukaException(ErrorCode.MODEL_ALREADY_EXISTS, model_id=identity.agent_id)
    return False


async def prepare_new_board(identity: AgentIdentity, *, refuse_claimed: bool) -> None:
    """建立新代理人：所有會拒絕的檢查都通過之後、第一個寫入之前呼叫.

    編號不屬於任何代理人 → 清掉殘留看板（例如刪除那一刻還在跑的對話之後才寫進來的）；屬於某個
    身分 → 不清（那是它的看板）。清不掉就回錯誤、不建立。
    """
    if not await new_board_unclaimed(identity, refuse_claimed=refuse_claimed):
        return
    if not await remove_board(identity):
        raise ToukaException(ErrorCode.SYSTEM_ERROR)


async def remove_board(identity: AgentIdentity) -> bool:
    """刪掉這個編號的看板；刪不掉回 False（呼叫端回錯誤、儲存與註冊不刪）."""
    try:
        await MissionManager().delete_board(identity)
    except (OSError, ValueError):
        logger.exception("❌ Mission board of %s not removed", identity.agent_id)
        return False
    return True


async def carry_board(source: AgentIdentity, target: AgentIdentity, *, clear_target: bool) -> None:
    """自訂模型改名後看板跟著搬（``clear_target``：新編號不屬於任何人，先清殘留）；失敗往外丟."""
    manager = MissionManager()
    if clear_target:
        await manager.delete_board(target)
    await manager.move_board(source, target)


async def return_board(source: AgentIdentity, target: AgentIdentity) -> bool:
    """改名回滾時把看板搬回；搬不回回 False（呼叫端記 AUDIT）."""
    try:
        await MissionManager().move_board(source, target)
    except (OSError, ValueError):
        logger.exception(
            "❌ Mission board of %s not moved back to %s", source.agent_id, target.agent_id
        )
        return False
    return True
