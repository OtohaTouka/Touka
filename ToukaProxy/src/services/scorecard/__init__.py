"""模型計分卡子系統（軌B）。

指定真實模型 → 沙箱／DebugAgent 下跑一組情境 → 量「結構正確性／token／成本／延遲」
→ 彙整每模型成績單。**手動觸發、不進 CI／guard_all／冒煙套件**；真實執行會花費 API 費用。

公開介面僅 engine 與 models（避免 __init__ 觸發 runner/cost_meter 的重匯入鏈）。
"""
from src.services.scorecard.engine import Budget, evaluate, run_scorecard
from src.services.scorecard.models import (
    RunOutcome,
    Scenario,
    ScenarioResult,
    Scorecard,
)

__all__ = [
    "Budget",
    "RunOutcome",
    "Scenario",
    "ScenarioResult",
    "Scorecard",
    "evaluate",
    "run_scorecard",
]
