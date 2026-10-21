"""微服務隔離測試 (Module Isolation Tests).

確保不同子專案的邊界清晰，不會跨越微服務的物理邊界匯入模組。
"""
import os
import subprocess
import sys
from pathlib import Path


def test_heartbeat_isolation_from_proxy():
    """確保 ToukaHeartbeat 無法匯入 ToukaProxy 的 src 模組."""
    project_root = Path(__file__).parent.parent.parent.resolve()
    heartbeat_dir = project_root / "ToukaHeartbeat"
    
    # 建立一個測試腳本，嘗試匯入 Proxy 的例外模組
    script = (
        "import sys\n"
        "try:\n"
        "    from src.core.exceptions import ToukaException\n"
        "    sys.exit(1)  # 如果匯入成功，代表隔離失敗！\n"
        "except ModuleNotFoundError:\n"
        "    sys.exit(0)  # 找不到模組才是正確的行為\n"
    )
    
    env = os.environ.copy()
    # 嚴格清除 PYTHONPATH，模擬純淨的 Docker 啟動環境
    env.pop("PYTHONPATH", None)
    
    res = subprocess.run(
        [sys.executable, "-c", script], 
        cwd=heartbeat_dir, 
        env=env,
        capture_output=True,
        text=True
    )
    
    assert res.returncode == 0, f"Isolation violated! Heartbeat can access Proxy src. Stderr: {res.stderr}"
