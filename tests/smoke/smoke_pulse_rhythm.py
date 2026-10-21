"""Smoke Test for Pulse & Rhythm (Autonomy & Rhythm Services).

Verifies tool-round budget resolution (ADR-0146), rhythm lifecycle, and quota consumption.
"""
import asyncio
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("pulse_rhythm")

# 1. 設置路徑 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.rhythm_service import HEADLESS_FALLBACK_ROUNDS, rhythm_service


async def run_pulse_rhythm_smoke_test():
    """執行脈衝與節律 Service 邏輯冒煙測試（ADR-0146 工具輪語意）。"""
    print("[*] Starting Pulse & Rhythm Smoke Test")  # noqa: T201

    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    request_id = "test_req_456"

    # --- 1. Tool-Round Resolution (Priority SSOT) 測試 ---
    print("\n[1] Testing Tool-Round Resolution (Priority SSOT)...")  # noqa: T201

    # 模擬配置（新制：工具輪保險絲）
    mock_config = {"react_loop": {"max_tool_rounds": 10}}

    # 優先級 1: 顯式指定（排程）
    rounds1 = rhythm_service.get_effective_rounds(mock_config, 15, headless=True)
    print(f"[OK] Priority 1 (Explicit 15): {rounds1}")  # noqa: T201
    if rounds1 != 15:
        print("[!] Round resolution failure (P1).")  # noqa: T201
        sys.exit(1)

    # 優先級 2: 全域保險絲
    rounds2 = rhythm_service.get_effective_rounds(mock_config, None, headless=False)
    print(f"[OK] Priority 2 (Global fuse 10): {rounds2}")  # noqa: T201
    if rounds2 != 10:
        print("[!] Round resolution failure (P2).")  # noqa: T201
        sys.exit(1)

    # 優先級 3: 互動預設不限（0）；無人強制保底
    rounds3 = rhythm_service.get_effective_rounds({}, None, headless=False)
    rounds4 = rhythm_service.get_effective_rounds({}, None, headless=True)
    print(  # noqa: T201
        f"[OK] Priority 3 (Interactive 0 / headless {HEADLESS_FALLBACK_ROUNDS}): "
        f"{rounds3}/{rounds4}"
    )
    if rounds3 != 0 or rounds4 != HEADLESS_FALLBACK_ROUNDS:
        print("[!] Round resolution failure (P3).")  # noqa: T201
        sys.exit(1)

    # --- 2. Rhythm Lifecycle 測試 ---
    print("\n[2] Testing Rhythm Lifecycle (Round Budget)...")  # noqa: T201

    rhythm_service.initialize_session(request_id, 3)
    if rhythm_service.get_total_rounds(request_id) != 3:
        print("[!] Initialization failure.")  # noqa: T201
        sys.exit(1)
    print("[OK] Initialized Rounds: 3")  # noqa: T201

    ok = rhythm_service.consume_round(request_id)
    used = rhythm_service.get_used_rounds(request_id)
    print(f"[OK] Consumed one round (granted={ok}, used={used})")  # noqa: T201
    if not ok or used != 1:
        print("[!] Round consumption failure.")  # noqa: T201
        sys.exit(1)

    rhythm_service.clear_session(request_id)
    if rhythm_service.get_total_rounds(request_id) != 0:
        print("[!] Session clear failure.")  # noqa: T201
        sys.exit(1)
    print("[OK] Rounds after clear: 0")  # noqa: T201

    # --- 3. Interruption 測試 ---
    print("\n[3] Testing Interruption Mechanism...")  # noqa: T201
    session_id = "sess_999"

    rhythm_service.interrupt_session(session_id)
    if not rhythm_service.is_interrupted(session_id):
        print("[!] Interruption flag not set correctly.")  # noqa: T201
        sys.exit(1)
    print("[OK] Interruption detected successfully.")  # noqa: T201

    rhythm_service.clear_session(session_id)
    if rhythm_service.is_interrupted(session_id):
        print("[!] Interruption flag not cleared.")  # noqa: T201
        sys.exit(1)
    print("[OK] Interruption cleared successfully.")  # noqa: T201

    # --- 4. Quota Consumption & Logging 測試 ---
    print("\n[4] Testing Quota Consumption & Persistence...")  # noqa: T201

    # 確保原始狀態 (重置)
    autonomy_service.reset_quota(identity)

    # 消耗配額（ADR-0146：只計無人 pulse；rounds 僅供日誌）
    autonomy_service.consume_quota(identity, request_id="smoke_test_q", rounds=8)
    print("[OK] Quota Consumed (pulse semantics).")  # noqa: T201

    # 驗證日誌是否存在且數據正確
    # autonomy_service._load_logs 是私有方法，我們可以直接呼叫來驗證 (測試許可)
    logs = autonomy_service._load_logs(identity)
    if logs.get("active_slots") != 8 or logs.get("daily_count", 0) < 1:
        print(f"[!] Log data mismatch. Got {logs}")  # noqa: T201
        sys.exit(1)
    print(f"[OK] Quota Logs persistent for {identity.agent_id}.")  # noqa: T201

    print("\n" + "="*50)  # noqa: T201
    print("[SUCCESS] All Pulse & Rhythm Service Tests Passed!")  # noqa: T201
    print("="*50)  # noqa: T201


if __name__ == "__main__":
    asyncio.run(run_pulse_rhythm_smoke_test())
