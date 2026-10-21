# ruff: noqa: T201, TRY003

"""Smoke Test for Local Dynamic Handshake (LDH).

Verifies the "Graceful Downgrade" mechanism where trusted local clients 
can obtain a valid JWT token automatically.
"""
import sys
from pathlib import Path

# 1. Path Setup (SSOT) — 含 tests/smoke 以匯入 lib.*
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
for _p in (PROXY_DIR, MEMORY_SRC_DIR, Path(__file__).parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ADR-0103：必須在 import 任何 src 之前啟用 sandbox（避免真實重量級啟動殘留 → teardown 死鎖）
from lib.sandbox_env import enable_smoke_sandbox  # noqa: E402

enable_smoke_sandbox("ldh")

from fastapi.testclient import TestClient  # noqa: E402

# Import core components
from src.main import app  # noqa: E402
from src.services.identity_service import identity_service  # noqa: E402


def run_ldh_smoke_test():
    print("[*] Starting LDH Handshake Smoke Test")
    
    # 使用 TestClient 並觸發 lifespan (以載入 app.state.context)
    with TestClient(app) as client:
    
        # --- 1. 本地開發自動握手測試 (Developer Auto-Handshake) ---
        print("\n[1] Testing Developer Auto-Handshake (No Token, No ID)...")
        # 使用 /v1/debug/tools，它具備 get_verified_identity 依賴
        response = client.get("/v1/debug/tools")
        
        print(f"[*] Status: {response.status_code}")
        new_token = response.headers.get("X-New-Auth-Token")
        
        if not new_token:
            print("[FAIL] X-New-Auth-Token not found in response headers.")
            sys.exit(1)
            
        # 驗證 Token 內容
        identity = identity_service.verify_token(new_token)
        if not identity or identity.agent_id != "DebugAgent":
            print(f"[FAIL] Invalid token identity: {identity.agent_id if identity else 'None'}")
            sys.exit(1)
        print(f"[OK] Local Handshake issued DebugAgent token: {new_token[:20]}...")

        # --- 2. 指名領卡測試 (Named Identity LDH) ---
        print("\n[2] Testing Named Identity LDH (X-Agent-ID: DebugAgent)...")
        response = client.get("/v1/debug/tools", headers={"X-Agent-ID": "DebugAgent"})
        
        print(f"[*] Status: {response.status_code}")
        new_token = response.headers.get("X-New-Auth-Token")
        
        if not new_token:
            print("[FAIL] X-New-Auth-Token not found for named identity.")
            sys.exit(1)
            
        identity = identity_service.verify_token(new_token)
        if not identity or identity.agent_id != "DebugAgent" or identity.display_role != "Debugger":
            print(f"[FAIL] Identity mismatch: {identity}")
            sys.exit(1)
        print("[OK] Named LDH issued Debugger token for DebugAgent.")

        # --- 3. 壞卡 fail-loud 測試 (Invalid Token → 401, ADR-0131 / 禁令 60) ---
        # 行為翻轉（刻意）：舊版「本地壞卡當無卡 → LDH 降級發新卡」會把過期人類會話
        # 無聲挾持成 DebugAgent；現在帶失效 token 一律 401，讓客戶端走 /auth/refresh。
        print("\n[3] Testing Invalid Token Fail-Loud (401, no downgrade card)...")
        response = client.get(
            "/v1/debug/tools", headers={"Authorization": "Bearer invalid_token_here"}
        )

        print(f"[*] Status: {response.status_code}")

        if response.status_code != 401:
            print(f"[FAIL] Expected 401 for invalid token, got {response.status_code}.")
            sys.exit(1)
        if response.headers.get("X-New-Auth-Token"):
            print("[FAIL] LDH must NOT issue a card for an invalid token (ADR-0131).")
            sys.exit(1)
        print("[OK] Invalid token rejected with 401 (no downgrade card issued).")

        # --- 4. 非信任網域模擬 (Untrusted IP) ---
        print("\n[4] Testing Untrusted Access (Simulated)...")
        # 注意：TestClient 很難直接模擬非 'testclient' 的 host，除非我們手動 Mock 
        # 這裡我們透過一個臨時的 Mock 來測試
        from unittest.mock import patch
        
        with patch(
            "src.services.identity_router.is_request_from_trusted_local",
            return_value=False,
        ):
            response = client.get("/v1/debug/tools")
            # 非信任且無 Token 應回傳 401 或不核發 Token
            if response.headers.get("X-New-Auth-Token"):
                print("[FAIL] LDH issued token to untrusted source!")
                sys.exit(1)
            print("[OK] LDH correctly ignored untrusted source.")

        print("\n" + "="*50)
        print("[SUCCESS] LDH Handshake Smoke Tests Passed!")
        print("="*50)

if __name__ == "__main__":
    # LDH 測試涉及 FastAPI 啟動，使用同步調用 TestClient 即可
    run_ldh_smoke_test()
