"""Update openapi.json from current backend models.

Ensures SSOT between code and schema file. 先開沙盒才載入產品程式（禁令 63）：載入時的
初始化（設定自癒、生成密鑰寫進 .env）只會落在暫存資料夾；合併規則在 ``openapi_merge.py``。
"""

import io
import json
import sys
from pathlib import Path

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (PROJECT_ROOT, PROJECT_ROOT / "ToukaProxy", PROJECT_ROOT / "ToukaMemory" / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.smoke.lib.sandbox_env import enable_smoke_sandbox  # noqa: E402

# 2. Sandbox before any product import
enable_smoke_sandbox("update_openapi")

from openapi_merge import merge_schemas  # noqa: E402
from src.main import app as proxy_app  # noqa: E402
from src.models.session_persistence import TRANSPORT_MODELS  # noqa: E402

HEARTBEAT_DIR = PROJECT_ROOT / "ToukaHeartbeat"
if str(HEARTBEAT_DIR) not in sys.path:
    sys.path.insert(0, str(HEARTBEAT_DIR))

from awaken import app as heartbeat_app  # noqa: E402

# 沙盒會把 WORKSPACE_ROOT 指到暫存資料夾，輸出位置只由本檔位置推出。
SCHEMA_FILE_PATH = PROJECT_ROOT / "openapi.json"


def update_openapi_file() -> None:
    """Update openapi.json from current backend models."""
    print("📡 Generating latest OpenAPI schema...")  # noqa: T201
    proxy_schema = merge_schemas(
        proxy_app.openapi(), heartbeat_app.openapi(), transport_models=TRANSPORT_MODELS
    )

    print(f"💾 Writing to {SCHEMA_FILE_PATH}...")  # noqa: T201
    with SCHEMA_FILE_PATH.open("w", encoding="utf-8") as f:
        json.dump(proxy_schema, f, indent=2, ensure_ascii=False)

    print("✅ openapi.json updated successfully!")  # noqa: T201


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    update_openapi_file()
