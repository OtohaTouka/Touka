# ruff: noqa: T201, TRY003

"""Smoke Test for Group Dispatcher (ToukaGroup).

Verifies alias resolution via group_whitelist.json and multi-agent dispatching logic.
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("group_dispatcher")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage  # noqa: E402
from src.models.context import PipelineContext  # noqa: E402
from src.services.dispatchers.group import dispatch_group_stream  # noqa: E402
from src.services.group_router import parse_group_roster  # noqa: E402
from src.services.resource_manager import resource_manager  # noqa: E402


async def run_group_dispatcher_smoke_test():
    print("[*] Starting Group Dispatcher Smoke Test")

    # Mock Registry & Whitelist
    mock_registry = {
        "Alice": ["Ember", "Ember"],
        "Charlie": ["代碼"]
    }
    mock_whitelist = {
        "ToukaSoul": {
            "小愛": "Alice",
            "代碼": "Charlie"
        }
    }

    # 1. Alias Resolution Test
    print("\n[1] Testing Alias Resolution (parse_group_roster)...")
    
    messages = [OpenAIMessage(role="user", content="你好 @小愛，請叫 @代碼 出來")]
    
    # Use patch.object for more reliable instance patching
    # Alice／Charlie are mock souls (group members must be souls, ADR-0245).
    with patch.object(resource_manager, "read_json", return_value=mock_whitelist), \
         patch("src.services.group_router.soul_service") as mock_souls:
        mock_souls.is_toukasoul.return_value = True
        roster = parse_group_roster(messages, mock_registry)
        
        if isinstance(roster, dict) and "error" in roster:
            print(f"[FAIL] Roster parsing error: {roster['error']}")
            sys.exit(1)
            
        names = [r["name"] for r in roster]
        ids = [r["id"] for r in roster]
        print(f"[*] Roster: {roster}")
        
        if "小愛" not in names or "代碼" not in names:
            print("[FAIL] Missing aliases in roster.")
            sys.exit(1)
        if "Alice" not in ids or "Charlie" not in ids:
            print("[FAIL] Failed to resolve real IDs.")
            sys.exit(1)
    print("[OK] Alias resolution passed.")

    # 2. Dispatcher Stream Test
    print("\n[2] Testing Dispatcher Stream (dispatch_group_stream)...")
    
    request = OpenAIChatRequest(
        model="ToukaGroup",
        messages=messages
    )
    
    # Mock stream generator to avoid real LLM calls
    async def mock_gen(*args, **kwargs):
        # 模擬一個簡單的流式回應
        yield 'data: {"choices": [{"delta": {"content": "Hello world"}, "index": 0}]}\n\n'
        yield "data: [DONE]\n\n"

    # 設定一系列的 Mock
    with patch("src.services.dispatchers.group.get_stream_generator", side_effect=mock_gen), \
         patch("src.services.dispatchers.group.get_active_brain", return_value="claude-3-haiku"), \
         patch("src.services.dispatchers.group.record_working_brain"), \
         patch("src.services.dispatchers.group.soul_service") as mock_soul, \
         patch("src.services.group_router.soul_service") as mock_souls, \
         patch.object(resource_manager, "read_json") as mock_read:
        mock_souls.is_toukasoul.return_value = True
        
        # 第一次 call 給 registry, 第二次 call 給 whitelist
        mock_read.side_effect = [mock_registry, mock_whitelist, mock_registry, mock_whitelist]
        mock_soul.apply_soul_to_request = MagicMock()
        mock_soul.inject_home_messages = AsyncMock()
        
        chunks = []
        async for chunk in dispatch_group_stream(
            request, "test_id", 0.0, PipelineContext(is_native=False)
        ):
            chunks.append(chunk)
        
        full_text = ""
        for chunk in chunks:
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    data = json.loads(chunk[6:])
                    if "choices" in data:
                        full_text += data["choices"][0]["delta"].get("content", "")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass
            elif not chunk.startswith("data: "):
                full_text += chunk

        print(f"[*] Decoded Full Text:\n{full_text}")
        
        if "小愛" not in full_text:
            print(f"[FAIL] Name '小愛' missing in decoded text. Got: {full_text}")
            sys.exit(1)
        if "【" not in full_text or "】" not in full_text:
            print("[FAIL] Brackets missing in decoded text.")
            sys.exit(1)
        if "代碼" not in full_text:
            print("[FAIL] Name '代碼' missing in decoded text.")
            sys.exit(1)
        if "---" not in full_text:
            print("[FAIL] Separator missing in decoded text.")
            sys.exit(1)
    print("[OK] Dispatcher stream labels and sequence passed.")

    # 3. Error Handling Test
    print("\n[3] Testing Invalid Alias Handling...")
    bad_messages = [OpenAIMessage(role="user", content="你好 @NoSuchAgent")]
    bad_request = OpenAIChatRequest(model="ToukaGroup", messages=bad_messages)
    
    with patch.object(resource_manager, "read_json") as mock_read:
        mock_read.side_effect = [mock_registry, mock_whitelist]
        chunks = []
        async for chunk in dispatch_group_stream(
            bad_request, "test_id", 0.0, PipelineContext(is_native=False)
        ):
            chunks.append(chunk)
        
        # 解碼 JSON
        full_text = ""
        for chunk in chunks:
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    data = json.loads(chunk[6:])
                    if "choices" in data:
                        full_text += data["choices"][0]["delta"].get("content", "")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass
        
        if "❌" not in full_text and "系統提示" not in full_text:
            print(f"[FAIL] Error message not found in decoded stream. Got: {full_text}")
            sys.exit(1)
    print("[OK] Invalid alias handling passed.")

    print("\n" + "="*50)
    print("[SUCCESS] Group Dispatcher Smoke Tests Passed!")
    print("="*50)


if __name__ == "__main__":
    # 強制使用 UTF-8 輸出
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    asyncio.run(run_group_dispatcher_smoke_test())
