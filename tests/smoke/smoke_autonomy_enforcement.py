# ruff: noqa: T201, TRY003, E402

"""Smoke Test for Autonomy Enforcement (Budget System).

Verifies the priority chain of autonomy slots and the enforcement of 
max turns in the ReAct loop.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("autonomy_enforcement")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))  # 共用串流假傳輸（tests/helpers，不含產品程式）

# Import core components
from src.adapters.schemas import OpenAIMessage  # noqa: E402
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.rhythm_service import rhythm_service

from tests.helpers.stream_transport import FakeUpstream, stream_reply


async def run_autonomy_enforcement_smoke_test():
    print("[*] Starting Autonomy Enforcement Smoke Test")

    # 1. Testing Decision Chain Priority（ADR-0146 工具輪語意）
    print("\n[1] Testing Decision Chain Priority...")

    # Priority 1: Requested Rounds（排程）
    config = {"react_loop": {"max_tool_rounds": 10}}
    eff = rhythm_service.get_effective_rounds(config, 3, headless=True)
    if eff != 3:
        print(f"[FAIL] Priority 1 (Requested) failed: expected 3, got {eff}")
        sys.exit(1)

    # Priority 2: 全域保險絲
    eff = rhythm_service.get_effective_rounds(config, None, headless=False)
    if eff != 10:
        print(f"[FAIL] Priority 2 (Config) failed: expected 10, got {eff}")
        sys.exit(1)

    # Priority 3: 互動預設不限（0）；無人強制保底 50
    eff_i = rhythm_service.get_effective_rounds({}, None, headless=False)
    eff_h = rhythm_service.get_effective_rounds({}, None, headless=True)
    if eff_i != 0 or eff_h != 50:
        print(f"[FAIL] Priority 3 failed: expected 0/50, got {eff_i}/{eff_h}")
        sys.exit(1)
    print("[OK] Decision chain priority passed.")

    # 2. Testing Turn Termination（ADR-0146：純文字＝回合結束，nudge 已移除）
    print("\n[2] Testing Turn Termination (text ends turn)...")

    request = MagicMock()
    request.model = "test-model"
    request.messages = [OpenAIMessage(role="user", content="Test Request")]
    request.model_dump.return_value = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Test Request"}]
    }

    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")

    # 模擬模型純文字回覆（無工具調用、無任何「停止關鍵字」——已無此概念）
    mock_resp_json = {
        "choices": [{
            "message": {"content": "I am thinking...", "role": "assistant"},
            "index": 0
        }],
        "usage": {"total_tokens": 10}
    }

    # 3b-U2：主迴圈對已知廠商走自寫 SSE 串流（共用假傳輸；送出次數＝請求數）
    upstream = FakeUpstream([stream_reply(mock_resp_json, "semantic")] * 2)
    with patch.object(httpx.AsyncClient, "send", upstream.send_fn()):
        context = {
            "identity": identity,
            "url": "http://test-api",
            "provider": "OpenAI",
            "custom_connection": False,  # 直接呼叫 handler：明寫不是自訂連線（R-3b ① L4）
            "tool_registry": {},
            "memory_tool_registry": {},
            "plugin_registry": {},
            "request_id": "test-autonomy-req",
            "is_toukasoul": True
        }

        from src.handlers.openai_handler import handle_openai_request

        # 重置狀態
        autonomy_service.reload_all()
        rhythm_service.reload_all()

        # 執行 Handler
        chunks = []
        async for chunk in handle_openai_request(request, context):
            chunks.append(chunk)

        # 驗證 POST 次數：純文字＝回合結束，恰好 1 次（舊制 nudge 會硬續成 2 次）
        if len(upstream.requests) != 1:
            print(f"[FAIL] Enforcement failed: expected 1 POST, got {len(upstream.requests)}")
            sys.exit(1)

        print("[OK] Text reply correctly ended the turn (no nudge).")

    # 3. Testing Daily Quota Enforcement
    print("\n[3] Testing Daily Quota Enforcement...")
    autonomy_service.config = {
        "quota": {"daily_max": 2, "reset_hour": 0},
        "react_loop": {"max_tool_rounds": 0}
    }
    autonomy_service.reset_quota(identity)
    
    # 第 1 次消費
    autonomy_service.consume_quota(identity, "req-1")
    # 第 2 次消費
    autonomy_service.consume_quota(identity, "req-2")
    
    # 檢查是否超出
    if autonomy_service.check_quota(identity):
        print("[FAIL] Quota should be exhausted after 2 requests.")
        sys.exit(1)
        
    print("[OK] Daily quota enforcement passed.")

    print("\n" + "="*50)
    print("[SUCCESS] Autonomy Enforcement Smoke Tests Passed!")
    print("="*50)


if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(run_autonomy_enforcement_smoke_test())
