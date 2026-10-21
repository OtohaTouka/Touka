"""核心計分情境 SSOT（軌B）。

涵蓋對話／推理／指令遵循／工具使用等模型表現面向。結構驗證看「有沒有壞」（無錯誤、
該有的回應在、該叫的工具有沒有叫），不做逐字比對。
"""
from __future__ import annotations

from src.services.scorecard.models import Scenario

CORE_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="chat_basic",
        surface="chat",
        prompt="請用一句話回覆：『連線正常』。",
    ),
    Scenario(
        id="chat_reasoning",
        surface="chat",
        prompt="3 加 4 等於多少？只回數字。",
    ),
    Scenario(
        id="instruction_following",
        surface="chat",
        prompt="請只輸出三個水果名稱，以逗號分隔，不要多餘文字。",
    ),
    Scenario(
        id="react_multistep",
        surface="react",
        prompt="請先想一下今天適合做什麼，再用一句話給我一個具體建議。",
        max_tokens=1024,
    ),
    # 工具使用（agentic）：誘使模型呼叫記憶工具；驗證 model 是否真的調用工具。
    # expect_tool＝後端記憶工具名（memory_server schema："memory_tools"）。
    Scenario(
        id="tool_memory_recall",
        surface="react",
        prompt="請使用你的記憶工具查詢有沒有關於「燈火」的記憶，並用一句話告訴我查到什麼。",
        expect_tool="memory_tools",
        max_tokens=1024,
    ),
    # 技能調度（Batch 4）：誘使模型用調度員啟用一項技能。activate_skill 因 system-skill-dispatcher
    # 常駐注入而恆在；測模型會不會正確使用技能調度機制。
    Scenario(
        id="skill_activation",
        surface="react",
        prompt="我待會要產生一份文件。請先用技能調度啟用『文件產生器（document-generator）』技能。",
        expect_tool="activate_skill",
        max_tokens=1024,
    ),
    # 網路搜尋（Batch 4）：誘使模型調用內建 research 工具。外部服務不穩時（429/連線失敗）
    # engine 會歸為 not-testable 略過（非品質失敗）。
    Scenario(
        id="web_search",
        surface="react",
        prompt="請用你的網路搜尋工具查一下「Model Context Protocol」是什麼，並用一句話總結。",
        expect_tool="research",
        max_tokens=1024,
    ),
    # 文件產生（Batch 4 便宜檔＋Batch 5 成品驗證，旗艦）：預先啟用 document-generator 技能
    # （連其 mcp-docgen MCP），誘使模型用結構化參數產 Excel。expect_tool 以 `__create_xlsx`
    # 後綴比對 MCP 工具名。**Batch 5**：artifact_kind="xlsx" → 從工具回傳取產出路徑、開檔驗
    # 「合法 xlsx ＋ 含 APAC/EMEA/120/90」——這是唯一「成品正確性」維度（不只驗有沒有叫工具）。
    # 未安裝 mcp-docgen 時工具不出現 → 該題失敗（informative）。
    Scenario(
        id="docgen_xlsx",
        surface="react",
        prompt=(
            "請用文件產生器把這個表格做成 Excel 檔：第一欄『地區』、第二欄『營收』，"
            "資料為 APAC=120、EMEA=90。"
        ),
        expect_tool="create_xlsx",
        skills=("document-generator",),
        max_tokens=1024,
        timeout_s=150.0,  # uvx 首次啟動 mcp-docgen ＋產檔，放寬上限
        artifact_kind="xlsx",
        artifact_must_contain=("APAC", "EMEA", "120", "90"),
    ),
)


def select_scenarios(ids: list[str] | None) -> tuple[Scenario, ...]:
    """依 id 篩選情境；None／空 → 全部核心情境。未知 id 自動忽略。"""
    if not ids:
        return CORE_SCENARIOS
    wanted = set(ids)
    return tuple(s for s in CORE_SCENARIOS if s.id in wanted)
