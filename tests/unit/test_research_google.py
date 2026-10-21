"""Google 原生搜尋分支測試（research.py；ADR-0091 收尾＋改用呼叫者模型）。

驗證 `_search_google`：
  - 非 gemini 模型 / 缺 GEMINI_API_KEY → 明確錯誤，且**不打網路**。
  - **只用呼叫者傳入的 Gemini 模型送一次**：URL 含該模型、不再換寫死的舊模型清單重試，
    也不 emit tool_retry 進度。
  - 失敗時依狀態給可診斷訊息：429 額度、401·403 金鑰/權限、其他狀態、連線層例外、
    200 但無文字——而非一律歸咎額度。
全程 mock `httpx.AsyncClient.post`，不打真實網路。
"""
from __future__ import annotations

import json
import sys

import httpx
import pytest
from src.tools.builtin.research import research

_RESEARCH_MOD = sys.modules["src.tools.builtin.research"]

# 刻意選一個不在舊寫死清單（2.5/2.0/1.5 flash、2.0-flash-exp）中的模型名。
_CALLER_MODEL = "gemini-3-pro-preview"


class _GResp:
    """模擬 Gemini generateContent 回應：可帶狀態碼與單一 text part。"""

    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self._text = text

    def json(self) -> dict:
        return {"candidates": [{"content": {"parts": [{"text": self._text}]}}]}


def _make_fake_post(behaviors: list, calls: list[str]):
    """依序套用 behaviors 於每次 post 呼叫（超出長度沿用最後一個），並記錄每次的 URL。

    元素可為：Exception 實例（拋出，模擬連線層失敗）或 (status, text) tuple。
    """

    async def _fake_post(self, url, **kwargs):  # noqa: ANN001 — 測試替身
        i = len(calls)
        calls.append(url)
        b = behaviors[i] if i < len(behaviors) else behaviors[-1]
        if isinstance(b, Exception):
            raise b
        status, text = b
        return _GResp(status, text)

    return _fake_post


async def _run_google(monkeypatch, behaviors, *, model: str = _CALLER_MODEL):
    """以給定 post 行為跑一次 google 搜尋，回傳 (輸出, 送出的 URL, 重試事件)。"""
    monkeypatch.setattr("src.config.GEMINI_API_KEY", "fake-key")
    calls: list[str] = []
    monkeypatch.setattr("httpx.AsyncClient.post", _make_fake_post(behaviors, calls))
    emitted: list[dict] = []

    async def _emit(ev):
        emitted.append(ev)

    out = await research("查詢", source="google", model=model, _emit_event=_emit)
    retries = [e for e in emitted if e.get("kind") == "tool_retry"]
    return out, calls, retries


@pytest.mark.asyncio
async def test_google_non_gemini_model_rejected(monkeypatch):
    """source=google 但非 gemini 模型 → 引導錯誤，且不打網路。"""

    async def _boom(self, *a, **k):
        raise AssertionError("非 gemini 模型不應打網路")

    monkeypatch.setattr("httpx.AsyncClient.post", _boom)
    out = await research("查詢", source="google", model="claude-opus-4-8")
    assert "Gemini model" in out


@pytest.mark.asyncio
async def test_google_missing_api_key(monkeypatch):
    """gemini 模型但缺 GEMINI_API_KEY → 明確錯誤，且不打網路。"""

    async def _boom(self, *a, **k):
        raise AssertionError("缺金鑰不應打網路")

    monkeypatch.setattr("src.config.GEMINI_API_KEY", "")
    monkeypatch.setattr("httpx.AsyncClient.post", _boom)
    out = await research("查詢", source="google", model="gemini-2.5-flash")
    assert "GEMINI_API_KEY not found" in out


@pytest.mark.asyncio
async def test_google_uses_caller_model_once(monkeypatch):
    """成功：只 POST 一次，URL 用呼叫者的模型，結果標註該模型、不 emit 重試。"""
    out, calls, retries = await _run_google(monkeypatch, [(200, "Gemini 答案")])
    assert "Gemini 答案" in out
    assert len(calls) == 1
    assert calls[0].endswith(f"/models/{_CALLER_MODEL}:generateContent")
    assert _CALLER_MODEL in out
    assert not retries


