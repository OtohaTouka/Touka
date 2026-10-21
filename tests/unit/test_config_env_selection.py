import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 將專案路徑加入 sys.path 確保能正確匯入
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
HEARTBEAT_DIR = PROJECT_ROOT / "ToukaHeartbeat"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(HEARTBEAT_DIR) not in sys.path:
    sys.path.insert(0, str(HEARTBEAT_DIR))

from src.config import Settings, resolve_env_file_path  # noqa: E402

from ToukaHeartbeat.config import HeartbeatSettings, resolve_heartbeat_env_file_path  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """清理環境變數以防止污染測試中的 .env 載入優先級。"""
    for key in [
        "GEMINI_API_KEY", "PORT", "HEARTBEAT_TOKEN", "API_BASE_URL",
        "TOUKA_INTEGRATED", "TOUKA_ENV_FILE",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def env_sandbox(tmp_path):
    """建立包含 mock 根目錄 .env 與子系統 .env 的沙盒目錄結構。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    proxy_root = workspace / "ToukaProxy"
    proxy_root.mkdir()
    
    heartbeat_root = workspace / "ToukaHeartbeat"
    heartbeat_root.mkdir()
    
    # 寫入不同的變數以利識別
    workspace_env = workspace / ".env"
    workspace_env.write_text(
        "GEMINI_API_KEY=workspace_api_key\nPORT=9090\nHEARTBEAT_TOKEN=workspace_hb_token\n",
        encoding="utf-8"
    )
    
    proxy_env = proxy_root / ".env"
    proxy_env.write_text(
        "GEMINI_API_KEY=proxy_api_key\nPORT=8080\n",
        encoding="utf-8"
    )
    
    heartbeat_env = heartbeat_root / ".env"
    heartbeat_env.write_text(
        "API_BASE_URL=http://heartbeat-local:8000\nHEARTBEAT_TOKEN=local_hb_token\n",
        encoding="utf-8"
    )
    
    return {
        "workspace": workspace,
        "proxy_root": proxy_root,
        "heartbeat_root": heartbeat_root,
        "workspace_env": workspace_env,
        "proxy_env": proxy_env,
        "heartbeat_env": heartbeat_env,
    }


def test_proxy_resolve_integrated_mode(env_sandbox, monkeypatch):
    """測試 ToukaProxy 在整合模式下的路徑解析（CWD 處於工作區根目錄，且根目錄 .env 存在）。"""
    workspace = env_sandbox["workspace"]
    proxy_root = env_sandbox["proxy_root"]
    
    # Mock PROXY_ROOT 與 WORKSPACE_ROOT
    with patch("src.config.PROXY_ROOT", proxy_root), \
         patch("src.config.WORKSPACE_ROOT", workspace), \
         patch.object(Path, "cwd", return_value=workspace):
        
        # 測試路徑解析
        resolved_path = resolve_env_file_path()
        assert resolved_path.resolve() == env_sandbox["workspace_env"].resolve()
        
        # 測試載入實體
        settings = Settings()
        assert settings.GEMINI_API_KEY == "workspace_api_key"
        assert settings.PORT == 9090


def test_proxy_resolve_standalone_mode(env_sandbox, monkeypatch):
    """測試 ToukaProxy 在獨立模式下的路徑解析（CWD 處於 ToukaProxy 目錄，且未強制指定整合）。"""
    workspace = env_sandbox["workspace"]
    proxy_root = env_sandbox["proxy_root"]
    
    # Mock PROXY_ROOT 與 WORKSPACE_ROOT，且 CWD 為 ToukaProxy
    with patch("src.config.PROXY_ROOT", proxy_root), \
         patch("src.config.WORKSPACE_ROOT", workspace), \
         patch.object(Path, "cwd", return_value=proxy_root):
        
        # 測試路徑解析
        resolved_path = resolve_env_file_path()
        assert resolved_path.resolve() == env_sandbox["proxy_env"].resolve()
        
        # 測試載入實體
        settings = Settings()
        assert settings.GEMINI_API_KEY == "proxy_api_key"
        assert settings.PORT == 8080


def test_proxy_forced_integrated_by_env(env_sandbox, monkeypatch):
    """測試 ToukaProxy 透過環境變數 TOUKA_INTEGRATED=true 強制進入整合模式。"""
    workspace = env_sandbox["workspace"]
    proxy_root = env_sandbox["proxy_root"]
    
    monkeypatch.setenv("TOUKA_INTEGRATED", "true")
    
    # 即使 CWD 是 Proxy 目錄，TOUKA_INTEGRATED=true 也應強制讀取工作區根目錄的 .env
    with patch("src.config.PROXY_ROOT", proxy_root), \
         patch("src.config.WORKSPACE_ROOT", workspace), \
         patch.object(Path, "cwd", return_value=proxy_root):
        
        resolved_path = resolve_env_file_path()
        assert resolved_path.resolve() == env_sandbox["workspace_env"].resolve()
        
        settings = Settings()
        assert settings.GEMINI_API_KEY == "workspace_api_key"
        assert settings.PORT == 9090


def test_proxy_resolve_explicit_env_file_override(env_sandbox, monkeypatch):
    """root-cause-A：TOUKA_ENV_FILE 顯式覆寫最高優先——供部署（docker）指向持久化且被
    compose env_file 讀取的宿主 .env（容器內以 bind mount 提供），凌駕整合/獨立自動偵測，
    避免寫入容器暫存層而在重建時遺失。"""
    explicit = env_sandbox["workspace_env"]  # 任一實體 .env
    monkeypatch.setenv("TOUKA_ENV_FILE", str(explicit))

    # 即使 CWD 在 Proxy 目錄（否則自動偵測會走獨立模式回 proxy_env），覆寫仍勝出。
    with patch("src.config.PROXY_ROOT", env_sandbox["proxy_root"]), \
         patch("src.config.WORKSPACE_ROOT", env_sandbox["workspace"]), \
         patch.object(Path, "cwd", return_value=env_sandbox["proxy_root"]):
        resolved = resolve_env_file_path()
        assert resolved.resolve() == explicit.resolve()

        # Settings 亦經由覆寫路徑載入（讀寫同源）。
        settings = Settings()
        assert settings.GEMINI_API_KEY == "workspace_api_key"
        assert settings.PORT == 9090


def test_heartbeat_resolve_integrated_mode(env_sandbox, monkeypatch):
    """測試 ToukaHeartbeat 在整合模式下的路徑解析。"""
    workspace = env_sandbox["workspace"]
    heartbeat_root = env_sandbox["heartbeat_root"]
    
    with patch("ToukaHeartbeat.config.HEARTBEAT_ROOT", heartbeat_root), \
         patch("ToukaHeartbeat.config.WORKSPACE_ROOT", workspace), \
         patch.object(Path, "cwd", return_value=workspace):
        
        resolved_path = resolve_heartbeat_env_file_path()
        assert resolved_path.resolve() == env_sandbox["workspace_env"].resolve()
        
        hb_settings = HeartbeatSettings()
        # 整合模式載入工作區 .env 裡的 HEARTBEAT_TOKEN
        assert hb_settings.HEARTBEAT_TOKEN == "workspace_hb_token"  # noqa: S105


def test_heartbeat_resolve_standalone_mode(env_sandbox, monkeypatch):
    """測試 ToukaHeartbeat 在獨立模式下的路徑解析。"""
    workspace = env_sandbox["workspace"]
    heartbeat_root = env_sandbox["heartbeat_root"]
    
    with patch("ToukaHeartbeat.config.HEARTBEAT_ROOT", heartbeat_root), \
         patch("ToukaHeartbeat.config.WORKSPACE_ROOT", workspace), \
         patch.object(Path, "cwd", return_value=heartbeat_root):
        
        resolved_path = resolve_heartbeat_env_file_path()
        assert resolved_path.resolve() == env_sandbox["heartbeat_env"].resolve()
        
        hb_settings = HeartbeatSettings()
        assert hb_settings.HEARTBEAT_TOKEN == "local_hb_token"  # noqa: S105
        assert hb_settings.API_BASE_URL == "http://heartbeat-local:8000"
