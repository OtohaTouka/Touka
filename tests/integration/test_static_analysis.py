import subprocess
import sys
from pathlib import Path

import pytest
from src.config import settings

# 獲取專案根目錄 (SSOT)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
VIOLATIONS_DIR = PROJECT_ROOT / "violations"


@pytest.fixture(autouse=True)
def sandbox_violation_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generated negative fixtures in the existing pytest sandbox."""
    fixture_dir = tmp_path / "violations"
    fixture_dir.mkdir()
    monkeypatch.setitem(globals(), "VIOLATIONS_DIR", fixture_dir)
    monkeypatch.setenv("RUFF_CACHE_DIR", str(tmp_path / "ruff-cache"))


def run_script(script_path: Path, target_path: Path) -> subprocess.CompletedProcess[str]:
    """執行腳本並獲取輸出與 Exit Code"""
    env = settings.get_full_env()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(  # noqa: S603 - fixed repo script and sandbox fixture argv
        [sys.executable, str(script_path), str(target_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_ruff_banned_api():
    """測試 Ruff 是否攔截了被禁用的 API (時間 SSOT)"""
    target = VIOLATIONS_DIR / "time_violation.py"
    config = PROJECT_ROOT / "ruff.toml"

    # 確保 violations 目錄存在 (由測試環境準備)
    if not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("import time\nprint(time.time())\n", encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - fixed Ruff module and sandbox fixture argv
        [sys.executable, "-m", "ruff", "check", str(target), "--config", str(config)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # TID251 是 banned-api 的規則
    assert result.returncode != 0
    assert "TID251" in result.stdout


def test_ruff_child_writes_cache_only_in_owned_sandbox():
    """Run the actual Ruff child, then verify its cache is owned by this fixture."""
    cache = Path(settings.get_full_env()["RUFF_CACHE_DIR"])
    assert cache.is_relative_to(VIOLATIONS_DIR.parent)
    test_ruff_banned_api()
    assert cache.is_dir()


def test_ssot_path_violation():
    """測試自定義腳本是否偵測到路徑 SSOT 違規"""
    script = SCRIPTS_DIR / "lint_ssot.py"
    target = VIOLATIONS_DIR / "path_violation.py"

    if not target.exists():
        target.write_text('path = "D:/Touka/data/soul/alice/config.json"\n', encoding="utf-8")

    result = run_script(script, target)
    assert result.returncode != 0
    assert "PATH_LITERAL_SSOT" in result.stdout or "Hardcoded path detected" in result.stdout


def test_ssot_pac_violation():
    """測試自定義腳本是否偵測到 PaC 邏輯違規"""
    script = SCRIPTS_DIR / "lint_ssot.py"
    target = VIOLATIONS_DIR / "pac_violation.py"

    if not target.exists():
        target.write_text('if context.get("auth_level") == "FullYOLO": pass\n', encoding="utf-8")

    result = run_script(script, target)
    assert result.returncode != 0
    assert (
        "PAC_MANUAL_LEVEL_CHECK" in result.stdout
        or "Manual auth_level comparison detected" in result.stdout
    )


def test_clean_code_passes():
    """測試正常代碼是否能通過檢查 (以 src/utils/time_utils.py 為例)"""
    script = SCRIPTS_DIR / "lint_ssot.py"
    target = PROXY_DIR / "src" / "utils" / "time_utils.py"

    result = run_script(script, target)
    assert result.returncode == 0, result.stdout


def test_ssot_time_violation():
    """測試是否偵測到時間 SSOT 違規 (直接調用 datetime.now)"""
    script = SCRIPTS_DIR / "lint_ssot.py"
    target = VIOLATIONS_DIR / "time_call_violation.py"

    if not target.exists():
        target.write_text("from datetime import datetime\nnow = datetime.now()\n", encoding="utf-8")

    result = run_script(script, target)
    assert result.returncode != 0
    assert "TIME_SSOT_VIOLATION" in result.stdout


def test_ssot_env_violation():
    """測試是否偵測到環境變數 SSOT 違規 (直接調用 os.getenv)"""
    script = SCRIPTS_DIR / "lint_ssot.py"
    target = VIOLATIONS_DIR / "env_call_violation.py"

    if not target.exists():
        target.write_text('import os\nkey = os.getenv("API_KEY")\n', encoding="utf-8")

    result = run_script(script, target)
    assert result.returncode != 0
    assert "ENV_SSOT_VIOLATION" in result.stdout
