# ruff: noqa: T201, S603
"""Module Boundary Guard — 防止 ToukaMemory 逆向依賴 ToukaProxy（禁令 6，ADR-0022）。

使用 import-linter 驗證 ToukaMemory 不直接 import ToukaProxy 的內部模組。
設定檔：.importlinter（專案根目錄）
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TOUKA_PROXY_SRC = PROJECT_ROOT / "ToukaProxy"
TOUKA_MEMORY_SRC = PROJECT_ROOT / "ToukaMemory" / "src"


def main() -> None:
    """Guard 執行入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("🔍 Module Boundary Guard (import-linter) 正在啟動...")

    # 設定 PYTHONPATH：讓 memory_server 與 src 都能被找到
    sep = ";" if os.name == "nt" else ":"
    python_path = sep.join([str(TOUKA_PROXY_SRC), str(TOUKA_MEMORY_SRC)])

    env = {**os.environ, "PYTHONPATH": python_path}

    result = subprocess.run(
        ["lint-imports", "--config", ".importlinter", "--no-cache"],  # noqa: S607
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

    if result.returncode == 0:
        print("✅ 模組邊界守衛通過：ToukaMemory 未逆向依賴 ToukaProxy。")
        sys.exit(0)
    else:
        print("❌ 偵測到模組邊界違規！請檢查上方報告。")
        sys.exit(1)


if __name__ == "__main__":
    main()
