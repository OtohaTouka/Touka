"""Security tests (TDD): Gemini/Google API key must travel in the
``x-goog-api-key`` header, never in the request URL query string.

同 ADR / 禁令 23（敏感資訊消毒）的漏洞類別：把金鑰拼進 ``...?key={api_key}``
會在 httpx 例外訊息與日誌（含 ``request.url``）洩漏金鑰。Google
``generativelanguage`` v1beta 同時支援 ``x-goog-api-key`` header，故所有
Gemini/Google 呼叫一律改走 header、URL 不帶金鑰。

涵蓋檔案：
- ``services/models/fetcher.py``        (fetch_gemini_models)
- ``services/models/health.py``         (Google/XAI 缺 key 早退、有效 key 行為)
- ``handlers/gemini_handler.py``        (generateContent；3b-U3 起主迴圈走 streamGenerateContent)
- ``tools/builtin/research.py``         (_search_google 原生搜尋)
- ``handlers/adapters/sse_stream.py``   (3b-U3 串流解析；只掃「URL 不帶金鑰」「不自建 client」)

並順手驗證這些 httpx client 皆設 ``follow_redirects=False``（禁令 22）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage  # noqa: E402
from src.handlers.adapters.gemini_adapter import GeminiAdapter  # noqa: E402
from src.services.models.fetcher import fetch_gemini_models  # noqa: E402
from src.services.models.health import validate_model_health  # noqa: E402

from tests.helpers.stream_transport import FakeUpstream, Reply, sse  # noqa: E402

# 所有會呼叫 Gemini/Google API 的檔案
GEMINI_CALLER_FILES: dict[str, Path] = {
    "fetcher": PROXY_DIR / "src" / "services" / "models" / "fetcher.py",
    "health": PROXY_DIR / "src" / "services" / "models" / "health.py",
    "gemini_handler": PROXY_DIR / "src" / "handlers" / "adapters" / "gemini_adapter.py",  # E2
    "research": PROXY_DIR / "src" / "tools" / "builtin" / "research.py",
}
# 不呼叫 API、但經手串流內容的模組：只套「URL 不帶金鑰」與「不自建 client」（3b-U3）
_SSE_MODULE = PROXY_DIR / "src" / "handlers" / "adapters" / "sse_stream.py"
_NO_KEY_IN_URL_FILES = {**GEMINI_CALLER_FILES, "sse_stream": _SSE_MODULE}


# ── 行為測試：金鑰進 header、不進 URL ──────────────────────────────────────────


def _extract_url_and_headers(call_args) -> tuple[str, dict]:
    """從 mock call_args 取出 URL（首個位置引數或 url kwarg）與 headers。"""
    url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
    headers = call_args.kwargs.get("headers", {}) or {}
    return str(url), headers


@pytest.mark.asyncio
async def test_fetch_gemini_models_key_in_header_not_url():
    """fetch_gemini_models 必須以 x-goog-api-key header 帶金鑰，URL 不含金鑰。"""
    secret = "AIzaSy-FETCHER-SUPER-SECRET"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": []}

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.models.fetcher.httpx.AsyncClient", return_value=mock_client):
        await fetch_gemini_models(secret, blacklist_checker=lambda _: False)

    mock_client.get.assert_called_once()
    url, headers = _extract_url_and_headers(mock_client.get.call_args)
    assert "key=" not in url, f"金鑰仍在 URL query：{url}"
    assert secret not in url, "金鑰字面值不得出現在 URL"
    assert headers.get("x-goog-api-key") == secret, "金鑰必須走 x-goog-api-key header"


def _mock_health_client(status_code: int) -> AsyncMock:
    response = MagicMock(status_code=status_code)
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


@pytest.mark.asyncio
async def test_gemini_stream_request_key_in_header_not_url(monkeypatch):
    """主迴圈串流請求（3b-U3）：網址帶 alt=sse、不帶金鑰；金鑰只在 x-goog-api-key 標頭。"""
    api_key = "AIzaSy-STREAM-FAKE-KEY"
    done = {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
    upstream = FakeUpstream([Reply(pieces=[sse(done, done=False)])]).install(monkeypatch)
    adapter = GeminiAdapter(stream=True)
    request = OpenAIChatRequest(model="gemini-2.5-flash",
                                messages=[OpenAIMessage(role="user", content="hi")])
    state = await adapter.prepare_request(request, {"gemini_api_key": api_key})
    _ = [item async for item in adapter.send_round(state)]
    await adapter.close(state)
    (sent,) = upstream.requests
    url = str(sent.url)
    assert url.endswith(":streamGenerateContent?alt=sse")
    assert "key=" not in url and api_key not in url
    assert sent.url.params.multi_items() == [("alt", "sse")]
    assert sent.headers["x-goog-api-key"] == api_key


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "", " \t\n"], ids=["none", "empty", "whitespace"])
async def test_validate_model_health_google_missing_key(key: str | None) -> None:
    """缺少 Google key 時，不得建立 HTTP client，即使 mock HTTP 會回 200。"""
    client = _mock_health_client(200)
    models = [{"id": "gemini-2.5-pro", "owned_by": "google"}]
    with (
        patch("src.config.GEMINI_API_KEY", key),
        patch("src.services.models.health.httpx.AsyncClient", return_value=client) as constructor,
    ):
        returned, results = await validate_model_health(models)

    assert returned is models
    assert results == [False]
    constructor.assert_not_called()
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grok_key", "xai_key"),
    [
        pytest.param(None, None, id="none-none"),
        pytest.param(None, "", id="none-empty"),
        pytest.param(None, " \t\n", id="none-whitespace"),
        pytest.param("", None, id="empty-none"),
        pytest.param("", "", id="empty-empty"),
        pytest.param("", " \t\n", id="empty-whitespace"),
        pytest.param(" \t\n", None, id="whitespace-none"),
        pytest.param(" \t\n", "", id="whitespace-empty"),
        pytest.param(" \t\n", " \t\n", id="whitespace-whitespace"),
        pytest.param(" \t\n", "xai-test-key", id="whitespace-precedence"),
    ],
)
async def test_validate_model_health_xai_missing_key(
    grok_key: str | None, xai_key: str | None
) -> None:
    """保留 GROK 優先序；選中的 key 為空時，不得建立 HTTP client。"""
    client = _mock_health_client(200)
    models = [{"id": "grok-3", "owned_by": "xai"}]
    with (
        patch("src.config.GROK_API_KEY", grok_key),
        patch("src.config.XAI_API_KEY", xai_key),
        patch("src.services.models.health.httpx.AsyncClient", return_value=client) as constructor,
    ):
        returned, results = await validate_model_health(models)

    assert returned is models
    assert results == [False]
    constructor.assert_not_called()
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 403])
async def test_validate_model_health_google_key_in_header_not_url(
    status_code: int,
) -> None:
    """validate_model_health 的 google 分支必須走 header，URL 不含金鑰。"""
    secret = " AIzaSy-HEALTH-SUPER-SECRET "
    mock_client = _mock_health_client(status_code)

    models = [{"id": "gemini-2.5-pro", "owned_by": "google"}]
    with (
        patch("src.config.GEMINI_API_KEY", secret),
        patch(
            "src.services.models.health.httpx.AsyncClient", return_value=mock_client
        ) as constructor,
    ):
        returned, results = await validate_model_health(models)

    assert returned is models
    assert results == [status_code == 200]
    constructor.assert_called_once_with(follow_redirects=False)
    mock_client.get.assert_awaited_once_with(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro",
        headers={"x-goog-api-key": secret},
        timeout=5.0,
    )
    url, headers = _extract_url_and_headers(mock_client.get.call_args)
    assert "key=" not in url, f"金鑰仍在 URL query：{url}"
    assert secret not in url, "金鑰字面值不得出現在 URL"
    assert headers.get("x-goog-api-key") == secret, "金鑰必須走 x-goog-api-key header"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 403])
@pytest.mark.parametrize(
    ("grok_key", "selected_key"),
    [
        pytest.param(" grok-test-key ", "grok-test-key", id="grok-precedence"),
        pytest.param(None, "xai-test-key", id="xai-fallback-none"),
        pytest.param("", "xai-test-key", id="xai-fallback-empty"),
    ],
)
async def test_validate_model_health_xai_configured_key(
    status_code: int, grok_key: str | None, selected_key: str
) -> None:
    """有效 XAI key 保留選擇／trim、Bearer header 與 HTTP status 判斷。"""
    client = _mock_health_client(status_code)
    models = [{"id": "grok-3", "owned_by": "xai"}]
    with (
        patch("src.config.GROK_API_KEY", grok_key),
        patch("src.config.XAI_API_KEY", " xai-test-key "),
        patch("src.services.models.health.httpx.AsyncClient", return_value=client) as constructor,
    ):
        returned, results = await validate_model_health(models)

    assert returned is models
    assert results == [status_code == 200]
    constructor.assert_called_once_with(follow_redirects=False)
    client.get.assert_awaited_once_with(
        "https://api.x.ai/v1/models/grok-3",
        headers={"Authorization": f"Bearer {selected_key}"},
        timeout=5.0,
    )


# ── 靜態測試：原始碼層級鎖死金鑰不入 URL ──────────────────────────────────────


@pytest.mark.parametrize("name", list(_NO_KEY_IN_URL_FILES))
def test_no_api_key_in_url_query(name: str):
    """這些檔案的原始碼皆不得把金鑰拼進 URL query（``?key=`` 或 ``params={"key"``）。"""
    src = _NO_KEY_IN_URL_FILES[name].read_text(encoding="utf-8")
    assert "?key=" not in src, f"{name}: 金鑰仍以 ?key= 拼進 URL"
    assert not re.search(r"""params\s*=\s*\{\s*['"]key['"]""", src), (
        f"{name}: 金鑰仍以 params={{'key': ...}} 進入 request.url"
    )


@pytest.mark.parametrize("name", list(GEMINI_CALLER_FILES))
def test_uses_goog_api_key_header(name: str):
    """四個檔案皆必須以 x-goog-api-key header 帶金鑰。"""
    src = GEMINI_CALLER_FILES[name].read_text(encoding="utf-8")
    assert "x-goog-api-key" in src, f"{name}: 未使用 x-goog-api-key header"


# ── 靜態測試：follow_redirects=False（禁令 22）────────────────────────────────


def _async_client_calls(path: Path, func_name: str | None = None) -> list[ast.Call]:
    """收集 httpx.AsyncClient(...) 呼叫；func_name 給定時僅限該函式（含巢狀）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _walk(scope: ast.AST) -> list[ast.Call]:
        out: list[ast.Call] = []
        for child in ast.walk(scope):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "AsyncClient"
            ):
                out.append(child)
        return out

    if func_name is None:
        return _walk(tree)

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == func_name:
            calls.extend(_walk(node))
    return calls


def _sets_no_follow_redirects(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "follow_redirects":
            return isinstance(kw.value, ast.Constant) and kw.value.value is False
    return False


_FOLLOW_REDIRECT_SCOPES = [
    ("fetcher", "fetch_gemini_models"),
    ("health", "validate_model_health"),
    ("gemini_handler", None),  # 檔內唯一 AsyncClient
    ("research", "_search_google"),
]


def test_sse_module_builds_no_http_client():
    """串流解析模組只吃位元組，不自己建 httpx client（3b-U3）。"""
    assert _async_client_calls(_SSE_MODULE) == []
    assert "httpx" not in _SSE_MODULE.read_text(encoding="utf-8")


@pytest.mark.parametrize(("name", "func"), _FOLLOW_REDIRECT_SCOPES)
def test_gemini_clients_set_follow_redirects_false(name: str, func: str | None):
    """禁令 22：Gemini/Google 呼叫的 httpx.AsyncClient 必須 follow_redirects=False。"""
    calls = _async_client_calls(GEMINI_CALLER_FILES[name], func)
    assert calls, f"{name}: 找不到 httpx.AsyncClient 呼叫"
    violations = [c.lineno for c in calls if not _sets_no_follow_redirects(c)]
    assert not violations, (
        f"{name}: 第 {violations} 行的 httpx.AsyncClient 缺少 follow_redirects=False"
    )
