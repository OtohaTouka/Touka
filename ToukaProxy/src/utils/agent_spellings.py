"""一個代理人編號在舊資料裡的兩種寫法（ADR-0326）.

依編號存放的資料（對話列、心跳排程、身分設定）可能用編號逐字，也可能用升級前的
``ToukaSoul`` + 編號。這裡固定回兩個值：對話資料庫的 SQL 寫死 ``IN (?, ?)``（禁令 24），
DebugAgent 沒有前綴寫法，就回兩次自己。

只比對逐字：只差大小寫的是另一個代理人（ADR-0303、ADR-0325）。
"""

LEGACY_SOUL_PREFIX = "ToukaSoul"
_NO_PREFIX_IDS = frozenset({"DebugAgent"})


def legacy_spellings(agent_id: str) -> tuple[str, str]:
    """回 ``(編號, ToukaSoul+編號)``；DebugAgent 回 ``(DebugAgent, DebugAgent)``."""
    if agent_id in _NO_PREFIX_IDS:
        return agent_id, agent_id
    return agent_id, f"{LEGACY_SOUL_PREFIX}{agent_id}"
