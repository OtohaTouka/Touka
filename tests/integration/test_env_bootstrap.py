import json
from unittest.mock import patch

import pytest
from src.utils.bootstrap import ESSENTIAL_CONFIGS, ensure_configs


@pytest.fixture
def temp_config_dir(tmp_path):
    """建立一個臨時的 config 目錄用於測試"""
    test_config_dir = tmp_path / "config"
    test_config_dir.mkdir()

    # 修改環境變數或 Monkeypatch (這裡假設 bootstrap 使用 CONFIG_DIR)
    with patch("src.utils.bootstrap.CONFIG_DIR", str(test_config_dir)):
        yield test_config_dir


def test_ensure_configs_creates_missing(temp_config_dir):
    """驗證是否能自動建立遺失的設定檔"""
    # 確保目錄是空的
    restored = ensure_configs()
    
    # 檢查是否建立了預設檔案 (ESSENTIAL_CONFIGS 中的檔案)
    assert len(restored) > 0
    assert (temp_config_dir / "agents_registry.json").exists()
    
    # 讀取內容，應該是空字典
    with open(temp_config_dir / "agents_registry.json", encoding="utf-8") as f:
        data = json.load(f)
        assert data == {}

def test_ensure_configs_restores_from_example(temp_config_dir):
    """驗證是否能從 .example 恢復檔案"""
    example_content = {"test": "data"}
    example_file = temp_config_dir / "models.json.example"
    with open(example_file, "w", encoding="utf-8") as f:
        json.dump(example_content, f)
        
    restored = ensure_configs()
    
    assert "models.json" in restored
    assert (temp_config_dir / "models.json").exists()
    
    with open(temp_config_dir / "models.json", encoding="utf-8") as f:
        data = json.load(f)
        assert data == example_content

def test_ensure_configs_does_not_overwrite(temp_config_dir):
    """驗證不會覆蓋已存在的設定檔 (保護現有資料)。

    早期 ADR 守衛缺口 E17（ADR-0007）修正：原版用 ``skills.json``——根本不在
    ``ESSENTIAL_CONFIGS``，迴圈連看都不看它＝保護分支（``if target_path.exists():
    continue``）從未被驅動、斷言空泛恆真（假綠）。本版改用清單內真檔 ``models.json``，
    並同時放一份內容不同的 ``.example`` 在場——證明「保護現有資料」優先於「從範本恢復」
    （移除保護分支 → example 覆蓋 → 本測試變紅）。
    """
    target_name = "models.json"
    assert target_name in ESSENTIAL_CONFIGS  # 前提自檢：驅動的必須是清單內真檔

    existing_content = {"status": "do_not_overwrite"}
    target_file = temp_config_dir / target_name
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(existing_content, f)

    # example 在場且內容不同：若保護分支失守，restore 分支會用它覆蓋
    example_file = temp_config_dir / f"{target_name}.example"
    with open(example_file, "w", encoding="utf-8") as f:
        json.dump({"status": "example_must_not_win"}, f)

    restored = ensure_configs()

    assert target_name not in restored

    with open(target_file, encoding="utf-8") as f:
        data = json.load(f)
        assert data == existing_content
