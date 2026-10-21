# ruff: noqa: E402

"""API Schema Synchronization Test.

Ensures that the backend Pydantic models match the root openapi.json.
"""

import json
import sys
from pathlib import Path

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

# Import FastAPI app
from openapi_merge import merge_schemas  # noqa: E402
from src.config import get_workspace_root  # noqa: E402
from src.main import app  # noqa: E402
from src.models.session_persistence import TRANSPORT_MODELS  # noqa: E402

# Heartbeat Setup (SSOT)
HEARTBEAT_DIR = PROJECT_ROOT / "ToukaHeartbeat"
if str(HEARTBEAT_DIR) not in sys.path:
    sys.path.insert(0, str(HEARTBEAT_DIR))

try:
    from awaken import app as heartbeat_app
except ImportError:
    heartbeat_app = None


def test_openapi_schema_synchronization():
    """驗證當前代碼生成的 OpenAPI Schema 與實體檔案 openapi.json 是否一致。"""
    # 1. 獲取當前最新的 Schema (FastAPI 自動生成)
    # 2. 與實際生成器共享重名拒絕及純 SSE 模型註冊，不維護第二套合併規則。
    current_schema = merge_schemas(
        app.openapi(),
        heartbeat_app.openapi() if heartbeat_app else {"paths": {}},
        transport_models=TRANSPORT_MODELS,
    )

    # 3. 獲取物理檔案路徑 (SSOT)
    workspace_root = get_workspace_root()
    schema_file_path = workspace_root / "openapi.json"

    if not schema_file_path.exists():
        raise FileNotFoundError(f"Missing openapi.json at: {schema_file_path}")  # noqa: TRY003

    # 3. 讀取並解析現有的 Schema
    with open(schema_file_path, encoding="utf-8") as f:
        existing_schema = json.load(f)

    # 4. 深度比對 (Deep Parity)
    current_str = json.dumps(current_schema, sort_keys=True, indent=2, ensure_ascii=False)
    existing_str = json.dumps(existing_schema, sort_keys=True, indent=2, ensure_ascii=False)

    if current_str != existing_str:
        mismatch_info = ""
        v_curr = current_schema.get("info", {}).get("version")
        v_exis = existing_schema.get("info", {}).get("version")
        if v_curr != v_exis:
            mismatch_info += f"\n- Version mismatch: Code({v_curr}) vs File({v_exis})"

        if len(current_schema.get("paths", {})) != len(existing_schema.get("paths", {})):
            p_code = len(current_schema.get("paths", {}))
            p_file = len(existing_schema.get("paths", {}))
            mismatch_info += f"\n- Path count mismatch: Code({p_code}) vs File({p_file})"

        diff_msg = (
            f"\n[!] API Contract Out of Sync! {mismatch_info}\n\n"
            "Please update openapi.json to match current backend models.\n"
        )
        assert current_str == existing_str, diff_msg


if __name__ == "__main__":
    # 強制使用 UTF-8 輸出，解決 Windows CP950 報錯
    if sys.platform == "win32":
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    try:
        test_openapi_schema_synchronization()
        print("✅ API Schema Synchronization Verified!")  # noqa: T201
    except AssertionError as e:
        print(str(e))  # noqa: T201
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")  # noqa: T201
        sys.exit(1)
