# ruff: noqa: T201, TRY003

"""Smoke Test for Mission Awareness.

Verifies the integration between MissionBoard (Storage) and Prompt Injection (Middleware).
Ensures the Agent receives current task status in its system prompt.
"""
import asyncio
import sys
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("mission_awareness")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

# Import core components
from memory_server.models.mission import TaskStatus  # noqa: E402
from memory_server.services.mission_manager import MissionManager  # noqa: E402
from src.adapters.schemas import AnthropicMessage, AnthropicRequest  # noqa: E402
from src.middleware.layers.context import ContextLayer  # noqa: E402
from src.middleware.layers.tool import ToolLayer  # noqa: E402
from src.models.context import PipelineContext  # noqa: E402
from src.models.identity import AgentIdentity  # noqa: E402
from src.services.resource_manager import resource_manager  # noqa: E402


async def run_mission_awareness_smoke_test():
    print("[*] Starting Mission Awareness Smoke Test")
    
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    manager = MissionManager()
    
    # --- 1. 初始化測試任務 ---
    print("\n[1] Initializing test mission...")
    goal = "Verify Mission Awareness Injection"
    tasks = [
        {"id": "task_1", "description": "Phase 1: Setup"},
        {"id": "task_2", "description": "Phase 2: Verification"}
    ]
    mission = await manager.initialize_mission(identity, goal, tasks)
    print(f"[*] Mission Created: {mission.mission_id} (Goal: {goal})")

    # --- 2. 模擬 Middleware 鏈路 ---
    print("\n[2] Simulating Middleware Pipeline...")
    request = AnthropicRequest(
        model="claude-3-5-sonnet-latest",
        messages=[AnthropicMessage(role="user", content="What's my next step?")],
        system="Original System Prompt."
    )
    ctx = PipelineContext(identity=identity, agent_id=identity.agent_id)
    
    # 執行 ToolLayer (負責載入 Mission 物件)
    tool_layer = ToolLayer()
    await tool_layer.process(request, ctx)
    
    if not ctx.get("mission_awareness"):
        print("[FAIL] ToolLayer failed to load mission into context.")
        sys.exit(1)
    print("[OK] ToolLayer loaded mission awareness data.")

    # 執行 ContextLayer (負責注入標籤)
    context_layer = ContextLayer()
    await context_layer.process(request, ctx)
    
    print(f"[*] Final System Prompt Sample:\n{request.system[:200]}...")
    
    if "<mission_awareness>" not in request.system:
        print("[FAIL] Mission awareness block NOT found in system prompt.")
        sys.exit(1)
    if goal not in request.system:
        print("[FAIL] Mission goal missing from injected block.")
        sys.exit(1)
    print("[OK] Mission awareness successfully injected into system prompt.")

    # --- 3. 進度同步測試 ---
    print("\n[3] Testing progress synchronization...")
    await manager.update_task(identity, "task_1", TaskStatus.DOING)
    await manager.update_task(identity, "task_1", TaskStatus.DONE)
    
    # 重新執行鏈路
    print("[*] Re-running middleware pipeline after update...")
    request.system = "Original System Prompt."
    # 清除舊的 awareness 資料以確保重新載入
    ctx["mission_awareness"] = None
        
    await tool_layer.process(request, ctx)
    
    # 偵錯：檢查 ctx 中的任務狀態
    m = ctx.get("mission_awareness")
    if m:
        print(f"[*] Mission state in ctx: {m.status}, Tasks: {[(t.id, t.status) for t in m.tasks]}")
    else:
        print("[!] No mission awareness found in ctx after ToolLayer.")

    await context_layer.process(request, ctx)
    
    snippet = request.system if len(request.system) < 500 else request.system[:500] + "..."
    print(f"[*] Updated System Prompt (Snippet):\n{snippet}")
    
    if "✅ task_1" not in request.system:
        print(f"[FAIL] Updated task status (DONE) not reflected in prompt. Prompt length: {len(request.system)}")
        sys.exit(1)
    if "1/2" not in request.system:
        print(f"[FAIL] Progress ratio not updated. Prompt was: {request.system}")
        sys.exit(1)
    print("[OK] Progress synchronization verified.")

    # --- 4. 環境隔離與清理 ---
    print("\n[4] Cleanup and Isolation...")
    # 清理測試資料 (刪除 current_mission.json)
    path = manager._get_mission_path(identity)
    if resource_manager.exists(str(path)):
        resource_manager.remove_file(str(path))
    print(f"[*] Deleted test mission at: {path}")

    print("\n" + "="*50)
    print("[SUCCESS] Mission Awareness Smoke Tests Passed!")
    print("="*50)

if __name__ == "__main__":
    # 強制使用 UTF-8 輸出
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    asyncio.run(run_mission_awareness_smoke_test())
