"""Touka → ToukaIDE「由人核准」標記（ADR-0246 U2c-7）：純函式＋模組常數（禁令 18）。

人類在 Touka 審批卡上核准某個 IDE 呼叫後，Proxy 把這個標記放進 tools/call 的 ``_meta``，
IDE 驗證通過才不跳第二次確認。MAC 金鑰是這次 IDE session 的 bearer（只有 Proxy 從
discovery 檔拿到），內容綁住工具名、完整參數、nonce 與期限，換到別的呼叫或重放都會失效。
IDE 端的驗證在 ``ToukaIDE/src/mcp/humanApproval.ts``，兩邊的位元組必須一致。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

HUMAN_APPROVAL_META_KEY = "touka.dev/human-approval"
HUMAN_APPROVAL_TTL_MS = 60_000
_MAC_DOMAIN = "touka-ide-human-approval/v1"


def build_human_approval_meta(
    key: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    now_ms: int,
    nonce: str,
) -> dict[str, Any]:
    """回傳要合併進 tools/call ``_meta`` 的標記；只有人類卡片核准過的呼叫才可以呼叫本函式。"""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    exp = now_ms + HUMAN_APPROVAL_TTL_MS
    message = "\n".join((_MAC_DOMAIN, nonce, str(exp), tool_name, digest))
    mac = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {HUMAN_APPROVAL_META_KEY: {"v": 1, "nonce": nonce, "exp": exp, "mac": mac}}
