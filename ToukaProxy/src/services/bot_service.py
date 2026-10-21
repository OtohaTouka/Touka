"""機器人管理服務模組."""
import asyncio
import logging
from typing import Any

from src.adapters.discord_bot import DiscordAdapter
from src.adapters.telegram_bot import TelegramAdapter
from src.config import get_config_dir
from src.services.resource_manager import resource_manager

logger = logging.getLogger(__name__)


class BotService:
    """機器人管理服務 (SOLID - SRP).

    負責從設定檔載入、啟動、停止以及管理所有 SNS 機器人適配器。
    """

    def __init__(self) -> None:
        """初始化機器人服務."""
        self.config_path = get_config_dir() / "bots.json"
        self._running_bots: dict[str, dict[str, Any]] = {"discord": {}, "telegram": {}}
        self._tasks: list[asyncio.Task[None]] = []

    def load_config(self) -> dict[str, list[dict[str, Any]]]:
        """從設定檔載入機器人配置.

        Returns:
            dict: 包含 'discord' 與 'telegram' 配置列表的字典。
        """
        data = resource_manager.read_json(self.config_path)
        return data if data else {"discord": [], "telegram": []}

    def save_config(self, config: dict[str, list[dict[str, Any]]]) -> None:
        """將機器人配置儲存至設定檔.

        Args:
            config: 要儲存的配置字典。
        """
        resource_manager.make_dir(self.config_path.parent)
        resource_manager.write_json(self.config_path, config)

    # handle_bot_request 已由 SNSGatewayService 接管

    async def start_bots(self) -> None:
        """根據配置啟動所有啟用的機器人."""
        config = self.load_config()

        # 啟動 Discord
        for bot_conf in config.get("discord", []):
            if bot_conf.get("enabled", False) and bot_conf.get("token"):
                token = bot_conf.get("token")
                key = f"discord_{token[:5]}"
                if key not in self._running_bots["discord"]:
                    bot = DiscordAdapter(
                        token=token,
                        agent_id=bot_conf.get("agent_id"),
                        is_multi_agent=bot_conf.get("is_multi_agent", False),
                        session_mode=bot_conf.get("session_mode", "independent"),
                    )
                    task = asyncio.create_task(bot.start())
                    self._tasks.append(task)
                    self._running_bots["discord"][key] = bot

        # 啟動 Telegram
        for bot_conf in config.get("telegram", []):
            if bot_conf.get("enabled", False) and bot_conf.get("token"):
                token = bot_conf.get("token")
                key = f"telegram_{token[:5]}"
                if key not in self._running_bots["telegram"]:
                    bot = TelegramAdapter(
                        token=token,
                        agent_id=bot_conf.get("agent_id"),
                        is_multi_agent=bot_conf.get("is_multi_agent", False),
                        session_mode=bot_conf.get("session_mode", "independent"),
                    )
                    task = asyncio.create_task(bot.start())
                    self._tasks.append(task)
                    self._running_bots["telegram"][key] = bot

    async def stop_bots(self) -> None:
        """停止所有正在運行的機器人."""
        for bot in self._running_bots["discord"].values():
            await bot.stop()
        for bot in self._running_bots["telegram"].values():
            await bot.stop()
        self._running_bots = {"discord": {}, "telegram": {}}
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    async def reload_bots(self) -> None:
        """重新載入並啟動所有機器人 (熱重載支援)."""
        logger.info("Reloading Bots...")
        await self.stop_bots()
        await self.start_bots()

    async def send_external_message(
        self, platform: str, channel_id: str, content: str
    ) -> bool:
        """從外部（如 Tool 或 Heartbeat）發送訊息至指定 SNS 頻道.

        Args:
            platform: 平台類型 ('discord' 或 'telegram')。
            channel_id: 頻道/對話 ID。
            content: 訊息內容。

        Returns:
            bool: 是否發送成功。
        """
        try:
            if platform == "discord":
                # 尋找第一個可用的 Discord Bot
                for bot in self._running_bots["discord"].values():
                    if not bot.client:
                        continue
                    channel = bot.client.get_channel(int(channel_id))
                    if not channel:
                        # 嘗試 fetch
                        try:
                            channel = await bot.client.fetch_channel(int(channel_id))
                        except Exception:
                            logger.debug("Failed to fetch Discord channel in external send")
                            continue
                    if channel:
                        await channel.send(content)
                        return True
            elif platform == "telegram":
                for bot in self._running_bots["telegram"].values():
                    try:
                        await bot.application.bot.send_message(
                            chat_id=channel_id, text=content
                        )
                    except Exception:
                        logger.debug("Telegram external send failed in loop")
                        continue
                    else:
                        return True
        except Exception:
            logger.exception(f"Failed to send external message to {platform}:{channel_id}")
        return False


bot_service = BotService()
