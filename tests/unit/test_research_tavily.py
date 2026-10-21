"""Tavily 搜尋調校測試（ADR-0090）。

驗證：
  - research 的 Tavily 分支以 env（src.config）為 SSOT 傳入 search_depth/max_results/include_answer。
  - per-call kwargs 可覆寫 env 預設（供 deep research 協調器擴大檢索）。
  - Tavily 直接答案（answer）會置頂於結果之前（對標大廠 AI Overview）。
"""

from __future__ import annotations

import sys

import pytest
from src.tools.builtin.research import research

# 注意：src.tools.builtin.__init__ 會 re-export `research` 函式，遮蔽同名子模組的
# 屬性查找（連 pytest 的字串路徑解析也會解到函式），故直接從 sys.modules 取真實模組物件。
_RESEARCH_MOD = sys.modules["src.tools.builtin.research"]


class _FakeTavily:
    """攔截 .search() 參數並回傳固定結果的假 TavilyClient。"""

    last_kwargs: dict = {}

    def __init__(self, api_key: str | None = None) -> None:
        pass

    def search(self, **kwargs: object) -> dict:
        _FakeTavily.last_kwargs = kwargs
        return {
            "answer": "天氣晴，氣溫 25 度。",
            "results": [
                {"title": "T1", "url": "http://a", "content": "C1"},
                {"title": "T2", "url": "http://b", "content": "C2"},
            ],
        }


@pytest.fixture
def patch_tavily(monkeypatch):
    monkeypatch.setattr(_RESEARCH_MOD, "TavilyClient", _FakeTavily)
    monkeypatch.setattr("src.config.TAVILY_API_KEY", "fake-key")
    _FakeTavily.last_kwargs = {}


@pytest.mark.asyncio
async def test_tavily_uses_env_tuning(patch_tavily, monkeypatch):
    """env 值（非預設）會被傳入 client.search()，證明非硬編碼。"""
    monkeypatch.setattr("src.config.TAVILY_SEARCH_DEPTH", "basic")
    monkeypatch.setattr("src.config.TAVILY_MAX_RESULTS", 7)
    monkeypatch.setattr("src.config.TAVILY_INCLUDE_ANSWER", False)

    await research("查天氣")

    kw = _FakeTavily.last_kwargs
    assert kw["query"] == "查天氣"
    assert kw["search_depth"] == "basic"
    assert kw["max_results"] == 7
    assert kw["include_answer"] is False


@pytest.mark.asyncio
async def test_tavily_kwargs_override_env(patch_tavily, monkeypatch):
    """per-call kwargs 優先於 env 預設。"""
    monkeypatch.setattr("src.config.TAVILY_SEARCH_DEPTH", "advanced")
    monkeypatch.setattr("src.config.TAVILY_MAX_RESULTS", 5)
    monkeypatch.setattr("src.config.TAVILY_INCLUDE_ANSWER", True)

    await research(
        "查天氣", max_results=12, search_depth="basic", include_answer=False
    )

    kw = _FakeTavily.last_kwargs
    assert kw["max_results"] == 12
    assert kw["search_depth"] == "basic"
    assert kw["include_answer"] is False


@pytest.mark.asyncio
async def test_tavily_falsy_override_falls_back_to_env(patch_tavily, monkeypatch):
    """刻意語義（ADR-0090）：falsy 的 max_results(0)/search_depth("") 視為未設 → 退回 env 預設。"""
    monkeypatch.setattr("src.config.TAVILY_MAX_RESULTS", 5)
    monkeypatch.setattr("src.config.TAVILY_SEARCH_DEPTH", "advanced")

    await research("查天氣", max_results=0, search_depth="")

    kw = _FakeTavily.last_kwargs
    assert kw["max_results"] == 5
    assert kw["search_depth"] == "advanced"


@pytest.mark.asyncio
async def test_tavily_answer_prepended(patch_tavily):
    """include_answer 回傳的 answer 置頂於逐條結果之前。"""
    out = await research("查天氣")
    assert "Answer" in out
    assert "天氣晴" in out
    # answer 在第一條結果（T1）之前
    assert out.index("天氣晴") < out.index("T1")


@pytest.mark.asyncio
async def test_tavily_answer_only_no_results(patch_tavily, monkeypatch):
    """只有 answer、無 results 時仍輸出 answer（不誤判為無結果）。"""

    class _AnswerOnly(_FakeTavily):
        def search(self, **kwargs: object) -> dict:
            _FakeTavily.last_kwargs = kwargs
            return {"answer": "只有摘要。", "results": []}

    monkeypatch.setattr(_RESEARCH_MOD, "TavilyClient", _AnswerOnly)
    out = await research("查天氣")
    assert "只有摘要" in out
    assert "No results found" not in out
