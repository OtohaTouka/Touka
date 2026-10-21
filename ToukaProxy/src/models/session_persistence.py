"""Session 保存與 resumable UI 終態的純公開合約；不載入設定或儲存服務。"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.models.history import TreeHistory
from src.models.identity import AgentIdentity
from src.utils.auth_mode import AuthMode


class PersistenceModel(BaseModel):
    """公開結果不容許原始錯誤、舊 revision 或其他未定義欄位回流。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class UiGenerationStopped(RuntimeError):
    """僅明示 UI producer 消費的固定安全生成結果；不是可恢復工具錯誤。"""

    def __init__(self, generation: Literal["error", "cancelled"]) -> None:
        self.generation = generation
        super().__init__("UI generation did not complete")


class SessionContentError(ValueError):
    """保存內容不合法；邊界只顯示固定代碼，不回傳原始輸入。"""

    def __init__(self) -> None:
        super().__init__("Invalid session persistence content")


class SessionChanges(PersistenceModel):
    """受控保存的有限欄位；省略保留，nullable 的顯式 null 可清除。

    owner 與 last_activity_at 由受管儲存決定，不能由公開參數設定。
    messages 先依既有 TreeHistory 驗證，再保存原 JSON，避免補預設或丟擴充欄位。
    """

    title: str | None = None
    messages: dict[str, Any] | None = None
    updatedAt: int | None = None
    is_home: bool | None = None
    home_for_agent: AgentIdentity | None = None
    agent_id: AgentIdentity | None = None
    project_id: str | None = None
    auth_level: AuthMode | None = None  # 寫入只收新名稱（ADR-0246）
    active_mcp_servers: list[str] | None = None
    active_skills: list[str] | None = None
    context_summary: str | None = None

    @model_validator(mode="after")
    def validate_content(self) -> "SessionChanges":
        """拒絕非 nullable 清除與不能保真保存的訊息內容。"""
        non_nullable = {
            "title",
            "messages",
            "updatedAt",
            "is_home",
            "auth_level",
            "active_mcp_servers",
            "active_skills",
        }
        for name in self.model_fields_set & non_nullable:
            if getattr(self, name) is None:
                raise SessionContentError
        if self.messages is not None:
            TreeHistory.model_validate(self.messages)
            json.dumps(self.messages, allow_nan=False)
        return self


class SessionDocument(PersistenceModel):
    """公開讀取投影；完整的既存欄位，不包括內部 owner 或授權物件。"""

    id: str
    title: str
    messages: dict[str, Any]
    updatedAt: int
    is_home: bool
    home_for_agent: str | None
    agent_id: str | None
    project_id: str | None
    auth_level: AuthMode  # 已存舊值由儲存層讀取時對照（normalize_auth_mode）
    active_mcp_servers: list[str]
    active_skills: list[str]
    last_activity_at: int
    context_summary: str | None


class PresentSessionSnapshot(PersistenceModel):
    """存在快照附內容，revision 只對本次已認證 scope 有效。"""

    exists: Literal[True]
    revision: str = Field(min_length=1)
    session: SessionDocument


class AbsentSessionSnapshot(PersistenceModel):
    """確認缺席才有 absence revision，不能附帶虛構 session。"""

    exists: Literal[False]
    revision: str = Field(min_length=1)
    session: None


type SessionSnapshotResponse = PresentSessionSnapshot | AbsentSessionSnapshot


class PersistedStreamAdmission(PersistenceModel):
    """UI 已確認的 user 全樹版本；不是模型供應商參數或授權憑證。"""

    mode: Literal["persistent"]
    revision: str = Field(min_length=1)
    user_parent_id: str = Field(min_length=1)
    assistant_node_id: str = Field(min_length=1)


class IncognitoStreamAdmission(PersistenceModel):
    """無痕固定節點，不得携帶資料庫 revision。"""

    mode: Literal["incognito"]
    user_parent_id: str = Field(min_length=1)
    assistant_node_id: str = Field(min_length=1)


type UiPersistence = Annotated[
    PersistedStreamAdmission | IncognitoStreamAdmission,
    Field(discriminator="mode"),
]


class SavedResult(PersistenceModel):
    """保存已確認；text 並不表示前端完整訊息樹已保存。"""

    status: Literal["saved"]
    representation: Literal["text", "full"]
    revision: str = Field(min_length=1)


class SkippedResult(PersistenceModel):
    """刻意不寫，不得表示成已保存。"""

    status: Literal["skipped"]
    reason: Literal["incognito", "no_text"]


class FailedResult(PersistenceModel):
    """只有已確認的提交前拒絕才可使用此結果。"""

    status: Literal["failed"]
    error_code: int = Field(gt=0)


class UnknownResult(PersistenceModel):
    """提交點不明；呼叫端必須保留內容並停止自動重送。"""

    status: Literal["unknown"]
    error_code: int = Field(gt=0)


type SaveResult = Annotated[
    SavedResult | SkippedResult | FailedResult | UnknownResult,
    Field(discriminator="status"),
]


class SessionSaveResponse(PersistenceModel):
    """沿用既有成功 envelope，增加可驗證的保存結果。"""

    status: Literal["success"] = "success"
    session_id: str = Field(min_length=1)
    persistence: SaveResult


class StreamCoordinates(PersistenceModel):
    """一個回合的固定 ID；不能靠 session ID 讓舊 producer 終結新串流。"""

    session_id: str = Field(min_length=1)
    stream_id: str = Field(min_length=1)
    user_parent_id: str = Field(min_length=1)
    assistant_node_id: str = Field(min_length=1)


class StreamStartedEvent(StreamCoordinates):
    """附於原內容 frame 前的回合對齊 metadata。"""

    type: Literal["touka_stream_started"] = "touka_stream_started"


class StreamTerminalEvent(StreamCoordinates):
    """生成終態與保存結果各自表達；取消不因寫入成功變完成。"""

    type: Literal["touka_stream_terminal"] = "touka_stream_terminal"
    generation: Literal["completed", "error", "cancelled"]
    persistence: SaveResult


class BufferedStreamState(PersistenceModel):
    """已授權的記憶體串流狀態；不包含 owner 或內部儲存憑證。"""

    session_id: str
    stream_id: str
    status: Literal["active", "done", "error", "cancelled"]
    last_seq: int
    started_at: float
    started: StreamStartedEvent | None = None
    terminal: StreamTerminalEvent | None = None


class SessionStreamStateResponse(PersistenceModel):
    """選定 session 的一次狀態核對；已過期的 buffer 不可推論已保存。"""

    state: BufferedStreamState | None


TRANSPORT_MODELS = (
    SessionSaveResponse,
    StreamStartedEvent,
    StreamTerminalEvent,
    PresentSessionSnapshot,
    AbsentSessionSnapshot,
    SessionStreamStateResponse,
)
