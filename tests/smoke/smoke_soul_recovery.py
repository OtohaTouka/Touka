# ruff: noqa: T201, TRY003
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("soul_recovery")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

# Import core components
from src.core.bootstrap import ensure_system_bootstrap  # noqa: E402
from src.utils.bootstrap import ensure_configs  # noqa: E402


async def run_soul_recovery_smoke_test():
    print("[*] Starting Soul Recovery (Self-Healing) Smoke Test")

    with tempfile.TemporaryDirectory(prefix="touka_recovery_test_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        
        # 1. 核心配置檔案復原測試 (Global Policy Recovery)
        print("\n[1] Testing Global Policy Recovery (permissions.json)...")
        perm_file = config_dir / "permissions.json"
        perm_example = config_dir / "permissions.json.example"
        
        # 建立範本
        example_data = {"roles": {"Owner": {"allowed_tools": ["*"]}}}
        with open(perm_example, "w", encoding="utf-8") as f:
            json.dump(example_data, f)
        
        if perm_file.exists():
            os.remove(perm_file)
            
        # 執行自癒 (Patching CONFIG_DIR to use our temp dir)
        with patch("src.utils.bootstrap.CONFIG_DIR", str(config_dir)):
            restored = ensure_configs()
            
            if "permissions.json" not in restored:
                print("[FAIL] permissions.json was not reported as restored.")
                sys.exit(1)
            
            if not perm_file.exists():
                print("[FAIL] permissions.json was not created on disk.")
                sys.exit(1)
            
            with open(perm_file, encoding="utf-8") as f:
                recovered_data = json.load(f)
                if recovered_data != example_data:
                    print(f"[FAIL] Recovered data mismatch. Got: {recovered_data}")
                    sys.exit(1)
        print("[OK] Global policy recovery passed.")

        # 2. 引導安全性驗證 (No Overwrite)
        print("\n[2] Testing Bootstrap Safety (No Overwrite)...")
        original_content = {"roles": {"Owner": {"allowed_tools": ["custom"]}}}
        with open(perm_file, "w", encoding="utf-8") as f:
            json.dump(original_content, f)
        
        # 再次執行自癒，不應覆蓋
        with patch("src.utils.bootstrap.CONFIG_DIR", str(config_dir)):
            ensure_configs()
            
            with open(perm_file, encoding="utf-8") as f:
                current_data = json.load(f)
                if current_data != original_content:
                    print("[FAIL] Bootstrap overwritten existing data!")
                    sys.exit(1)
        print("[OK] Bootstrap safety (no overwrite) passed.")

        # 3. 預設結構建立測試 (Default JSON Structure)
        print("\n[3] Testing Default JSON Structure (No template)...")
        # autonomy.json 在 ESSENTIAL_CONFIGS 中且測試目錄內無 .example → 應建立預設 {}
        # (skills.json 已遷移為目錄式 SKILL.md，不再屬於 bootstrap 範疇 — ADR-0066)
        default_file = config_dir / "autonomy.json"
        if default_file.exists():
            os.remove(default_file)

        # 執行自癒
        with patch("src.utils.bootstrap.CONFIG_DIR", str(config_dir)):
            ensure_configs()

            if not default_file.exists():
                print("[FAIL] autonomy.json was not created as default.")
                sys.exit(1)

            with open(default_file, encoding="utf-8") as f:
                data = json.load(f)
                if data != {}:
                    print(f"[FAIL] Default structure is not empty dict. Got: {data}")
                    sys.exit(1)
        print("[OK] Default JSON structure creation passed.")

    print("\n" + "="*50)
    print("[SUCCESS] Soul Recovery Smoke Tests Passed!")
    print("="*50)


async def run_system_bootstrap_smoke_test():
    print("\n[*] Starting System Bootstrap (Root level) Smoke Test")

    with tempfile.TemporaryDirectory(prefix="touka_system_bootstrap_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # 建立 Proxy/config 目錄
        proxy_config_dir = tmp_path / "ToukaProxy" / "config"
        proxy_config_dir.mkdir(parents=True)
        
        # 1. .env 復原測試
        print("\n[1] Testing .env Restoration...")
        env_example = tmp_path / ".env.example"
        env_file = tmp_path / ".env"
        env_example.write_text("DEBUG=true")
        
        ensure_system_bootstrap(tmp_path)
        
        if not env_file.exists() or env_file.read_text() != "DEBUG=true":
            print("[FAIL] .env was not restored correctly.")
            sys.exit(1)
        print("[OK] .env restoration passed.")
        
        # 2. Proxy config 範本復原測試
        print("\n[2] Testing Proxy Config Template Restoration...")
        conf_example = proxy_config_dir / "test.json.example"
        conf_file = proxy_config_dir / "test.json"
        conf_example.write_text("{}")
        
        ensure_system_bootstrap(tmp_path)
        
        if not conf_file.exists():
            print("[FAIL] Proxy config test.json was not restored.")
            sys.exit(1)
        print("[OK] Proxy config template restoration passed.")

    print("[SUCCESS] System Bootstrap Smoke Tests Passed!")


if __name__ == "__main__":
    # 強制使用 UTF-8 輸出
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    async def main():
        await run_soul_recovery_smoke_test()
        await run_system_bootstrap_smoke_test()

    asyncio.run(main())
