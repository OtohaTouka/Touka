"""整合式研究與搜尋工具 (Integrated Research Tool).

提供具備備援鏈（ADR-0091）的網路檢索能力：

  - ``auto``（預設）：Tavily → SearXNG（自架/免金鑰/隱私）→ DuckDuckGo（免金鑰）
    依序嘗試，第一個有結果者勝出；任一供應者不可用（缺金鑰/URL）或失敗即跳下一個。
  - 顯式來源（``tavily`` / ``searxng`` / ``ddg``）：只用指定供應者。
  - ``google``：Gemini 原生 Google 搜尋（需 gemini 模型，與上述鏈正交）。

設計：各供應者為模組級 async 純函式，回傳正規化結果 ``{"answer", "items"}`` 或
``None``（不可用）；研究入口負責鏈式嘗試、進度事件與格式化。全程僅依賴 httpx
（SearXNG/DuckDuckGo 皆免金鑰、零新增套件依賴）。
"""

import asyncio
import logging
from typing import Any

import httpx
from src.services import tool_errors
from src.utils.progress_events import tool_retry_event

logger = logging.getLogger(__name__)

# --- [依賴管理：選用搜尋引擎] ---
try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

# 供應者顯示名稱（附在結果開頭，讓模型/使用者知道答案來自哪個後端）。
_PROVIDER_LABELS = {
    "tavily": "Tavily",
    "searxng": "SearXNG（自架/隱私）",
    "ddg": "DuckDuckGo",
}
# auto 模式的嘗試順序：品質優先（Tavily）→ 隱私免金鑰（SearXNG）→ 零設定免金鑰（DDG）。
_AUTO_CHAIN = ["tavily", "searxng", "ddg"]


def _format_results(label: str, normalized: dict[str, Any]) -> str:
    """把正規化結果組成給模型閱讀的文字（answer 置頂、逐條列來源）。"""
    out = [f"🔎 來源：{label}"]
    answer = normalized.get("answer")
    if answer:
        out.append(f"📝 Answer: {answer}")
    for i, r in enumerate(normalized.get("items", [])):
        title = r.get("title") or "No Title"
        url = r.get("url") or "No URL"
        content = r.get("content") or "No Content"
        out.append(f"{i + 1}. {title}\nURL: {url}\nSnippet: {content}")
    return "\n\n".join(out)


def _flatten_ddg_topics(
    topics: list[Any], items: list[dict[str, Any]]
) -> None:
    """攤平 DuckDuckGo RelatedTopics（可能巢狀於 'Topics' 之下），原地收進 items。

    DDG 的 RelatedTopics 是異質清單（扁平結果 dict 與分組 dict 混雜），偶有非 dict
    或 Topics 非 list 的畸形項；逐項以 isinstance 守衛，只跳過畸形項而不中斷整批
    （避免一個怪項害得已收集的 Abstract/Results 全被吞掉）。
    """
    for t in topics:
        if not isinstance(t, dict):
            continue
        sub = t.get("Topics")
        if isinstance(sub, list):
            _flatten_ddg_topics(sub, items)
        elif t.get("Text") and t.get("FirstURL"):
            text = t.get("Text", "")
            items.append({"title": text[:60], "url": t["FirstURL"], "content": text})


