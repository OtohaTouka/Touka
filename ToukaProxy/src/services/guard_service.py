"""幻覺與誠實度防護服務 (Guard Service).

負責分析 AI 產出的回覆內容，偵測是否包含模擬未調用的工具日誌、虛假的記憶承諾或未查詢資料即憑空捏造的內容。
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 「查證」工具集（誠實度閘，漂移修復批 C2）：模型提到過去/檔案時，出現任一即視為
# 已實際查閱。含記憶查詢（memory_tools 限定名，見 tool_executor.record_tool_usage）、
# 九裸動詞 read-family（ADR-0119 重設計後的主廣告面；其內部委派 vault_tools 屬
# Python 呼叫、不經 tool_executor，故必須顯式列名）與 vault_tools 讀側 shim 相容。
VERIFICATION_TOOLS = frozenset({
    "memory_tools.query",
    "read", "list", "find", "grep",
    "vault_tools.read", "vault_tools.list", "vault_tools.find", "vault_tools.grep",
})

# 記憶儲存訊號（記憶承諾閘）：memory_tools(action=save) 的限定名。
MEMORY_SAVE_SIGNAL = "memory_tools.save"

# 記憶查詢訊號（e_index 檢索分量等消費）：memory_tools(action=query) 的限定名。
MEMORY_QUERY_SIGNAL = "memory_tools.query"


class GuardService:
    """負責偵測與防治模型產生的幻覺內容（如模擬工具日誌、虛假記憶承諾等）。
    """

    def __init__(self):
        """初始化防護服務.

        建立工具日誌特徵、記憶承諾與誠實度觸發的正則表達式 Pattern。
        """
        # 僅攔截帶有日誌特徵的模擬 (必須在同一行或極近距離)
        # 防止單純提到工具名與無關勾選符號觸發誤判
        # 支援格式：> search, search ✅, (開始調用工具): search, > 正在執行工具：search
        self.log_features_pattern = r"(?:> ?{tool_name}|{tool_name} ?[✅✓]|[✅✓] ?{tool_name}|\(開始調用工具\)\:? ?{tool_name}|> ?正在執行工具[\:：]? ?`?{tool_name}`?)"
        # 記憶承諾相關關鍵字
        self.memory_promise_pattern = (
            r"(記下來|存入記憶|記在心裡|紀錄下來|幫你記住|已經存檔)"
        )
        # 誠實度偵測關鍵字 (偵測：提到「外部」過去或檔案但沒用工具)
        # 排除掉單純的「記得」、「上次」、「妳說過」，因為這些可能指當前對話
        self.honesty_trigger_pattern = r"(過去的記憶系統|大腦記憶庫|遠端檔案內容|系統文件資料|儲存硬碟裡|過去的完整紀錄)"

    async def collect_known_tool_names(
        self,
        tool_registry: dict[str, Any] | None,
        memory_tool_registry: dict[str, Any] | None,
        plugin_registry: dict[str, Any] | None,
        protocol_manager: Any,
    ) -> set[str]:
        """幻覺偵測的「已知工具」全集：三個靜態 registry ＋ 動態 MCP 工具名（SSOT）。

        禁令 59(c)：已知工具集**必須涵蓋 MCP 工具**——過去只比對靜態 registry，
        模型文字提及（未實際呼叫的）MCP 工具時偵測不到（假陰性）。MCP 列舉失敗
        時退回靜態集合（防禦性，不讓偵測器壞掉整個回應流）。

        漂移修復批 C1：本函式自 claude_handler 的私有副本下沉至此成為唯一實作
        （openai/gemini 過去用靜態三 registry ＝ MCP 盲區，即三份拷貝的漂移）。
        """
        names: set[str] = set()
        for reg in (tool_registry, memory_tool_registry, plugin_registry):
            if reg:
                names.update(reg.keys())
        if protocol_manager is None:
            return names
        try:
            mcp_tools = await protocol_manager.get_all_tools()
        except Exception:
            logger.exception(
                "collect_known_tool_names: MCP listing failed; using statics only"
            )
            return names
        for t in mcp_tools or []:
            t_name = (t.get("function") or {}).get("name") or t.get("name")
            if t_name:
                names.add(t_name)
        return names

    def detect_tool_hallucinations(
        self,
        response_text: str,
        actually_used_tools: set[str],
        all_known_tools: set[str],
    ) -> list[str]:
        """檢查回應文字中是否包含未經調用的工具模擬日誌。
        """
        hallucination_warnings = []
        for tool_name in all_known_tools:
            if tool_name in response_text and tool_name not in actually_used_tools:
                # 移除 re.DOTALL，確保日誌特徵與工具名稱必須在同一行或非常接近，避免跨段落誤殺
                pattern = re.compile(
                    self.log_features_pattern.format(tool_name=re.escape(tool_name))
                )
                if pattern.search(response_text):
                    hallucination_warnings.append(tool_name)
        return hallucination_warnings

    def check_memory_hallucination(
        self, response_text: str, actually_used_tools: set[str]
    ) -> bool:
        """檢查是否口頭承諾記錄記憶但未實際呼叫 memory_tools(action=save)。

        C2：舊判定閘在已退役的工具名 ``save_memory``（記憶大一統後僅存為
        ToukaMemory 內部函式、永不出現在 actually_used_tools）＝抑制分支死碼、
        誤報恆真；改讀 tool_executor 產生的限定名 SSOT。
        """
        if MEMORY_SAVE_SIGNAL in actually_used_tools:
            return False

        return bool(re.search(self.memory_promise_pattern, response_text))

    def check_fact_without_tools(
        self, response_text: str, actually_used_tools: set[str]
    ) -> bool:
        """檢查是否提到過去或文件但未實際調用查證工具（集合見 VERIFICATION_TOOLS）。

        C2：舊判定閘在已退役的名字（query_memory/access_personal_drive）＝
        永不知道模型有查證；改讀限定名＋裸動詞 read-family。
        """
        # 如果已經用了任一查證工具，就視為誠實
        if VERIFICATION_TOOLS & actually_used_tools:
            return False

        # 檢查是否出現疑似「查證」或「回想」的關鍵字
        return bool(re.search(self.honesty_trigger_pattern, response_text))

    def get_hallucination_warning_message(self, tool_names: list[str]) -> str:
        """格式化幻覺警告訊息。
        """
        if not tool_names:
            return ""
        return f"\n\n⚠️ **[工具幻覺警告]** 您模擬了工具 `{', '.join(tool_names)}` 但未正式呼叫。"

    def get_memory_warning_message(self) -> str:
        """格式化記憶幻覺警告訊息（C2：文案改引實存工具，勿教已退役名）。
        """
        return "\n\n⚠️ **[記憶幻覺警告]** 您承諾記錄但未呼叫 `memory_tools` 進行儲存。"

    def get_honesty_warning_message(self) -> str:
        """格式化誠實度（沒查資料就瞎掰）警告訊息（C2：文案改引實存工具）。
        """
        return "\n\n⚠️ **[誠實度警報]** 妳提到了過去或檔案內容，但並未實際查閱工具。請先以 `memory_tools`（action=query）查記憶、或以 `read`/`grep` 等檔案工具核對檔案後再正式回答，不要憑空編造。"

    def get_honesty_correction_prompt(self) -> str:
        """誠實守衛注入模型的矯正提示（上游 prompt 面 SSOT，ADR-0157）。

        與 :meth:`get_honesty_warning_message`（下游使用者警告）分工：本文案進
        對話歷史、供模型下一輪遵循。過去 gemini handler 內嵌版教已退役工具名
        （query_memory/access_personal_drive）＝防幻覺系統自己教幻覺；收斂於此
        並改引實存工具（VERIFICATION_TOOLS 的主廣告面）。
        """
        return (
            "Warning: You mentioned memories or file contents "
            "without actually using tools. "
            "Verify first — query memory with `memory_tools` (action=\"query\") "
            "or read files with `read`/`grep` — then answer honestly."
        )


guard_service = GuardService()
