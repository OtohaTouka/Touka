"""Discord 適配器模組."""
import asyncio
import contextlib
import logging
from typing import Any

import discord

from src.models.sns import SNSAttachment, SNSIncomingEvent
from src.protocols.sns_base import BaseSNSAdapter
from src.services.sns_gateway import sns_gateway

logger = logging.getLogger(__name__)


class DiscordAdapter(BaseSNSAdapter):
    """Discord 平台的 SNS 適配器 (SOLID - SRP)."""

    def __init__(
        self,
        token: str,
        agent_id: str,
        is_multi_agent: bool = False,
        session_mode: str = "independent",
    ) -> None:
        """初始化 Discord 適配器.

        Args:
            token: Discord Bot Token。
            agent_id: 預設代理人 ID。
            is_multi_agent: 是否啟用多代理人自動偵測。
            session_mode: 對話模式。
        """
        super().__init__(token, agent_id)
        self.is_multi_agent = is_multi_agent
        self.session_mode = session_mode
        self.client: discord.Client | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """啟動 Discord 機器人背景任務."""

        async def run_bot() -> None:
            """執行 Discord 機器人的核心迴圈."""
            try:
                intents = discord.Intents.default()
                intents.message_content = True
                self.client = discord.Client(intents=intents)

                @self.client.event
                async def on_ready() -> None:
                    logger.info(f"Discord Bot {self.client.user} is ready. (Gateway Mode)")

                @self.client.event
                async def on_message(message: discord.Message) -> None:
                    if message.author == self.client.user:
                        return

                    is_mentioned = self.client.user in message.mentions
                    is_dm = isinstance(message.channel, discord.DMChannel)

                    if not is_mentioned and not is_dm:
                        return

                    content = message.content.replace(f"<@{self.client.user.id}>", "").strip()
                    if not content:
                        return

                    # 1. 建立標準化事件 (SSOT)
                    attachments = []
                    for attachment in message.attachments:
                        attachments.append(
                            SNSAttachment(
                                type="file",
                                url=attachment.url,
                                content_type=attachment.content_type,
                            )
                        )

                    event = SNSIncomingEvent(
                        platform="discord",
                        sender_id=str(message.author.id),
                        channel_id=str(message.channel.id),
                        content=content,
                        attachments=attachments,
                        raw_event=message,
                    )

                    # 2. 交給 Gateway 處理 (SRP)
                    try:
                        await sns_gateway.on_event(self, event, self.session_mode)
                    except Exception:
                        logger.exception("Discord Gateway error during message processing")
                        await message.reply("❌ 內部錯誤，請稍後再試。")

                await self.client.start(self.token)
            except asyncio.CancelledError:
                logger.info("Discord Bot task cancelled.")
            except Exception:
                logger.exception("Discord Bot crashed unexpectedly")
            finally:
                if self.client and not self.client.is_closed():
                    await self.client.close()

        self._task = asyncio.create_task(run_bot())

    async def stop(self) -> None:
        """停止 Discord 機器人並清理任務."""
        if self.client and not self.client.is_closed():
            await self.client.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def send_message(self, channel_id: str, response: Any) -> bool:
        """實體發送訊息至 Discord.

        Args:
            channel_id: Discord 頻道 ID。
            response: 標準化的輸出訊息物件。

        Returns:
            bool: 是否發送成功。
        """
        if not self.client:
            return False
        try:
            channel = self.client.get_channel(int(channel_id))
            if not channel:
                channel = await self.client.fetch_channel(int(channel_id))

            if channel:
                # 這裡處理文字與可能的媒體
                await channel.send(response.text)
                return True
        except Exception:
            logger.exception(f"Discord send failed to channel {channel_id}")
        return False

    async def show_typing(self, channel_id: str) -> None:
        """在 Discord 頻道顯示輸入狀態."""
        if not self.client:
            return
        try:
            channel = self.client.get_channel(int(channel_id))
            if not channel:
                channel = await self.client.fetch_channel(int(channel_id))
            if channel:
                async with channel.typing():
                    # typing 是一個 context manager，但我們這裡只需要觸發一次
                    pass
        except Exception:
            logger.debug(f"Failed to show typing on Discord channel {channel_id}")
