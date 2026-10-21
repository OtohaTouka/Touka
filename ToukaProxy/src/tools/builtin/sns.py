"""社交網路服務 (SNS) 控制工具模組.

負責將 Agent 的訊息主動發送至外部平台 (如 Discord, Telegram)。
這是 ToukaProxy 跨平台交互能力的一部分，可用於廣播通知或向外部系統回報狀態.
"""

import logging
from typing import Any

from src.services.bot_service import bot_service

logger = logging.getLogger(__name__)


async def send_message(
    platform: str,
    channel_id: str,
    content: str,
    **_kwargs: Any,
) -> str:
    """SNS 主動訊息發送（Tier 3：動詞＋名詞，取代 sns_control 的假 action enum）.

    透過 BotService 將訊息路由至指定的社群平台頻道.

    Args:
        platform (str): 目標平台 (如 'discord', 'telegram')。
        channel_id (str): 目標頻道或群組的唯一識別碼.
        content (str): 要發送的訊息內文.
        **_kwargs (Any): 其他擴充參數.

    Returns:
        str: 發送結果描述或錯誤訊息.
    """
    if not content:
        return "Error: 'content' is required for send_message."

    success = await bot_service.send_external_message(
        platform.lower(), channel_id, content
    )
    if success:
        logger.info(f"✅ SNS: Message sent to {platform} ({channel_id})")
        return f"✅ Successfully sent message to {platform} channel {channel_id}."

    logger.error(f"❌ SNS: Failed to send message to {platform} ({channel_id})")
    return (
        "❌ Failed to send message. "
        "Please ensure the bot is in the channel and the ID is correct."
    )


async def sns_control(
    action: str,
    platform: str,
    channel_id: str,
    content: str,
    **kwargs: Any,
) -> str:
    """舊名 shim（Tier 3 雙軌）：轉呼 :func:`send_message`，不再廣告。"""
    if action == "send_message":
        return await send_message(platform, channel_id, content, **kwargs)
    return f"Error: Unknown action '{action}'."
