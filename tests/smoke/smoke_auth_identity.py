"""Smoke Test for Authentication & Identity Sovereignty (Service Logic).

Verifies JWT lifecycle, identity mapping, and capability resolution within the ToukaProxy system.
"""
import asyncio
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("auth_identity")

# 1. 設置路徑 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
# 注意：ToukaMemory 的 src 目錄需要被加入 sys.path
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

from src.models.identity import AgentIdentity
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service


async def run_auth_smoke_test():
    """執行認證與身分主權 Service 邏輯冒煙測試。"""
    print("[*] Starting Auth & Identity Sovereignty Smoke Test")  # noqa: T201
    
    agent_id = "DebugAgent"
    
    # --- 1. Identity Mapping & Capability 測試 ---
    print("\n[1] Testing Identity Mapping & Capabilities...")  # noqa: T201
    
    # 從實體設定檔載入身分資訊
    role = permission_service.get_role_key(agent_id)
    caps = permission_service.get_capabilities_by_id(agent_id)
    
    print(f"[OK] Identity Resolved: {agent_id}")  # noqa: T201
    print(f"     Role: {role}")  # noqa: T201
    print(f"     Capabilities: {caps}")  # noqa: T201
    
    if role != "Debugger":
        print(f"[!] Warning: Expected role 'Debugger' for {agent_id}, got '{role}'.")  # noqa: T201
        print("    (Please check if identity_map.json is correctly configured)")  # noqa: T201
        sys.exit(1)
        
    # 驗證特權
    identity = AgentIdentity.for_role(agent_id=agent_id, role=role, capabilities=caps)
    can_awaken = permission_service.can_awaken(identity)
    print(f"[OK] Can Awaken (Heartbeat Privilege): {can_awaken}")  # noqa: T201
    
    # --- 2. JWT Lifecycle 測試 ---
    print("\n[2] Testing JWT Lifecycle (Issue & Verify)...")  # noqa: T201
    
    # 核發 Token
    token = identity_service.issue_token(identity, expires_delta=60)
    if not token:
        print("[!] Failed to issue JWT token.")  # noqa: T201
        sys.exit(1)
    print("[OK] JWT Issued successfully.")  # noqa: T201
    
    # 驗證 Token
    verified_identity = identity_service.verify_token(token)
    if not verified_identity:
        print("[!] JWT Verification failed.")  # noqa: T201
        sys.exit(1)
        
    if verified_identity.agent_id != agent_id:
        print(f"[!] ID Mismatch: Expected {agent_id}, got {verified_identity.agent_id}")  # noqa: T201
        sys.exit(1)
        
    print("[OK] JWT Verified successfully (Identity Restored).")  # noqa: T201
    print(f"     Restored Role: {verified_identity.display_role}")  # noqa: T201
    print(f"     Restored Caps: {verified_identity.capabilities}")  # noqa: T201

    # --- 3. Sovereignty & Default Protection 測試 ---
    print("\n[3] Testing Default Role & Spoofing Protection...")  # noqa: T201
    
    random_id = "RandomStranger_999"
    default_role = permission_service.get_role_key(random_id)
    print(f"[OK] Default Role for unknown ID: {default_role}")  # noqa: T201
    
    if default_role == "Owner":
         print("[!] CRITICAL: Default role should NOT be Owner.")  # noqa: T201
         sys.exit(1)

    print("\n" + "="*50)  # noqa: T201
    print("[SUCCESS] Auth & Identity Sovereignty Tests Passed!")  # noqa: T201
    print("="*50)  # noqa: T201


if __name__ == "__main__":
    asyncio.run(run_auth_smoke_test())
