"""伺服器生命週期整合測試 (Server Lifecycle Integration Tests).

確保 FastAPI/Uvicorn 在啟動與關閉的過程中，
能夠優雅地處理背景任務與 AnyIO/MCP 資源，不拋出 RuntimeError 等異常。
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def test_proxy_graceful_shutdown_no_runtime_errors():
    """啟動 ToukaProxy 並觸發關閉，斷言無 RuntimeError."""
    project_root = Path(__file__).parent.parent.parent.resolve()
    proxy_dir = project_root / "ToukaProxy"
    
    # 複製環境變數，只改 Port
    env = os.environ.copy()
    env["PORT"] = "8011" # 使用不同的 Port 避免衝突
    
    # 設置正確的 PYTHONPATH 支援整合模式
    memory_src = project_root / "ToukaMemory" / "src"
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{proxy_dir}{os.pathsep}{memory_src}" + (f"{os.pathsep}{existing_pythonpath}" if existing_pythonpath else "")  # noqa: E501
    
    kwargs = {
        "cwd": proxy_dir,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "ignore"
    }
    if os.name == 'nt':
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "uvicorn", "src.main:app", "--port", "8011"],
        **kwargs
    )
    
    try:
        # 等待伺服器啟動並初始化 MCP
        # 通常 Uvicorn 啟動並初始化 ProtocolManager 需要幾秒鐘
        time.sleep(8)
        
        # 傳送中斷訊號以觸發 graceful shutdown
        if os.name == 'nt':
            # Windows 上的 CTRL_C_EVENT 需要使用特定方式傳遞
            # 這裡我們使用 taskkill 送出終止訊號，或者直接呼叫 process.terminate()
            # 但 Popen.terminate() 在 Windows 是強制殺死，不觸發 finally。
            # 因此，在 Windows 我們發送 CTRL_C_EVENT (需要在建立 Popen 時加上 creationflags)
            # 為了測試穩定性，我們直接檢查 stdout/stderr 是否有例外
            pass
        
    finally:
        # 在不同平台有不同的優雅關閉方式
        if os.name == 'nt':
            # 在 Windows 上，我們呼叫 terminate 會被強制中止。
            # 但我們可以透過寫一個小的測試路由來從內部觸發 os._exit(0) 或引發 KeyboardInterrupt
            # 為了簡單起見，我們發送信號 (如果可用)
            try:
                os.kill(process.pid, signal.CTRL_C_EVENT)
            except Exception:
                process.terminate()
        else:
            process.send_signal(signal.SIGINT)
            
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            stdout, stderr = process.communicate()
            
        # 確保 stderr 和 stdout 是字串
        stdout = stdout or ""
        stderr = stderr or ""
            
        # 將輸出印出以便除錯
        print(f"STDOUT:\n{stdout}")  # noqa: T201
        print(f"STDERR:\n{stderr}")  # noqa: T201
        
        # 關鍵斷言：不應該出現 Attempted to exit a cancel scope 這種 AnyIO 錯誤
        assert "RuntimeError: Attempted to exit a cancel scope" not in stderr, "MCP Shutdown Cancel Scope Error found!"  # noqa: E501
        assert "RuntimeError: Attempted to exit cancel scope in a different task" not in stderr, "MCP Shutdown Task Error found!"  # noqa: E501

