"""小幫手權杖的產生與比對（ADR-0311，禁令 18：跨切面安全邏輯抽成純函式）."""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

_TOKEN_BYTES: Final = 32


def new_token() -> str:
    """產生新的權杖。"""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """權杖的 SHA-256。權杖本身是高亂度亂數，不需要慢雜湊。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, expected_hash: str) -> bool:
    """定時比對。空值一律不符。"""
    if not token or not expected_hash:
        return False
    return secrets.compare_digest(hash_token(token), expected_hash)
