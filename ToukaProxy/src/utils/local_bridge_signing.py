"""Touka 在小幫手的決定上蓋的章（ADR-0317，禁令 18：跨切面安全邏輯抽成純函式）.

金鑰只有 Touka 和配對的小幫手知道；冒充 Touka 的程式蓋不出對的章，小幫手就改跳視窗。
小幫手的 ``touka_local/signing.py`` 存一份同樣的算法，兩邊用同一組固定測資核對。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Final

DECISION_KEY_BYTES: Final = 32
SIGNATURE_VERSION: Final = 2
PAIRING_CODE_SEPARATOR: Final = "."

ACT_PROPOSE: Final = "propose"

DECIDED_BY_CARD: Final = "user_card"
DECIDED_BY_REVIEWER: Final = "auto_review"
DECIDED_BY_YOLO: Final = "yolo"
# 只有這三種會簽給小幫手；在小幫手視窗按的由小幫手自己記
DECIDED_BY_MODES: Final = (DECIDED_BY_CARD, DECIDED_BY_REVIEWER, DECIDED_BY_YOLO)

# 和 tools/execution_context.APPROVED_BY_AUTO_REVIEWER 相同（utils 不往上匯入服務層，測試釘住）
_APPROVED_BY_AUTO_REVIEWER: Final = "auto_reviewer"
_YOLO_LEVEL: Final = "YOLO"

_KEY_HEX: Final = re.compile(r"[0-9a-f]{64}")


def new_decision_key() -> bytes:
    """產生新的金鑰，配對時才產生一次。"""
    return secrets.token_bytes(DECISION_KEY_BYTES)


def decision_key_from_hex(value: str | None) -> bytes | None:
    """存起來的金鑰；格式不對就當作沒有，不送決定。"""
    if value is None or _KEY_HEX.fullmatch(value) is None:
        return None
    return bytes.fromhex(value)


def pairing_code(token: str, key: bytes) -> str:
    """配對碼：權杖、點、金鑰（base64url、不帶補齊）。只顯示一次。"""
    encoded = base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")
    return f"{token}{PAIRING_CODE_SEPARATOR}{encoded}"


def text_fingerprint(text: str) -> str:
    """顯示文字的指紋：UTF-8 的 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sign(key: bytes, fields: dict[str, object]) -> str:
    message = json.dumps(
        {**fields, "v": SIGNATURE_VERSION},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def sign_decision(
    key: bytes, *, job_id: str, nonce: str, digest: str, shown: str, decided_by: str
) -> str:
    """搬移、刪除清單的決定。工作編號、預覽的一次性號碼、清單與顯示文字的指紋都綁進去。"""
    return _sign(
        key,
        {
            "act": ACT_PROPOSE,
            "by": decided_by,
            "digest": digest,
            "job": job_id,
            "nonce": nonce,
            "shown": shown,
        },
    )


def derive_decided_by(
    *,
    hitl_preapproved: bool,
    preview_shown_complete: bool,
    resolver_matches: bool,
    approved_by: str | None,
    auth_level: str,
) -> str | None:
    """誰決定的。只看 Touka 內部的可信值，回 None 就不送決定，小幫手跳視窗。"""
    if hitl_preapproved:
        if preview_shown_complete and resolver_matches:
            return DECIDED_BY_CARD
        return None
    if approved_by is not None:
        return DECIDED_BY_REVIEWER if approved_by == _APPROVED_BY_AUTO_REVIEWER else None
    if auth_level == _YOLO_LEVEL:
        return DECIDED_BY_YOLO
    return None
