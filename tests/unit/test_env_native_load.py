"""Native 模式 .env → os.environ 平價（bug②：精靈嵌入模型顯示預設值）的 TDD.

Docker 由 compose ``env_file`` 把 .env 注入成進程環境變數；native 直接跑 uvicorn 時
無人載入，導致「走 os.environ」的讀取（EMBEDDING_MODEL 等非 Settings 欄位、setup 精靈
狀態）永遠讀不到 .env、退回預設值。config.load_env_file_into_environ() 補上這一步，
並以 is_bootstrap_enabled 閘保護測試/沙盒（避免真實金鑰污染 os.environ）。
"""

from __future__ import annotations

from src import config

_PROBE = "TOUKA_NATIVE_LOAD_PROBE"


def _write_env(tmp_path, line: str):
    env_file = tmp_path / ".env"
    env_file.write_text(line, encoding="utf-8")
    return env_file


def test_load_populates_os_environ_when_bootstrap_enabled(tmp_path, monkeypatch):
    env_file = _write_env(tmp_path, f"{_PROBE}=paraphrase-multilingual-MiniLM-L12-v2\n")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env_file))
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    monkeypatch.delenv(_PROBE, raising=False)

    config.load_env_file_into_environ()

    # 經 SSOT 橋接讀取（config.get_full_env → os.environ 複本），避免直接觸 os.environ。
    assert config.get_full_env().get(_PROBE) == "paraphrase-multilingual-MiniLM-L12-v2"


def test_load_noop_when_bootstrap_disabled(tmp_path, monkeypatch):
    env_file = _write_env(tmp_path, f"{_PROBE}=should-not-load\n")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env_file))
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "false")
    monkeypatch.delenv(_PROBE, raising=False)

    config.load_env_file_into_environ()

    assert _PROBE not in config.get_full_env()


def test_load_does_not_override_real_env(tmp_path, monkeypatch):
    env_file = _write_env(tmp_path, f"{_PROBE}=from-file\n")
    monkeypatch.setenv("TOUKA_ENV_FILE", str(env_file))
    monkeypatch.setenv("TOUKA_BOOTSTRAP_ENABLED", "true")
    monkeypatch.setenv(_PROBE, "from-real-env")

    config.load_env_file_into_environ()

    # 真實環境變數優先序不變（env > .env 檔），override=False 不覆寫。
    assert config.get_full_env().get(_PROBE) == "from-real-env"
