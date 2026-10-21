#!/usr/bin/env python3
"""Touka Documentation Automation Script.

This script ensures that the technical documentation is always in sync with the
latest source code and API schema.

SSOT:
- Repo root is resolved from this file's location (the sandbox moves WORKSPACE_ROOT).
- API Schema is exported directly from the FastAPI app instance.
"""

import io
import logging
import subprocess
import sys
from pathlib import Path

# --- 1. 路徑初始化 (SSOT) ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))  # 重用 update_openapi.py 的合併邏輯

# 使用 update_openapi.py 作為 OpenAPI 契約的唯一生成器（SSOT）：它會合併
# ToukaProxy + ToukaHeartbeat 路由，避免 schema-only 匯出遺失 /v1/schedules 等
# Heartbeat 端點。請勿改回僅含 Proxy 的 export_schema。它在匯入時先開沙盒才載入
# 產品程式（禁令 63），所以必須是本檔第一個專案匯入。
from update_openapi import update_openapi_file  # noqa: E402

# 沙盒在 root logger 放了 NullHandler；force 換回本腳本自己的主控台輸出。
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logger = logging.getLogger(__name__)

def update_docs():
    """Execute the full documentation update pipeline."""
    logger.info(f"🚀 Starting documentation update at: {PROJECT_ROOT}")

    # A. 同步 OpenAPI Schema (SSOT for API Contract)
    #    透過 update_openapi.py 的合併邏輯（ToukaProxy + ToukaHeartbeat），
    #    確保 /v1/schedules 等 Heartbeat 端點不會在重生時遺失。
    openapi_path = PROJECT_ROOT / "openapi.json"
    logger.info(f"📝 Syncing OpenAPI schema (Proxy + Heartbeat merge) to {openapi_path}...")
    try:
        update_openapi_file()
    except Exception as e:
        logger.error(f"❌ Failed to export OpenAPI schema: {e}")
        sys.exit(1)

    # B. 檢查 MkDocs 環境
    logger.info("🔍 Checking MkDocs environment...")
    try:
        subprocess.run(["mkdocs", "--version"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error("❌ MkDocs is not installed or not in PATH.")
        sys.exit(1)

    # C. 執行 MkDocs Build
    logger.info("🔨 Building static documentation site...")
    mkdocs_yml = PROJECT_ROOT / "mkdocs.yml"
    if not mkdocs_yml.exists():
        logger.error(f"❌ mkdocs.yml not found at {mkdocs_yml}")
        sys.exit(1)

    try:
        # 使用 --clean 確保生成純淨的 site/ 資料夾
        subprocess.run(["mkdocs", "build", "--clean"], check=True, cwd=str(PROJECT_ROOT))
        logger.info("✅ Documentation site built successfully in 'site/' directory.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ MkDocs build failed: {e}")
        sys.exit(1)

    logger.info("🎉 Documentation update complete.")

if __name__ == "__main__":
    # update_openapi_file() 會 print emoji（📡💓💾）；Windows 主控台預設 cp950
    # 無法編碼，故先將 stdout 包成 UTF-8（與 update_openapi.py __main__ 相同處理）。
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    update_docs()
