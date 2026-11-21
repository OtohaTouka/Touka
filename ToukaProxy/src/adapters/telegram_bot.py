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
from src.utils.sns_sender import telegram_sender_fields

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

        self.application = ApplicationBuilder().token(self.token).build()

        async def start_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
            """處理 /start 指令."""
            if update.message:
                await update.message.reply_text("Hello! I am your Touka proxy bot (Unified Mode).")

        # 沒有 /switch：任何外人都能用它把整台機器人換到任何代理人（ADR-0327）。
        # 要找同一位擁有者的別的靈魂，在訊息裡寫它的名字或「@名字」。
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
                    # 經平台驗證的 bot 連線收到（long polling），不是 webhook（ADR-0324）
                    is_verified=True,
                    **telegram_sender_fields(update),
                )

                # 2. 交給 Gateway 處理 (SRP)；一律用機器人綁的靈魂
                await sns_gateway.on_event(self, event, self.session_mode)

            except Exception:
                logger.exception("Telegram Gateway error during message processing")
                await update.message.reply_text("❌ 內部錯誤，請稍後再試。")

        self.application.add_handler(CommandHandler("start", start_command))
        self.application.add_handler(
            MessageHandler(filters.TEXT & (~filters.COMMAND), respond_to_message)
        )

    async def start(self) -> None:
        """啟動 Telegram 機器人輪詢；已停用的不啟動，啟動途中被停用的自己停下（ADR-0326）."""
        if self.retired:
            return
        try:
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram Bot is ready. (Gateway Mode)")
        except Exception:
            logger.exception("Failed to start Telegram bot polling")
        # 啟動途中被停用時，停用那次的 stop() 還停不到（還沒在跑），這裡補停
        if self.retired:
            await self.stop()

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
