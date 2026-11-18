"""SNS 訊息開頭的發言者說明（ADR-0322 PR 3 SNS-1）.

外人訊息存進對話與送給模型時是「固定開頭＋這一行＋內容」：平台、私訊或公開、發言者名稱與代號、
附件數。名稱是外人能控制的文字：壓成一行、拿掉控制字元與標記符號、``@`` 換成全形（群聊點名只認
半形），最多 ``SENDER_NAME_MAX`` 字。代號是平台加帳號編號的雜湊前幾碼，不存帳號編號本身。
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import TYPE_CHECKING, Any

from src.utils.interjection_notes import DELIVERY_PLATFORM_NAMES, OTHER_PLATFORM_NAME
from src.utils.user_intent import SNS_MESSAGE_PREFIX

if TYPE_CHECKING:
    from src.models.sns import SNSIncomingEvent

SENDER_NAME_MAX = 40
SENDER_CODE_CHARS = 6
UNKNOWN_FIELD = "不明"
SENDER_LINE = "平台：{platform}｜{place}｜發言者：{name}（代號 {code}）｜{attachments}"
PLACE_TEXTS = {True: "私訊", False: "公開頻道", None: "私訊或公開不明"}
NO_ATTACHMENTS = "沒有附件"
ATTACHMENTS_TEMPLATE = "附件 {count} 個（讀不到內容）"
# 名稱裡偽造得出開頭、記號、分隔或點名的字：拿掉或換掉
_NAME_MAP = str.maketrans({"@": "＠", **dict.fromkeys("〔〕【】<>＜＞｜")})
_MISSING_SENDER_IDS = ("", "unknown")  # Telegram 沒有使用者時填 "unknown"
# 內文裡的【】換成相近字：外人假造不出另一組「固定開頭＋發言者行」冒充別人（審查 LOW-1）
_CONTENT_MAP = str.maketrans({"【": "〖", "】": "〗"})


def _clean_name(name: str | None) -> str:
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in name or ""
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    return " ".join(kept.translate(_NAME_MAP).split())[:SENDER_NAME_MAX] or UNKNOWN_FIELD


def sns_sender_line(event: SNSIncomingEvent) -> str:
    """一行說明這則外人訊息從哪個平台、私訊或公開、誰傳來、有幾個附件."""
    platform = DELIVERY_PLATFORM_NAMES.get(event.platform.lower(), OTHER_PLATFORM_NAME)
    code = (
        UNKNOWN_FIELD
        if event.sender_id in _MISSING_SENDER_IDS
        else hashlib.sha256(f"{event.platform}:{event.sender_id}".encode()).hexdigest()
    )[:SENDER_CODE_CHARS]
    count = len(event.attachments)
    return SENDER_LINE.format(
        platform=platform,
        place=PLACE_TEXTS[event.is_direct],
        name=_clean_name(event.sender_name),
        code=code,
        attachments=ATTACHMENTS_TEMPLATE.format(count=count) if count else NO_ATTACHMENTS,
    )


def sns_message_content(event: SNSIncomingEvent) -> str:
    """存檔與送出一致：固定開頭（審查員靠它排除外人訊息）、發言者行、內容."""
    content = event.content.translate(_CONTENT_MAP)
    return f"{SNS_MESSAGE_PREFIX}\n{sns_sender_line(event)}\n{content}"


def discord_sender_fields(author: Any, *, is_dm: bool) -> dict[str, Any]:
    """Discord 訊息的私訊旗標與作者顯示名稱（給 ``SNSIncomingEvent``）."""
    return {"is_direct": is_dm, "sender_name": getattr(author, "display_name", None)}


def telegram_sender_fields(update: Any) -> dict[str, Any]:
    """Telegram 更新的私人聊天旗標與使用者全名（給 ``SNSIncomingEvent``）."""
    chat, user = update.effective_chat, update.effective_user
    return {
        "is_direct": None if chat is None else chat.type == "private",
        "sender_name": None if user is None else user.full_name,
    }
