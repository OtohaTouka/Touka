"""SNS 訊息模型 (SSOT)."""

from typing import Any

from pydantic import BaseModel


class SNSAttachment(BaseModel):
    """SNS 附件模型，支援圖片、影片、音訊與通用檔案."""

    type: str  # image, video, file, audio
    url: str | None = None
    file_id: str | None = None  # 平台特定的 ID (例如 Telegram file_id)
    content_type: str | None = None


class SNSIncomingEvent(BaseModel):
    """標準化 SNS 輸入事件模型 (SSOT)."""

    platform: str  # discord, telegram, slack, etc.
    sender_id: str
    channel_id: str
    content: str
    attachments: list[SNSAttachment] = []
    raw_event: Any | None = None  # 保留原始事件以供特殊處理
    # C7 fix: the platform verified where this event came from — a webhook whose signature
    # was checked (e.g., Discord Ed25519, Telegram secret token, Line HMAC-SHA256), or a
    # connection opened with the bot token (gateway / long polling). It does not mean the
    # sender is trusted (ADR-0276 still decides what outsiders can do). Every adapter must
    # pass is_verified explicitly (ADR-0324).
    is_verified: bool = False
    # 發言者說明（ADR-0322 PR 3）：私訊或公開、平台上的顯示名稱；沒有就是不明
    is_direct: bool | None = None
    sender_name: str | None = None


class SNSOutgoingMessage(BaseModel):
    """標準化 SNS 輸出訊息模型 (SSOT)."""

    text: str
    media_urls: list[str] = []
    components: list[dict] = []  # 互動按鈕等
    metadata: dict = {}
