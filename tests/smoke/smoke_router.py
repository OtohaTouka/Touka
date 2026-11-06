# ruff: noqa: T201, TRY003, E402
"""Standalone executable smoke test for all Router GET endpoints.

Verifies that every registered GET endpoint does not return 500 when called.
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

# ADR-0103：import src 前先啟用 sandbox（避免重量級啟動 → teardown 死鎖）
from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("router")

from src.main import app
from src.models.identity import AgentIdentity
from src.models.user import User
from src.routers.auth import get_current_user
from src.services.identity_router import get_verified_identity
from src.services.identity_service import identity_service
from src.services.permission_service import permission_service


def run_router_smoke_test():
    print("[*] Starting All-Router GET Endpoints Smoke Test")

    # Reload services
    permission_service.reload_all()

    # Create mock identities and tokens
    normal_identity = AgentIdentity.for_role(
        agent_id="DebugAgent", role="Debugger", capabilities=["heartbeat_awakening"]
    )
    identity_service.issue_token(normal_identity)

    admin_identity = AgentIdentity.for_role(agent_id="DebugAgent", role="admin")
    identity_service.issue_token(admin_identity)

    mock_user = User(
        id="smoke-user",
        email="smoke@touka.io",
        hashed_password="hashed",  # noqa: S106
        role="admin",
        owned_agents=["DebugAgent"],
    )

    # We will test endpoints directly on the TestClient with appropriate overrides
    endpoints = [
        # --- Normal client ---
        {"path": "/v1/autonomy", "role": "normal", "expected_status": {200}},
        {"path": "/v1/bots", "role": "normal", "expected_status": {200}},
        {"path": "/v1/ui/custom_models", "role": "normal", "expected_status": {200}},
        {"path": "/v1/models", "role": "normal", "expected_status": {200}},
        {"path": "/v1/skills/list", "role": "normal", "expected_status": {200}},
        {"path": "/v1/souls", "role": "normal", "expected_status": {200}},
        {"path": "/v1/stats", "role": "normal", "expected_status": {200}},
        {"path": "/v1/system/sliding_window", "role": "normal", "expected_status": {200}},
        {"path": "/v1/system_health", "role": "normal", "expected_status": {200}},
        {"path": "/v1/memory", "role": "normal", "expected_status": {200}},
        {"path": "/v1/tools", "role": "normal", "expected_status": {200}},
        {"path": "/ui/sessions", "role": "normal", "expected_status": {200}},
        {"path": "/ui/settings/workspace", "role": "normal", "expected_status": {200}},
        {"path": "/ui/projects/list", "role": "normal", "expected_status": {200}},
        {"path": "/ui/mission", "role": "normal", "expected_status": {200, 404}},
        {"path": "/terminal/history", "role": "normal", "expected_status": {200}},
        {"path": "/monitor/summary", "role": "normal", "expected_status": {200}},

        # --- Admin client ---
        {"path": "/v1/permissions", "role": "admin", "expected_status": {200}},
        {"path": "/v1/system/backups", "role": "admin", "expected_status": {200}},

        # --- User client ---
        {"path": "/v1/agents", "role": "user", "expected_status": {200}},
        {"path": "/v1/config/env", "role": "user", "expected_status": {200}},
    ]

    with TestClient(app) as client:
        for ep in endpoints:
            path = ep["path"]
            role = ep["role"]
            expected = ep["expected_status"]

            print(f"[*] Checking GET {path:<30} (role={role}) ... ", end="", flush=True)

            # Setup overrides
            app.dependency_overrides.clear()
            if role == "normal":
                app.dependency_overrides[get_verified_identity] = lambda: normal_identity
            elif role == "admin":
                app.dependency_overrides[get_verified_identity] = lambda: admin_identity
            elif role == "user":
                app.dependency_overrides[get_current_user] = lambda: mock_user

            try:
                resp = client.get(path)
                status = resp.status_code
                if status == 500:
                    print("[FAIL] status=500")
                    sys.exit(1)
                elif status not in expected:
                    print(
                        f"[WARN] status={status} not in expected {expected} "
                        "(ignored for smoke check unless 500)"
                    )
                else:
                    print(f"[OK] status={status}")
            except Exception as e:
                print(f"[FAIL] exception={e}")
                sys.exit(1)
            finally:
                app.dependency_overrides.clear()

    print("\n" + "=" * 50)
    print("[SUCCESS] All GET Endpoints Passed Router Smoke Test!")
    print("=" * 50)


if __name__ == "__main__":
    # Force UTF-8 output
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    run_router_smoke_test()
