"""不可信識別字的非可逆診斷 handle。"""

import hashlib


def opaque_identifier(value: object, *, namespace: str) -> str:
    """回傳固定長度 handle；不得將原始值帶入 log、API 或 diagnostic。"""
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    return f"{namespace}-{digest}"
