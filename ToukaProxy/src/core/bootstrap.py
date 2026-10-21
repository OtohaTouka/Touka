"""引導與自癒中心 (Self-Healing Bootstrap).

負責 Touka 系統啟動時的基礎環境引導與法規校驗 (LaC)。
實施「引導白名單 (Bootstrap Whitelist)」原則：
1. 本模組擁有環境變數 (.env) 與 實體配置 (*.json) 的自癒管理權。
2. 確保缺失的配置能自動從 .example 範本中復原。
3. 扮演系統啟動的第一道防線，確保環境純淨且符合 SSOT 規範。
"""

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def hydrate_config_examples(source_dir: Path, target_dir: Path) -> None:
    """從 tracked examples 補齊獨立目標目錄，不覆寫既有設定。"""
    if not source_dir.exists():
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    for example_file in sorted(source_dir.glob("*.json.example")):
        target_name = example_file.name.removesuffix(".example")
        target_path = target_dir / target_name
        if target_path.exists():
            continue

        logger.warning("⚠️ %s missing. Restoring from template...", target_name)
        shutil.copy(example_file, target_path)
        logger.info("✅ %s restored.", target_name)


def ensure_system_bootstrap(root_path: Path) -> None:
    """確保系統基礎環境已完成引導與自癒 (Self-healing).
    
    執行流程:
    1. 環境自癒: 檢查 .env，若遺失則從 .env.example 復原。
    2. 配置自癒: 檢查 config/*.json，若遺失則從 .json.example 復原。
    3. 狀態錨定: 確保系統啟動時具備正確的 SSOT 物理基準。
    
    Args:
        root_path (Path): 系統根目錄實體路徑。
    """
    # 1. 處理 .env (SSOT: 系統運行根目錄)
    env_file = root_path / ".env"
    env_example = root_path / ".env.example"
    
    if not env_file.exists() and env_example.exists():
        logger.warning("⚠️ .env missing. Restoring from .env.example...")
        shutil.copy(env_example, env_file)
        logger.info("✅ .env restored.")

    # 2. 處理 config/*.json (SSOT: Proxy 專屬配置)
    # 假設 root_path 為 Touka 根目錄，Proxy 配置在 ToukaProxy/config
    proxy_config_dir = root_path / "ToukaProxy" / "config"
    if not proxy_config_dir.exists():
        # 如果路徑結構不同，嘗試當前目錄下的 config
        proxy_config_dir = root_path / "config"
    
    hydrate_config_examples(proxy_config_dir, proxy_config_dir)

def get_bootstrap_status(root_path: Path) -> dict:
    """獲取當前環境的引導狀態"""
    proxy_config_dir = root_path / "ToukaProxy" / "config"
    if not proxy_config_dir.exists():
        proxy_config_dir = root_path / "config"
        
    return {
        "env_exists": (root_path / ".env").exists(),
        "config_dir_exists": proxy_config_dir.exists(),
        "missing_configs": [
            f.name.replace(".json.example", ".json") 
            for f in proxy_config_dir.glob("*.json.example") 
            if not (proxy_config_dir / f.name.replace(".json.example", ".json")).exists()
        ] if proxy_config_dir.exists() else []
    }
