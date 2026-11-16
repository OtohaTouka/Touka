"""核對 Touka 在決定上蓋的章（ADR-0317）。

Touka 端的 ``ToukaProxy/src/utils/local_bridge_signing.py`` 是準；這裡另存一份是因為小幫手
不能載入 Touka 的程式。兩邊用同一組固定測資核對。章不對、格式不對、沒有鑰匙，一律當作
沒有決定，改跳視窗。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import TYPE_CHECKING, Final

from touka_local.protocol import DECIDED_BY_MODES

if TYPE_CHECKING:
    from touka_local.protocol import Decision

SIGNATURE_VERSION: Final = 2
DECISION_KEY_BYTES: Final = 32
ACT_PROPOSE: Final = "propose"

_NONCE: Final = re.compile(r"[A-Za-z0-9_-]{22}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_KEY_TEXT: Final = re.compile(r"[A-Za-z0-9_-]{43}")


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
    """和 Touka 相同的算法，用來核對。"""
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


def decision_signature_ok(key: bytes | None, job_id: str, decision: Decision) -> bool:
    """章是不是 Touka 用配對的鑰匙蓋在這件工作上的。格式不對也算不對。"""
    if (
        key is None
        or len(key) != DECISION_KEY_BYTES
        or decision.decided_by not in DECIDED_BY_MODES
        or _NONCE.fullmatch(decision.nonce) is None
        or _HEX64.fullmatch(decision.digest) is None
        or _HEX64.fullmatch(decision.shown) is None
        or _HEX64.fullmatch(decision.signature) is None
    ):
        return False
    expected = sign_decision(
        key,
        job_id=job_id,
        nonce=decision.nonce,
        digest=decision.digest,
        shown=decision.shown,
        decided_by=decision.decided_by,
    )
    return hmac.compare_digest(expected, decision.signature)


def split_pairing_code(code: str) -> tuple[str, bytes | None]:
    """配對碼拆成權杖和鑰匙。舊的配對碼沒有鑰匙；鑰匙格式不對也當作沒有。"""
    token, separator, key_text = code.partition(".")
    if not separator or _KEY_TEXT.fullmatch(key_text) is None:
        return token, None
    try:
        key = base64.urlsafe_b64decode(key_text + "=")
    except ValueError:  # binascii.Error 是 ValueError 的子類
        return token, None
    return token, key if len(key) == DECISION_KEY_BYTES else None
