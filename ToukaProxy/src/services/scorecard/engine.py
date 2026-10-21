"""計分卡引擎：預算護欄、結構驗證、編排（純函式，runner 注入 → 可零 API 花費測試）。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src.services.scorecard.models import (
    RunOutcome,
    Scenario,
    ScenarioResult,
    Scorecard,
)

logger = logging.getLogger(__name__)

Runner = Callable[[str, Scenario], Awaitable[RunOutcome]]
ProgressCb = Callable[[dict], None]

# 「環境/供應商不可用」標記（小寫比對）：金鑰未設、連線失敗、4xx、模型不存在等——歸為
# not-testable（略過，非品質失敗），與真正的內部錯誤（Traceback）區分。
_NOT_TESTABLE_MARKERS = (
    "provider/env error", "連線", "access denied", "missing `", "api key",
    "api error (4", "http 4", "no stream body", "not found", "not_found",
    "unknown_token", "auth", "connect",
)


def _is_not_testable(error: str) -> bool:
    """錯誤是否屬「本環境不可測」（供應商/金鑰/連線/4xx）而非模型品質問題。"""
    e = (error or "").lower()
    return any(m in e for m in _NOT_TESTABLE_MARKERS)


def _tool_called(expect: str, called: tuple[str, ...]) -> bool:
    """期待的工具有無被呼叫——相容 MCP 前綴命名（`{server}__{tool}`）。

    內建/記憶工具精確比對；MCP 工具（如 `mcp-docgen__create_xlsx`）以 `__create_xlsx`
    後綴比對，避免綁死 server key。
    """
    return any(c == expect or c.endswith(f"__{expect}") for c in called)


class Budget:
    """單輪累計預算護欄。

    誠實註記（completeness-2）：上限為「**累計、回合間**」檢查——每個情境**開始前**檢查
    累計花費是否已達上限。串流式回應只能在回合**結束後**得知成本，故**一個進行中情境可能
    小幅超支**；預設預算須含此 headroom。本護欄保證「超支後不再開新情境」，而非「絕不超過
    一分一毫」。
    """

    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.spent_usd = 0.0

    def exceeded(self) -> bool:
        """累計花費已達上限（cap<=0 視為無上限）。"""
        return self.cap_usd > 0 and self.spent_usd >= self.cap_usd

    def add(self, cost_usd: float) -> None:
        """累加一個情境的花費（負值視為 0）。"""
        self.spent_usd += max(0.0, cost_usd)


def evaluate(scenario: Scenario, outcome: RunOutcome) -> ScenarioResult:
    """結構驗證：看「有沒有壞」（無錯誤標記、該有的回應在），不做逐字比對。"""
    if outcome.skipped:
        return ScenarioResult(
            scenario_id=scenario.id,
            surface=scenario.surface,
            structural_pass=False,
            detail=f"skipped: {outcome.skip_reason}",
            skipped=True,
        )
    failures: list[str] = []
    if outcome.error:
        failures.append(f"error: {outcome.error}")
    if scenario.expect_text and not (outcome.text or "").strip():
        failures.append("expected non-empty text response")
    if scenario.expect_tool and not _tool_called(scenario.expect_tool, outcome.tools_called):
        failures.append(
            f"expected tool '{scenario.expect_tool}' not called "
            f"(called: {list(outcome.tools_called)})"
        )
    # Batch 5 成品維度：產檔情境必須產出合法且含指定內容的檔案（artifact_valid 由 runner 設）。
    if scenario.artifact_kind and outcome.artifact_valid is not True:
        failures.append(f"artifact 驗證失敗: {outcome.artifact_detail or 'not verified'}")
    ok = not failures
    return ScenarioResult(
        scenario_id=scenario.id,
        surface=scenario.surface,
        structural_pass=ok,
        detail="ok" if ok else "; ".join(failures),
        tokens=outcome.tokens,
        cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms,
        estimated=outcome.estimated,
        tool_trace=outcome.tool_trace,
        react_turns=outcome.react_turns,
        finish_reason=outcome.finish_reason,
        early_stop=outcome.early_stop,
        artifact_valid=outcome.artifact_valid,
        artifact_detail=outcome.artifact_detail,
    )


async def run_scorecard(
    model: str,
    budget_usd: float,
    scenarios: tuple[Scenario, ...],
    runner: Runner,
    *,
    on_progress: ProgressCb | None = None,
) -> Scorecard:
    """逐情境執行 → 結構驗證 → 增量計費；回合間檢查預算上限（超支後略過其餘情境）。"""
    budget = Budget(budget_usd)
    results: list[ScenarioResult] = []
    total = len(scenarios)
    for i, sc in enumerate(scenarios):
        if budget.exceeded():
            results.append(
                ScenarioResult(
                    scenario_id=sc.id,
                    surface=sc.surface,
                    structural_pass=False,
                    detail="skipped: budget_exhausted",
                    skipped=True,
                )
            )
            if on_progress:
                on_progress({
                    "type": "progress", "index": i + 1, "total": total,
                    "scenario": sc.id, "status": "skipped",
                    "spent_usd": round(budget.spent_usd, 6),
                })
            continue
        try:
            # wall-clock 上限（completeness-5）：防 ask_user 等待或失控迴圈卡死整輪。
            outcome = await asyncio.wait_for(runner(model, sc), timeout=sc.timeout_s)
        except TimeoutError:
            results.append(ScenarioResult(
                scenario_id=sc.id, surface=sc.surface, structural_pass=False,
                detail=f"timeout after {sc.timeout_s:.0f}s",
            ))
            if on_progress:
                on_progress({
                    "type": "progress", "index": i + 1, "total": total,
                    "scenario": sc.id, "status": "fail",
                    "spent_usd": round(budget.spent_usd, 6),
                })
            continue
        except Exception as exc:  # 單一情境失敗不應中斷整輪
            logger.warning("scorecard scenario %s runner error: %s", sc.id, exc)
            outcome = RunOutcome(error=str(exc))
        # 環境/供應商不可用（金鑰/連線/4xx）→ 略過（不算品質失敗）。
        if outcome.error and _is_not_testable(outcome.error):
            results.append(ScenarioResult(
                scenario_id=sc.id, surface=sc.surface, structural_pass=False,
                detail=f"not_testable: {outcome.error[:120]}", skipped=True,
            ))
            if on_progress:
                on_progress({
                    "type": "progress", "index": i + 1, "total": total,
                    "scenario": sc.id, "status": "skipped",
                    "spent_usd": round(budget.spent_usd, 6),
                })
            continue
        sr = evaluate(sc, outcome)
        budget.add(sr.cost_usd)
        results.append(sr)
        if on_progress:
            on_progress({
                "type": "progress", "index": i + 1, "total": total,
                "scenario": sc.id,
                "status": "pass" if sr.structural_pass else "fail",
                "cost_usd": round(sr.cost_usd, 6),
                "spent_usd": round(budget.spent_usd, 6),
            })
    return Scorecard(
        model=model,
        budget_usd=budget_usd,
        spent_usd=round(budget.spent_usd, 6),
        results=results,
    )
