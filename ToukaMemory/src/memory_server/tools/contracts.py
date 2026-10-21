"""Domain-owned public memory inputs and pure JSON-schema projection helpers.

The parent memory_server package bootstraps configuration: static guards read this
source with AST; runtime contract tests import it only after sandbox setup.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

# 查對話一頁最多幾則（每則只取開頭一段，見 legacy_vault）
CONVERSATION_PAGE_MAX = 20


def _calendar_date(value: str | None) -> str | None:
    """YYYY-MM-DD 日曆驗證；換成時間範圍由 manager 負責."""
    if value is not None:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise PydanticCustomError("calendar_date", "Expected YYYY-MM-DD calendar date") from exc
        if parsed.isoformat() != value:
            raise PydanticCustomError("calendar_date", "Expected YYYY-MM-DD calendar date")
    return value


class PublicToolInput(BaseModel):
    """Only model-owned arguments; trusted execution metadata is kept outside."""

    model_config = ConfigDict(extra="forbid", strict=True)


class MemorySaveInput(PublicToolInput):
    """Public input model for MemorySave."""

    action: Literal["save"]
    content: str = Field(description="save 必填：記憶內容；沿用內容清理及長度上限")
    tags: list[str] | None = Field(None, description="一般標籤，與 emotions 感情標籤不同")
    importance: Literal["1", "2", "3", "4", "5"] | None = Field(None, description="重要度 (1-5)")
    emotions: list[str] | None = Field(
        None, description="自由感情標籤，例如感謝、期待；不限制固定詞彙"
    )


class MemoryQueryInput(PublicToolInput):
    """Public input model for MemoryQuery."""

    action: Literal["query"]
    query: str = Field("", description="檢索字詞；最近／recent 字樣只切檢索策略，不自動限制七日")
    limit: int = Field(5, ge=1, description="回傳筆數上限")
    tags: list[str] | None = Field(None, description="一般標籤篩選")
    importance: Literal["1", "2", "3", "4", "5"] | None = Field(
        None, description="最低重要度 (1-5)"
    )
    emotions: list[str] | None = Field(None, description="自由感情標籤篩選，與 tags 分開")
    date_start: str | None = Field(
        None, description="YYYY-MM-DD 起日；與 is_recent 同給時採較晚起點"
    )
    date_end: str | None = Field(None, description="YYYY-MM-DD 迄日，包含該日最後一秒")
    is_recent: bool = Field(False, description="限制最近 7 日；可與 date_start/date_end 交集使用")
    include_sources: bool = Field(
        False, description="每筆附一行來源（哪段對話或哪次心跳、原文還在不在）；只讀，不回原文"
    )
    include_history: bool = Field(
        False,
        description=(
            "每筆附上被新記憶取代前的舊版本（只限被 supersede 取代的；"
            "用 forget 封存的不會出現）；只讀"
        ),
    )

    @field_validator("date_start", "date_end")
    @classmethod
    def validate_calendar_date(cls, value: str | None) -> str | None:
        """Calendar validation only; time-range conversion remains manager-owned."""
        return _calendar_date(value)


class MemoryForgetInput(PublicToolInput):
    """Public input model for MemoryForget."""

    action: Literal["forget"]
    fact_id_to_archive: str = Field(
        min_length=1,
        description="forget 必填：query/save 結果中的 factual ID，不是顯示序號或 vector point ID",
    )


class MemoryDuplicatesInput(PublicToolInput):
    """Public input model for MemoryDuplicates."""

    action: Literal["manage_dups"]
    operation: Literal["scan", "resolve", "supersede"] = Field(
        "scan",
        description=(
            "scan 找重複；resolve 封存指定記憶（要 target_id）；supersede 用新內容取代 target_id"
            " 那筆（要 content；舊的保留、封存並連到新的）；皆依寫入風險審批"
        ),
    )
    target_id: str | None = Field(  # noqa: IDENTITY_SSOT — factual record key, not principal
        None,
        min_length=1,
        description="resolve：要封存的 factual ID；supersede：要被取代、自己仍有效的 factual ID",
    )
    content: str | None = Field(
        None,
        min_length=1,
        description="operation=supersede 必填：新的記憶內容；和 save 同樣清理、查重與長度上限",
    )
    tags: list[str] | None = Field(
        None, description="operation=supersede：新版本的標籤；沒給沿用舊的，[] 清空"
    )
    importance: Literal["1", "2", "3", "4", "5"] | None = Field(
        None, description="operation=supersede：新版本的重要度 (1-5)；沒給沿用舊的"
    )
    emotions: list[str] | None = Field(
        None, description="operation=supersede：新版本的感情標籤；沒給沿用舊的"
    )

    @model_validator(mode="after")
    def require_operation_fields(self) -> MemoryDuplicatesInput:
        """Each operation takes exactly its own fields; the new memory belongs to supersede."""
        if self.operation in ("resolve", "supersede") and not self.target_id:
            raise PydanticCustomError("duplicate_target", "resolve and supersede require target_id")
        if self.operation == "supersede" and self.content is None:
            raise PydanticCustomError("supersede_content", "supersede requires content")
        new_memory = (self.content, self.tags, self.importance, self.emotions)
        if self.operation != "supersede" and any(value is not None for value in new_memory):
            raise PydanticCustomError(
                "supersede_fields", "content, tags, importance and emotions only apply to supersede"
            )
        return self


class MemoryRandomInput(PublicToolInput):
    """Public input model for MemoryRandom."""

    action: Literal["random"]


class MemoryDiagnosticsInput(PublicToolInput):
    """Public input model for MemoryDiagnostics."""

    action: Literal["diagnostics"]


class MemoryReindexInput(PublicToolInput):
    """Public input model for MemoryReindex: only the caller's own facts, so no target."""

    action: Literal["reindex"]


