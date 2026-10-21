"""模型管理單例匯出模組 (Model Manager Single Export).

提供全域對外的 model_manager 單例，以相容舊有的模型存取機制。
"""

from .models import ModelManager, model_manager

__all__ = ["model_manager", "ModelManager"]
