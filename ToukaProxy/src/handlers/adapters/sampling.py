"""取樣參數名稱 SSOT（model-p0；ADR-0241 決定 A）。

三家 adapter 一律不把這些參數放進送出內容（用廠商預設）。請求與設定的 Pydantic
欄位保留，舊設定照常載入、值被忽略。OpenAI adapter 與跨 adapter 契約測試共用。
"""

from __future__ import annotations

SAMPLING_PARAM_NAMES: frozenset[str] = frozenset({"temperature", "top_p", "top_k"})
