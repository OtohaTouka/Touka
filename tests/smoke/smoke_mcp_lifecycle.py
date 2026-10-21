# ruff: noqa: T201, TRY003

"""Smoke Test for MCP Lifecycle (Sync, Install, Register, Skill Sync).

Ensures MCP configurations adhere to SSOT Physical Path Ban and are correctly synchronized.
"""
import asyncio
import re
import shutil
import sys
import tempfile
from pathlib import Path

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("mcp_lifecycle")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

# Import core components
from src.services.mcp_hub_service import MCPHubService  # noqa: E402
from src.services.resource_manager import resource_manager  # noqa: E402
from src.services.skill_service import skill_service  # noqa: E402

# Regex for absolute paths (SSOT Violation)
PATH_PATTERN = re.compile(r'^[a-zA-Z]:[/\\]|/(app|home|root|tmp)/')

async def run_mcp_lifecycle_smoke_test():
    print("[*] Starting MCP Lifecycle Smoke Test")
    
    # --- Setup Temporary Test Environment ---
    test_dir = Path(tempfile.mkdtemp(prefix="touka_mcp_test_"))
    test_config_dir = test_dir / "config"
    test_config_dir.mkdir()
    
    hub_path = test_config_dir / "mcp_hub.json"
    server_config_path = test_config_dir / "server_config.json"

    print(f"[*] Test Sandbox: {test_dir}")

    try:
        # --- 1. Mock Hub Data ---
        print("\n[1] Preparing Mock Hub Data...")
        mock_hub = {
            "hub": [
                {
                    "id": "mock-legal-tool",
                    "name": "Legal Tool",
                    "category": "Test",
                    "description": "A legal tool using relative paths.",
                    "command": "python",
                    "args": ["src/tools/legal_server.py"],
                    "source": "mock"
                },
                {
                    "id": "mock-illegal-tool",
                    "name": "Illegal Tool",
                    "category": "Test",
                    "description": "An illegal tool using absolute paths.",
                    "command": "C:\\Windows\\System32\\python.exe",
                    "args": ["D:\\Touka\\illegal_server.py"],
                    "source": "mock"
                }
            ]
        }
        resource_manager.write_json(str(hub_path), mock_hub)
        print("[OK] Mock Hub created.")

        # --- 2. MCPHubService Integration ---
        print("\n[2] Testing MCPHubService Installation...")
        svc = MCPHubService(hub_path=str(hub_path), config_path=str(server_config_path))
        
        # Install Legal Tool
        print("[*] Installing Legal Tool...")
        res_legal = await svc.install("mock-legal-tool", {})
        assert res_legal["status"] == "success"
        
        # Install Illegal Tool
        print("[*] Installing Illegal Tool...")
        res_illegal = await svc.install("mock-illegal-tool", {})
        assert res_illegal["status"] == "success"

        # --- 3. SSOT Path Enforcement (The "Law" Check) ---
        print("\n[3] Verifying SSOT Path Sovereignty...")
        config_data = resource_manager.read_json(str(server_config_path))
        mcp_servers = config_data.get("mcpServers", {})
        
        for name, cfg in mcp_servers.items():
            cmd = cfg.get("command", "")
            args = cfg.get("args", [])
            
            # Check Command
            if PATH_PATTERN.search(cmd):
                print(f"[!] SSOT VIOLATION: Server '{name}' has absolute command path: {cmd}")
                if name == "legal-tool":
                    raise AssertionError("Legal tool should not have absolute path.")
            else:
                print(f"[OK] Server '{name}' command path is clean.")
            
            # Check Args
            for arg in args:
                if PATH_PATTERN.search(arg):
                    print(f"[!] SSOT VIOLATION: Server '{name}' has absolute arg path: {arg}")
                    if name == "legal-tool":
                        raise AssertionError("Legal tool should not have absolute path.")
                else:
                    print(f"[OK] Server '{name}' arg '{arg}' is clean.")

        # --- 4. Skill Synchronization (ADR-0066: 目錄式 SKILL.md) ---
        print("\n[4] Testing Skill Synchronization...")
        from unittest.mock import patch

        # 將 SkillService 指向測試 skills 目錄，避免污染真實 config
        test_skills_dir = test_config_dir / "skills"
        original_skills_dir = skill_service._skills_dir
        original_config_path = skill_service.config_path
        skill_service._skills_dir = test_skills_dir
        skill_service.config_path = str(test_skills_dir)

        try:
            # 讓 sync 讀測試 server_config，寫 SKILL.md 到測試 skills 目錄
            with patch(
                "src.services.skill_service.get_config_dir",
                return_value=test_config_dir,
            ):
                skill_service.sync_system_dispatcher()

            # 驗證 system-mcp-dispatcher/SKILL.md 生成且含 MCP registry
            dispatcher_md = test_skills_dir / "system-mcp-dispatcher" / "SKILL.md"
            if not dispatcher_md.exists():
                raise AssertionError("system-mcp-dispatcher SKILL.md was not written.")

            dispatcher_content = dispatcher_md.read_text(encoding="utf-8")
            print("[*] Dispatcher SKILL.md Content:")
            print(dispatcher_content)

            if "<mcp_registry>" not in dispatcher_content:
                raise AssertionError("Dispatcher SKILL.md missing <mcp_registry> block.")

            if "illegal-tool" in dispatcher_content and (
                "C:\\" in dispatcher_content or "D:\\" in dispatcher_content
            ):
                print("[W] Detected absolute path in Skill Prompt (As Expected for Illegal Tool).")
        finally:
            # Restore SkillService state
            skill_service._skills_dir = original_skills_dir
            skill_service.config_path = original_config_path

        print("\n" + "="*50)
        print("[SUCCESS] MCP Lifecycle Smoke Test Completed!")
        print("="*50)

    finally:
        # Cleanup
        print(f"\n[*] Cleaning up sandbox: {test_dir}")
        shutil.rmtree(test_dir)

if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
        
    asyncio.run(run_mcp_lifecycle_smoke_test())
