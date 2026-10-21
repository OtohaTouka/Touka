# ruff: noqa: T201, S603, S607
"""Ruff Banned-API Guard — 第 44 道守衛 (Guard #44).

早期 ADR 守衛缺口 E09（家族①「ruff 宣告卻零執法」）：`ruff.toml` 的
`[lint.flake8-tidy-imports.banned-api]`（規則碼 **TID251**）以 SSOT 形式宣告了
時間／路徑／配置／bootstrap 的禁用 API（`datetime.now`／`time.time`／`os.getcwd`／
`os.path.abspath`／`os.path.realpath`／`os.environ`／`os.getenv`／`sys.path.insert`／
`src.config.get_app_data_dir` 等），但 **ruff 從未被 guard_all 或 CI 執行**——刪任何
banned-api 條目、或新代碼引入被禁呼叫，皆零守衛變紅。

其中 `os.getcwd`／`os.path.abspath`／`os.path.realpath`／`sys.path.insert`／
`get_app_data_dir` 甚至連 `lint_ssot.py`（Guard #1）也不涵蓋（見 E09/ADR-0004/0017），
故此守衛是它們在真實源碼樹上的**唯一**機器執法。

本守衛對真實 Python 源碼樹（ToukaProxy／ToukaMemory／ToukaHeartbeat／ToukaDispatch／
scripts）跑 `ruff check --select TID251`，尊重 `ruff.toml` 的 per-file-ignores
（bootstrapper／基建合法豁免），任一違規即非零退出。

⚠️ 刻意不掃 repo 根的 `violations/`——那是 `tests/integration/test_static_analysis.py`
動態生成的合成違規 fixture（`test_ruff_banned_api` 靠 ruff flag 它），非真實代碼。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
RUFF_CONFIG = PROJECT_ROOT / "ruff.toml"

# 掃描的真實 Python 源碼樹（天然排除 repo 根的 violations/ 合成 fixture、docs/、site/）
SCAN_DIRS = [
    "ToukaProxy",
    "ToukaMemory",
    "ToukaHeartbeat",
    "ToukaDispatch",
    "scripts",
]

# banned-api 規則碼（flake8-tidy-imports.banned-api SSOT）
BANNED_API_RULE = "TID251"


def run_guard(project_root: Path | None = None) -> tuple[bool, str]:
    """對源碼樹跑 ruff TID251。回傳 (是否通過, 輸出文字)。

    Args:
        project_root: 掃描根（測試可注入合成樹）；banned-api SSOT 一律用專案
            真實 ``ruff.toml``（``RUFF_CONFIG``），確保 per-file-ignores 一致。
    """
    root = project_root or PROJECT_ROOT
    targets = [str(root / d) for d in SCAN_DIRS if (root / d).is_dir()]
    if not targets:
        return False, f"找不到任何掃描目標目錄於 {root}"

    result = subprocess.run(
        [
            "ruff", "check", *targets,
            "--select", BANNED_API_RULE,
            "--config", str(RUFF_CONFIG),
            "--output-format", "concise",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    """守衛執行入口。"""
    if sys.stdout.encoding != "utf-8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("Ruff Banned-API Guard (Guard #44) — TID251 SSOT 執法")
    print("=" * 60)

    ok, output = run_guard()

    if ok:
        print(
            "[OK] 真實源碼樹無 banned-api（TID251）違規"
            "（時間／路徑／配置／bootstrap SSOT 全數受執法）。"
        )
        return 0

    print("[FAIL] 偵測到 banned-api（TID251）違規：\n")
    print(output.strip())
    print(
        "\n修復：改用 SSOT 替代（時間→src.utils.time_utils、路徑→resource_manager、"
        "配置→src.config、子程序→src.tools.terminal）。\n"
        "若為合法基建豁免，於 ruff.toml 的 [lint.per-file-ignores] 補該檔並附理由"
        "（比照 config.py／terminal.py 等既有條目）。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
