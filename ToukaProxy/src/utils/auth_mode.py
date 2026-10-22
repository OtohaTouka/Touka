"""三種模式的名稱、舊值對照與模式授權（ADR-0246）：純函式＋模組常數（禁令 18）。

- 寫入與 API 輸入只收新名稱（``AuthMode``）。
- 讀取已存的對話時經 ``normalize_auth_mode``：``ReadOnly``、``SafeAuto`` 與看不懂的值一律
  當 ``Manual``（最嚴），``FullYOLO`` 當 ``YOLO``，新名稱原樣。不做資料庫遷移，存檔時自然寫
  新名稱。
- 只有 admin 或該靈魂的擁有者能讓對話處於 Auto／YOLO（``authorize_auth_mode``）；其他人寫入
  一律降成 ``Manual``。
"""

from __future__ import annotations

from typing import Final, Literal

AuthMode = Literal["Manual", "Auto", "YOLO"]

AUTO_MODES: Final[frozenset[str]] = frozenset({"Auto", "YOLO"})
AUTH_MODE_READ_MAP: Final[dict[str, AuthMode]] = {
    "Manual": "Manual",
    "Auto": "Auto",
    "YOLO": "YOLO",
    "ReadOnly": "Manual",
    "SafeAuto": "Manual",
    "FullYOLO": "YOLO",
}


def normalize_auth_mode(value: object) -> AuthMode:
    """已存的值 → 三種模式之一；看不懂的值（含非字串）一律 ``Manual``。"""
    if not isinstance(value, str):
        return "Manual"
    return AUTH_MODE_READ_MAP.get(value, "Manual")


def enforce_incognito_manual(mode: str | None, *, incognito: bool) -> str | None:
    """無痕對話無法追溯，一律 ``Manual``（ADR-0246）；非無痕原樣不動。"""
    return "Manual" if incognito else mode


# 無痕對話碰不到的檔案範圍：舊對話庫，讀寫都不給（hist-bak b，禁令 20）
INCOGNITO_HIDDEN_VAULTS: Final[frozenset[str]] = frozenset({"legacy"})


def incognito_hides_vault(vault: str, ctx: object) -> bool:
    """執行器注入的 ``_ctx`` 是無痕，而且要碰的是無痕不給的範圍；沒有 ctx 時不算無痕。"""
    get = getattr(ctx, "get", None)
    return vault in INCOGNITO_HIDDEN_VAULTS and callable(get) and bool(get("incognito"))


def request_is_incognito(request: object) -> bool:
    """``request.incognito`` 或 ``ui_persistence.mode == "incognito"`` 任一成立就算無痕（R-2c）。"""
    admission = getattr(request, "ui_persistence", None)
    return bool(getattr(request, "incognito", False)) or (
        getattr(admission, "mode", None) == "incognito"
    )


def mode_for_acting_agent(
    stored: object, *, bound_agent: str | None, acting_agent: str | None
) -> AuthMode:
    """存下的 Auto／YOLO 只給 session 綁的那個 agent；不一致或判斷不了一律 Manual（R-2c）。"""
    mode = normalize_auth_mode(stored)
    if mode in AUTO_MODES and not (bound_agent and bound_agent == acting_agent):
        return "Manual"
    return mode


def authorize_auth_mode(requested: AuthMode, *, is_admin: bool, owns_agent: bool) -> AuthMode:
    """Auto／YOLO 只給 admin 或該靈魂的擁有者；其他人降成 ``Manual``。"""
    if requested in AUTO_MODES and not (is_admin or owns_agent):
        return "Manual"
    return requested