async def _search_tavily(query: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Tavily 搜尋（需 TAVILY_API_KEY）。不可用回 None；Phase B 調校沿用。"""
    if TavilyClient is None:
        return None
    try:
        from src.config import (
            TAVILY_API_KEY,
            TAVILY_INCLUDE_ANSWER,
            TAVILY_MAX_RESULTS,
            TAVILY_SEARCH_DEPTH,
        )
        api_key = (TAVILY_API_KEY or "").strip()
        if not api_key:
            return None

        # 調校參數（ADR-0090）：env 為預設 SSOT，允許 per-call kwargs 覆寫
        # （如 deep research 協調器要更廣的結果數）。
        # search_depth/max_results 用 `or`：falsy（0/""）視為「未設定」→ 退回 env 預設
        # （0 筆結果或空深度對 Tavily 無意義）。include_answer 改用 get(default)，
        # 以保留呼叫端顯式傳入的 False。
        search_depth = kwargs.get("search_depth") or TAVILY_SEARCH_DEPTH
        max_results = kwargs.get("max_results") or TAVILY_MAX_RESULTS
        include_answer = kwargs.get("include_answer", TAVILY_INCLUDE_ANSWER)

        client = TavilyClient(api_key=api_key)
        # Tavily SDK 為同步阻塞調用；以 to_thread 卸載至執行緒，避免阻塞事件迴圈
        # （慢端點會讓備援鏈在 SDK 預設逾時前都動彈不得）。
        raw_res = await asyncio.to_thread(
            client.search,
            query=query,
            search_depth=search_depth,
            max_results=max_results,
            include_answer=include_answer,
        )
        items = [
            {
                "title": r.get("title", "No Title"),
                "url": r.get("url", "No URL"),
                "content": r.get("content") or r.get("snippet", "No Content"),
            }
            for r in raw_res.get("results", [])
        ]
    except Exception:
        logger.exception("🔥 Tavily Search Branch Error")
        return None
    else:
        return {"answer": raw_res.get("answer"), "items": items}


async def _search_searxng(query: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """SearXNG 搜尋（自架、免金鑰、查詢不外洩）。未設 SEARXNG_BASE_URL 回 None。"""
    try:
        from src.config import SEARXNG_BASE_URL
        base = (SEARXNG_BASE_URL or "").strip().rstrip("/")
        if not base:
            return None

        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(
                f"{base}/search",
                params={"q": query, "format": "json"},
                timeout=5.0,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = [
            {
                "title": r.get("title", "No Title"),
                "url": r.get("url", "No URL"),
                "content": r.get("content") or "No Content",
            }
            for r in data.get("results", [])
        ]
        # answers 在不同 SearXNG 版本可能是字串或物件清單
        answers = data.get("answers") or []
        answer = None
        if answers:
            first = answers[0]
            answer = first.get("answer") if isinstance(first, dict) else first
    except Exception:
        logger.exception("🔥 SearXNG Search Branch Error")
        return None
    else:
        return {"answer": answer, "items": items}


async def _search_ddg(query: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """DuckDuckGo Instant Answer（官方、免金鑰、零設定）。最後備援。"""
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.get(
                "https://api.duckduckgo.com",
                params={
                    "q": query,
                    "format": "json",
                    "no_html": "1",
                    "no_redirect": "1",
                    "t": "touka_research",
                },
                timeout=5.0,
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        answer = data.get("AbstractText") or data.get("Answer") or None
        items: list[dict[str, Any]] = []
        if data.get("AbstractText") and data.get("AbstractURL"):
            items.append(
                {
                    "title": data.get("Heading") or "Abstract",
                    "url": data.get("AbstractURL"),
                    "content": data.get("AbstractText"),
                }
            )
        _flatten_ddg_topics(data.get("RelatedTopics") or [], items)
        for r in data.get("Results") or []:
            if isinstance(r, dict) and r.get("Text") and r.get("FirstURL"):
                items.append(
                    {
                        "title": r.get("Text", "")[:60],
                        "url": r["FirstURL"],
                        "content": r.get("Text", ""),
                    }
                )
    except Exception:
        logger.exception("🔥 DuckDuckGo Search Branch Error")
        return None
    else:
        return {"answer": answer, "items": items}


async def _search_google(query: str, model: str, emit: Any) -> str:
    """Gemini 原生 Google 搜尋分支（需 gemini 模型）。回傳最終文字。

    只用呼叫者傳入、已確認含 ``gemini`` 的模型送一次：不再改試寫死的舊模型
    清單，也不換模型重試，所以不 emit ``tool_retry``。失敗時依結果回可診斷訊息（429 額度／
    401·403 金鑰無效／其他狀態／連線層例外／200 但無文字），而非一律歸咎額度。
    """
    del emit  # 單次嘗試不發重試進度；參數保留以維持 Guard #45 的 consumer 形狀
    if "gemini" not in model:
        return (
            "Error: Google Search source is only available when using a "
            "Gemini model. Please switch source to 'tavily' or "
            "change your current brain."
        )
    # 延遲匯入（巢狀於 try）：與其他供應者一致，讓測試可精準 monkeypatch
    # src.config.GEMINI_API_KEY，並守住 config 未定義該鍵的情況。
    try:
        from src.config import GEMINI_API_KEY
    except ImportError:
        return "Error: GEMINI_API_KEY not found."
    api_key = GEMINI_API_KEY
    if not api_key:
        return "Error: GEMINI_API_KEY not found."

    payload = {
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )
    async with httpx.AsyncClient(follow_redirects=False) as client:  # 禁令 22
        try:
            # 禁令 23: 金鑰走 x-goog-api-key header（非 URL query/params），
            # 避免 httpx 例外訊息與日誌（含 request.url）洩漏金鑰。
            resp = await client.post(
                url,
                headers={"x-goog-api-key": api_key},
                json=payload,
                timeout=30.0,
            )
        except Exception as e:
            logger.warning("🔥 Google Search 模型 %s 連線失敗：%s", model, type(e).__name__)
            return f"❌ 搜尋失敗：無法連線 Google 服務（{type(e).__name__}）。"

    if resp.status_code == 429:
        return "❌ 搜尋失敗：Google API 額度已達上限（HTTP 429），請稍後再試。"
    if resp.status_code in (401, 403):
        return (
            f"❌ 搜尋失敗：Google API 金鑰無效或無權限（HTTP {resp.status_code}），"
            "請檢查 GEMINI_API_KEY。"
        )
    if resp.status_code != 200:
        logger.warning("🔥 Google Search 模型 %s 回應 HTTP %s", model, resp.status_code)
        return f"❌ 搜尋失敗：Google 服務回應 HTTP {resp.status_code}，目前不可用。"

    try:
        data = resp.json()
        candidates = data.get("candidates") or [{}]
        parts = candidates[0].get("content", {}).get("parts", [])
        res_text = "".join(p.get("text", "") for p in parts if "text" in p)
    except Exception:
        logger.exception("🔥 Google Search 模型 %s 回應解析失敗", model)
        res_text = ""

    if res_text.strip():
        return f"--- Google Search Result ({model}) ---\n\n{res_text}"
    logger.warning("🔥 Google Search 模型 %s 回應為空或無法解析", model)
    return "❌ 搜尋失敗：Google 服務沒有回傳可用的文字結果。"


# 供應者分派表（auto 與顯式來源共用）。置於各 helper 之後以引用其函式物件。
_PROVIDERS = {
    "tavily": _search_tavily,
    "searxng": _search_searxng,
    "ddg": _search_ddg,
}


async def research(query: str, source: str = "auto", **kwargs: Any) -> str:
    """整合式外部調研入口（備援鏈，ADR-0091）.

    Args:
        query (str): 搜尋關鍵字或調研問題.
        source (str): 'auto'（預設，Tavily→SearXNG→DuckDuckGo 備援鏈）、
            'tavily' / 'searxng' / 'ddg'（指定單一供應者）、
            'google'（Gemini 原生搜尋，需 gemini 模型）.
        **kwargs (Any): 其他參數，如 'model'（google 需含 gemini）、
            'search_depth'/'max_results'/'include_answer'（Tavily 覆寫）、
            '_emit_event'（進度回呼）.

    Returns:
        str: 搜尋結果清單、摘要或錯誤訊息.
    """
    model = (kwargs.get("model") or "").lower()
    emit = kwargs.get("_emit_event")

    # --- Google 原生搜尋（與備援鏈正交）---
    if source == "google":
        if not model:  # 禁令 59：沒有模型時回結構化錯誤，不當掉
            return tool_errors.build_tool_error(
                tool_errors.VALIDATION_ERROR,
                "source='google' needs a Gemini model, but no model was given.",
                suggestion="Use source='auto', or set a Gemini primary model.",
                param="model",
            )
        return await _search_google(query, model, emit)

    providers = _AUTO_CHAIN if source == "auto" else [source]
    last_unavailable: str | None = None

    for idx, prov in enumerate(providers):
        handler = _PROVIDERS.get(prov)
        if handler is None:
            return f"Error: 未知的搜尋來源 '{prov}'。可用：auto/tavily/searxng/ddg/google。"

        # 備援進度（第一個供應者不算重試）：沿用 tool_retry 事件形狀。
        if emit and idx > 0:
            await emit(
                tool_retry_event(
                    tool="research", attempt=idx + 1, max_attempts=len(providers)
                )
            )

        normalized = await handler(query, kwargs)
        if normalized is None:
            last_unavailable = prov
            continue
        if normalized.get("items") or normalized.get("answer"):
            return _format_results(_PROVIDER_LABELS.get(prov, prov), normalized)
        # 有跑但空結果 → auto 模式續試下一個供應者

    # 全鏈無可用結果
    if last_unavailable and len(providers) == 1:
        label = _PROVIDER_LABELS.get(last_unavailable, last_unavailable)
        return f"🔎 {label} 目前不可用（未設定金鑰/URL 或暫時失敗）。"
    return (
        "No results found for your query. "
        "建議嘗試更具體的關鍵字，或設定其他搜尋來源（SEARXNG_BASE_URL / TAVILY_API_KEY）。"
    )
