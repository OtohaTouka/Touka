"""Pydantic 請求與響應的模型定義，用於適配不同的 AI 模型提供商。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.session_persistence import UiPersistence

_BASE_URL_ALIAS_ERROR = "base_url aliases are not accepted"


class _PublicModelRequest(BaseModel):
    """Shared public-model request boundary for caller-controlled route aliases。"""

    ui_persistence: UiPersistence | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_ui_mode(self) -> "_PublicModelRequest":
        """只允許明示、模式一致的 UI 串流；內部欄位永不序列化給 provider。"""
        if self.ui_persistence is not None and (
            (self.ui_persistence.mode == "incognito") != bool(getattr(self, "incognito", False))
            or getattr(self, "stream", False) is not True
            or not getattr(self, "session_id", None)
        ):
            raise InvalidUiStreamRequest
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_base_url_aliases(cls, value: Any) -> Any:
        """Reject spelling variants that Pydantic would otherwise ignore as extras。"""
        if isinstance(value, dict):
            for key in value:
                if (
                    isinstance(key, str)
                    and key != "base_url"
                    and key.casefold().replace("_", "").replace("-", "") == "baseurl"
                ):
                    raise ValueError(_BASE_URL_ALIAS_ERROR)
        return value


class InvalidUiStreamRequest(ValueError):
    """不回傳原始 admission 或提示內容。"""

    def __init__(self) -> None:
        super().__init__("Invalid UI stream persistence request")


# --- Anthropic Models (Target) ---
class AnthropicThinking(BaseModel):
    """Anthropic 思考預算設定模型。"""

    type: str = "enabled"
    budget_tokens: int = 4000


class AnthropicOutputConfig(BaseModel):
    """原生入口的 output_config：只轉送 effort（r55）；其他鍵收下但不轉送（adapter 只記鍵名）。

    effort 用字串不用列舉：等級清單只在每模型規則表一處，型號不收的等級丟掉並記 warning。
    """

    model_config = ConfigDict(extra="allow")

    effort: str | None = Field(default=None, max_length=16)


# --- OpenAI Models ---
class OpenAIMessage(BaseModel):
    """OpenAI 相容的訊息資料結構。"""

    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class OpenAIChatRequest(_PublicModelRequest):
    """OpenAI 相容的對話請求資料結構。"""

    model: str
    messages: list[OpenAIMessage]
    tools: list[dict[str, Any]] | None = None
    stream: bool | None = False
    temperature: float | None = 1.0
    max_tokens: int | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop: str | list[str] | None = None
    thinking: AnthropicThinking | None = None  # 支援擴充思考模式
    safety_mode: str | None = None  # Grok 2026: strict, balanced, off
    safety_settings: list[dict[str, Any]] | None = None  # Grok 2026: [{category, threshold}]
    # 上游端點只能來自 Provider Registry／Owner 管理的 Connections；非空值由 Pydantic 拒絕。
    base_url: None = None
    ide_context: dict[str, Any] | None = None
    project_id: str | None = None
    session_id: str | None = None
    auth_level: str | None = None
    incognito: bool = False
    deep_research: bool = False  # ADR-0087：深入研究模式（多步搜尋＋引用報告）
    mcp_dispatcher: bool = False  # ADR-0161：MCP 動態掛載模式（漸進揭露＋connect_mcp_server）


# --- Anthropic Models (Target) ---
class AnthropicMessage(BaseModel):
    """Anthropic 相容的訊息資料結構。"""

    role: str
    content: str | list[dict[str, Any]]


class AnthropicRequest(_PublicModelRequest):
    """Anthropic 相容的對話請求資料結構。"""

    model: str
    max_tokens: int = 8192
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    stream: bool | None = False
    temperature: float | None = 1.0
    # ADR-0111：預設改 None（原為 1.0）——否則 claude_handler 會對每個原生 /messages 請求
    # 一律帶 top_p=1.0；改 None 後僅在明確設定/原生預設填入時才轉發（對齊 OpenAIChatRequest）。
    top_p: float | None = None
    top_k: int | None = None
    thinking: AnthropicThinking | None = None
    output_config: AnthropicOutputConfig | None = None
    safety_mode: str | None = None
    safety_settings: list[dict[str, Any]] | None = None
    # 上游端點只能來自 Provider Registry／Owner 管理的 Connections；非空值由 Pydantic 拒絕。
    base_url: None = None
    ide_context: dict[str, Any] | None = None
    project_id: str | None = None
    session_id: str | None = None
    auth_level: str | None = None
    incognito: bool = False
    deep_research: bool = False  # ADR-0087：深入研究模式（多步搜尋＋引用報告）
    mcp_dispatcher: bool = False  # ADR-0161：MCP 動態掛載模式（漸進揭露＋connect_mcp_server）


class DebugExecuteRequest(BaseModel):
    """除錯工具執行請求模型。"""

    agent_id: str
    tool_name: str
    args: dict[str, Any]
    session_id: str | None = "home"
    auth_level: str | None = "Manual"
