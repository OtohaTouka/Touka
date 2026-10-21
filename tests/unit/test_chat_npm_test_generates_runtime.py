"""ToukaChat 的 `npm run test` 先產生 artifact runtime，再跑 Vitest（ADR-0250 C7）。

`src/generated/artifactRuntime.ts` 被 git 忽略，`npm ci --ignore-scripts` 也不會產生；
`ArtifactView.tsx` 動態匯入它，Vite 轉譯時就要解析這個路徑。新 clone 或新工作資料夾直接
跑 `npm run test` 會因為缺產生檔而假紅（4 個測試檔 `Failed to resolve import`）。CI 的
frontend job 在 `npm run test` 前另外跑 gen，本機沒有這一步。

本測試只讀文字檔，不執行 node、不載入專案程式。
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_JSON = PROJECT_ROOT / "ToukaChat" / "package.json"

BRACES_VERIFY = "npm run braces:verify"
GEN_RUNTIME = "npm run gen:artifact-runtime"


def _scripts() -> dict[str, str]:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]


def _test_steps() -> list[str]:
    return [step.strip() for step in _scripts()["test"].split("&&")]


def test_braces_verify_still_runs_first() -> None:
    steps = _test_steps()
    assert steps[0] == BRACES_VERIFY, steps


def test_artifact_runtime_is_generated_before_vitest() -> None:
    steps = _test_steps()
    vitest = [i for i, step in enumerate(steps) if step.split()[0] == "vitest"]
    assert len(vitest) == 1, steps
    assert GEN_RUNTIME in steps, steps
    assert steps.index(GEN_RUNTIME) < vitest[0], steps


def test_gen_step_runs_the_runtime_builder() -> None:
    gen = _scripts()["gen:artifact-runtime"]
    assert gen.split("&&")[-1].strip() == "node scripts/build-artifact-runtime.mjs", gen
