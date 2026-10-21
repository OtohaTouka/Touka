"""記憶來源定位 (SourceRef).

存記憶時由系統（不是模型）記下這段記憶從哪來：哪段對話或哪次心跳、觸發它的已保存訊息，
以及那則訊息的 SHA-256。只記位置與指紋，不存原文。查回只回狀態，原文不在也不刪記憶。
"""

from __future__ import annotations

import hashlib
import json
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from memory_server.protocols import ISourceResolver

logger = logging.getLogger(__name__)

CONTENT_SHA256_PATTERN = r"^[0-9a-f]{64}$"

# 查詢帶來源時每筆附的一行（給模型看；只講狀態，不帶原文）
SOURCE_NOT_RECORDED = "來源未記錄"  # sources 是 NULL：沒記到，不代表可信或不可信
SOURCE_UNREADABLE_NOW = "來源暫時無法讀取"  # 結構不是這版的目前版（升級沒完成或降版執行）
SOURCE_UNCONFIRMED = "無法確認"  # 查回失敗、沒有查回器，或這一欄格式壞掉


class SourceKind(StrEnum):
    """來源種類；第一批只有這兩類."""

    CONVERSATION = "conversation"
    HEARTBEAT = "heartbeat"


class SourceStatus(StrEnum):
    """查回狀態：讀時算、不寫回事實庫."""

    AVAILABLE = "available"
    CHANGED = "changed"
    MISSING = "missing"


SOURCE_KIND_LABELS = {SourceKind.CONVERSATION: "對話", SourceKind.HEARTBEAT: "心跳"}
SOURCE_STATUS_TEXT = {
    SourceStatus.AVAILABLE: "原文仍在",
    SourceStatus.CHANGED: "原文已修改",
    SourceStatus.MISSING: "原文已不在",
}


class SourceRef(BaseModel):
    """一筆來源定位；只由 Proxy 記憶閘道依可信執行資訊組成，模型不能提供."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SourceKind
    session_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1, description="觸發這次存記憶的已保存 user 節點")
    request_id: str | None = None
    captured_at: int = Field(ge=0, description="記下來源的時間（秒）")
    content_sha256: str = Field(pattern=CONTENT_SHA256_PATTERN)


def content_fingerprint(content: Any) -> str:
    """訊息內容的 SHA-256（存記憶與查回共用同一算法）."""
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_sources(raw: str | None, fact_id: Any) -> list[dict[str, Any]] | None:
    """事實的 sources 欄 → 普通 dict 清單（GDPR 匯出可直接序列化）；NULL 回 None，壞掉回空清單."""
    if raw is None:
        return None
    try:
        return [SourceRef.model_validate(item).model_dump(mode="json") for item in json.loads(raw)]
    except (TypeError, ValueError):
        logger.warning("[Sources] fact %s has a malformed sources field", fact_id)  # 只記 id
        return []


async def describe_sources(
    fact: dict[str, Any], identity: Any, resolver: ISourceResolver | None
) -> str:
    """查詢結果每筆附的一行來源摘要；查回失敗只影響這一筆，不讓整個查詢失敗."""
    if "sources" not in fact:  # 結構還停在舊版：這一欄讀不到
        return SOURCE_UNREADABLE_NOW
    if fact["sources"] is None:
        return SOURCE_NOT_RECORDED
    parts = []
    for item in fact["sources"]:
        ref = SourceRef.model_validate(item)
        status = SOURCE_UNCONFIRMED
        if resolver is not None:
            try:
                status = SOURCE_STATUS_TEXT[await resolver.resolve(identity, ref)]
            except Exception:
                logger.warning("[Sources] could not check the source of %s", fact.get("id"))
        where = f"{SOURCE_KIND_LABELS[ref.kind]} {ref.session_id}／訊息 {ref.message_id}"
        parts.append(f"{where}：{status}")
    return "來源：" + ("；".join(parts) or SOURCE_UNCONFIRMED)
