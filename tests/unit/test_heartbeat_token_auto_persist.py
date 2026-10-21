"""HEARTBEAT_TOKEN 自動生成即持久化（禁令 15，比照 DISPATCH_TOKEN/TOUKA_AUTH_SECRET）的 TDD.

問題 1：``.env.example`` 過去出貨佔位字串 ``<your-generated-heartbeat-token>``，複製成
.env 後被當成真 token（非空 → 三階第一階直接回傳），既非機密又壓過自動生成。改為：
proxy 於 config 載入尾聲，若 HEARTBEAT_TOKEN 為空且 bootstrap 啟用，就生成並持久化到
.env（消費端 ``from src.config import HEARTBEAT_TOKEN`` 綁常數，故必須在載入尾聲完成）。
持久化僅在 bootstrap 啟用時發生（測試/沙盒 no-op → 維持空值、不污染真實 .env）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from src import config


def test_keeps_configured_token_without_persist():
    configured = "already-set-heartbeat-token"  # noqa: S105
    with patch.object(config, "persist_generated_secret") as mock_persist:
        resolved = config._resolve_heartbeat_token(configured)
    assert resolved == configured
    mock_persist.assert_not_called()


def test_generates_and_persists_when_empty():
    with (
        patch.object(config, "is_bootstrap_enabled", return_value=True),
        patch.object(config, "persist_generated_secret", return_value=True) as mock_persist,
    ):
        resolved = config._resolve_heartbeat_token("")
    assert resolved  # 非空
    assert resolved != ""
    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[0] == "HEARTBEAT_TOKEN"
    assert mock_persist.call_args.args[1] == resolved


def test_stays_empty_when_bootstrap_disabled():
    with (
        patch.object(config, "is_bootstrap_enabled", return_value=False),
        patch.object(config, "persist_generated_secret") as mock_persist,
    ):
        resolved = config._resolve_heartbeat_token("")
    assert resolved == ""
    mock_persist.assert_not_called()  # bootstrap 關 → 連生成都不做


def test_stays_empty_when_persist_fails():
    with (
        patch.object(config, "is_bootstrap_enabled", return_value=True),
        patch.object(config, "persist_generated_secret", return_value=False) as mock_persist,
    ):
        resolved = config._resolve_heartbeat_token("")
    # 寫檔失敗（或 bootstrap off）→ 不硬塞臨時值：heartbeat 兩端只認 .env，臨時值反而
    # 造成 proxy 與 heartbeat 不對稱，故維持空值（由既有錯誤路徑處理「未設定」）。
    assert resolved == ""
    mock_persist.assert_called_once()


@pytest.mark.parametrize("configured", [" padded-token ", "\tpadded-token\n"])
def test_normalizes_shared_token_whitespace(configured):
    """Both services use the same nonempty normalized shared value."""
    with patch.object(config, "persist_generated_secret") as persist:
        assert config._resolve_heartbeat_token(configured) == "padded-token"
    persist.assert_not_called()


def test_whitespace_token_with_bootstrap_disabled_is_not_ready():
    """Whitespace cannot become a usable shared credential."""
    with patch.object(config, "is_bootstrap_enabled", return_value=False):
        assert config._resolve_heartbeat_token("  \t ") == ""
