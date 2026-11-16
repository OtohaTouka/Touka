"""小幫手決定用的金鑰與配對碼（ADR-0317，禁令 18：跨切面安全邏輯抽成純函式）.

金鑰只有 Touka 和配對的小幫手知道。小幫手已經會驗 Touka 蓋的章；Touka 這一側蓋章的部分，
等 Touka 真的送出決定時才加（ADR-0317 的下一個 PR），在那之前小幫手一律跳視窗。
"""

from __future__ import annotations

import base64
import re
import secrets
from typing import Final

DECISION_KEY_BYTES: Final = 32
PAIRING_CODE_SEPARATOR: Final = "."

DECIDED_BY_CARD: Final = "user_card"
DECIDED_BY_REVIEWER: Final = "auto_review"
DECIDED_BY_YOLO: Final = "yolo"
# 只有這三種會簽給小幫手；在小幫手視窗按的由小幫手自己記
DECIDED_BY_MODES: Final = (DECIDED_BY_CARD, DECIDED_BY_REVIEWER, DECIDED_BY_YOLO)

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