@pytest.mark.asyncio
async def test_google_caller_model_is_lowercased_id(monkeypatch):
    """呼叫者模型名大小寫不一時，以小寫 API ID 送出（research 入口既有正規化）。"""
    _, calls, _ = await _run_google(monkeypatch, [(200, "ok")], model="Gemini-2.5-Pro")
    assert calls == [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
    ]


@pytest.mark.asyncio
async def test_google_connection_error_does_not_switch_model(monkeypatch):
    """連線逾時 → 不再換別的模型重試：只送一次、無重試事件、訊息點名無法連線。"""
    out, calls, retries = await _run_google(
        monkeypatch, [httpx.ConnectTimeout("逾時"), (200, "不該被用到")]
    )
    assert len(calls) == 1
    assert not retries
    assert "無法連線" in out and "ConnectTimeout" in out
    assert "不該被用到" not in out


@pytest.mark.asyncio
async def test_google_non200_does_not_switch_model(monkeypatch):
    """回 503 → 不續試別的模型；訊息誠實附上狀態碼。"""
    out, calls, retries = await _run_google(monkeypatch, [(503, ""), (200, "不該被用到")])
    assert len(calls) == 1
    assert not retries
    assert "503" in out
    assert "不該被用到" not in out


@pytest.mark.asyncio
async def test_google_empty_text_is_failure(monkeypatch):
    """200 但只有空白文字 → 不續試，回可讀的失敗訊息。"""
    out, calls, _ = await _run_google(monkeypatch, [(200, "   "), (200, "不該被用到")])
    assert len(calls) == 1
    assert "搜尋失敗" in out
    assert "不該被用到" not in out


@pytest.mark.asyncio
async def test_google_quota_exceeded(monkeypatch):
    """429 → 失敗訊息點名額度(HTTP 429)，不誤報為連線問題。"""
    out, calls, _ = await _run_google(monkeypatch, [(429, "")])
    assert len(calls) == 1
    assert "額度" in out and "429" in out


@pytest.mark.asyncio
async def test_google_auth_error(monkeypatch):
    """403 → 失敗訊息點名金鑰/權限(HTTP 403)，不誤報為額度。"""
    out, _, _ = await _run_google(monkeypatch, [(403, "")])
    assert "金鑰" in out and "403" in out


@pytest.mark.asyncio
async def test_google_other_status(monkeypatch):
    """其他狀態(500) → 訊息誠實附上狀態碼，不歸咎額度。"""
    out, _, _ = await _run_google(monkeypatch, [(500, "")])
    assert "500" in out
    assert "額度" not in out


@pytest.mark.asyncio
async def test_google_connection_error(monkeypatch):
    """連線層例外 → 失敗訊息點名無法連線。"""
    out, _, _ = await _run_google(monkeypatch, [httpx.ConnectError("x")])
    assert "無法連線" in out


async def _no_network(self, *a, **k):  # noqa: ANN001 — 測試替身
    raise AssertionError("沒有模型時不應打網路")


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"model": None}, {}, {"model": ""}])
async def test_google_without_model_returns_structured_validation_error(monkeypatch, kwargs):
    """ADR-0250 C5：source=google 但沒有模型 → 結構化 validation_error（param=model），不當掉。"""
    monkeypatch.setattr("httpx.AsyncClient.post", _no_network)
    out = await research("查詢", source="google", **kwargs)
    payload = json.loads(out)
    assert list(payload)[0] == "error_class"
    assert payload["error_class"] == "validation_error"
    assert payload["param"] == "model"


@pytest.mark.asyncio
async def test_auto_without_model_does_not_crash(monkeypatch):
    """ADR-0250 C5：source=auto 用不到模型；model=None 時照常走備援鏈（供應者 stub）。"""
    calls: list[str] = []

    async def _stub(query, kwargs):  # noqa: ANN001 — 測試替身
        calls.append(query)
        return {"answer": "stub-answer", "items": []}

    monkeypatch.setattr("httpx.AsyncClient.post", _no_network)
    monkeypatch.setitem(_RESEARCH_MOD._PROVIDERS, "tavily", _stub)
    out = await research("查詢", source="auto", model=None)
    assert calls == ["查詢"]
    assert "stub-answer" in out
