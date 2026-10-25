"""Custom models router module."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends

from src.core.exceptions import ErrorCode, ToukaException
from src.models.custom_models import (  # noqa: TC001
    CustomModelRenameRequest,
    CustomModelUpdateRequest,
)
from src.models.identity import SYSTEM_AGENT_IDS, AgentIdentity  # noqa: TC001
from src.services import memory_collection_guard
from src.services.agent_service import agent_service
from src.services.authorization_gateway import require_privileged
from src.services.custom_model_service import custom_model_service
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService
from src.utils import custom_io, soul_io

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ui/custom_models", tags=["custom_models"])

user_service = UserService()

# TOCTOU fix: 變更端點（建立/改名/刪除）皆為「查存在 → 驗擁有權 → 寫入」的
# check-then-act 形態，必須對同一 model_id 互斥串行，否則兩個使用者毫秒級
# 搶建同一新 ID 時會互相覆寫並同時取得擁有權。模式沿用 async_db._get_session_lock。
_model_locks: dict[str, asyncio.Lock] = {}


def _get_model_lock(model_id: str) -> asyncio.Lock:  # noqa: IDENTITY_SSOT
    """取得特定 model_id 的互斥鎖（同 ID 的建立/改名/刪除互相串行）。"""
    if model_id not in _model_locks:
        _model_locks[model_id] = asyncio.Lock()
    return _model_locks[model_id]


# Require Owner/Debugger/admin to manage custom models.
_require_models_role = require_privileged


def _validate_model_access(model_identity: AgentIdentity, identity: AgentIdentity) -> None:
    """Verify that the current identity has ownership of the target custom model (SSOT)."""
    if not resource_manager.is_owner(model_identity, identity):
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)


def _is_model(model: AgentIdentity) -> bool:
    """custom/ 底下名稱完全相同、而且有 config.json 的資料夾才是自訂模型（舊審查報告丙-2）.

    - 只認實際的資料夾名稱：不分大小寫的磁碟上 mine 也找得到 Mine，擁有清單卻是精確比對，
      admin 會搬走別人的模型，非人類的 Owner 身分查不到擁有者、會被當成公用模型放行。
    - 沒有 config.json 的是別的身分的個人空間（custom/<id>/drive，例如 DebugAgent 或人類），
      不能被當成模型改名、刪除，也不能連帶改動擁有清單裡的這個 ID。
    """
    return model.agent_id in custom_io.list_custom_ids() and custom_io.custom_file_exists(
        model, "config.json"
    )


def _assert_is_model(model: AgentIdentity) -> None:
    """不是自訂模型就 404（見 ``_is_model``）。"""
    if not _is_model(model):
        raise ToukaException(ErrorCode.MODEL_NOT_FOUND, model_id=model.agent_id)


def _names_a_soul(name: str, soul_ids: list[str]) -> bool:
    """去掉 MonitorLayer 認得的寫法（@、toukasoul 前綴）後和某個靈魂 ID 不分大小寫相同。

    明確用 casefold 比對靈魂 ID 清單，不靠檔案系統的大小寫規則（ADR-0250 A5 保護 2）。
    """
    folded = name.casefold()
    candidates = {folded.removeprefix(mark) for mark in ("", "@", "toukasoul-", "toukasoul")}
    return any(soul_id.casefold() in candidates for soul_id in soul_ids)


def _validate_new_model_id(model: AgentIdentity, *, renaming: str | None = None) -> None:
    """新 ID（建立、改名）不能含路徑字元，也不能和靈魂撞名（ADR-0245 修補）。

    ADR-0250 A5：和靈魂 ID、其他自訂模型 ID 只差大小寫也算撞名（Windows 與 Linux 一樣）；
    改名時排除被改名的自己，只改大小寫的自我改名不擋。已存在的資料不動。
    DebugAgent、DispatchAgent 也不能用：擁有清單和它們共用 ID（舊審查報告丙-2）。
    """
    if not custom_io.is_plain_custom_name(model.agent_id):
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    # 和 MonitorLayer 同樣解析模型名（@名稱、toukasoul 前綴）
    passport = agent_service.identify_model_target(model.agent_id)
    others = [mid for mid in custom_io.list_custom_ids() if mid != renaming]
    folded = model.agent_id.casefold()
    if (
        (passport and passport["is_toukasoul"])
        or _names_a_soul(model.agent_id, soul_io.list_soul_ids())
        or any(mid.casefold() == folded for mid in [*others, *SYSTEM_AGENT_IDS])
    ):
        raise ToukaException(ErrorCode.MODEL_ALREADY_EXISTS, model_id=model.agent_id)


async def _assert_collection_free(model: AgentIdentity, *, renaming: str | None = None) -> None:
    """會和既有 ID 共用向量 collection（a-b 與 a_b、只差大小寫）就拒絕；讀事實庫移出事件迴圈.

    改名時舊 ID 只從名稱來源排除；舊 ID 在事實庫裡的記憶照樣算（改名不搬記憶）。
    """
    ignore = frozenset({renaming}) if renaming else frozenset()
    await asyncio.to_thread(memory_collection_guard.assert_id_free, model, ignore=ignore)


@router.get("")
async def list_custom_models(
    _identity: AgentIdentity = Depends(_require_models_role),  # noqa: B008
) -> dict[str, Any]:
    """列出所有自定義模型 (Level 2)"""
    try:
        models = custom_model_service.list_models()
        full_models = []
        for m in models:
            temp_id = AgentIdentity(agent_id=m["id"], agent_role="Owner")
            # IDOR fix: Only allow users to list custom models they own (or bypass for admin/Debugger)
            if not resource_manager.is_owner(temp_id, _identity):
                continue
            prompt, config = custom_model_service.get_model_data(temp_id)
            full_models.append({
                "id": m["id"],
                "name": config.get("name", m["id"]),
                "description": config.get("description", ""),
                "systemPrompt": prompt,
                "type": "custom",
                "config": config,
            })
    except Exception as e:
        logger.exception("Error listing custom models")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    else:
        return {"models": full_models}


@router.get("/{model_id}")
async def get_custom_model(
    model_id: str,  # noqa: IDENTITY_SSOT
    _identity: AgentIdentity = Depends(_require_models_role),  # noqa: B008
) -> dict[str, Any]:
    """獲取特定自定義模型的詳細資料"""
    # IDOR fix: enforce ownership check
    temp_id = AgentIdentity(agent_id=model_id, agent_role="Owner")
    _validate_model_access(temp_id, _identity)
    _assert_is_model(temp_id)

    prompt, config = custom_model_service.get_model_data(temp_id)
    if not prompt and not config:
        raise ToukaException(ErrorCode.MODEL_NOT_FOUND)

    return {
        "id": model_id,
        "name": config.get("name", model_id),
        "description": config.get("description", ""),
        "systemPrompt": prompt,
        "type": "custom",
        "config": config,
    }


@router.post("/{model_id}")
async def update_custom_model(
    model_id: str,  # noqa: IDENTITY_SSOT
    data: CustomModelUpdateRequest,
    _identity: AgentIdentity = Depends(_require_models_role),  # noqa: B008
) -> dict[str, Any]:
    """建立或更新自定義模型"""
    temp_id = AgentIdentity(agent_id=model_id, agent_role="Owner")
    async with _get_model_lock(model_id):
        # 只認實際的資料夾名稱：只差大小寫的 ID 在 Windows 上也找得到 config.json，
        # 改走新建的撞名檢查回 409，和 Linux 一樣（舊審查報告丙-2）
        exists = _is_model(temp_id)

        # IDOR fix: if the custom model already exists, verify that the caller owns it
        if exists:
            _validate_model_access(temp_id, _identity)
        else:
            _validate_new_model_id(temp_id)
            await _assert_collection_free(temp_id)

        name = data.name or model_id
        try:
            saved = custom_model_service.save_model(
                temp_id, name, data.description, data.systemPrompt, data.config
            )
            # IDOR fix：新模型只屬於建立它的人類；別人清單裡同一個 ID 是刪除後留下的殘留，
            # 一併拿掉，免得新模型落到他手上（舊審查報告丙-2）
            if saved and not exists:
                user_service.claim_owned_agent(
                    temp_id, _identity.agent_id if _identity.is_human else None
                )
        except Exception as e:
            logger.exception("Error saving custom model %s", model_id)
            raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
        if not saved:
            # 寫不進去就不回成功、不認領。新建時把寫了一半的資料夾清掉：沒有 config.json 的
            # 資料夾不算模型，之後查看、刪除都找不到它（舊審查報告丙-2）
            if not exists and not custom_model_service.delete_model(temp_id):
                logger.warning("Could not remove half-written custom model %s", model_id)
            raise ToukaException(ErrorCode.SYSTEM_ERROR)
        return {"status": "success", "model_id": model_id}


@router.post("/{model_id}/rename")
async def rename_custom_model(
    model_id: str,  # noqa: IDENTITY_SSOT
    data: CustomModelRenameRequest,
    _identity: AgentIdentity = Depends(_require_models_role),  # noqa: B008
) -> dict[str, Any]:
    """重命名自定義模型的 ID"""
    new_id = data.new_id
    if not new_id:
        raise ToukaException(ErrorCode.MODEL_ID_REQUIRED)

    # TOCTOU fix: 改名涉及兩個 ID（舊與新），兩把鎖皆須取得；
    # 依字典序取得以避免與反向改名請求互相死鎖。
    async with contextlib.AsyncExitStack() as stack:
        for lock_id in sorted({model_id, new_id}):
            await stack.enter_async_context(_get_model_lock(lock_id))

        # IDOR fix: verify old ID ownership
        old_identity = AgentIdentity(agent_id=model_id, agent_role="Owner")
        _validate_model_access(old_identity, _identity)
        _assert_is_model(old_identity)
        if new_id == model_id:
            return {"status": "success", "old_id": model_id, "new_id": new_id}
        # 和靈魂同名的舊資料夾（ADR-0245 之前留下的）：清單裡的這個 ID 也是靈魂的擁有權，
        # 改名會連靈魂一起拿走，所以不改（舊審查報告丙-2）
        if model_id in soul_io.list_soul_ids():
            raise ToukaException(ErrorCode.MODEL_ALREADY_EXISTS, model_id=model_id)
        new_identity = AgentIdentity(agent_id=new_id, agent_role="Owner")
        _validate_new_model_id(new_identity, renaming=model_id)
        await _assert_collection_free(new_identity, renaming=model_id)

        try:
            custom_model_service.rename_model(old_identity, new_id)
        except FileNotFoundError as e:
            raise ToukaException(ErrorCode.MODEL_NOT_FOUND, model_id=model_id) from e
        except FileExistsError as e:
            raise ToukaException(ErrorCode.MODEL_ALREADY_EXISTS, model_id=new_id) from e
        except Exception as e:
            logger.exception("Error renaming custom model %s to %s", model_id, new_id)
            raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e

        # 所有人的擁有清單一起換名（一次原子寫入）：admin 替別人改名時擁有權留給原主人，
        # 呼叫者不是人類也照搬；寫不進去就把資料夾與 config 搬回（舊審查報告丙-2）。
        try:
            user_service.rename_owned_agent(model_id, new_id)
        except Exception as e:
            logger.exception("Error moving ownership of custom model %s to %s", model_id, new_id)
            if not custom_model_service.undo_rename(model_id, new_id):
                logger.critical(
                    "AUDIT: custom model rename rollback FAILED old=%s new=%s by=%s",
                    model_id,
                    new_id,
                    _identity.agent_id,
                )
            raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
        return {"status": "success", "old_id": model_id, "new_id": new_id}


@router.delete("/{model_id}")
async def delete_custom_model(
    model_id: str,  # noqa: IDENTITY_SSOT
    _identity: AgentIdentity = Depends(_require_models_role),  # noqa: B008
) -> dict[str, Any]:
    """刪除自定義模型"""
    identity = AgentIdentity(agent_id=model_id, agent_role="Owner")
    async with _get_model_lock(model_id):
        # IDOR fix: verify ownership before deletion
        _validate_model_access(identity, _identity)
        _assert_is_model(identity)

        success = custom_model_service.delete_model(identity)
        if not success:
            raise ToukaException(ErrorCode.MODEL_DELETE_FAILED)

        # IDOR fix：所有人的清單都拿掉這個 ID，不只呼叫者的，免得留下殘留（舊審查報告丙-2）。
        # 和靈魂同名的舊資料夾：清單裡的這個 ID 也是靈魂的擁有權，留著。
        if model_id not in soul_io.list_soul_ids():
            user_service.remove_owned_agent_from_all(identity)

        return {"status": "success"}
