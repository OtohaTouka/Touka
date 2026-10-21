"""SNS 通訊協議基底模組."""
from abc import ABC, abstractmethod
from typing import Any

from src.models.sns import SNSOutgoingMessage


class BaseSNSAdapter(ABC):
    """SNS 適配器的抽象基底類別.

    所有通訊軟體處理器 (Discord, Telegram, etc.) 都必須實作此介面 (SOLID - DIP)。
    """

    def __init__(self, token: str, agent_id: str, **kwargs: Any) -> None:
        """初始化適配器.

        Args:
            token: 平台的 API Token。
            agent_id: 綁定的代理人 ID。
            **kwargs: 平台特定的額外參數。
        """
        self.token = token
        self.agent_id = agent_id
        self.kwargs = kwargs

    @abstractmethod
    async def start(self) -> None:
        """啟動機器人監聽."""

    @abstractmethod
    async def stop(self) -> None:
        """停止機器人."""

    @abstractmethod
    async def send_message(self, channel_id: str, response: SNSOutgoingMessage) -> bool:
        """將 AI 回應發送至物理平台."""

    @abstractmethod
    async def show_typing(self, channel_id: str) -> None:
        """在物理平台上顯示『正在輸入中...』."""
