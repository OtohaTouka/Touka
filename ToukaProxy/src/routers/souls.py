"""Souls router for managing agent personalities and configurations."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.souls import (  # noqa: TC001
    SoulCreateRequest,
    SoulDeleteRequest,
    SoulUpdateRequest,
)
from src.services import memory_collection_guard
from src.services.agent_reset.delete_guard import (
    assert_memory_cleared,
    assert_timeline_cleared,
)
from src.services.auth_service import AuthService
from src.services.dispatchers.route_family import is_selectable_brain
from src.services.identity_router import get_verified_identity
from src.services.isolation.home_wipe import HomeWipeError, wipe_agent_homes
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.user_service import UserService
from src.utils import custom_io, soul_io
from src.utils.async_db import session_service
from src.utils.brain_persistence import (
    clear_agent,
    get_active_brain,
    normalize_model_choice,
    set_active_brain,
    should_set_active_brain,
)
from src.utils.path_security import validate_no_symlink_escape

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/souls", tags=["souls"])

# DebugAgent＝系統除錯身分，不可經此端點刪除（ADR-0142 D5）。
_UNDELETABLE_SOULS = frozenset({"DebugAgent"})


def _verify_admin_stepup(identity: AgentIdentity, password: str, action: str, target: str) -> None:
    """Admin 步升重驗（ADR-0142 D2）：呼叫者須為**登入的人類 admin** 且重新提供密碼。

    敏感的靈魂資料變更（編輯／刪除）即使持有 admin JWT 也須步升——被竊或忘記登出的
    session 無法靜默摧毀靈魂資料。只有 is_human 的真人有密碼可驗（assumed-admin 代打／
    agent 一律過不了）。成功／失敗皆留 AUDIT log（不記密碼本身）。
    """
    if not identity.is_human or identity.human_role != "admin":
        raise ToukaException(ErrorCode.AUTH_ADMIN_REQUIRED)
    auth_service = AuthService()
    user = auth_service.get_user_by_id(identity.agent_id)
    if user is None or not auth_service.verify_password(password, user.hashed_password):
        logger.warning(
            "AUDIT: soul %s step-up FAILED for admin %s on %s", action, identity.agent_id, target
        )
        raise ToukaException(ErrorCode.AUTH_REAUTH_FAILED)
    logger.warning(
        "AUDIT: admin %s performed soul %s on %s (step-up ok)", identity.agent_id, action, target
    )


@router.post("")
async def create_soul(
    req: SoulCreateRequest,
    identity: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, str]:
    """建立新的靈魂: 建立資料夾、初始化設定、更新 models.json 與 registry"""
    if not identity.is_human or identity.human_role != "admin":
        raise ToukaException(ErrorCode.AUTH_ADMIN_REQUIRED)

    agent_id = req.agent_id
    if agent_id.casefold().startswith("toukasoul"):
        raise ToukaException(ErrorCode.VALIDATION_ERROR)
    persona = req.persona or f"我是 {agent_id}，一個由 Touka 驅動的代理人。"
    config_data = req.config.model_dump()
    # 比照存檔（ADR-0250 A1）：None、空字串、只有空白都當沒指定，有值時去掉前後空白
    base_model = normalize_model_choice(req.config.base_model)
    config_data["base_model"] = base_model

    # ADR-0147（Decision 1a）：禁止 soul.agent_id 撞任何人類 user.id。verify_token
    # 先查 user_by_id 命中即當人類短路（is_human=True、agent_role=None），撞名會讓
    # 帶該 id 的 token 永遠解析成人類、拿不到靈魂的 agent 角色（二義性身分）。建立時
    # fail-loud 杜絕，比事後解析靠優先序更乾淨。
    if any(u.id == agent_id for u in AuthService().get_all_users()):
        raise ToukaException(
            ErrorCode.VALIDATION_ERROR,
            detail_override=(
                f"agent_id '{agent_id}' collides with an existing user id; "
                "choose a different soul id."
            ),
        )

    # 新 ID 會和既有 ID 共用向量 collection（a-b 與 a_b、只差大小寫）就拒絕；放在任何檔案系統
    # 檢查之前，Windows 與 Linux 的結果才一致。讀不到事實庫就拒絕（不能確認就不建立）。
    new_identity = AgentIdentity(agent_id=agent_id)
    await asyncio.to_thread(memory_collection_guard.assert_id_free, new_identity)

    soul_root_str = resource_manager.get_vault_root("soul", identity)
    if not soul_root_str:
        raise ToukaException(ErrorCode.SOUL_VAULT_ERROR)

    soul_root = Path(soul_root_str)
    soul_path = soul_root / agent_id
    # 路徑監獄（先於任何建立動作）：agent_id 含 ../ 等逃逸會把目錄建到 soul/ 之外，
    # 且恰好繞過只認 soul/legacy 底下的 Soul Protection——一律拒絕。
    if (
        not validate_no_symlink_escape(soul_path, soul_root)
        or soul_path.resolve() == soul_root.resolve()
    ):
        raise ToukaException(ErrorCode.SOUL_VAULT_ERROR)
    if soul_path.exists():
        raise ToukaException(ErrorCode.SOUL_ALREADY_EXISTS)
    # 和既有的自訂模型完全同名（collection 撞名檢查不算完全相同的 ID）：擁有清單共用同一筆，
    # 自訂模型的主人會一起變成這個靈魂的擁有者（舊審查報告丙-2）
    if agent_id in custom_io.list_custom_ids():
        raise ToukaException(ErrorCode.MODEL_ALREADY_EXISTS, model_id=agent_id)
    # 主要模型比照存檔先驗（先於任何建立動作）：不能用就 404；只差大小寫分不出時
    # resolve_custom_route 直接拋 409 MODEL_NAME_AMBIGUOUS。
    if base_model and not is_selectable_brain(base_model, manager=model_manager):
        logger.warning("Soul %s create rejected: model %r is not selectable", agent_id, base_model)
        raise ToukaException(ErrorCode.MODEL_NOT_FOUND)

    await session_service.invalidate_agent_sessions(new_identity)
    app_data_dir = Path(resource_manager.get_base_path())
    dir_specs = {
        "soul": soul_path,
        "personal_drive": app_data_dir / "personal_drive" / agent_id,
        "legacy": app_data_dir / "legacy" / agent_id,
    }
    # D6 回滾範圍＝本次「新建」的目錄；personal_drive/legacy 可能先於本次即存在
    # （舊資料），失敗回滾絕不誤刪既有資料。
    newly_created = [name for name, d in dir_specs.items() if not d.exists()]

    try:
        for d in dir_specs.values():
            resource_manager.make_dir(d)

        # 寫入人格＋設定：走 soul_io 受認可通道（_safe_target 監獄＋原子寫＋自動備份）。
        # 不弱化 resource_manager 的 Soul Protection 原語——它照擋其他一切呼叫者。
        soul_identity = AgentIdentity(agent_id=agent_id, agent_role="Owner")
        _persist_soul_files(soul_identity, persona, config_data)
        await session_service.invalidate_agent_sessions(soul_identity)

        # SSOT 註冊
        _update_models_config(agent_id, action="add")
        _update_agents_registry(agent_id, aliases=[agent_id], action="add")

        # 自動歸屬（ADR-0158）：建立者（登入的人類 admin）即擁有者——寫 users.json
        # owned_agents，與 custom_models 建立路徑同型。owned_agents 是心跳喚醒
        # （守衛 B：孤兒即拒、無 admin 旁路）／前景代打／Auto、YOLO 模式授權／審批解算的授權面；
        # 過去建立側漏寫（刪除側反而會清＝不對稱），新靈魂在手動歸屬前心跳全滅。
        # 角色面毋須寫 identity_map——tag_rules is_toukasoul→Owner（ADR-0147）已涵蓋，
        # 寫 override 反而有蓋掉 tag_rules 的降級風險。
        # 舊審查報告丙-2：用認領，別人清單裡同一個 ID 的殘留（刪掉的自訂模型留下的）一併拿掉，
        # 不然殘留的主人會變成這個新靈魂的擁有者。
        UserService().claim_owned_agent(soul_identity, identity.agent_id)

        # 如果有大腦型號，就立刻寫入設定紀錄 (SSOT 原子化建立)
        if base_model:
            set_active_brain(soul_identity, base_model)
            logger.info("🧠 Initialized brain for new soul %s -> %s", agent_id, base_model)
    except Exception:
        # D6 回滾：任何一步失敗 → best-effort 撤銷本次建立（不留孤兒、同 id 可重試；
        # 註冊/brain 清除皆冪等），再原樣拋出。每步各自吞錯——導致建立失敗的那一步
        # 在 remove 側很可能再失敗，不得因此中斷其餘撤銷。
        with contextlib.suppress(Exception):
            _update_models_config(agent_id, action="remove")
        with contextlib.suppress(Exception):
            _update_agents_registry(agent_id, action="remove")
        with contextlib.suppress(Exception):
            clear_agent(AgentIdentity(agent_id=agent_id))
        with contextlib.suppress(Exception):
            # 歸屬回滾（冪等：尚未歸屬即 no-op）——失敗不得留下指向不存在靈魂的歸屬
            UserService().remove_owned_agent(identity.agent_id, AgentIdentity(agent_id=agent_id))
        for name in newly_created:
            soul_io.remove_soul_storage_dir(name, agent_id)  # 自身即 best-effort 不拋
        logger.warning("↩️ create_soul rolled back for %s", agent_id)
        raise

    return {"status": "success", "agent_id": agent_id}


@router.get("")
async def list_souls(
    identity: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, list[dict[str, Any]]]:
    """列出所有可用的靈魂"""
    try:
        soul_dir_str = resource_manager.get_vault_root("soul", identity)
        if not soul_dir_str:
            return {"souls": []}
            
        soul_dir = Path(soul_dir_str)
        if not soul_dir.is_dir():
            return {"souls": []}

        souls = []
        for folder_path in soul_dir.iterdir():
            if not folder_path.is_dir():
                continue

            folder = folder_path.name

            # RBAC 過濾 (Gate 2 — 資料存取主權)
            # 1. 管理員或擁有者 (不論是否為人類)：允許存取全部
            # 2. 一般人類使用者：僅允許存取 owned_agents 清單中的靈魂
            # 3. 其他非人類 Agent：一律不允許枚舉
            if identity.is_privileged():
                pass
            elif not identity.is_human or folder not in identity.owned_agents:
                continue
            persona_path = folder_path / "soul.md"
            persona = ""
            try:
                if resource_manager.exists(persona_path):
                    persona = resource_manager.read_file_text(persona_path)
                else:
                    persona = ""
            except Exception:
                logger.exception("Error reading persona for %s", folder)

            config_path = folder_path / "config.json"
            config = resource_manager.read_json(config_path) or {}

            # 沒設或讀取出錯都回 null，畫面自己翻譯「未配置」（ADR-0250 A4）
            try:
                active_brain = get_active_brain(AgentIdentity(agent_id=folder))
            except Exception:
                logger.exception("Error getting active brain for %s", folder)
                active_brain = None

            souls.append(
                {
                    "id": folder,
                    "config": config,
                    "persona": persona,
                    "active_brain": active_brain,
                }
            )
    except Exception as e:
        logger.exception("Critical error in list_souls")
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    else:
        return {"souls": souls}


@router.post("/{soul_id}")
async def update_soul(
    soul_id: str,  # noqa: IDENTITY_SSOT
    data: SoulUpdateRequest,
    verified: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, str]:
    """更新靈魂的設定與 Persona (Identity SSOT).

    Grandfather Batch-1 fix: replaced hardcoded AgentIdentity bypass with
    Depends(get_verified_identity). Only Owner/Debugger/admin may update souls.
    """
    if not verified:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # ADR-0142 D3：人類編輯靈魂資料一律步升重驗密碼；agent（含自我演化）走原本的
    # 角色＋擁有權閘、不受步升影響（agent 無密碼可打，自我演化另由 edit_soul 工具管控）。
    if verified.is_human:
        _verify_admin_stepup(verified, data.admin_password or "", "update", soul_id)
    else:
        if not verified.is_privileged():
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        # IDOR fix: the role gate alone let any Owner overwrite ANY soul_id's
        # config/persona (soul_id is an attacker-controlled path param). Scope writes to
        # ownership of the target soul (admin/Debugger bypass inside is_owner). Self-access
        # (the caller IS the soul) is always allowed — an agent must not be locked out of
        # its own soul by the autonomy-grace window inside is_owner; the caller's agent_id
        # is token-derived, so it can only equal soul_id for a legitimate self/owner.
        target = AgentIdentity(agent_id=soul_id, agent_role="Owner")
        if verified.agent_id != soul_id and not resource_manager.is_owner(target, verified):
            logger.warning(
                "🚫 Cross-owner soul update denied: %s → %s", verified.agent_id, soul_id
            )
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
    # Use an Owner identity for vault access (permission already decided above).
    identity = AgentIdentity(agent_id=soul_id, agent_role="Owner")
    soul_dir_str = resource_manager.get_vault_root("soul", identity)
    if not soul_dir_str:
        raise ToukaException(ErrorCode.SOUL_VAULT_ERROR)
        
    soul_dir = Path(soul_dir_str)
    agent_dir = soul_dir / soul_id
    # 路徑監獄（與 create 對齊）：soul_id 逃逸會把寫入導到 soul/ 之外並繞過 Soul Protection。
    if (
        not validate_no_symlink_escape(agent_dir, soul_dir)
        or agent_dir.resolve() == soul_dir.resolve()
    ):
        raise ToukaException(ErrorCode.SOUL_VAULT_ERROR)

    # 主要模型兩欄（ADR-0250 A1）：寫入前先讀出已存的「你指定的」與目前在用的主要模型；
    # 要換時先驗證，不能用就 404，config.json 與 soul.md 都不寫。
    stored = resource_manager.read_json(agent_dir / "config.json")
    old_model = stored.get("base_model") if isinstance(stored, dict) else None
    config_data = data.config.model_dump() if data.config is not None else None
    new_model = normalize_model_choice(config_data["base_model"] if config_data else None)
    switch = should_set_active_brain(
        is_human=verified.is_human,
        old=normalize_model_choice(old_model),
        new=new_model,
        has_brain=get_active_brain(identity) is not None,
    )
    switch_to = new_model if switch else None
    if switch_to is not None and not is_selectable_brain(switch_to, manager=model_manager):
        logger.warning("Soul %s save rejected: model %r is not selectable", soul_id, switch_to)
        raise ToukaException(ErrorCode.MODEL_NOT_FOUND)

    was_soul = isinstance(stored, dict) and stored.get("is_toukasoul", stored.get("is_soul", False))
    creating_soul = bool(config_data and config_data["is_toukasoul"] and not was_soul)
    if creating_soul:
        if soul_id.casefold().startswith("toukasoul"):
            raise ToukaException(ErrorCode.VALIDATION_ERROR)
        await session_service.invalidate_agent_sessions(identity)
    new_dir = not agent_dir.exists()
    had_config = resource_manager.exists(agent_dir / "config.json")
    resource_manager.make_dir(agent_dir)
    # 寫入走 soul_io 受認可通道（原子寫＋自動 .bak 備份；Soul Protection 原語不弱化）。
    if config_data is not None:
        config_data["base_model"] = new_model
        if not soul_io.write_soul_json(identity, "config.json", config_data):
            raise ToukaException(ErrorCode.SOUL_WRITE_FAILED)
        if creating_soul:
            try:
                await session_service.invalidate_agent_sessions(identity)
            except Exception:
                if new_dir:
                    soul_io.remove_soul_storage_dir("soul", soul_id)
                elif had_config:
                    soul_io.undo_soul_edit(identity, "config.json")
                else:
                    soul_io.delete_soul_file(identity, "config.json")
                raise

    if data.persona is not None and not soul_io.write_soul_file(
        identity, "soul.md", data.persona
    ):
        raise ToukaException(ErrorCode.SOUL_WRITE_FAILED)

    if switch_to is not None:
        set_active_brain(identity, switch_to)
        logger.info("🧠 Brain for soul %s set to chosen model %s", soul_id, switch_to)

    logger.info("✨ Updated soul data for %s", soul_id)
    return {"status": "success", "soul_id": soul_id}


@router.delete("/{soul_id}")
async def delete_soul(
    soul_id: str,  # noqa: IDENTITY_SSOT
    req: SoulDeleteRequest,
    verified: Annotated[AgentIdentity, Depends(get_verified_identity)],
) -> dict[str, Any]:
    """刪除靈魂及其全部 SSOT 面（ADR-0142 D2/D4/D5）：admin 步升＋冪等清除。

    清除範圍：三實體目錄（soul／legacy／personal_drive）＋models.json／agents_registry／
    brain_settings／各使用者 owned_agents／identity_map override。**冪等**——孤兒（空目錄、
    零註冊）照清、清完 agent_id 可重用（直解「建立失敗連清都清不掉」）。聊天 sessions 與
    記憶 vault 不在此範圍（既有 per-agent 破壞性重置涵蓋）。
    """
    _verify_admin_stepup(verified, req.admin_password, "delete", soul_id)
    if soul_id in _UNDELETABLE_SOULS:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    # 路徑監獄：soul_id 逃逸不得清到 soul/ 之外（purge_soul_storage 內部逐 base 亦過監獄，
    # 此處先擋提早 fail-loud）。
    soul_base = Path(resource_manager.get_base_path()) / "soul"
    if (
        not validate_no_symlink_escape(soul_base / soul_id, soul_base)
        or (soul_base / soul_id).resolve() == soul_base.resolve()
    ):
        raise ToukaException(ErrorCode.SOUL_VAULT_ERROR)
    # 還有記憶就 409 擋下、什麼都不刪，要先重置（避免孤兒記憶與同名新靈魂接手舊記憶）。
    assert_memory_cleared(AgentIdentity(agent_id=soul_id))
    await assert_timeline_cleared(AgentIdentity(agent_id=soul_id))  # 活動紀錄也屬記憶
    # 隔離暫存區的 HOME／TMP 也要清（同名新靈魂不沿用舊檔案）；清不掉就 503、什麼都不刪。
    # Owner 身分：soul/ 已不在的孤兒也要解析到 personal_drive（不是 custom 的 drive）。
    try:
        await wipe_agent_homes(AgentIdentity(agent_id=soul_id, agent_role="Owner"))
    except HomeWipeError as exc:
        logger.warning("Soul %s not deleted: sandbox home not cleared (%s)", soul_id, exc.reason)
        raise ToukaException(ErrorCode.SOUL_SANDBOX_HOME_NOT_CLEARED) from exc

    storage = soul_io.purge_soul_storage(soul_id)
    # SSOT 註冊面（皆冪等；缺項即 no-op）——各自吞錯，任一面失敗不阻其餘。
    with contextlib.suppress(Exception):
        _update_models_config(soul_id, action="remove")
    with contextlib.suppress(Exception):
        _update_agents_registry(soul_id, action="remove")
    with contextlib.suppress(Exception):
        clear_agent(AgentIdentity(agent_id=soul_id))
    with contextlib.suppress(Exception):
        _remove_from_all_owned_agents(soul_id)
    with contextlib.suppress(Exception):
        _remove_identity_override(soul_id)

    removed = [f"{base}" for base, status in storage.items() if status == "removed"]
    skipped = [f"{base}:{status}" for base, status in storage.items() if status != "removed"]
    logger.info("🗑️ Deleted soul %s (removed=%s)", soul_id, removed)
    return {"status": "success", "soul_id": soul_id, "removed": removed, "skipped": skipped}


def _remove_from_all_owned_agents(soul_id: str) -> None:  # noqa: IDENTITY_SSOT
    """把 soul_id 從所有使用者的 owned_agents 清單移除（冪等）。"""
    auth_service = AuthService()
    user_service = UserService(auth_service=auth_service)
    for user in auth_service.get_all_users():
        if soul_id in user.owned_agents:
            user_service.remove_owned_agent(user.id, AgentIdentity(agent_id=soul_id))


def _remove_identity_override(soul_id: str) -> None:  # noqa: IDENTITY_SSOT
    """移除 identity_map 的 agent_overrides[soul_id]（冪等；正常建立的靈魂本就無此條目）。"""
    current = permission_service.identity_map.model_dump()
    overrides = current.get("agent_overrides", {})
    if soul_id in overrides:
        del overrides[soul_id]
        permission_service.update_identity(current)


def _persist_soul_files(
    soul_identity: AgentIdentity, persona: str, config_data: dict[str, Any]
) -> None:
    """寫入 soul.md＋config.json（soul_io 受認可通道）；任一失敗即 fail-loud。"""
    if not soul_io.write_soul_file(soul_identity, "soul.md", persona):
        raise ToukaException(ErrorCode.SOUL_WRITE_FAILED)
    if not soul_io.write_soul_json(soul_identity, "config.json", config_data):
        raise ToukaException(ErrorCode.SOUL_WRITE_FAILED)


def _update_models_config(soul_id: str, action: str = "add") -> None: # noqa: IDENTITY_SSOT
    """更新 models.json 以註冊靈魂路由（改走 model_manager，快取與磁碟一致；ADR-0145）。

    過去此處以 resource_manager 直接寫檔、繞過 model_manager 的記憶體快取，導致啟動時的
    check_models_health 等背景 _save_config 會以過時快取整包覆蓋、抹掉剛註冊的 ToukaSoul<id>
    （隨機次序 flake 與正式環境靈魂模型註冊靜默流失的根因）。改由單例為權威 SSOT。
    """
    if action == "add":
        model_manager.register_soul_model(soul_id)
    elif action == "remove":
        model_manager.unregister_soul_model(soul_id)


def _update_agents_registry(
    soul_id: str, # noqa: IDENTITY_SSOT
    aliases: list[str] | None = None,
    action: str = "add"
) -> None:
    """更新 agents_registry.json 以支援別名解析 (SSOT)"""
    registry_path = Path(get_config_dir()) / "agents_registry.json"
    registry = resource_manager.read_json(registry_path)
    if registry is None:
        # 同 _update_models_config：僅缺失/損毀略過，空 {} 照常寫入（fresh install）。
        return

    try:

        if action == "add":
            registry[soul_id] = aliases or [soul_id]
        elif action == "remove" and soul_id in registry:
            del registry[soul_id]

        resource_manager.write_json(registry_path, registry)
    except Exception:
        logger.exception("Error updating agents_registry.json")
