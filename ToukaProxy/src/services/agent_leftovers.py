"""刪掉的靈魂留下、不該給同名新靈魂的設定與數字（ADR-0326）.

依靈魂編號存放、靈魂刪掉後還留著的四樣：心跳排程（ToukaHeartbeat）、綁著它的 SNS 機器人
（``bots.json`` 與正在跑的 adapter）、身分設定（``identity_map.json`` 的 ``agent_overrides``）、統計
（``data/stats/<編號>``）。各自的讀寫仍在原處（排程橋、``bot_service``、``permission_service``、
``resource_manager``），這裡只組合。看板歸 ``mission_boards``（ADR-0325），對話另外處理。

- 刪除靈魂：鎖內刪統計（``remove_stats_on_soul_delete``）；身分設定在取消註冊那步刪；放開鎖之後
  ``clean_up_after_delete``：機器人一律停用（不看名字有沒有人用），排程先確認名字仍然沒人用再刪，
  最後等停用的機器人停下來；沒清成的回給呼叫端列進 ``skipped``。
- 建立靈魂：所有拒絕檢查之後、名字不屬於任何代理人時呼叫 ``clear_for_new_soul``。排程第一個：
  它是唯一會因為連不上而拒絕的一步，拒絕時機器人、看板、身分設定、統計都還沒動。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from src.config import HEARTBEAT_EXTERNAL_URL, HEARTBEAT_TOKEN
from src.core.exceptions import ErrorCode, ToukaException
from src.services import mission_boards
from src.services.bot_service import BotsDisabled, bot_service
from src.services.heartbeat_schedule_bridge import HeartbeatScheduleBridge
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.utils.agent_spellings import legacy_spellings

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity

logger = logging.getLogger(__name__)

# 刪除靈魂放開鎖之後清排程的上限（回應最多多等這麼久）。
SCHEDULE_CLEANUP_SECONDS = 15.0


def _system_bridge() -> HeartbeatScheduleBridge | None:
    """系統清理用的排程橋（不經人類擁有權）；沒設定心跳位址回 None."""
    if not HEARTBEAT_EXTERNAL_URL:
        return None
    return HeartbeatScheduleBridge(HEARTBEAT_EXTERNAL_URL, HEARTBEAT_TOKEN, lambda _agent: True)


async def _remove_schedules(identity: AgentIdentity) -> int:
    """刪這個名字兩種寫法的排程，回刪了幾筆；位址沒設定回 0 並記警告；其他錯誤往外丟."""
    bridge = _system_bridge()
    if bridge is None:
        logger.warning(
            "There is no Heartbeat address, so schedules of %s were not checked", identity.agent_id
        )
        return 0
    return await bridge.delete_for_agent(legacy_spellings(identity.agent_id))


def _remove_stats(identity: AgentIdentity) -> bool:
    """刪 ``data/stats/<編號>``（根本身是連結時 ``remove_dir`` 拒絕）；刪不掉回 False.

    什麼例外都接：刪除靈魂時這步在鎖內，看板已經刪了，不能讓意料外的例外（例如 data 根沒登記的
    ``RuntimeError``）把刪除變成 500、留下一半；建立時呼叫端把 False 變成 ``SYSTEM_ERROR``。
    """
    try:
        resource_manager.remove_dir(
            Path(resource_manager.get_stats_path(identity)), missing_ok=True
        )
    except Exception:
        logger.exception("❌ Stats of %s were not removed", identity.agent_id)
        return False
    return True


async def clear_for_new_soul(identity: AgentIdentity) -> None:
    """建立靈魂前清這個名字的舊排程、機器人、身分設定、統計；呼叫端已判定名字不屬於任何代理人.

    排程清不掉（連不上、結果不明）丟原本的錯誤碼；機器人設定（讀不出來、寫不進去）、身分設定、
    統計清不掉丟 ``SYSTEM_ERROR``。正在跑的機器人不論設定寫不寫得成都先停。丟出時靈魂什麼都還沒建。
    """
    removed = await _remove_schedules(identity)
    if removed:
        logger.info("🧹 Removed %d old schedule(s) of %s", removed, identity.agent_id)
    disabled = bot_service.disable_bots_for(identity)
    await bot_service.stop_adapters(disabled.adapters)
    if not disabled.config_ok:
        raise ToukaException(ErrorCode.SYSTEM_ERROR)
    if not permission_service.remove_agent_overrides(legacy_spellings(identity.agent_id)):
        raise ToukaException(ErrorCode.SYSTEM_ERROR)
    if not _remove_stats(identity):
        raise ToukaException(ErrorCode.SYSTEM_ERROR)


def remove_stats_on_soul_delete(identity: AgentIdentity) -> bool:
    """刪除靈魂時（鎖內）刪它的統計；刪不掉回 False（呼叫端列進 ``skipped``）."""
    return _remove_stats(identity)


def _disable_bots_after_delete(identity: AgentIdentity) -> BotsDisabled:
    """刪除後停用綁著它的機器人；意料外的例外當成設定沒寫成（刪除已經做完，不往外丟）."""
    try:
        return bot_service.disable_bots_for(identity)
    except Exception:
        logger.exception("❌ Bots bound to %s were not disabled", identity.agent_id)
        return BotsDisabled(config_ok=False, adapters=[])


async def _remove_schedules_after_delete(identity: AgentIdentity) -> bool:
    """刪除後刪它的排程；名字有人用、清不掉、逾時都記警告、回 False，不往外丟.

    先確認名字仍然沒人用。呼叫端在放開鎖之後、第一個 await 之前走到這裡，所以「有人用」只會是
    這次刪除沒做完（資料夾沒刪掉、登記表沒寫成、``custom/<名字>`` 留著）：排程不刪，回應要看得出來。
    """
    try:
        if not mission_boards.is_unclaimed(identity):
            logger.warning(
                "Schedules of %s kept: the name still belongs to an agent (delete incomplete?)",
                identity.agent_id,
            )
            return False
        async with asyncio.timeout(SCHEDULE_CLEANUP_SECONDS):
            await _remove_schedules(identity)
    except Exception:
        logger.warning(
            "The schedules of %s were not removed; deleting the soul again retries",
            identity.agent_id,
            exc_info=True,
        )
        return False
    return True


async def clean_up_after_delete(identity: AgentIdentity) -> list[str]:
    """刪除靈魂放開鎖之後：停用機器人、刪排程、等機器人停下來；回沒清成的項目（列進 ``skipped``）.

    機器人一律停用，不看名字有沒有人用：放開鎖到這裡沒有 await，同名的新靈魂不可能已經建好，
    綁著這個名字的機器人這時也接不到任何靈魂。停用是同步的，排程那步的判斷因此仍在第一個 await
    之前。等機器人停下來放最後、包在 ``finally`` 裡（請求被取消也照停）。刪除已經做完，不往外丟。
    """
    failed: list[str] = []
    disabled = _disable_bots_after_delete(identity)
    if not disabled.config_ok:
        failed.append("bots")
    try:
        if not await _remove_schedules_after_delete(identity):
            failed.append("schedules")
    finally:
        await bot_service.stop_adapters(disabled.adapters)
    return failed
