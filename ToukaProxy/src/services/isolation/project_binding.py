"""專用空間的專案登記（ADR-0332）：一資料夾一專案、綁定前清 HOME、刪除後盡力清.

登記順序寫死：權限 → 根目錄檢查（呼叫端）→ 結構層 → 寫法 → 資料夾存在 → 重複 → 清 HOME
→ 寫入。整段在 ``binding_lock`` 裡（proxy 是單一程序）；清除不放在資料庫交易裡（最長 60 秒，
會鎖住其他存檔）。被拒的登記不會清到任何 HOME。專用空間外的專案完全照舊。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from src.core.exceptions import ErrorCode, ToukaException
from src.services.isolation.home_wipe import HomeWipeError, wipe_project_home
from src.services.isolation.profiles import configured_projects_root
from src.services.isolation.project_space import OUTSIDE, is_project_folder, structural_scope
from src.services.resource_manager import resource_manager
from src.utils.async_db import get_async_db, session_service

if TYPE_CHECKING:
    from collections.abc import Iterable

    from src.services.isolation.project_space import ProjectScope

logger = logging.getLogger(__name__)

binding_lock = asyncio.Lock()
# 在錨點底下，但不是工作區根的寫法（靈魂與子代理過不了工作區檢查）
PROJECT_PATH_FORM = "project_path_form"
_SPACE_RULE = (
    "Projects in the dedicated project space must be a folder directly inside it, written "
    "under the workspace root (for example <workspace root>/touka-projects/<folder>)."
)


async def project_scope(root_path: str | None) -> ProjectScope:
    """登記的根目錄翻譯成這個部署的路徑後的結構層範圍."""
    if not root_path:
        return OUTSIDE
    translated = await resource_manager.translate_path(root_path)
    return structural_scope(Path(translated), configured_projects_root())


async def prepare_binding(project_id: str, root_path: str | None) -> None:
    """寫入前的檢查與清除（呼叫端拿著 ``binding_lock``）；不合格拋 ``ToukaException``."""
    scope = await project_scope(root_path)
    if scope.kind == "outside":
        return
    in_workspace = await resource_manager.is_within_workspace(root_path or "")
    if scope.kind == "refused" or not in_workspace:
        reason = scope.reason or PROJECT_PATH_FORM
        raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=_SPACE_RULE, reason=reason)
    if not is_project_folder(scope):
        raise ToukaException(ErrorCode.PROJECT_FOLDER_MISSING)
    if await _already_bound(project_id, scope):
        return
    try:
        await wipe_project_home(scope.name or "", scope.path or Path())
    except HomeWipeError as exc:
        raise ToukaException(ErrorCode.PROJECT_HOME_NOT_CLEARED, reason=exc.reason) from exc


async def _already_bound(project_id: str, scope: ProjectScope) -> bool:
    """這個資料夾已經是這個專案（不用再清）；是別的專案就拋 ``PROJECT_FOLDER_TAKEN``.

    比結構層解析出的名稱（不分大小寫，偏嚴），不比字串：寫法不同、結尾斜線都算同一個。
    讀不到登記就拋錯（不當成沒有重複）。
    """
    name = (scope.name or "").casefold()
    mine = False
    async with get_async_db() as db, db.execute("SELECT id, root_path FROM projects") as cursor:
        rows = [(row["id"], row["root_path"]) for row in await cursor.fetchall()]
    for other_id, other_root in rows:
        other = await project_scope(other_root)
        if other.kind != "ok" or (other.name or "").casefold() != name:
            continue
        if other_id != project_id:
            raise ToukaException(ErrorCode.PROJECT_FOLDER_TAKEN)
        mine = True
    return mine


async def forget_bindings(root_paths: Iterable[str | None]) -> None:
    """專案列刪掉之後盡力清它們的 HOME；失敗只記 log，不擋刪除（下次登記會再清）."""
    for root_path in root_paths:
        scope = await project_scope(root_path)
        if not is_project_folder(scope):
            continue
        async with binding_lock:
            try:
                await wipe_project_home(scope.name or "", scope.path or Path())
            except HomeWipeError as exc:
                logger.warning("Project space home of %s not cleared: %s", scope.name, exc.reason)


async def clear_owned_projects(user_id: str) -> int:
    """刪掉這個使用者擁有的專案列（``clear_projects_owned_by``），再盡力清專用空間的 HOME."""
    projects = await session_service.get_all_projects() if user_id else []
    roots = [p.get("root_path") for p in projects if p.get("owner_user_id") == user_id]
    cleared = await session_service.clear_projects_owned_by(user_id)
    await forget_bindings(roots)
    return cleared
