"""TDD: StorageService 缺檔初始化、毀損保留與範本合法性測試。

覆蓋場景：
- config.json 含 // 註解時固定失敗且保留原件，不產生新 token
- 只有確認缺檔才初始化有效的隨機 heartbeat_token
- config.json.example 本身必須是合法 JSON（無 // 註解）
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ToukaHeartbeat import storage_service
from ToukaHeartbeat.errors import HeartbeatError
from ToukaHeartbeat.storage_service import StorageService

HEARTBEAT_DIR = Path(__file__).resolve().parents[2] / "ToukaHeartbeat"
EXAMPLE_FILE = HEARTBEAT_DIR / "config.json.example"

JS_COMMENT_CONTENT = """{
    // 這是非法的 JS 風格行內註解
    "api_base_url": "http://localhost:8000",
    "heartbeat_token": "old-token",
    "schedules": []
}"""


class TestExampleFileValidity:
    """config.json.example 必須是合法 JSON（無任何 // 或 /* */ 註解）。"""

    def test_example_file_exists(self):
        assert EXAMPLE_FILE.exists(), "config.json.example 應存在於 ToukaHeartbeat 目錄"

    def test_example_file_is_valid_json(self):
        """config.json.example 必須能被 json.loads() 正確解析。"""
        content = EXAMPLE_FILE.read_text(encoding="utf-8")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            pytest.fail(f"config.json.example 含有非法 JSON 內容（可能是 // 註解）：{e}")
        assert isinstance(parsed, dict), "config.json.example 必須是 JSON object"

    def test_example_file_has_no_js_comments(self):
        """明確斷言不含 // 開頭的行。"""
        content = EXAMPLE_FILE.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.lstrip()
            assert not stripped.startswith("//"), (
                f"config.json.example 第 {i} 行含有非法 JS 風格註解：{line!r}"
            )


class TestStorageServiceRecovery:
    """毀損設定不能被自動重建掩蓋；原件由擁有者處理。"""

    def test_load_config_with_js_comments_fails_visibly(self, tmp_path):
        """含 // 註解時固定 parse failure，不得靜默回傳空 dict。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(JS_COMMENT_CONTENT, encoding="utf-8")

        storage = StorageService(config_file)
        with pytest.raises(HeartbeatError) as error:
            storage.load_config()
        assert error.value.reason == "config_parse_failed"

    def test_load_config_failure_never_generates_replacement_token(self, tmp_path, monkeypatch):
        """解析失敗不得產生新密鑰或回傳不可信舊密鑰。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(JS_COMMENT_CONTENT, encoding="utf-8")

        storage = StorageService(config_file)
        generator = Mock()
        monkeypatch.setattr(storage_service.secrets, "token_hex", generator)
        with pytest.raises(HeartbeatError):
            storage.get_token()
        generator.assert_not_called()

    def test_load_config_repeated_failure_preserves_broken_bytes(self, tmp_path):
        """重複讀取保持可辨識失敗與 exact 原件，不自動覆寫。"""
        config_file = tmp_path / "config.json"
        config_file.write_text(JS_COMMENT_CONTENT, encoding="utf-8")

        storage = StorageService(config_file)
        before = config_file.read_bytes()
        for _ in range(2):
            with pytest.raises(HeartbeatError):
                storage.load_config()
            assert config_file.read_bytes() == before

    def test_load_config_normal_flow_unchanged(self, tmp_path):
        """正常流程（合法 JSON）不受影響。"""
        config_file = tmp_path / "config.json"
        storage = StorageService(config_file)

        # 首次載入（檔案不存在）→ 自動生成
        result = storage.load_config()
        assert result["schedules"] == []
        assert len(result["heartbeat_token"]) >= 16

        # 寫入自訂資料後再讀取
        storage.save_config(
            {"schedules": [{"id": "x", "agent_id": "DebugAgent"}], "heartbeat_token": "abc"}
        )
        reloaded = storage.load_config()
        assert reloaded["schedules"][0]["id"] == "x"
