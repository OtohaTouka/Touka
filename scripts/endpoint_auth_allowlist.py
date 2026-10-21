# ruff: noqa: T201
"""端點 Auth 守衛白名單 (Endpoint Auth Guard Allowlist — SSOT).

列出所有「刻意公開」的端點（不需要 auth dependency）。
新增公開端點時，必須同時在此處補記，否則 guard_endpoint_auth.py 會失敗。

每個條目格式：{"method": "<http verb>", "path_prefix": "<path prefix>"}
使用 prefix 匹配，例如 "/auth" 會匹配 /auth/login、/auth/refresh 等。

設計原則 (SOLID-OCP)：
    新增條目只需擴充此清單，守衛邏輯本身不需要改動。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 刻意公開端點（方法 + 路徑前綴）
# ---------------------------------------------------------------------------

ALLOWLIST: list[dict[str, str]] = [
    # ── 認證流程入口（Auth flow entry points）──────────────────────────────
    # 登入/註冊/換 token 是認證前操作，必須無驗證
    {"method": "post", "path_prefix": "/auth/login"},
    {"method": "post", "path_prefix": "/auth/register"},
    {"method": "post", "path_prefix": "/auth/refresh"},
    # P3（認證縫）：logout 改憑 refresh cookie 撤銷（不再需有效 Bearer）——過期 access
    # 卡也能真正登出（舊制依賴 get_current_user＝過期即 401、後端撤銷從未執行、session
    # 復活）。cookie 為 HttpOnly＋credentials，只能撤銷自己的 token；非破壞性。
    {"method": "post", "path_prefix": "/auth/logout"},

    # ── 觀察性端點（Observability — 健康檢查無需驗證）──────────────────────
    # P22 fix: /logs, /memory, /tools, /tools_stats moved to protected (require auth)
    {"method": "get", "path_prefix": "/health"},
    {"method": "get", "path_prefix": "/system_health"},
    {"method": "get", "path_prefix": "/monitor"},
    {"method": "get", "path_prefix": "/events"},
    {"method": "websocket", "path_prefix": "/ws"},

    # ── 模型探索（OpenAI 相容性要求公開）───────────────────────────────────
    {"method": "get", "path_prefix": "/models"},
    {"method": "get", "path_prefix": "/v1/models"},

    # ── Heartbeat 連通性探針（無驗證設計）──────────────────────────────────
    {"method": "get", "path_prefix": "/heartbeat/test"},
    {"method": "get", "path_prefix": "/v1/heartbeat/test"},

    # ── 系統日誌別名（observability — 移至 allowlist）───────────────────────
    # GET /system_logs 是 /logs/recent 的別名，與其他 observability 端點一致。
    {"method": "get", "path_prefix": "/system_logs"},

    # ── 內部 Dispatch 回呼端點（ADR-0051）──────────────────────────────────
    # ToukaDispatch → ToukaProxy 的 server-to-server 回呼通道，有自訂雙層驗證：
    #   1. localhost IP 來源驗證（防外部觸發）
    #   2. callback_token 存在驗證（防偽造 token）
    # 不使用 get_verified_identity，因為呼叫方是 ToukaDispatch server（非 Agent）。
    {"method": "post", "path_prefix": "/internal/dispatch/callback"},
    {"method": "post", "path_prefix": "/v1/internal/dispatch/callback"},

    # ── MCP OAuth callback（state capability，無 JWT）─────────────────────
    # callback 只能依一次性、高熵、短 TTL 且 HMAC-only lookup 的 state 進入；
    # route/method/response-mode 綁定、CAS consume 與 PKCE 在 OAuth runtime 執法。
    {"method": "post", "path_prefix": "/mcp/oauth/callback"},
    {"method": "get", "path_prefix": "/mcp/oauth/callback"},
]
