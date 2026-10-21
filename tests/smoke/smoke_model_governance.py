"""Smoke Test for Model Governance & Discovery (ModelManager Logic).

Verifies model discovery, registry merging, blacklist enforcement, and agent unification.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("model_governance")

# 1. 設置路徑 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

from src.services.models.manager import ModelManager  # noqa: E402
from src.utils.time_utils import get_current_epoch  # noqa: E402


async def run_model_governance_smoke_test():
    """執行模型治理與發現 Service 邏輯冒煙測試。"""
    print("[*] Starting Model Governance & Discovery Smoke Test")  # noqa: T201
    
    # 建立記憶體沙盒配置，確保不污染實體 models.json
    in_memory_config = {"models": []}

    def mock_read_config(filename: str):
        if filename == "models.json":
            return in_memory_config
        return None

    def mock_save_config(filename: str, data: dict):
        nonlocal in_memory_config
        if filename == "models.json":
            in_memory_config = data

    with patch("src.services.models.manager.config_utils.read_config_json", side_effect=mock_read_config), \
         patch("src.services.models.manager.config_utils.save_config_json", side_effect=mock_save_config):  # noqa: E501
         
        # 初始化獨立的 ModelManager 實例以避免污染全域單例
        manager = ModelManager()
        
        # --- 1. Blacklist 測試 ---
        print("\n[1] Testing Blacklist Enforcement...")  # noqa: T201
        # 假設黑名單中有 "vision" (依據系統慣例)
        manager._blacklist = ["vision", "test-hidden"]
        
        is_blocked = manager.is_blacklisted("gpt-4-vision-preview")
        print(f"[OK] Vision model blocked: {is_blocked}")  # noqa: T201
        if not is_blocked:
            print("[!] Blacklist logic failure.")  # noqa: T201
            sys.exit(1)
            
        is_allowed = manager.is_blacklisted("gpt-4o")
        print(f"[OK] Normal model allowed: {not is_allowed}")  # noqa: T201
        if is_allowed:
            print("[!] Blacklist false positive.")  # noqa: T201
            sys.exit(1)

        # --- 2. Registry Merging 測試 ---
        print("\n[2] Testing Registry Merging (New vs Existing)...")  # noqa: T201
        
        initial_model = {"id": "m1", "name": "Model 1", "enabled": False}
        manager._models_cache = [initial_model]
        
        new_discovery = [
            {"id": "m1", "name": "Model 1 Updated"}, # 既有模型
            {"id": "m2", "name": "Model 2"},         # 新模型
            {"id": "test-hidden-1", "name": "Hidden"} # 黑名單模型
        ]
        
        manager._merge_models(new_discovery)
        
        ids = [m["id"] for m in manager._models_cache]
        print(f"[OK] Merged Registry IDs: {ids}")  # noqa: T201
        
        # 驗證既有模型保留了 enabled=False
        m1 = next(m for m in manager._models_cache if m["id"] == "m1")
        if m1.get("enabled") is not False:
            print("[!] Existing model settings overwritten.")  # noqa: T201
            sys.exit(1)
        print("[OK] Existing model settings (enabled=False) preserved.")  # noqa: T201
        
        # 驗證黑名單被過濾
        if "test-hidden-1" in ids:
            print("[!] Blacklisted model leaked into registry.")  # noqa: T201
            sys.exit(1)
        print("[OK] Blacklisted models correctly filtered during merge.")  # noqa: T201

        # --- 3. Unified Registry (Agents + Base) 測試 ---
        print("\n[3] Testing Unified Registry (Agent Integration)...")  # noqa: T201
        
        # get_models 會調用 agent_service.get_all_agents()
        # 我們檢查輸出的結構是否正確包含 is_agent 標記
        all_models = manager.get_models()
        agent_models = [m for m in all_models if m.get("is_agent")]
        base_models = [m for m in all_models if not m.get("is_agent")]
        
        print(f"[OK] Total Models in Unified Registry: {len(all_models)}")  # noqa: T201
        print(f"     Base Models: {len(base_models)}")  # noqa: T201
        print(f"     Agent Models: {len(agent_models)}")  # noqa: T201
        
        if len(all_models) == 0:
            print("[!] Unified registry is empty.")  # noqa: T201
            sys.exit(1)
            
        # 驗證 Agent 屬性
        if agent_models:
            sample_agent = agent_models[0]
            if "owned_by" not in sample_agent or "status" not in sample_agent:
                print("[!] Agent model structure missing core fields.")  # noqa: T201
                sys.exit(1)
            print(f"[OK] Agent model structure verified: {sample_agent['id']}")  # noqa: T201

        # --- 4. Status Logic (Online/Offline) 測試 ---
        print("\n[4] Testing Status & Health Logic...")  # noqa: T201
        
        # 模擬一個很久沒見的模型 (超過一週)
        old_ts = get_current_epoch() - (86400 * 8)
        
        manager._models_cache = [
            {"id": "online-model", "last_seen": get_current_epoch(), "owned_by": "custom"},
            {"id": "offline-model", "last_seen": old_ts, "owned_by": "custom"}
        ]
        
        final_list = manager.get_models()
        m_online = next(m for m in final_list if m["id"] == "online-model")
        m_offline = next(m for m in final_list if m["id"] == "offline-model")
        
        print(f"[OK] Model 'online-model' status: {m_online['status']}")  # noqa: T201
        print(f"[OK] Model 'offline-model' status: {m_offline['status']}")  # noqa: T201
        
        if m_online["status"] != "online" or m_offline["status"] != "offline":
            print("[!] Status determination logic failure.")  # noqa: T201
            sys.exit(1)

        print("\n" + "="*50)  # noqa: T201
        print("[SUCCESS] All Model Governance Service Tests Passed!")  # noqa: T201
        print("="*50)  # noqa: T201


if __name__ == "__main__":
    asyncio.run(run_model_governance_smoke_test())
