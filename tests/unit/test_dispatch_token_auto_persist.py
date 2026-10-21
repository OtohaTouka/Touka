"""DISPATCH_TOKEN 首次生成即持久化——DispatchSupervisor.build_child_env 的 TDD.

派工 master token 空時，於 spawn 組子環境時自動生成並持久化到 .env（禁令 15），
讓皮使用者不跑精靈也能用派工。安全：生成不會自動啟用派工伺服器（spawn 另由
DISPATCH_AUTOSPAWN 把關），且持久化受 TOUKA_BOOTSTRAP_ENABLED 閘保護（測試/沙盒
不寫真實 .env）。
"""

from __future__ import annotations

from unittest.mock import patch

from src.config import settings
from src.services.dispatch_supervisor import DispatchSupervisor


def test_dispatch_token_generated_and_used_when_empty(monkeypatch):
    monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "")
    with patch(
        "src.services.dispatch_supervisor.persist_generated_secret", return_value=True
    ) as mock_persist:
        env = DispatchSupervisor().build_child_env()
    token = env.get("DISPATCH_TOKEN")
    assert token  # 非空
    mock_persist.assert_called_once()
    assert mock_persist.call_args.args[0] == "DISPATCH_TOKEN"
    # 持久化的值＝實際放進子環境的值（同一把）
    assert mock_persist.call_args.args[1] == token


def test_dispatch_token_absent_when_persist_disabled(monkeypatch):
    monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", "")
    with patch(
        "src.services.dispatch_supervisor.persist_generated_secret", return_value=False
    ):
        env = DispatchSupervisor().build_child_env()
    assert not env.get("DISPATCH_TOKEN")  # bootstrap off → 不生成、子環境無 token


def test_dispatch_token_passthrough_when_set(monkeypatch):
    preset = "preset-token"
    monkeypatch.setattr(settings, "DISPATCH_TOKEN", preset)
    with patch("src.services.dispatch_supervisor.persist_generated_secret") as mock_persist:
        env = DispatchSupervisor().build_child_env()
    assert env["DISPATCH_TOKEN"] == preset
    mock_persist.assert_not_called()  # 已設定→不生成
