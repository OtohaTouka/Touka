"""Touka 系統異常處理模組 (Exceptions)."""

import json
from enum import Enum
from typing import Any, Never

from fastapi import HTTPException

from src.config import CONFIG_DIR, PROXY_ROOT

# --- 錯誤碼主權 (Error SSOT) ---
ERRORS_JSON_PATH = CONFIG_DIR / "errors.json"
ERRORS_EXAMPLE_PATH = PROXY_ROOT / "config" / "errors.json.example"


def load_error_definitions() -> dict[str, Any]:
    """從 SSOT 載入錯誤定義。

    出貨範本補齊新增代碼；既有明確覆寫優先，僅在記憶體合併。
    """
    with ERRORS_EXAMPLE_PATH.open(encoding="utf-8") as source:
        definitions = json.load(source)
    path = ERRORS_JSON_PATH
    if not path.exists():
        return definitions

    with path.open(encoding="utf-8") as f:
        return {**definitions, **json.load(f)}


# 動態生成 ErrorCode 枚舉
_defs = load_error_definitions()
ErrorCode = Enum("ErrorCode", {k: k for k in _defs})


class ToukaException(HTTPException):
    """Touka 系統主權異常。

    強制要求使用預定義的 ErrorCode，確保錯誤訊息與狀態碼符合 SSOT。
    """

    def __init__(self, code: ErrorCode, detail_override: str | None = None, **kwargs: Any) -> None:
        """初始化 Touka 系統異常.

        Args:
            code (ErrorCode): 定義於 errors.json 的標準錯誤碼。
            detail_override (str | None, optional): 覆寫預設錯誤訊息的自訂字串。
            **kwargs (Any): 附加於 JSON 回應主體中的額外欄位資料。
        """
        error_def = _defs.get(code.value, _defs["SYSTEM_ERROR"])

        self.code = code
        self.error_id = error_def["id"]
        self.status_code = error_def["status"]
        self.detail = detail_override or error_def["msg"]
        self.i18n_key = error_def.get("i18n")

        # 封裝為標準 FastAPI HTTPException
        super().__init__(
            status_code=self.status_code,
            detail={
                "code": code.value,
                "id": self.error_id,
                "message": self.detail,
                "i18n": self.i18n_key,
                **kwargs,
            },
        )


def raise_error(code: ErrorCode, detail: str | None = None, **kwargs: Any) -> Never:
    """便捷拋出錯誤。"""
    raise ToukaException(code, detail_override=detail, **kwargs)
