"""Bot 設定路由 (Bots Router)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity  # noqa: TC001
from src.services.authorization_gateway import require_owner_or_admin
from src.services.bot_service import bot_service
from src.utils.secrets import is_masked, mask_secret

router = APIRouter(prefix="/bots", tags=["UI Bots"])
logger = logging.getLogger(__name__)

_PLATFORMS = ("discord", "telegram")


# ── 純函式：遮罩 / 正規化 / write-only 保留 (SSOT, 易測) ─────────────────────────


def _flatten_agent_id(value: Any) -> str:
    """B3: 接受字串或舊版 AgentIdentity-dict，回傳 agent id 字串。

    歷史上 `agent_id` 經 AgentIdentity 驗證器序列化成完整物件落盤；adapter 與前端
    都只要字串 id，故在 API 邊界一律攤平為字串（執行期 identity 於 dispatch 時解析）。
    """
    if isinstance(value, dict):
        return str(value.get("agent_id", ""))
    return str(value) if value else ""


def _normalize_bot(item: dict[str, Any]) -> dict[str, Any]:
    """B2+B3：補穩定 id（缺則生）、攤平 agent_id 為字串。"""
    out = dict(item)
    if not out.get("id"):
        out["id"] = uuid.uuid4().hex
    out["agent_id"] = _flatten_agent_id(out.get("agent_id"))
    return out


def _normalize_config(
    config: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """正規化整份設定；回傳 (新設定, 是否有變更) 供 GET 一次性遷移落盤。"""
    result: dict[str, list[dict[str, Any]]] = {}
    changed = False
    for platform in _PLATFORMS:
        items = []
        for bot in config.get(platform, []):
            norm = _normalize_bot(bot)
            if norm != bot:
                changed = True
            items.append(norm)
        result[platform] = items
    return result, changed


def _mask_tokens(
    config: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """S1：GET 回傳前遮罩每個 bot 的 token（非空→MASK，空維持空）。"""
    return {
        platform: [{**b, "token": mask_secret(b.get("token"))} for b in config.get(platform, [])]
        for platform in _PLATFORMS
    }


def _preserve_tokens(
    new_config: dict[str, list[dict[str, Any]]],
    existing_config: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """S1 write-only：incoming token 為 MASK 佔位時，從既值還原真 token。

    比對策略：①優先依穩定 id 比對（遷移後的常態）；②若 incoming bot **原本就沒有 id**
    （pre-migration localStorage 在 GET 補 id 落盤前就直接 POST，兩邊都無穩定 id），
    退而依平台內**位置**比對既值，避免真 token 被誤清成空字串（R2/DLC-1 修補）。
    新 bot（前端帶 id、真 token）照原樣保留；確實找不到既值的 MASK→空字串（不落盤遮罩）。
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for platform in _PLATFORMS:
        existing_list = existing_config.get(platform, [])
        existing_by_id = {b["id"]: b for b in existing_list if b.get("id")}
        items = []
        for idx, bot in enumerate(new_config.get(platform, [])):
            had_id = bool(bot.get("id"))
            norm = _normalize_bot(bot)
            if is_masked(norm.get("token")):
                prior = existing_by_id.get(norm["id"]) if had_id else None
                # pre-migration 退路：原本無 id 者依位置對既值（兩邊皆無穩定 id 時的唯一還原依據）
                if prior is None and not had_id and idx < len(existing_list):
                    prior = existing_list[idx]
                norm["token"] = (prior or {}).get("token", "")
            items.append(norm)
        result[platform] = items
    return result

# Batch5 S4: bot config controls external messaging channels (token = channel
# credential). Tightened to {Owner, admin}; Debugger (auto-assigned to
# unauthenticated LAN clients) is excluded, aligning with the reset/destructive
# role posture (ADR-0104 _require_reset_role) and sliding_window admin-only.
# Require Owner/admin to read or write bot configurations.
_require_bots_role = require_owner_or_admin


class BotConfigItem(BaseModel):
    """Bot 設定項目模型（POST 輸入）。

    `agent_id` 維持 AgentIdentity（IDENTITY_SSOT，前端送字串經驗證器 coerce）；
    `token` 為 write-only 機密（POST 收 MASK 代表未變更）；`id` 為穩定識別碼（B2）。
    GET 回應改回 dict 並把 token 遮罩、agent_id 攤平成字串（B3），故不掛 response_model。
    """

    id: str = ""
    token: str
    agent_id: AgentIdentity  # Identity SSOT（字串輸入自動轉物件）
    enabled: bool
    session_mode: str = "independent"
    is_multi_agent: bool = False


class BotsData(BaseModel):
    """Bot 設定資料模型（POST 輸入）。"""

    discord: list[BotConfigItem]
    telegram: list[BotConfigItem]


@router.get("")
async def get_bots_config(
    _identity: AgentIdentity = Depends(_require_bots_role),  # noqa: B008
) -> dict[str, list[dict[str, Any]]]:
    """獲取所有 Bot 設定（token 遮罩、agent_id 攤平為字串；首次讀取一次性補 id 落盤）。"""
    config = bot_service.load_config()
    normalized, changed = _normalize_config(config)
    if changed:
        # 一次性遷移：穩定 id 須先落盤，write-only 才可靠。best-effort——磁碟不可寫時
        # 仍回傳遮罩設定（R7：GET 不因遷移寫入失敗而 500），id 留待下次可寫時穩定。
        try:
            bot_service.save_config(normalized)
        except Exception:
            logger.warning("bots.json id-migration write failed; serving normalized config")
    return _mask_tokens(normalized)


@router.post("")
async def save_bots_config(
    data: BotsData,
    _identity: AgentIdentity = Depends(_require_bots_role),  # noqa: B008
) -> dict:
    """更新並重載 Bot 設定（write-only：MASK token 保留既值）。"""
    try:
        existing, _ = _normalize_config(bot_service.load_config())
        merged = _preserve_tokens(data.model_dump(), existing)
        bot_service.save_config(merged)
        await bot_service.reload_bots()
    except Exception as e:
        raise ToukaException(ErrorCode.SYSTEM_ERROR, error_output=str(e)) from e
    else:
        return {"status": "success", "message": "Bots reloaded."}
