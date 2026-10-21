# ruff: noqa
"""Vulture 白名單 — 告知 vulture 哪些是刻意保留的符號。

此檔案由 `scripts/guard_dead_code.py` 傳入 vulture 作為「白名單輸入」。
每當 vulture 誤報某個符號時，在此加一行「引用」以消除誤報。

常見誤報類別：
- FastAPI 路由函式（被 @router.get 裝飾而非直接呼叫）
- Pydantic 模型欄位（透過 JSON 動態讀寫）
- Protocol 介面方法（由子類別實作）
- pytest fixtures（被 pytest 自動發現）
- __all__ 中聲明的公開 API

用法：
    vulture ToukaProxy/src ToukaMemory/src ToukaHeartbeat scripts/vulture_whitelist.py --min-confidence 80
"""

# ADR-0069 Phase B — Judge Protocol 介面參數（由 Phase D 的 ModelJudge 實作使用；
# 本階段僅 NoopJudge 哨兵，故 vulture 於介面宣告處誤判 artifact_md 未用）。
artifact_md
