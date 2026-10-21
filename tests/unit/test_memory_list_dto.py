"""記憶寶庫 bug 修復 — 後端 DTO 單元測試。

驗證 `/v1/memory/list` 的事實列轉 DTO 邏輯：
- `created_at` 以「秒」儲存（後端時間 SSOT），於 API 邊界轉「毫秒」供前端時間 SSOT 使用。
- `MEMORY_LIST_LIMIT` 為有界高上限，確保不再把跨 agent 記憶截斷到 200。
"""
from __future__ import annotations

from src.routers.observability import MEMORY_LIST_LIMIT, _fact_to_memory_dto


def test_dto_converts_seconds_to_milliseconds():
    """秒制 created_at 應 ×1000 變成毫秒（前端 new Date 吃 ms）。"""
    fact = {
        "content": "記得喝水",
        "created_at": 1780569921,  # 2026-06-04（秒）
        "tags": ["health"],
        "agent_id": "Alice",
    }
    dto = _fact_to_memory_dto(fact)
    assert dto["timestamp"] == 1780569921000
    assert dto["content"] == "記得喝水"
    assert dto["tags"] == ["health"]
    assert dto["agent_id"] == "Alice"


def test_dto_handles_missing_created_at():
    """created_at 為 None/缺漏時不應崩潰，timestamp 退回 0。"""
    assert _fact_to_memory_dto({"created_at": None})["timestamp"] == 0
    assert _fact_to_memory_dto({})["timestamp"] == 0


def test_dto_only_exposes_expected_fields():
    """DTO 僅輸出寶庫頁需要的欄位，避免外洩內部欄位。"""
    dto = _fact_to_memory_dto(
        {"content": "c", "created_at": 1, "tags": [], "agent_id": "Bob", "importance": "9"}
    )
    assert set(dto.keys()) == {"content", "timestamp", "tags", "agent_id"}


def test_memory_list_limit_is_high_bound():
    """上限必須遠高於舊的 200，才能完整顯示每個 persona 的記憶。"""
    assert isinstance(MEMORY_LIST_LIMIT, int)
    assert MEMORY_LIST_LIMIT >= 1000
