"""模型管理層整合入口 (Model Service Entrypoint).

集中匯出全系統唯一的模型管理員單例 (model_manager)，提供模型發現、健康感知與配置持久化服務。
"""

from .manager import ModelManager

# 全系統唯一的模型管理員單例（SSOT）
model_manager = ModelManager()

__all__ = ["ModelManager", "model_manager"]