class MemoryConversationsInput(PublicToolInput):
    """Public input model for MemoryConversations: search old conversations, read only."""

    action: Literal["conversations"]
    query: str = Field("", description="只比對訊息內容的關鍵字；空字串＝不篩")
    date_start: str | None = Field(None, description="YYYY-MM-DD 起日（台北整天，含當日）")
    date_end: str | None = Field(None, description="YYYY-MM-DD 迄日（台北整天，含當日）")
    role: Literal["user", "assistant"] | None = Field(
        None, description="只看使用者（user）或助理（assistant）說的"
    )
    page_size: int = Field(
        10, ge=1, le=CONVERSATION_PAGE_MAX, description="每頁則數（1–20，預設 10）"
    )
    offset: int = Field(0, ge=0, description="略過前幾則，用來翻頁")
    source: Literal["legacy", "ui", "all"] | None = Field(
        None,
        description=(
            "資料來源：legacy＝舊對話庫；ui＝聊天介面的對話；all＝兩者。"
            "省略＝目前查得到的全部來源（結果開頭會寫實際查了哪些）"
        ),
    )

    @field_validator("date_start", "date_end")
    @classmethod
    def validate_calendar_date(cls, value: str | None) -> str | None:
        """Same calendar rule as query."""
        return _calendar_date(value)

    @model_validator(mode="after")
    def require_ordered_dates(self) -> MemoryConversationsInput:
        """A reversed range is an error, never "no filter"."""
        if self.date_start and self.date_end and self.date_start > self.date_end:
            raise PydanticCustomError("date_range", "date_start is after date_end")
        return self


MEMORY_ACTION_CONTRACTS = {
    "save": MemorySaveInput,
    "query": MemoryQueryInput,
    "forget": MemoryForgetInput,
    "manage_dups": MemoryDuplicatesInput,
    "random": MemoryRandomInput,
    "diagnostics": MemoryDiagnosticsInput,
    "reindex": MemoryReindexInput,
    "conversations": MemoryConversationsInput,
}
MEMORY_INTERNAL_ACTIONS = frozenset({"export_facts"})


def flatten_local_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Expand only closed local refs and optional-null unions before projection."""
    definitions = schema.get("$defs", {})

    def visit(node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [visit(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        current = deepcopy(node)
        ref = current.pop("$ref", None)
        if ref is not None:
            prefix = "#/$defs/"
            if (
                not isinstance(ref, str)
                or not ref.startswith(prefix)
                or ref[len(prefix) :] not in definitions
                or ref in stack
            ):
                raise PydanticCustomError(
                    "closed_schema_ref", "Unsupported or recursive schema reference"
                )
            current = {**visit(definitions[ref[len(prefix) :]], (*stack, ref)), **current}
        current.pop("$defs", None)
        current.pop("title", None)
        choices = current.get("anyOf")
        if isinstance(choices, list):
            non_null = [item for item in choices if item.get("type") != "null"]
            if len(non_null) == 1 and len(non_null) != len(choices):
                current.pop("anyOf")
                current = {**visit(non_null[0], stack), **current}
                if current.get("default") is None:
                    current.pop("default", None)
        if "const" in current:
            current["enum"] = [current.pop("const")]
        return {key: visit(value, stack) for key, value in current.items()}

    return visit(schema)


def public_parameters(model: type[PublicToolInput]) -> dict[str, Any]:
    """Generate parameters from the same model used at the execution boundary."""
    return flatten_local_schema(model.model_json_schema())


def action_parameters(models: dict[str, type[PublicToolInput]]) -> dict[str, Any]:
    """Keep public fields outside action combinators for provider projection."""
    properties: dict[str, Any] = {}
    branches = []
    descriptions: dict[str, list[str]] = {}
    action_hints = []
    for action, model in models.items():
        parameters = public_parameters(model)
        for key, value in parameters["properties"].items():
            if key != "action":
                properties.setdefault(key, value)
                if value.get("description"):
                    descriptions.setdefault(key, []).append(f"{action}: {value['description']}")
        required = [key for key in parameters.get("required", []) if key != "action"]
        action_hints.append(f"{action}: required {', '.join(required) or 'none'}")
        branches.append(
            {
                "properties": {"action": {"const": action}},
                "required": [key for key in parameters.get("required", []) if key != "action"],
            }
        )
    for key, parts in descriptions.items():
        properties[key]["description"] = "; ".join(parts)
    properties = {
        "action": {
            "type": "string",
            "enum": list(models),
            "description": "動作與必要參數：" + "; ".join(action_hints),
        },
        **properties,
    }
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"],
        "oneOf": branches,
        "additionalProperties": False,
    }
