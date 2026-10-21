"""Telegram 適配器模組."""
import contextlib
import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.models.sns import SNSIncomingEvent
from src.protocols.sns_base import BaseSNSAdapter
from src.services.sns_gateway import sns_gateway

logger = logging.getLogger(__name__)


class TelegramAdapter(BaseSNSAdapter):
    """Telegram 平台的 SNS 適配器 (SOLID - SRP)."""

    def __init__(
        self,
        token: str,
        agent_id: str,
        is_multi_agent: bool = False,
        session_mode: str = "independent",
    ) -> None:
        """初始化 Telegram 適配器.

        Args:
            token: Telegram Bot Token。
            agent_id: 預設代理人 ID。
            is_multi_agent: 是否啟用多代理人自動偵測。
            session_mode: 對話模式。
        """
        super().__init__(token, agent_id)
        self.is_multi_agent = is_multi_agent
        self.session_mode = session_mode
        self.current_agent_override: str | None = None

        self.application = ApplicationBuilder().token(self.token).build()

        async def start_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
            """處理 /start 指令."""
            if update.message:
                await update.message.reply_text("Hello! I am your Touka proxy bot (Unified Mode).")

        async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            """處理 /switch 指令以手動切換代理人."""
            if not update.message:
                return

            if not context.args:
                await update.message.reply_text("請提供要切換的 Agent ID，例如: /switch AgentName")
                return

            new_agent = context.args[0]
            self.current_agent_override = new_agent
            await update.message.reply_text(f"已手動切換至 Agent: {new_agent}")

        async def respond_to_message(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
            """處理一般文字訊息."""
            if not update.message or not update.message.text:
                return

            content = update.message.text

            try:
                # 1. 建立標準化事件 (SSOT)
                sender_id = (
                    str(update.effective_user.id) if update.effective_user else "unknown"
                )
                channel_id = (
                    str(update.effective_chat.id) if update.effective_chat else "unknown"
                )

                event = SNSIncomingEvent(
                    platform="telegram",
                    sender_id=sender_id,
                    channel_id=channel_id,
                    content=content,
                    raw_event=update,
                )

                # 2. 處理 Agent Override
                original_agent_id = self.agent_id
                if self.current_agent_override:
                    self.agent_id = self.current_agent_override

                # 3. 交給 Gateway 處理 (SRP)
                try:
                    await sns_gateway.on_event(self, event, self.session_mode)
                finally:
                    # 恢復原始設定
                    self.agent_id = original_agent_id

            except Exception:
                logger.exception("Telegram Gateway error during message processing")
                await update.message.reply_text("❌ 內部錯誤，請稍後再試。")

        self.application.add_handler(CommandHandler("start", start_command))
        self.application.add_handler(CommandHandler("switch", switch_command))
        self.application.add_handler(
            MessageHandler(filters.TEXT & (~filters.COMMAND), respond_to_message)
        )

    async def start(self) -> None:
        """啟動 Telegram 機器人輪詢."""
        try:
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram Bot is ready. (Gateway Mode)")
        except Exception:
            logger.exception("Failed to start Telegram bot polling")

    async def stop(self) -> None:
        """停止 Telegram 機器人服務."""
        try:
            if self.application.updater and self.application.updater.running:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        except Exception:
            logger.exception("Error while stopping Telegram bot")

    async def send_message(self, channel_id: str, response: Any) -> bool:
        """實體發送訊息至 Telegram.

        Args:
            channel_id: Telegram 對話 ID。
            response: 標準化的輸出訊息物件。

        Returns:
            bool: 是否發送成功。
        """
        try:
            await self.application.bot.send_message(chat_id=channel_id, text=response.text)
        except Exception:
            logger.exception(f"Telegram send failed to chat {channel_id}")
            return False
        else:
            return True

    async def show_typing(self, channel_id: str) -> None:
        """在 Telegram 對話中顯示正在輸入狀態."""
        try:
            await self.application.bot.send_chat_action(chat_id=channel_id, action="typing")
        except Exception:
            with contextlib.suppress(Exception):
                logger.debug(f"Failed to show typing on Telegram chat {channel_id}")
