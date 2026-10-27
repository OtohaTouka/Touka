# ruff: noqa: E402, T201
"""Smoke Test for Memory Tool Integration (ResourceManager, Vault, Memory, Mission).

Verifies the real connectivity between ToukaProxy and ToukaMemory in Integrated Mode.
"""

import asyncio
import io
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

from lib.sandbox_env import enable_smoke_sandbox, run_cleanup_steps

enable_smoke_sandbox("memory_tools")

# 1. 設置路徑 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
# 注意：ToukaMemory 的 src 目錄需要被加入 sys.path
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

# 匯入核心組件
# 整合模式下的工具載入
from memory_server.factual import FactualStore
from memory_server.tools import memory_tool_registry
from memory_server.vector import VectorStore
from src.models.identity import AgentIdentity
from src.services.resource_manager import resource_manager


class _EmbeddingVector(list[float]):
    def tolist(self) -> list[float]:
        return list(self)


class _DeterministicSentenceTransformer:
    """Local embedding fake; never resolves or downloads a Hugging Face model."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_sentence_embedding_dimension(self) -> int:
        return 8

    def encode(self, text: str) -> _EmbeddingVector:
        seed = sum((index + 1) * ord(char) for index, char in enumerate(text))
        return _EmbeddingVector(float((seed + offset * 17) % 97 + 1) for offset in range(8))


@patch(
    "memory_server.vector.SentenceTransformer",
    new=_DeterministicSentenceTransformer,
)
async def run_memory_smoke_test():
    print("[*] Starting Memory & Tool Integration Smoke Test")

    # 使用 DebugAgent 進行測試 (Identity SSOT)
    identity = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger", capabilities=["*"])

    print(f"[*] Identity: {identity.agent_id} (Role: {identity.display_role})")

    # --- 1. ResourceManager 測試 ---
    print("\n[1] Testing ResourceManager...")
    personal_vault = resource_manager.get_vault_path("personal", identity)
    if not personal_vault:
        print("[!] Failed to resolve personal vault path.")
        sys.exit(1)
    print(f"[OK] Resolved Personal Vault: {personal_vault}")

    # --- 2. VaultTools 測試 ---
    print("\n[2] Testing Integrated VaultTools...")
    vault_tool = memory_tool_registry.get("vault_tools")
    if not vault_tool:
        print("[!] VaultTools not found in registry.")
        sys.exit(1)

    # 建立測試檔案 (使用 vault_tool 以符合 SSOT 法規)
    await vault_tool(
        vault="personal",
        action="write",
        path="smoke_test.txt",
        content="Hello from Smoke Test",
        identity=identity,
    )

    # 執行 list
    result = await vault_tool(vault="personal", action="list", identity=identity)
    print(f"[OK] Vault List Result: {result}")
    if "smoke_test.txt" not in str(result):
        print("[!] smoke_test.txt not found in list.")
        sys.exit(1)

    # --- 3. MemoryManager 測試 ---
    print("\n[3] Testing Integrated MemoryManager (Vector DB)...")
    memory_tool = memory_tool_registry.get("memory_tools")
    if not memory_tool:
        print("[!] memory_tools not found in registry.")
        sys.exit(1)

    # 儲存事實
    fact = "Touka system was optimized on 2026-05-03."
    save_result = await memory_tool(action="save", content=fact, identity=identity)
    print(f"[OK] Memory Save Result: {save_result}")
    fact_id_match = re.search(r"ID:\s*([^)]+)", str(save_result))
    if fact_id_match is None:
        print("[!] Memory save did not return a fact ID.")
        sys.exit(1)
    fact_id = fact_id_match.group(1)

    # 檢索事實
    query_result = await memory_tool(action="query", query="optimized date", identity=identity)
    print(f"[OK] Memory Query Result: {query_result}")
    if "2026-05-03" not in str(query_result):
        print("[!] Fact not retrieved correctly.")
        sys.exit(1)

    forget_result = await memory_tool(
        action="forget",
        fact_id_to_archive=fact_id,
        identity=identity,
    )
    print(f"[OK] Memory Forget Result: {forget_result}")
    if "has been archived" not in str(forget_result).lower():
        print("[!] Saved fact was not archived by forget.")
        sys.exit(1)
    after_forget = await memory_tool(
        action="query",
        query="optimized date",
        identity=identity,
    )
    if "2026-05-03" in str(after_forget):
        print("[!] Forgotten fact remained queryable.")
        sys.exit(1)

    # --- 4. MissionManager 測試 ---
    print("\n[4] Testing Integrated MissionManager (SQLite/JSON)...")
    mission_tool = memory_tool_registry.get("mission_tools")
    if not mission_tool:
        print("[!] mission_tools not found in registry.")
        sys.exit(1)

    # 實體建立任務
    mission_result = await mission_tool(
        action="init",
        goal="Smoke Test Goal",
        tasks=[{"description": "Subtask 1"}],
        identity=identity,
    )
    print(f"[OK] Mission Init Result: {mission_result}")

    # 讀取任務
    get_result = await mission_tool(action="get", identity=identity)
    print(f"[OK] Mission Get Result: {json.dumps(get_result, indent=2, ensure_ascii=False)}")

    print("\n" + "=" * 50)
    print("[SUCCESS] All Memory Integration Tests Passed!")
    print("=" * 50)


if __name__ == "__main__":
    # 強制使用 UTF-8 輸出 (Windows 環境)
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    try:
        asyncio.run(run_memory_smoke_test())
    finally:
        run_cleanup_steps(
            VectorStore.close_all_clients,
            FactualStore.close_all_connections,
        )
