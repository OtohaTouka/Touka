"""密鑰首次生成即持久化（禁令 15）——config helper ＋ AuthSecretManager 的 TDD.

比照 ToukaHeartbeat StorageService 三階（env > 持久檔 > 生成並持久化），把
TOUKA_AUTH_SECRET 補上「生成即持久化到 .env」那一階，讓皮使用者不跑精靈也不會
每次重啟登出。持久化僅在 bootstrap 啟用時發生（測試/沙盒 TOUKA_BOOTSTRAP_ENABLED=false
→ 退回臨時記憶體密鑰，不污染真實 .env）。
"""

from __future__ import annotations

from unittest.mock import patch

from src import config
from src.core.security import AuthSecretManager

# --------------------------------------------------------------------------- #
# config.persist_generated_secret
# --------------------------------------------------------------------------- #


def test_persist_writes_and_applies_when_bootstrap_enabled(monkeypatch):
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    with (
        patch.object(config, "set_key") as mock_set,
        patch.object(config, "apply_runtime_env") as mock_apply,
    ):
        ok = config.persist_generated_secret("TOUKA_AUTH_SECRET", "abc123")
    assert ok is True
    mock_set.assert_called_once()
    assert mock_set.call_args.args[1] == "TOUKA_AUTH_SECRET"
    assert mock_set.call_args.args[2] == "abc123"
    mock_apply.assert_called_once_with({"TOUKA_AUTH_SECRET": "abc123"})


def test_persist_noop_when_bootstrap_disabled(monkeypatch):
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "false")
    with (
        patch.object(config, "set_key") as mock_set,
        patch.object(config, "apply_runtime_env") as mock_apply,
    ):
        ok = config.persist_generated_secret("TOUKA_AUTH_SECRET", "abc123")
    assert ok is False
    mock_set.assert_not_called()
    mock_apply.assert_not_called()


def test_persist_swallows_oserror(monkeypatch):
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    with (
        patch.object(config, "set_key", side_effect=OSError("disk full")),
        patch.object(config, "apply_runtime_env") as mock_apply,
    ):
        ok = config.persist_generated_secret("K", "V")
    assert ok is False
    mock_apply.assert_not_called()  # 未寫成不套用


def test_is_bootstrap_enabled_default_true(monkeypatch):
    monkeypatch.delenv("TOUKA_BOOTSTRAP_ENABLED", raising=False)
    assert config.is_bootstrap_enabled() is True
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "FALSE")
    assert config.is_bootstrap_enabled() is False


# --------------------------------------------------------------------------- #
# AuthSecretManager：生成即持久化
# --------------------------------------------------------------------------- #


def test_auth_secret_generates_and_persists_when_empty(monkeypatch):
    AuthSecretManager.reset_for_test()
    monkeypatch.setattr("src.core.security.TOUKA_AUTH_SECRET", "")
    with patch("src.core.security.persist_generated_secret", return_value=True) as mock_persist:
        secret = AuthSecretManager.get_secret()
    assert len(secret) == 64  # secrets.token_hex(32)
    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[0] == "TOUKA_AUTH_SECRET"
    assert mock_persist.call_args.args[1] == secret
    AuthSecretManager.reset_for_test()


def test_auth_secret_ephemeral_when_persist_fails(monkeypatch):
    AuthSecretManager.reset_for_test()
    monkeypatch.setattr("src.core.security.TOUKA_AUTH_SECRET", "")
    with patch("src.core.security.persist_generated_secret", return_value=False) as mock_persist:
        secret = AuthSecretManager.get_secret()
    assert len(secret) == 64  # 仍可用（臨時）
    mock_persist.assert_called_once()  # 嘗試過但 bootstrap off / 寫失敗
    AuthSecretManager.reset_for_test()


def test_auth_secret_uses_configured_without_persist(monkeypatch):
    AuthSecretManager.reset_for_test()
    configured_value = "a-configured-secret"
    monkeypatch.setattr("src.core.security.TOUKA_AUTH_SECRET", configured_value)
    with patch("src.core.security.persist_generated_secret") as mock_persist:
        resolved = AuthSecretManager.get_secret()
    assert resolved == configured_value
    mock_persist.assert_not_called()
    AuthSecretManager.reset_for_test()
