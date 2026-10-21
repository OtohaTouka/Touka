"""搜尋備援鏈測試（ADR-0091，Web Search Phase C）。

驗證 `research` 的 auto 鏈（Tavily→SearXNG→DuckDuckGo）與顯式來源：
  - Tavily 不可用時自動降級到 SearXNG、再到 DuckDuckGo。
  - SearXNG/DuckDuckGo 經 httpx JSON API 正確解析（含 DDG 巢狀 RelatedTopics、SearXNG
    answers 字串/物件兩種形狀）。
  - 降級時 emit tool_retry 進度（第 2 個供應者起）。
  - 未知來源回明確錯誤；顯式來源只用指定供應者。
全程以 mock httpx，不打真實網路。
"""

from __future__ import annotations

import sys

import pytest
from src.tools.builtin.research import research

_RESEARCH_MOD = sys.modules["src.tools.builtin.research"]

_SEARXNG_PAYLOAD = {
    "results": [
        {"title": "SX1", "url": "http://sx1", "content": "sx content 1"},
        {"title": "SX2", "url": "http://sx2", "content": "sx content 2"},
    ],
    "answers": ["SX直接答案"],
}

_DDG_PAYLOAD = {
    "Heading": "DDG標題",
    "AbstractText": "DDG摘要",
    "AbstractURL": "http://ddg/abstract",
    "RelatedTopics": [
        {"Text": "RT1", "FirstURL": "http://rt1"},
        {"Topics": [{"Text": "RT2巢狀", "FirstURL": "http://rt2"}]},
    ],
    "Results": [{"Text": "R1", "FirstURL": "http://r1"}],
}


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


def _make_fake_get(searxng_payload: dict = _SEARXNG_PAYLOAD, ddg_payload: dict = _DDG_PAYLOAD):
    async def _fake_get(self, url: str, **kwargs: object) -> _Resp:
        if "duckduckgo" in url:
            return _Resp(ddg_payload)
        return _Resp(searxng_payload)

    return _fake_get


@pytest.mark.asyncio
async def test_auto_falls_back_tavily_to_searxng(monkeypatch):
    """Tavily 無金鑰 → auto 降級到 SearXNG（含 answers 置頂）。"""
    monkeypatch.setattr("src.config.TAVILY_API_KEY", "")
    monkeypatch.setattr("src.config.SEARXNG_BASE_URL", "http://touka-searxng:8080")
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get())

    out = await research("查詢")

    assert "SearXNG" in out
    assert "SX1" in out
    assert "SX直接答案" in out  # answers 置頂


@pytest.mark.asyncio
async def test_auto_falls_back_to_ddg(monkeypatch):
    """Tavily 無金鑰、SearXNG 未設 → auto 降級到 DuckDuckGo（含巢狀 RelatedTopics）。"""
    monkeypatch.setattr("src.config.TAVILY_API_KEY", "")
    monkeypatch.setattr("src.config.SEARXNG_BASE_URL", "")
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get())

    out = await research("查詢")

    assert "DuckDuckGo" in out
    assert "DDG摘要" in out      # AbstractText 作為 answer
    assert "RT2巢狀" in out      # 巢狀 Topics 被攤平
    assert "R1" in out           # Results 收錄


@pytest.mark.asyncio
async def test_auto_emits_fallback_progress(monkeypatch):
    """降級時 emit tool_retry（第 2 個供應者起）：tavily(無)→searxng(無)→ddg(中)。"""
    monkeypatch.setattr("src.config.TAVILY_API_KEY", "")
    monkeypatch.setattr("src.config.SEARXNG_BASE_URL", "")
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get())

    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    out = await research("查詢", _emit_event=_emit)

    retries = [e for e in emitted if e.get("kind") == "tool_retry"]
    assert [e["attempt"] for e in retries] == [2, 3]
    assert all(e["max"] == 3 and e["tool"] == "research" for e in retries)
    assert "DuckDuckGo" in out


@pytest.mark.asyncio
async def test_explicit_searxng(monkeypatch):
    """顯式 source=searxng 只用 SearXNG。"""
    monkeypatch.setattr("src.config.SEARXNG_BASE_URL", "http://sx:8080")
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get())

    out = await research("查詢", source="searxng")

    assert "SearXNG" in out
    assert "SX1" in out


@pytest.mark.asyncio
async def test_explicit_ddg(monkeypatch):
    """顯式 source=ddg 只用 DuckDuckGo。"""
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get())

    out = await research("查詢", source="ddg")

    assert "DuckDuckGo" in out
    assert "R1" in out


@pytest.mark.asyncio
async def test_searxng_answers_object_shape(monkeypatch):
    """SearXNG answers 為物件清單時，取 .answer 欄位。"""
    payload = {
        "results": [{"title": "S", "url": "http://s", "content": "c"}],
        "answers": [{"answer": "物件型答案"}],
    }
    monkeypatch.setattr("src.config.SEARXNG_BASE_URL", "http://sx:8080")
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get(searxng_payload=payload))

    out = await research("查詢", source="searxng")

    assert "物件型答案" in out


@pytest.mark.asyncio
async def test_ddg_malformed_related_topics_survives(monkeypatch):
    """畸形 RelatedTopics（非 dict／Topics 非 list）不應吞掉已收集的 Abstract/Results。"""
    payload = {
        "Heading": "H",
        "AbstractText": "摘要保留",
        "AbstractURL": "http://a",
        "RelatedTopics": [
            "意外的字串",                                  # 非 dict
            {"Text": "正常", "FirstURL": "http://ok"},
            {"Topics": "不是清單"},                        # Topics 非 list
            {"Topics": [{"Text": "巢狀ok", "FirstURL": "http://n"}]},
        ],
        "Results": [{"Text": "R", "FirstURL": "http://r"}],
    }
    monkeypatch.setattr("httpx.AsyncClient.get", _make_fake_get(ddg_payload=payload))

    out = await research("查詢", source="ddg")

    assert "摘要保留" in out   # Abstract 未被畸形項吞掉
    assert "正常" in out
    assert "巢狀ok" in out     # 巢狀正常項仍被攤平收錄
    assert "DuckDuckGo" in out


@pytest.mark.asyncio
async def test_unknown_source_returns_error(monkeypatch):
    """未知來源回明確錯誤訊息。"""
    out = await research("查詢", source="bogus")
    assert "未知的搜尋來源" in out


@pytest.mark.asyncio
async def test_explicit_searxng_unavailable(monkeypatch):
    """顯式 searxng 但未設 URL → 回「不可用」提示（不打網路）。"""
    monkeypatch.setattr("src.config.SEARXNG_BASE_URL", "")
    out = await research("查詢", source="searxng")
    assert "不可用" in out


@pytest.mark.asyncio
async def test_auto_short_circuits_at_tavily(monkeypatch):
    """Tavily 有結果 → auto 立即回傳、不觸發 httpx（不打 SearXNG/DDG）。"""

    class _FakeTavily:
        def __init__(self, api_key: str | None = None) -> None:
            pass

        def search(self, **kwargs: object) -> dict:
            return {"answer": None, "results": [{"title": "TV1", "url": "http://tv", "content": "tc"}]}

    async def _boom(self, *a, **k):
        raise AssertionError("httpx 不應被呼叫——Tavily 已短路勝出")

    monkeypatch.setattr(_RESEARCH_MOD, "TavilyClient", _FakeTavily)
    monkeypatch.setattr("src.config.TAVILY_API_KEY", "fake-key")
    monkeypatch.setattr("httpx.AsyncClient.get", _boom)

    out = await research("查詢")

    assert "Tavily" in out
    assert "TV1" in out
