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

from touka_local.protocol import DECIDED_BY_MODES, DECIDED_BY_OPEN_REQUEST

if TYPE_CHECKING:
    from touka_local.protocol import Decision

SIGNATURE_VERSION: Final = 2
DECISION_KEY_BYTES: Final = 32
ACT_PROPOSE: Final = "propose"
ACT_REQUEST_OPEN: Final = "request_open"
ACT_REVOKE: Final = "revoke"

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


def sign_open_request(key: bytes, *, job_id: str, nonce: str, path: str) -> str:
    """開放請求的章（ADR-0320），和 Touka 相同的算法。``act`` 不同，和清單的章不能互用。"""
    fingerprint = text_fingerprint(path)
    return _sign(
        key,
        {
            "act": ACT_REQUEST_OPEN,
            "by": DECIDED_BY_OPEN_REQUEST,
            "digest": fingerprint,
            "job": job_id,
            "nonce": nonce,
            "shown": fingerprint,
        },
    )


def sign_revoke(key: bytes, *, job_id: str, nonce: str, name: str, path: str, change: str) -> str:
    """設定頁縮小名單的章（ADR-0330 2b），和 Touka 相同的算法。路徑與名稱的指紋、改法都綁進去。"""
    return _sign(
        key,
        {
            "act": ACT_REVOKE,
            "by": DECIDED_BY_OPEN_REQUEST,
            "change": change,
            "digest": text_fingerprint(path),
            "job": job_id,
            "nonce": nonce,
            "shown": text_fingerprint(name),
        },
    )


def _well_formed(key: bytes | None, decision: Decision) -> bool:
    return (
        key is not None
        and len(key) == DECISION_KEY_BYTES
        and _NONCE.fullmatch(decision.nonce) is not None
        and _HEX64.fullmatch(decision.digest) is not None
        and _HEX64.fullmatch(decision.shown) is not None
        and _HEX64.fullmatch(decision.signature) is not None
    )


def decision_signature_ok(key: bytes | None, job_id: str, decision: Decision) -> bool:
    """章是不是 Touka 用配對的鑰匙蓋在這件工作上的。格式不對也算不對。"""
    if not _well_formed(key, decision) or decision.decided_by not in DECIDED_BY_MODES:
        return False
    assert key is not None  # _well_formed 已確認
    expected = sign_decision(
        key,
        job_id=job_id,
        nonce=decision.nonce,
        digest=decision.digest,
        shown=decision.shown,
        decided_by=decision.decided_by,
    )
    return hmac.compare_digest(expected, decision.signature)


def open_request_signature_ok(
    key: bytes | None, job_id: str, path: str, decision: Decision
) -> bool:
    """開放請求的章：鑰匙、格式、兩個指紋都是這個路徑的、章是蓋在這件工作上的。"""
    fingerprint = text_fingerprint(path)
    if (
        not _well_formed(key, decision)
        or decision.decided_by != DECIDED_BY_OPEN_REQUEST
        or decision.digest != fingerprint
        or decision.shown != fingerprint
    ):
        return False
    assert key is not None  # _well_formed 已確認
    expected = sign_open_request(key, job_id=job_id, nonce=decision.nonce, path=path)
    return hmac.compare_digest(expected, decision.signature)


def revoke_signature_ok(
    key: bytes | None, job_id: str, name: str, path: str, change: str, decision: Decision
) -> bool:
    """縮小名單的章：鑰匙、格式、路徑與名稱的指紋都對、章是蓋在這件工作與這個改法上的。

    名稱與路徑要先確認沒有單獨的代理字元（算不出指紋）。
    """
    if (
        not _well_formed(key, decision)
        or decision.decided_by != DECIDED_BY_OPEN_REQUEST
        or decision.digest != text_fingerprint(path)
        or decision.shown != text_fingerprint(name)
    ):
        return False
    assert key is not None  # _well_formed 已確認
    expected = sign_revoke(
        key, job_id=job_id, nonce=decision.nonce, name=name, path=path, change=change
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
