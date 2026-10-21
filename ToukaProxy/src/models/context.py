"""PipelineContext (Context SSOT)

此模型為 ToukaProxy Pipeline 傳遞參數的強型別容器。
實作了字典魔術方法 (__getitem__, __setitem__ 等) 以保證向後相容性，
避免破壞現有各層中直接透過 ctx["key"] 存取資料的寫法。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, InstanceOf

from .advertised_tools import AdvertisedTools
from .identity import AgentIdentity


class PipelineContext(BaseModel):
    """代理人處理管線的統一上下文容器。"""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)
    
    # 基礎請求資訊
    request_id: str | None = Field(default=None, description="請求追蹤 ID")
    t0: float | None = Field(default=None, description="請求開始時間 (Epoch)")
    session_id: str | None = Field(default=None, description="對話會話 ID")
    
    # 身分識別資訊
    agent_id: str | None = Field(default=None, description="辨識出的 Agent ID")
    is_toukasoul: bool = Field(default=False, description="是否為 L1 靈魂")
    is_custom: bool = Field(default=False, description="是否為 L2 自定義模型")
    identity: AgentIdentity | None = Field(default=None, description="已驗證的 Agent 身份物件")
    human_principal_id: str | None = Field(
        default=None,
        description="Gate 1 原始人類 principal；身分代打後仍保留供 OAuth/handle 隔離",
    )
    
    # 權限與工具資訊
    auth_level: str | None = Field(
        default=None, description="模式 (Manual, Auto, YOLO)"
    )
    desired_tools: list[str] | None = Field(default=None, description="預期啟用的工具名稱清單")
    
    # 任務與環境資訊
    mission_awareness: Any | None = Field(default=None, description="任務看板感知物件")

    # Tier 1（可讀拒絕）：Gate 1 過濾的觀測資料
    denied_tools: list[dict[str, str]] | None = Field(
        default=None, description="Gate 1 政策拒絕的工具（{tool, reason}）"
    )
    tools_filtered: bool = Field(default=False, description="Gate 1 是否過濾了任何 builtin 工具")

    # 無痕模式（HAX G17，ADR-0086）：零殘留對話旗標
    incognito: bool = Field(default=False, description="無痕模式：零殘留對話")

    # 深入研究模式（ADR-0087）：旗標啟用時注入研究協調 Skill、引導多步搜尋＋引用報告
    deep_research: bool = Field(default=False, description="深入研究模式")

    # 記憶來源：觸發這輪的已保存 user 節點（UI 持久模式與心跳由路由寫入；無痕不寫）
    source_message_id: str | None = Field(
        default=None, description="存記憶時記下的來源訊息節點 ID（系統寫入，模型不能提供）"
    )

    # 執行前核對用：只收 AdvertisedTools（dict、frozenset 一律拒絕）
    advertised_tools: InstanceOf[AdvertisedTools] | None = Field(
        default=None, description="這一輪實際列給模型的工具（ToolLayer 寫入；模型不能提供）"
    )

    def __getitem__(self, key: str) -> Any:
        """支援 ctx['key'] 的讀取"""
        if key in self.__class__.model_fields:
            return getattr(self, key)
        if self.model_extra is not None and key in self.model_extra:
            return self.model_extra[key]
        raise KeyError(key)
        
    def __setitem__(self, key: str, value: Any) -> None:
        """支援 ctx['key'] = value 的寫入"""
        if key in self.__class__.model_fields:
            setattr(self, key, value)
        else:
            if self.model_extra is None:
                # Pydantic extra dict might not be initialized if not set during init
                object.__setattr__(self, '__pydantic_extra__', {})
            self.model_extra[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """支援 ctx.get('key', default) 的讀取"""
        try:
            return self[key]
        except KeyError:
            return default
            
    def __contains__(self, key: str) -> bool:
        """支援 'key' in ctx 的判斷"""
        return key in self.__class__.model_fields or (
            self.model_extra is not None and key in self.model_extra
        )
        
    def update(self, mapping: dict[str, Any]) -> None:
        """支援 ctx.update() 批次更新"""
        for k, v in mapping.items():
            self[k] = v
