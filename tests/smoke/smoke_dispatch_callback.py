# ruff: noqa: T201, TRY003, E402, B008
"""Standalone executable smoke test for Dispatch Callback (ADR-0051 / ADR-0064).

Verifies:
1. Dispatch Callback endpoint security rejects external origin requests (403).
2. Unregistered tokens are rejected with unauthorized status (401).
3. Successful token registration, callback notification delivery, and await resolution.
"""
import asyncio
import sys
from pathlib import Path

from fastapi import Header
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

enable_smoke_sandbox("dispatch_callback")

from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.routers.dispatch_callback import verify_dispatch_callback
from src.services.dispatch_callback_store import (
    _reset_callback_store,
    get_callback_store,
)


def run_dispatch_callback_smoke_test():
    print("[*] Starting Dispatch Callback Smoke Test")

    # Ensure clean state
    _reset_callback_store()
    store = get_callback_store()

    with TestClient(app, raise_server_exceptions=False) as client:
        # --- 1. Test External Origin Blocking (Should fail with 403) ---
        print("\n[1] Testing Security: Rejects external origin...")
        resp = client.post(
            "/v1/internal/dispatch/callback",
            headers={"X-Callback-Token": "test-token"},
            json={"task_id": "smoke-t1", "status": "DONE", "result_summary": "Smoke Test"},
        )
        print(f"[*] Response status: {resp.status_code}")
        if resp.status_code != 403:
            print(f"[FAIL] Expected 403 for external origin, got {resp.status_code}")
            sys.exit(1)
        print("[OK] External origin correctly blocked with 403.")

        # --- 2. Test Unregistered Token Blocking (Should fail with 401) ---
        print("\n[2] Testing Security: Rejects unregistered token (IP bypassed)...")

        async def ip_bypassed_verifier(
            x_callback_token: str = Header(alias="X-Callback-Token"),
        ) -> str:
            if not store.is_registered(x_callback_token):
                raise ToukaException(ErrorCode.AUTH_INVALID_TOKEN)
            return x_callback_token

        app.dependency_overrides[verify_dispatch_callback] = ip_bypassed_verifier
        try:
            resp = client.post(
                "/v1/internal/dispatch/callback",
                headers={"X-Callback-Token": "unregistered-token"},
                json={"task_id": "smoke-t1", "status": "DONE", "result_summary": ""},
            )
            print(f"[*] Response status: {resp.status_code}")
            if resp.status_code != 401:
                print(f"[FAIL] Expected 401 for unregistered token, got {resp.status_code}")
                sys.exit(1)
            print("[OK] Unregistered token correctly rejected with 401.")
        finally:
            app.dependency_overrides.clear()

        # --- 3. Test Full Successful Roundtrip (Should succeed with 200 and deliver result) ---
        print("\n[3] Testing Bidirectional Callback Roundtrip...")
        token = store.generate_token()
        store.register(token)

        app.dependency_overrides[verify_dispatch_callback] = ip_bypassed_verifier
        try:
            resp = client.post(
                "/v1/internal/dispatch/callback",
                headers={"X-Callback-Token": token},
                json={
                    "task_id": "smoke-t1",
                    "status": "DONE",
                    "result_summary": "Successfully resolved via smoke test",
                },
            )
            print(f"[*] Response status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"[FAIL] Expected 200, got {resp.status_code}")
                print(f"[*] Response: {resp.text}")
                sys.exit(1)

            data = resp.json()
            if data.get("status") != "delivered":
                print(f"[FAIL] Expected status=delivered, got {data}")
                sys.exit(1)

            print("[OK] Callback successfully delivered to proxy.")

            # Validate that wait_for_result resolves correctly
            async def verify_resolution():
                result = await store.wait_for_result(token, timeout=1.0)
                if result.get("status") != "DONE" or result.get("task_id") != "smoke-t1":
                    print(f"[FAIL] Unexpected wait_for_result content: {result}")
                    sys.exit(1)
                print("[OK] Async wait_for_result resolved with correct data.")

            asyncio.run(verify_resolution())

        finally:
            app.dependency_overrides.clear()

    # Reset state to clean up
    _reset_callback_store()
    print("\n[SUCCESS] Dispatch Callback Smoke Test completed successfully.")


if __name__ == "__main__":
    run_dispatch_callback_smoke_test()
