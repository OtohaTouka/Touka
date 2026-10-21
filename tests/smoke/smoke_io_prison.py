# ruff: noqa: T201, TRY003

"""Smoke Test for IO Prison & Path Sovereignty.

Verifies that Touka's sandbox boundaries are strictly enforced against:
1. Path Traversal (../)
2. Identity Confusion (Cross-Agent access)
3. Soul/Legacy Protection (Prohibiting automated edits to core files)
4. Absolute Path Injection
"""
import asyncio
import sys
from pathlib import Path, PureWindowsPath
from string import ascii_uppercase

from lib.sandbox_env import enable_smoke_sandbox

enable_smoke_sandbox("io_prison")

# 1. Path Setup (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
MEMORY_SRC_DIR = PROJECT_ROOT / "ToukaMemory" / "src"

if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))
if str(MEMORY_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_SRC_DIR))

# Import core components
from memory_server.tools.core_file_ops import (  # noqa: E402
    PathGuardError,
    _enforce_root_bound,
)
from src.models.identity import AgentIdentity  # noqa: E402
from src.services.resource_manager import resource_manager  # noqa: E402


async def run_io_prison_smoke_test():
    print("[*] Starting IO Prison & Path Sovereignty Smoke Test")
    
    # 建立兩個測試身分
    alice = AgentIdentity.for_role(agent_id="Alice", role="Base")
    bob = AgentIdentity.for_role(agent_id="Bob", role="Base")
    
    print(f"[*] Identities Created: {alice.agent_id}, {bob.agent_id}")

    # --- 1. 身分監獄隔離測試 (Identity Sandbox) ---
    print("\n[1] Testing Identity Sandbox Isolation...")
    alice_personal = resource_manager.get_vault_path("personal", alice)
    bob_personal = resource_manager.get_vault_path("personal", bob)
    
    print(f"[*] Alice Vault: {alice_personal}")
    print(f"[*] Bob Vault: {bob_personal}")
    
    if alice_personal == bob_personal:
        print("[FAIL] Identity isolation failed! Both agents share the same path.")
        sys.exit(1)
    if "Alice" not in str(alice_personal) or "Bob" not in str(bob_personal):
        print("[FAIL] Vault path does not include Agent ID.")
        sys.exit(1)
    print("[OK] Identity sandbox isolation verified.")

    # --- 2. 路徑穿越防禦測試 (Path Traversal) ---
    print("\n[2] Testing Path Traversal Defense...")
    root_bound = alice_personal
    malicious_paths = [
        "../",
        "../../etc/passwd",
        "personal/../../../Windows/System32",
        "drive/../../secret.txt"
    ]
    
    for p in malicious_paths:
        try:
            _enforce_root_bound(p, root_bound)
            print(f"[FAIL] Path traversal escape NOT detected for: {p}")
            sys.exit(1)
        except PathGuardError as e:
            print(f"[OK] Traversal blocked for '{p}': {e}")

    # --- 3. 靈魂核心禁區保護測試 (Soul Protection) ---
    print("\n[3] Testing Soul Protection Enforcement...")
    # 嘗試直接透過 ResourceManager 修改受保護目錄 (soul/legacy)
    # 這裡我們模擬一個路徑指向 Alice 的靈魂目錄
    soul_file = Path(resource_manager.get_base_path()) / "soul" / "Alice" / "config.json"
    
    try:
        resource_manager.write_file_text(soul_file, "{}")
        print("[FAIL] Soul protection failed! Direct edit allowed.")
        sys.exit(1)
    except PermissionError as e:
        print(f"[OK] Soul protection blocked edit: {e}")

    # --- 4. 絕對路徑注入測試 (Absolute Path Injection) ---
    print("\n[4] Testing Absolute Path Injection...")
    # 在 Windows 下測試磁碟代號注入，在 Unix 下測試 /tmp 注入
    abs_path = "C:\\Windows\\win.ini" if sys.platform == "win32" else "/etc/shadow"
    
    try:
        _enforce_root_bound(abs_path, root_bound)
        print(f"[FAIL] Absolute path injection NOT detected for: {abs_path}")
        sys.exit(1)
    except PathGuardError as e:
        print(f"[OK] Absolute path blocked: {e}")

    # --- 5. 跨磁碟逃逸測試 (Cross-Drive) ---
    if sys.platform == "win32":
        print("\n[5] Testing Cross-Drive Escape (Windows Only)...")
        # 使用已解析的 sandbox vault 作邊界，合成另一磁碟的逃逸目標。
        root_drive = PureWindowsPath(root_bound).drive.casefold()
        escape_drive = next(
            drive for drive in ascii_uppercase if f"{drive}:".casefold() != root_drive
        )
        escape_path = f"{escape_drive}:\\some_file.txt"
        try:
            _enforce_root_bound(escape_path, root_bound)
            print("[FAIL] Cross-drive escape NOT detected.")
            sys.exit(1)
        except PathGuardError as e:
            print(f"[OK] Cross-drive blocked: {e}")

    print("\n" + "=" * 50)
    print("[SUCCESS] IO Prison & Path Sovereignty Tests Passed!")
    print("=" * 50)


if __name__ == "__main__":
    # 強制使用 UTF-8 輸出 (Windows 環境)
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
        
    asyncio.run(run_io_prison_smoke_test())
