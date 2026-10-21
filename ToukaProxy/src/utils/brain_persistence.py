"""大腦持久化管理模組.

負責 Agent 大腦型號（模型名稱）的讀寫與持久化。
所有公開 API 均強制使用 AgentIdentity 物件 (Identity SSOT)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)


def _get_settings_path() -> Path:
    """獲取大腦設定檔的實體路體 (SSOT)。"""
    return Path(resource_manager.get_base_path()) / "vault" / "brain_settings.json"


def _normalize_agent_id(agent_id: str) -> str:
    """身分正規化 (Identity Normalization)。"""
    # Normalize common variations to PascalCase (e.g., "debugagent" -> "DebugAgent")
    # The full registry-based normalization is handled by agent_detector.py
    return agent_id


def _get_settings() -> dict[str, Any]:
    """從檔案系統讀取大腦設定 (Private)。"""
    settings_path = _get_settings_path()
    if settings_path.exists():
        try:
            with settings_path.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("Error reading brain_settings")
    return {}


def _save_settings(settings: dict[str, Any]) -> None:
    """將大腦設定寫回檔案系統 (Private DRY helper)。"""
    settings_path = _get_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with settings_path.open("w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4, ensure_ascii=False)
    except Exception:
        logger.exception("Error saving brain_settings")


def get_active_brain(identity: AgentIdentity) -> str | None:
    """獲取 Agent 目前指定的大腦型號 (Identity SSOT)。

    Args:
        identity: 經驗證的代理人身分物件。

    Returns:
        目前的大腦型號名稱，若未設定則回傳 None。
    """
    agent_id = _normalize_agent_id(identity.agent_id)
    settings = _get_settings()
    val = settings.get(agent_id)

    if not val:
        logger.warning("No brain setting found for Agent '%s'.", agent_id)
        return None

    if isinstance(val, dict):
        val = val.get("active_brain")
        if not val:
            logger.error(
                "Agent '%s' setting is missing 'active_brain' field.",
                agent_id,
            )
            return None

    # 舊的靈魂路由 ID 不是模型：當作沒設，不偷換寫死模型（ADR-0250 L1；ADR-0241 決策 6）
    if "toukasoul" in str(val).lower():
        logger.warning("Agent '%s' brain is a legacy soul route; treated as not set.", agent_id)
        return None
    return str(val)


def normalize_model_choice(value: object) -> str | None:
    """「你指定的模型」：None、空字串、只有空白（以及壞檔裡的非字串）都當沒指定（ADR-0250）。"""
    return (value.strip() or None) if isinstance(value, str) else None


def should_set_active_brain(
    *, is_human: bool, old: str | None, new: str | None, has_brain: bool
) -> bool:
    """靈魂存檔要不要把主要模型換成「你指定的」（ADR-0250 A1，修訂 ADR-0241 決策 6）。

    人類存檔、有指定，而且改了值或目前沒有主要模型（救援）才換；代理人存檔一律不換。
    新值能不能用由呼叫端驗證，不能用要回錯誤、不能默默略過。
    """
    if not is_human or not new:
        return False
    return new != old or not has_brain


def get_last_working_brain(identity: AgentIdentity) -> str | None:
    """獲取該 Agent 上一個連線成功的大腦 (Identity SSOT)。

    Args:
        identity: 經驗證的代理人身分物件。

    Returns:
        上一個可靠大腦型號名稱，若未紀錄則回傳 None。
    """
    agent_id = _normalize_agent_id(identity.agent_id)
    settings = _get_settings()
    val = settings.get(agent_id)
    if isinstance(val, dict):
        return val.get("last_working_brain")
    return None


def set_active_brain(identity: AgentIdentity, model_id: str) -> None:
    """更新 Agent 的大腦型號 (Identity SSOT)。

    Args:
        identity: 經驗證的代理人身分物件。
        model_id: 目標模型 ID。
    """
    agent_id = _normalize_agent_id(identity.agent_id)
    if not agent_id or agent_id.lower() == "system":
        return

    settings = _get_settings()

    if agent_id not in settings or not isinstance(settings[agent_id], dict):
        settings[agent_id] = {
            "active_brain": model_id,
            "last_working_brain": model_id,
        }
    else:
        settings[agent_id]["active_brain"] = model_id

    _save_settings(settings)
    logger.info("Brain updated for %s -> %s", agent_id, model_id)


def record_working_brain(identity: AgentIdentity, model_id: str) -> None:
    """當型號執行成功時，記錄為可靠大腦 (Identity SSOT)。

    Args:
        identity: 經驗證的代理人身分物件。
        model_id: 執行成功的模型 ID。
    """
    agent_id = _normalize_agent_id(identity.agent_id)
    if not agent_id or agent_id.lower() == "system":
        return

    settings = _get_settings()
    if agent_id not in settings or not isinstance(settings[agent_id], dict):
        settings[agent_id] = {
            "active_brain": model_id,
            "last_working_brain": model_id,
        }
    else:
        settings[agent_id]["last_working_brain"] = model_id

    _save_settings(settings)


def clear_agent(identity: AgentIdentity) -> bool:
    """移除某 agent 的大腦設定鍵（破壞性重置 Batch 6 用）。回是否有刪到。"""
    agent_id = _normalize_agent_id(identity.agent_id)
    settings = _get_settings()
    if agent_id in settings:
        del settings[agent_id]
        _save_settings(settings)
        return True
    return False


def has_agent(identity: AgentIdentity) -> bool:
    """某 agent 是否有大腦設定（完整性自檢）。"""
    return _normalize_agent_id(identity.agent_id) in _get_settings()
