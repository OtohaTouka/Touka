# ruff: noqa: T201, TRY003, E402
"""Smoke Test for Creation Engine (ADR-0069).

Verifies:
1. Non-admin roles are restricted from accessing creation endpoints (403).
2. Authorized admin/Owner roles can successfully scaffold a new FastMCP server.
3. Code generation uses standard syntax, parses safely, and does not overwrite existing components.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

# 1. Path Setup (SSOT) — 含 tests/smoke 以匯入 lib.*
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
for _p in (PROXY_DIR, MEMORY_SRC_DIR, Path(__file__).parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ADR-0103：import src 前先啟用 sandbox（避免重量級啟動 → teardown 死鎖；
# 且 creation_engine 改 server_config 改在 sandbox、不污染真實設定）
from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("creation_engine")

from src.config import get_config_dir
from src.main import app
from src.models.identity import AgentIdentity
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager


def _clean_up_config(test_server_name: str):
    config_path = get_config_dir() / "server_config.json"
    if resource_manager.exists(config_path):
        try:
            data = resource_manager.read_json(config_path)
            if data and "mcpServers" in data and test_server_name in data["mcpServers"]:
                del data["mcpServers"][test_server_name]
                resource_manager.write_json(config_path, data)
                print(f"[*] Cleaned up '{test_server_name}' from server_config.json")
        except Exception as e:
            print(f"[*] Warning: could not clean up server_config.json: {e}")


def run_creation_smoke_test():
    print("[*] Starting Creation Engine Smoke Test")

    # Reload services
    permission_service.reload_all()

    # Define identities
    # ADR-0245：Guest 退役，低權限樣本改用 Base。
    guest_identity = AgentIdentity.for_role(agent_id="LowAgent", role="Base")
    admin_identity = AgentIdentity.for_role(agent_id="DebugAgent", role="admin")

    # Issue tokens
    identity_service.issue_token(guest_identity)
    identity_service.issue_token(admin_identity)

    test_server_name = "smoke_scaffold_server"
    target_py_file = get_config_dir() / "mcp_servers" / test_server_name / "server.py"

    # Clean up any lingering config registration
    _clean_up_config(test_server_name)

    # Ensure no lingering files from previous test failures
    if target_py_file.exists():
        target_py_file.unlink()
    if target_py_file.parent.exists():
        try:
            target_py_file.parent.rmdir()
        except Exception as e:
            print(f"[*] Note: pre-test parent directory cleanup skipped: {e}")

    with TestClient(app) as client:
        # --- 1. Test Auth Gate Restriction (low-privilege Base should get 403) ---
        print("\n[1] Testing Auth Gate Restriction for creation endpoints...")
        app.dependency_overrides[get_verified_identity] = lambda: guest_identity
        try:
            # Attempt to call scaffold creation endpoint
            payload = {
                "name": test_server_name,
                "description": "A temporary smoke scaffold test server.",
                "tools": [
                    {
                        "name": "add_numbers",
                        "description": "Add two numbers.",
                        "params": [
                            {"name": "a", "type": "int"},
                            {"name": "b", "type": "int"}
                        ]
                    }
                ]
            }
            resp = client.post("/v1/creation/scaffold", json=payload)
            print(f"[*] Low-privilege request status: {resp.status_code}")
            if resp.status_code != 403:
                print(f"[FAIL] Low-privilege should be blocked with 403, got {resp.status_code}")
                sys.exit(1)
            print("[OK] Low-privilege role correctly restricted with 403.")
        finally:
            app.dependency_overrides.clear()

        # --- 2. Test Authorized Scaffold Creation (Admin role should succeed) ---
        print("\n[2] Testing Authorized Scaffold MCP Server Generation...")
        app.dependency_overrides[get_verified_identity] = lambda: admin_identity
        try:
            resp = client.post("/v1/creation/scaffold", json=payload)
            print(f"[*] Admin request status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"[FAIL] Admin scaffold request failed with status {resp.status_code}")
                print(f"[*] Response: {resp.text}")
                sys.exit(1)

            data = resp.json()
            if not data.get("committed", False):
                print(f"[FAIL] Scaffold was not marked as committed! Errors: {data.get('errors')}")
                sys.exit(1)

            print("[OK] Scaffold request completed successfully.")

            # --- 3. Verify physical file generation and syntactic correctness ---
            print("\n[3] Verifying generated source file...")
            if not target_py_file.exists():
                print(f"[FAIL] Generated file not found on disk: {target_py_file}")
                sys.exit(1)

            print(f"[OK] File generated on disk: {target_py_file}")
            file_content = target_py_file.read_text(encoding="utf-8")
            
            # Simple syntax verification using compilation
            import ast
            try:
                ast.parse(file_content)
                print("[OK] Generated source code parsed successfully (syntax is valid).")
            except SyntaxError as e:
                print(f"[FAIL] Generated source code has syntax errors: {e}")
                sys.exit(1)

            # --- 4. Verify Overwrite Prevention ---
            print("\n[4] Testing Overwrite Prevention...")
            # Sending same scaffold request again should be blocked to prevent
            # overwriting existing ID
            resp_second = client.post("/v1/creation/scaffold", json=payload)
            print(f"[*] Second request status: {resp_second.status_code}")
            # Creation engine VALIDATE blocks overwriting and reports in 200 list
            # with committed=False, or 400
            second_data = resp_second.json()
            if second_data.get("committed", False):
                print("[FAIL] Re-creation should not overwrite existing files.")
                sys.exit(1)
            print("[OK] Overwrite correctly blocked by creation engine validation.")

        finally:
            # Clean up target file and directory to maintain zero clutter
            if target_py_file.exists():
                target_py_file.unlink()
                print(f"[*] Cleaned up temporary test file: {target_py_file}")
            if target_py_file.parent.exists():
                try:
                    target_py_file.parent.rmdir()
                    print(f"[*] Cleaned up temporary test directory: {target_py_file.parent}")
                except Exception as e:
                    print(f"[*] Could not clean up directory {target_py_file.parent}: {e}")
            _clean_up_config(test_server_name)
            app.dependency_overrides.clear()

    print("\n" + "=" * 50)
    print("[SUCCESS] Creation Engine Smoke Tests Passed!")
    print("=" * 50)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    run_creation_smoke_test()
