# ruff: noqa: T201, TRY003, E402

"""Smoke Test for Multi-Agent Interaction (Context Accumulation).

Verifies that sequential agents in a group chat receive the previous responses.
Uses generic AgentAlpha and AgentBeta IDs for privacy.
"""
import asyncio
import copy
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("agent_interaction")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

# Import core components
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services.dispatchers.group import dispatch_group_stream  # noqa: E402
from src.utils.group_member_view import PEER_HEADER_TEMPLATE


async def mock_stream_gen(text):
    """Simulates a model stream response."""
    payload = {"choices": [{"delta": {"content": text}, "index": 0}]}
    yield f"data: {json.dumps(payload)}\n\n"
    yield "data: [DONE]"


async def run_agent_interaction_smoke_test():
    print("[*] Starting Multi-Agent Interaction Smoke Test (Generic IDs)")

    # 1. Prepare generic request (calling Alpha and Beta)
    messages = [
        OpenAIMessage(role="user", content="@AgentAlpha @AgentBeta 幫我分析這段邏輯。")
    ]
    request = OpenAIChatRequest(
        model="ToukaGroup",
        messages=messages,
        stream=True
    )

    # 2. Mock registries and services
    # 這裡的映射必須與 parse_group_roster 的期待一致
    mock_registry = {
        "AgentAlpha": ["Alpha"],
        "AgentBeta": ["Beta"]
    }
    
    # We will capture the requests sent to each agent
    captured_requests = []

    async def side_effect(model, current_req, identity, *args, **kwargs):
        # 深拷貝請求以供後續驗證
        captured_requests.append(copy.deepcopy(current_req))
        if identity.agent_id == "AgentAlpha":
            return mock_stream_gen("我是 Alpha，這是初步分析。")
        return mock_stream_gen("我是 Beta，我補充幾點。")

    print("\n[1] Testing Sequential Dispatch & Context Accumulation...")
    
    # 讓 parse_group_roster 成功：load_whitelist 回傳 ToukaSoul 區的 別名→靈魂 ID（ADR-0245）
    mock_whitelist = {
        "AgentAlpha": "AgentAlpha",
        "AgentBeta": "AgentBeta"
    }

    with patch("src.services.dispatchers.group.get_config_dir") as mock_conf_dir, \
         patch("src.services.resource_manager.resource_manager.read_json") as mock_read_json, \
         patch(
             "src.services.dispatchers.group.get_stream_generator", 
             new_callable=AsyncMock
         ) as mock_get_gen, \
         patch("src.services.dispatchers.group.soul_service") as mock_soul, \
         patch("src.services.dispatchers.group.get_active_brain", return_value="test-model"), \
         patch("src.services.dispatchers.group.record_working_brain"), \
         patch("src.services.dispatchers.group.inject_mainline", new_callable=AsyncMock), \
         patch("src.services.group_router.load_whitelist", return_value=mock_whitelist), \
         patch("src.services.group_router.soul_service") as mock_souls:
        mock_souls.is_toukasoul.return_value = True  # AgentAlpha／AgentBeta are mock souls

        mock_conf_dir.return_value = Path("/tmp")  # noqa: S108
        mock_read_json.return_value = mock_registry
        mock_get_gen.side_effect = side_effect
        mock_soul.apply_soul_to_request = MagicMock()
        mock_soul.inject_home_messages = AsyncMock()

        # Execute dispatcher
        responses = []
        # 群聊要有呼叫者身分（ADR-0327）
        admin = AgentIdentity.for_role("group-admin", "admin", is_human=True)
        ctx = PipelineContext(is_native=True, identity=admin)
        async for chunk in dispatch_group_stream(request, "test-req-id", 0.0, ctx):
            responses.append(chunk)

        # 3. 驗證斷言 (Assertions)
        
        # 驗證調用順序與次數
        if len(captured_requests) != 2:
            print(f"[FAIL] Expected 2 agent calls, got {len(captured_requests)}")
            sys.exit(1)

        # 驗證 AgentAlpha 的請求 (第一位)
        alpha_req = captured_requests[0]
        # 第一位應該只看到原始 User Message + 系統注入的 awareness_note
        if len(alpha_req.messages) != 1:
            print(f"[FAIL] AgentAlpha expected 1 message, got {len(alpha_req.messages)}")
            sys.exit(1)
        print("[OK] AgentAlpha received the initial request correctly.")

        # 驗證 AgentBeta 的請求 (第二位 - 核心測試點)
        beta_req = captured_requests[1]
        
        # ADR-0322：Alpha 的話是別人說的，包成群聊成員發言放在 user 那側，和使用者那句
        # 合成一則（awareness_note 接在後面）；請求以 user 結尾，不用補接續發言。
        if len(beta_req.messages) != 1:
            msg_count = len(beta_req.messages)
            print(f"[FAIL] AgentBeta expected 1 message, got {msg_count}")
            for m in beta_req.messages:
                print(f"  - {m.role}: {m.content[:50]}...")
            sys.exit(1)

        beta_msg = beta_req.messages[0]
        peer_header = PEER_HEADER_TEMPLATE.format(alias="AgentAlpha")
        if beta_msg.role != "user" or peer_header not in beta_msg.content \
                or "我是 Alpha" not in beta_msg.content:
            print("[FAIL] AgentBeta did not receive AgentAlpha's words as a peer block.")
            sys.exit(1)

        if "接續發言" in beta_msg.content:
            print("[FAIL] AgentBeta got a nudge although its request already ends with user.")
            sys.exit(1)

        print("[OK] AgentBeta received AgentAlpha's words as someone else's.")

        # 驗證標籤注入 (UX Sovereignty)
        full_output = "".join(responses)
        # 由於是 JSON 串流，標籤可能會被轉義成 \u3010...
        # 直接檢查是否包含 Agent 名稱，這在 JSON 中通常是保持原樣的（AgentAlpha 是 ASCII）
        if "AgentAlpha" not in full_output or "AgentBeta" not in full_output:
            print(f"[FAIL] Agent names missing in output stream. Output: {full_output[:100]}...")
            sys.exit(1)
        print("[OK] UI labels correctly injected into stream.")

    print("\n" + "="*50)
    print("[SUCCESS] Multi-Agent Interaction Smoke Tests Passed!")
    print("="*50)


if __name__ == "__main__":
    # 強制使用 UTF-8 輸出
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(run_agent_interaction_smoke_test())
