"""TDD: heartbeat_token SSOT 優先鏈測試。

驗證 StorageService.get_token() 的三層優先鏈：
  1. env_token 非空 → 直接回傳（env var 最高優先）
  2. env_token 為空 + config.json 有 token → 回傳 config.json 的值
  3. 兩者皆空 → 自動生成、持久化、回傳

同時確認管理入口與 client 注入同一個 StorageTokenProvider。
"""

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from ToukaHeartbeat import awaken
from ToukaHeartbeat.awaken import verify_schedule_token
from ToukaHeartbeat.proxy_client import ProxyClient
from ToukaHeartbeat.storage_service import StorageService
from ToukaHeartbeat.token_provider import StorageTokenProvider

HEARTBEAT_DIR = Path(__file__).resolve().parents[2] / "ToukaHeartbeat"
KNOWN_INSECURE = "touka_heartbeat_secret_2026"  # noqa: S105


class TestGetTokenPriorityChain:
    """StorageService.get_token() 三層優先鏈。"""

    def test_env_token_wins_over_file_token(self, tmp_path):
        """env_token 非空時直接回傳，完全忽略 config.json。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"heartbeat_token": "file-token", "schedules": []}),
            encoding="utf-8",
        )
        storage = StorageService(config_file, env_token="env-token-xyz")
        assert storage.get_token() == "env-token-xyz"

    def test_env_token_wins_even_when_file_missing(self, tmp_path):
        """env_token 非空時，config.json 不存在也能正常回傳。"""
        config_file = tmp_path / "config.json"
        storage = StorageService(config_file, env_token="env-only-token")
        assert storage.get_token() == "env-only-token"
        assert not config_file.exists(), "env var 優先時不應寫入 config.json"

    def test_falls_back_to_file_when_env_empty(self, tmp_path):
        """env_token 為空字串時，使用 config.json 的 token。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"heartbeat_token": "file-only-token", "schedules": []}),
            encoding="utf-8",
        )
        storage = StorageService(config_file, env_token="")
        assert storage.get_token() == "file-only-token"

    def test_auto_generates_when_both_empty(self, tmp_path):
        """env_token 與 config.json token 均為空時，自動生成有效 token。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"heartbeat_token": "", "schedules": []}),
            encoding="utf-8",
        )
        storage = StorageService(config_file, env_token="")
        token = storage.get_token()

        assert token != "", "自動生成的 token 不得為空字串"
        assert len(token) >= 16, f"自動生成的 token 長度不足：{token!r}"
        assert token != KNOWN_INSECURE, "不得為已知弱密鑰"

    def test_auto_generated_token_persisted_to_file(self, tmp_path):
        """自動生成的 token 必須寫回 config.json 以確保重啟後一致。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps({"heartbeat_token": "", "schedules": []}),
            encoding="utf-8",
        )
        storage = StorageService(config_file, env_token="")
        generated = storage.get_token()

        persisted = json.loads(config_file.read_text(encoding="utf-8"))
        assert persisted["heartbeat_token"] == generated, (
            "持久化的 token 必須與 get_token() 回傳值一致"
        )

    def test_get_token_idempotent_with_env(self, tmp_path):
        """env_token 設定時，多次呼叫 get_token() 回傳相同值。"""
        config_file = tmp_path / "config.json"
        storage = StorageService(config_file, env_token="stable-env-token")
        assert storage.get_token() == storage.get_token()

    def test_default_env_token_is_empty_string(self, tmp_path):
        """StorageService 預設 env_token 為空字串（向下相容）。"""
        config_file = tmp_path / "config.json"
        storage = StorageService(config_file)
        assert storage._env_token == ""  # noqa: SLF001


class TestVerifyScheduleTokenUsesSSOT:
    """verify_schedule_token 必須透過 get_token() 取得 token，而非 load_config()。"""

    def test_verify_schedule_token_calls_get_token_not_load_config(self):
        """靜態確認：verify_schedule_token 函式體不得直接呼叫 load_config()。"""
        src = inspect.getsource(verify_schedule_token)
        assert "load_config" not in src, (
            "SSOT 違規：verify_schedule_token 不得直接呼叫 load_config()，"
            "必須改用 storage.get_token()"
        )
        assert "token_provider.resolve" in src, (
            "verify_schedule_token 必須透過共用 provider 取得同一次解析結果"
        )

    def test_env_token_accepted_by_verify_schedule_token(self, tmp_path, monkeypatch):
        """當 env_token 設定時，verify_schedule_token 應接受對應的 Bearer token。"""
        storage = StorageService(tmp_path / "config.json", env_token="env-driven-token")
        provider = StorageTokenProvider(storage)
        monkeypatch.setattr(awaken, "token_provider", provider)
        client = ProxyClient(provider)
        assert client.token_provider is provider
        request = MagicMock()
        request.headers.get = lambda k, default="": (
            "Bearer env-driven-token" if k == "Authorization" else default
        )
        verify_schedule_token(request)

    def test_wrong_token_rejected_when_env_set(self, tmp_path, monkeypatch):
        """Bearer token 與 env_token 不符時應拒絕。"""
        storage = StorageService(tmp_path / "config.json", env_token="correct-env-token")
        monkeypatch.setattr(awaken, "token_provider", StorageTokenProvider(storage))
        request = MagicMock()
        request.headers.get = lambda k, default="": (
            "Bearer wrong-token" if k == "Authorization" else default
        )
        with pytest.raises(HTTPException) as exc_info:
            verify_schedule_token(request)
        assert exc_info.value.status_code == 401
