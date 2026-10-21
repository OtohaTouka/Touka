"""P1: MCP env 機密遮罩（write-only）。

GET /mcp/ui/config 回傳時遮罩 env 的 secret 類值；POST 收到遮罩佔位時保留磁碟既值
（sentinel 永不落盤），比照 config/connections 的金鑰遮罩標準（ADR-0099）。
純函式測試（無 FastAPI/IO）。
"""
from src.routers.mcp import _redact_env, _preserve_masked_env
from src.utils.secrets import MASK


def test_redact_env_masks_secret_keys():
    cfg = {"mcpServers": {"gh": {"command": "npx", "args": [], "env": {"API_KEY": "secret123", "DEBUG": "1"}}}}
    out = _redact_env(cfg)
    assert out["mcpServers"]["gh"]["env"]["API_KEY"] == MASK
    assert out["mcpServers"]["gh"]["env"]["DEBUG"] == "1"  # 非 secret 鍵維持明文


def test_redact_env_empty_value_unchanged():
    out = _redact_env({"mcpServers": {"x": {"env": {"API_KEY": ""}}}})
    assert out["mcpServers"]["x"]["env"]["API_KEY"] == ""  # 空值維持空（前端據此判斷未設定）


def test_preserve_masked_env_keeps_disk_value():
    existing = {"gh": {"env": {"API_KEY": "real-secret"}}}
    incoming = {"mcpServers": {"gh": {"env": {"API_KEY": MASK}}}}
    out = _preserve_masked_env(incoming, existing)
    assert out["mcpServers"]["gh"]["env"]["API_KEY"] == "real-secret"


def test_preserve_masked_env_writes_new_value():
    existing = {"gh": {"env": {"API_KEY": "old"}}}
    incoming = {"mcpServers": {"gh": {"env": {"API_KEY": "new-secret"}}}}
    out = _preserve_masked_env(incoming, existing)
    assert out["mcpServers"]["gh"]["env"]["API_KEY"] == "new-secret"


def test_preserve_masked_env_sentinel_never_persists():
    incoming = {"mcpServers": {"gh": {"env": {"API_KEY": MASK}}}}
    out = _preserve_masked_env(incoming, {})  # 無既有
    assert MASK not in out["mcpServers"]["gh"]["env"].values()
