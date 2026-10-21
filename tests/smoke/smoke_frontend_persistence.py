"""Smoke Test for Frontend Data & Persistence (UI Sessions, Settings, Assets).

Verifies the integrity of SQLite storage, TreeHistory serialization, and path resolution.
"""

import asyncio
import json
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("frontend_persistence")

# 1. 設置路徑 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
# 注意：ToukaMemory 的 src 目錄需要被加入 sys.path
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

from src.models.history import MessageNode, TreeHistory
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager
from src.utils.async_db import (
    get_system_setting,
    init_db_async,
    session_service,
    set_system_setting,
)
from src.utils.time_utils import get_current_epoch


async def run_frontend_smoke_test():
    """執行前端數據與持久化冒煙測試。"""
    print("[*] Starting Frontend Data & Persistence Smoke Test")  # noqa: T201
    await init_db_async()

    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    session_id = "smoke_test_session_123"

    # --- 1. UI Session 持久化測試 ---
    print("\n[1] Testing UI Session Persistence...")  # noqa: T201

    # 建立一個測試對話樹
    now = int(get_current_epoch())
    msg1 = MessageNode(id="m1", role="user", content="Hello", timestamp=now)
    msg2 = MessageNode(id="m2", parentId="m1", role="assistant", content="Hi there!", timestamp=now)
    msg1.childrenIds.append("m2")

    tree = TreeHistory(messages={"m1": msg1, "m2": msg2}, currentId="m2")

    # 儲存
    success = await session_service.upsert_session(
        session_id=session_id,
        title="Smoke Test Session",
        messages=tree.model_dump(),
        updatedAt=int(get_current_epoch() * 1000),
        identity=identity,
    )
    if not success:
        print("[!] Failed to save UI session.")  # noqa: T201
        sys.exit(1)
    print("[OK] Session saved successfully.")  # noqa: T201

    # 讀取並驗證
    loaded = await session_service.get_session_by_id(session_id)
    if not loaded:
        print("[!] Failed to load saved session.")  # noqa: T201
        sys.exit(1)

    # 驗證 TreeHistory 結構
    try:
        # DB 中存儲的是 JSON 字串，session_service 會在讀取時 parse
        msgs_data = loaded["messages"]
        if isinstance(msgs_data, str):
            msgs_data = json.loads(msgs_data)

        loaded_tree = TreeHistory.model_validate(msgs_data)
        if loaded_tree.currentId != "m2" or "m1" not in loaded_tree.messages:
            print("[!] Loaded tree structure mismatch.")  # noqa: T201
            sys.exit(1)
    except Exception as e:
        print(f"[!] TreeHistory validation failed: {e}")  # noqa: T201
        sys.exit(1)
    print("[OK] Session loaded and validated (TreeHistory intact).")  # noqa: T201

    # 刪除
    await session_service.delete_session(session_id)
    verify_del = await session_service.get_session_by_id(session_id)
    if verify_del:
        print("[!] Session was not deleted correctly.")  # noqa: T201
        sys.exit(1)
    print("[OK] Session deleted successfully.")  # noqa: T201

    # --- 2. System Settings 持久化測試 ---
    print("\n[2] Testing System Settings Persistence...")  # noqa: T201
    original_root = await get_system_setting("workspace_root", "DEFAULT")
    test_val = "tmp/smoke_test_workspace"

    await set_system_setting("workspace_root", test_val)
    new_val = await get_system_setting("workspace_root")
    if new_val != test_val:
        print(f"[!] Setting mismatch. Expected {test_val}, got {new_val}")  # noqa: T201
        sys.exit(1)

    # 還原
    await set_system_setting("workspace_root", original_root)
    print("[OK] System Settings read/write verified.")  # noqa: T201

    # --- 3. Asset Path Resolution 測試 ---
    print("\n[3] Testing Asset Path Resolution (SSOT)...")  # noqa: T201
    output_path_str = resource_manager.get_vault_path("personal", identity)
    if not output_path_str:
        print("[!] Failed to resolve personal vault via ResourceManager.")  # noqa: T201
        sys.exit(1)

    output_path = Path(output_path_str)
    print(f"[OK] Resolved Physical Path: {output_path}")  # noqa: T201

    # 驗證路徑邏輯是否符合預期
    if "DebugAgent" not in str(output_path):
        print(f"[!] Path does not contain agent_id: {output_path}")  # noqa: T201
        sys.exit(1)

    print("[OK] Asset path resolution compliant with Identity SSOT.")  # noqa: T201

    print("\n" + "=" * 50)  # noqa: T201
    print("[SUCCESS] All Frontend Persistence Tests Passed!")  # noqa: T201
    print("=" * 50)  # noqa: T201


if __name__ == "__main__":
    asyncio.run(run_frontend_smoke_test())
