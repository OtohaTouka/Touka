"""自定義模型資料結構描述 (Custom Models Schema).

定義 L2 自定義模型配置、更新與重命名操作的 Pydantic 模型。
"""

from typing import Any

from pydantic import BaseModel, Field


class CustomModelConfig(BaseModel):
    """自定義模型 (Level 2) 的運行配置"""
    name: str | None = None
    description: str | None = ""
    # 這裡可以包含任何 L2 特有的配置，目前主要儲存於 config.json
    # 如 base_model, temperature, tools 等 (目前 custom_model_service 內部處理)

class CustomModelUpdateRequest(BaseModel):
    """更新或建立自定義模型的請求模型"""
    name: str | None = None
    description: str | None = ""
    systemPrompt: str = Field(..., description="自定義系統指令 (Persona)")
    config: dict[str, Any] = Field(default_factory=dict, description="底層配置")

class CustomModelRenameRequest(BaseModel):
    """自定義模型重命名的請求模型"""
    # 縱深防禦：限制 new_id 字元集，於請求層即擋掉路徑分隔字元 (/、\)，
    # 配合 custom_io.move_custom_dir 的沙箱看守阻斷路徑穿越逃逸。
    new_id: str = Field(..., description="新的模型 ID", pattern=r"^[A-Za-z0-9._-]+$")
