"""remote MCP（streamable HTTP）連線設定的解析與驗證（SSOT，ADR-0192）。

三家桌面版對 remote MCP 已收斂到同一形態：streamable HTTP 傳輸＋靜態 header 帶憑證
（Claude Code 的 ``--header``／Codex 的 ``bearer_token_env_var``＋``http_headers``／
Antigravity 的 ``serverUrl``＋``headers``）。Touka 取同形狀，但**憑證一律留在 ``env``**、
header 只寫 ``${VAR}`` 引用：

    {"url": "https://api.example.com/mcp",
     "headers": {"Authorization": "Bearer ${API_PAT}"},
     "env": {"API_PAT": "<secret>"}}

如此 remote 條目的密鑰自動沿用既有保護——安裝精靈的 ``env_requirements`` 表單、
``routers/mcp.py`` 的遮罩回填與「前端不得清掉磁碟既值」——不必為 header 另造一套。

設計：純函式、零 IO/時間/環境依賴（體例比照 ``mcp_category``／``mcp_official_map``）。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# ``${VAR}`` 引用；變數名採 env 慣例（英數與底線）。
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 明文 http 僅允許 loopback——remote header 帶憑證，走明文外傳等於公開金鑰。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def is_remote_server_config(server: dict) -> bool:
    """判別條目為 remote（有 ``url``）或 stdio（有 ``command``）。"""
    return bool(server.get("url"))


def validate_remote_url(url: str) -> None:
    """驗證 remote 端點；不合格拋 ``ValueError``（Pydantic validator 慣例）。

    只認 http/https，且明文 http 僅限 loopback。此閘是憑證外洩的最後一道防線——
    header 帶著使用者的 PAT，若允許 ``http://`` 連外部主機，金鑰就是明文過網。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        msg = (
            f"MCP remote URL 必須是 http/https，收到 {url!r}。"
        )
        raise ValueError(msg)
    if not parsed.hostname:
        msg = f"MCP remote URL 缺少主機名：{url!r}。"
        raise ValueError(msg)
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        msg = (
            f"MCP remote URL 連線外部主機時必須使用 https（收到 {url!r}）——"
            "header 會攜帶憑證，明文傳輸等同洩漏金鑰。"
        )
        raise ValueError(msg)


def resolve_headers(
    headers: dict[str, str] | None, env: dict[str, str] | None
) -> dict[str, str]:
    """把 header 值中的 ``${VAR}`` 以 ``env`` 實值取代；缺變數即 fail-closed。

    缺憑證時**拋錯而非放行**：送出帶 ``${VAR}`` 字面值的 header 只會換來一台
    「連得上、但每次工具呼叫都 401」的殭屍伺服器（ADR-0191 的 Notion fail-open
    教訓），那比乾脆連不上更難診斷。
    """
    if not headers:
        return {}
    source = env or {}
    resolved: dict[str, str] = {}
    for key, value in headers.items():
        missing = [
            name for name in _ENV_REF_RE.findall(value) if not source.get(name)
        ]
        if missing:
            msg = (
                f"MCP remote header {key!r} 引用了未設定的環境變數："
                f"{', '.join(sorted(set(missing)))}。請於安裝精靈或設定頁補上憑證。"
            )
            raise ValueError(msg)
        resolved[key] = _ENV_REF_RE.sub(lambda m: source[m.group(1)], value)
    return resolved
