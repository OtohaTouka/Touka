"""模型計分卡資料模型（軌B 批次1）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    """單一計分情境（宣告式 SSOT）。"""

    id: str
    surface: str  # "chat" | "tool" | "react"
    prompt: str = ""
    expect_text: bool = True  # 期待非空文字回應
    expect_tool: str = ""  # 期待呼叫的工具名（空＝不檢查；MCP 工具以 `__工具名` 後綴比對）
    skills: tuple[str, ...] = ()  # 預先啟用的技能（經 x-touka-skills；docgen 等需先啟用其 MCP）
    max_tokens: int = 512
    timeout_s: float = 90.0  # 單情境 wall-clock 上限（防 ask_user 等卡死；逾時記為失敗）
    # 軌B Batch 5（成品驗證）：非空＝該情境會產檔，打開產出檔驗「格式合法＋含內容」。
    artifact_kind: str = ""  # "xlsx" | "docx" | "pptx"（空＝不驗成品）
    artifact_must_contain: tuple[str, ...] = ()  # 產出檔內必須出現的字串（如標題/某格值）


@dataclass
class RunOutcome:
    """runner 執行一個情境後的**正規化**結果（與 pipeline 事件形狀解耦，便於測試）。"""

    text: str = ""
    tools_called: tuple[str, ...] = ()
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str = ""  # 非空＝執行出錯／偵測到內部錯誤標記
    estimated: bool = False  # True＝上游未回報 usage，token/成本為文字估算值
    skipped: bool = False
    skip_reason: str = ""
    # 軌B 加強（telemetry）：工具軌跡與 ReAct 迴圈統計（皆已遮罩截斷、零生產熱路徑改動）。
    tool_trace: tuple[dict, ...] = ()  # 每個工具呼叫 {name, args, result, status}
    react_turns: int = 0  # ReAct 自主迴圈額外回合數（0＝單回合完成）
    finish_reason: str = ""  # 串流最後的 finish_reason（"stop"＝正常結束）
    early_stop: bool = False  # 是否因預算/總量上限提早停
    # 軌B Batch 5（成品驗證）：None＝該情境不驗成品；True/False＝產出檔開檔驗證結果。
    artifact_path: str = ""
    artifact_valid: bool | None = None
    artifact_detail: str = ""


@dataclass
class ScenarioResult:
    """單一情境的評估結果（結構正確性＋成本量測）。"""

    scenario_id: str
    surface: str
    structural_pass: bool
    detail: str
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    estimated: bool = False  # token/成本為估算值（上游未回報 usage）
    skipped: bool = False
    # 軌B 加強（telemetry）：供前端攤開「為何過/敗」。
    tool_trace: tuple[dict, ...] = ()
    react_turns: int = 0
    finish_reason: str = ""
    early_stop: bool = False
    # 軌B Batch 5（成品驗證）：None＝不驗；True/False＝產出檔開檔驗證結果。
    artifact_valid: bool | None = None
    artifact_detail: str = ""


@dataclass
class Scorecard:
    """一輪計分的彙整成績單。"""

    model: str
    budget_usd: float
    spent_usd: float
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        """實際執行（未略過）的情境數。"""
        return len([r for r in self.results if not r.skipped])

    @property
    def passed(self) -> int:
        """結構通過且未略過的情境數。"""
        return len([r for r in self.results if r.structural_pass and not r.skipped])

    @property
    def skipped(self) -> int:
        """被略過（多為預算耗盡）的情境數。"""
        return len([r for r in self.results if r.skipped])

    @property
    def total_tokens(self) -> int:
        """全情境 token 總和。"""
        return sum(r.tokens for r in self.results)
