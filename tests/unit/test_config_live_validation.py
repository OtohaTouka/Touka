"""live 設定檔啟動健檢測試（ADR-0096）。

驗證 `bootstrap.validate_live_configs`——守 gitignore 的 **live 真檔**（與 Guard #34
`test_config_examples` 守「範本 `.example`」互補；live 檔 CI 上不存在無法靜態守衛，故改在
每次啟動時驗證）：
  - 合法 JSON ＋ 符合 schema 的 live 檔通過、回傳已驗證清單。
  - **malformed JSON → fail-fast** 拋 `ConfigValidationError` 並點名該檔（`read_json` 會把壞
    JSON 當空字典靜默吞掉，本健檢直接 `json.loads` 故能偵測——這正是回歸守的點）。
  - 有模型者型別/必填錯 → fail-fast 並點名該檔。
  - 缺檔（交由 `ensure_configs` 還原）不報錯；不存在的目錄回空清單。
  - 有模型者帶 extra 鍵（`extra="ignore"`）→ 不誤殺（與範本守衛擋過時鍵的取捨不同）。
全程以 tmp 目錄注入，不碰真實 config。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.models.autonomy import AutonomyConfigModel
from src.utils.bootstrap import ConfigValidationError, validate_live_configs


def _write(d, name: str, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    (d / name).write_text(text, encoding="utf-8")


def test_valid_live_configs_pass(tmp_path):
    """合法 JSON＋符合 schema 的 live 檔通過，回傳已驗證檔名。"""
    _write(tmp_path, "autonomy.json", AutonomyConfigModel().model_dump(mode="json"))
    _write(tmp_path, "bots.json", {"some": "free-form config"})  # 無模型、僅檢 JSON
    checked = validate_live_configs(config_dir=tmp_path)
    assert set(checked) == {"autonomy.json", "bots.json"}


def test_malformed_json_fails_fast(tmp_path):
    """壞 JSON → 拋 ConfigValidationError 且點名該檔（read_json 會靜默吞、此處能抓）。"""
    _write(tmp_path, "autonomy.json", AutonomyConfigModel().model_dump(mode="json"))
    _write(tmp_path, "bots.json", "{ this is not valid json ")
    with pytest.raises(ConfigValidationError) as ei:
        validate_live_configs(config_dir=tmp_path)
    assert "bots.json" in str(ei.value)


def test_schema_violation_fails_fast(tmp_path):
    """有模型的 live 檔型別錯（daily_max 非整數）→ 拋 ConfigValidationError 且點名該檔。"""
    _write(tmp_path, "autonomy.json", {"quota": {"daily_max": "not-an-int"}})
    with pytest.raises(ConfigValidationError) as ei:
        validate_live_configs(config_dir=tmp_path)
    assert "autonomy.json" in str(ei.value)


def test_the_fix_hint_says_which_broken_files_are_rebuilt_from_the_example(tmp_path):
    """同資料夾有 .example 的檔才說可以刪掉重建（下次啟動照範本補回）；hooks.json 的範本是
    關閉的（ADR-0292）；沒有範本的檔刪掉就是少了那份設定。"""
    for name in ("autonomy.json", "hooks.json"):
        _write(tmp_path, f"{name}.example", {})
    _write(tmp_path, "autonomy.json", "{ broken ")
    _write(tmp_path, "hooks.json", {"hooks": "not-a-mapping"})
    _write(tmp_path, "custom.json", "{ broken ")
    with pytest.raises(ConfigValidationError) as ei:
        validate_live_configs(config_dir=tmp_path)
    message = str(ei.value)
    assert "autonomy.json、hooks.json 也可以刪除後重啟，系統會從 .example 範本重建" in message
    assert "hooks.json 的範本是關閉的：所有 hook 都不執行" in message
    assert "custom.json 沒有範本，刪除後不會重建" in message


def test_the_fix_hint_does_not_guess_when_the_example_cannot_be_read(tmp_path, monkeypatch):
    """讀不到 .example（權限一類）：提示說看不出來，照樣回報原本的壞檔錯誤，不另外丟例外。"""
    _write(tmp_path, "autonomy.json.example", {})
    _write(tmp_path, "autonomy.json", "{ broken ")
    real_is_file = Path.is_file

    def is_file(path: Path) -> bool:
        if path.name == "autonomy.json.example":
            raise PermissionError(13, "denied", str(path))
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    with pytest.raises(ConfigValidationError) as ei:
        validate_live_configs(config_dir=tmp_path)
    message = str(ei.value)
    assert "autonomy.json 看不出有沒有 .example 範本" in message
    assert "重建" not in message


def test_missing_modeled_file_skipped(tmp_path):
    """缺的 modeled 檔（交由 ensure_configs 還原）不報錯——只驗存在的檔。"""
    _write(tmp_path, "bots.json", {"ok": True})  # 無 autonomy.json
    assert validate_live_configs(config_dir=tmp_path) == ["bots.json"]


def test_nonexistent_dir_returns_empty(tmp_path):
    """設定目錄不存在 → 回空清單、不拋（極早啟動的保險）。"""
    assert validate_live_configs(config_dir=tmp_path / "nope") == []


def test_extra_keys_tolerated(tmp_path):
    """有模型的 live 檔帶模型未定義的鍵（extra='ignore'）→ 不應因此失敗。

    與範本守衛（擋過時鍵）不同：live 檔可能領先模型，啟動健檢只擋「型別/必填錯」與壞
    JSON，不擋 extra（否則模型演進期間會誤殺正常運作中的 live 檔）。
    """
    payload = AutonomyConfigModel().model_dump(mode="json")
    payload["some_future_key"] = 123
    _write(tmp_path, "autonomy.json", payload)
    assert validate_live_configs(config_dir=tmp_path) == ["autonomy.json"]
