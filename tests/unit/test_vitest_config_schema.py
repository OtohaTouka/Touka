"""ADR-0058: vitest 4.x poolOptions → maxWorkers 遷移 — 結構性測試

Red → Green 確認：
- vitest.config.ts 不得含已移除的 poolOptions 屬性
- vitest.config.ts 必須使用頂層 maxWorkers（Vitest 4.x API）
- tsc --noEmit 對 vitest.config.ts 零型別錯誤
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
VITEST_CONFIG = ROOT / "ToukaChat" / "vitest.config.ts"
FRONTEND_DIR = ROOT / "ToukaChat"


def test_vitest_config_no_poolOptions():
    """vitest.config.ts 不得使用 Vitest 4.x 已移除的 poolOptions 屬性"""
    content = VITEST_CONFIG.read_text(encoding="utf-8")
    assert "poolOptions" not in content, (
        "vitest.config.ts 仍含有已在 Vitest 4.x 中移除的 `poolOptions` 屬性，"
        "請改用頂層 `maxWorkers`"
    )


def test_vitest_config_uses_maxWorkers():
    """vitest.config.ts 必須使用 Vitest 4.x 的 maxWorkers 屬性"""
    content = VITEST_CONFIG.read_text(encoding="utf-8")
    assert "maxWorkers" in content, (
        "vitest.config.ts 缺少 `maxWorkers` 屬性（Vitest 4.x 替代方案）"
    )


def test_vitest_config_tsc_type_check_passes():
    """vitest.config.ts 必須通過 TypeScript 型別檢查，防止 Docker 建置失敗"""
    result = subprocess.run(  # noqa: S602
        "npx tsc --noEmit",  # noqa: S607
        capture_output=True,
        text=True,
        cwd=str(FRONTEND_DIR),
        timeout=120,
        shell=True,
    )
    output = result.stdout + result.stderr
    vitest_errors = [
        line for line in output.splitlines()
        if "vitest.config.ts" in line and "error TS" in line
    ]
    assert len(vitest_errors) == 0, (
        "vitest.config.ts 仍有 TypeScript 型別錯誤：\n" + "\n".join(vitest_errors)
    )
