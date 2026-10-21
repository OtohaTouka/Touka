"""TDD 測試：模型 fetcher 的 SSOT 約束驗證。

涵蓋：
- 所有原生 fetcher 必須設定 follow_redirects=False（N8 禁令）
- OpenAI fetcher 過濾器必須含 o4- 系列
- main.py 不得以 os.environ 讀取 ENABLE_DYNAMIC_MODELS
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_DIR = PROJECT_ROOT / "ToukaProxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

from src.services.models.fetcher import (  # noqa: E402
    DiscoveryError,
    fetch_anthropic_models,
    fetch_deepseek_models,
    fetch_gemini_models,
    fetch_grok_models,
    fetch_openai_compatible_models,
    fetch_openai_models,
)

FETCHER_PATH = PROXY_DIR / "src" / "services" / "models" / "fetcher.py"
MAIN_PATH = PROXY_DIR / "src" / "main.py"

# 四個原生 fetcher 函式名稱（不含 fetch_openai_compatible_models）
_NATIVE_FETCHER_NAMES = {
    "fetch_gemini_models",
    "fetch_openai_models",
    "fetch_grok_models",
    "fetch_anthropic_models",
}


def _collect_async_client_calls(tree: ast.AST, func_name: str) -> list[ast.Call]:
    """回傳指定函式內所有 httpx.AsyncClient() 呼叫節點。"""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != func_name:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            # 匹配 httpx.AsyncClient(...)
            if isinstance(func, ast.Attribute) and func.attr == "AsyncClient":
                calls.append(child)
    return calls


def test_all_native_fetchers_set_no_follow_redirects():
    """Gemini / OpenAI / Grok / Anthropic fetcher 必須含 follow_redirects=False。

    N8 禁令：follow_redirects=False 防止 open-redirect SSRF 鏈。
    fetch_openai_compatible_models 已修復，此測試確保四個原生 fetcher 同等合規。
    """
    source = FETCHER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    violations: list[str] = []
    for func_name in _NATIVE_FETCHER_NAMES:
        for call in _collect_async_client_calls(tree, func_name):
            kw_names = {kw.arg for kw in call.keywords}
            if "follow_redirects" not in kw_names:
                violations.append(func_name)

    assert not violations, (
        f"以下 fetcher 的 httpx.AsyncClient 缺少 follow_redirects=False：{violations}"
    )


@pytest.mark.asyncio
async def test_openai_fetcher_includes_o4_models():
    """OpenAI fetcher 的模型過濾器必須含 'o4-'，確保 o4-mini 等新模型能被發現。"""
    api_response = {
        "data": [
            {"id": "gpt-4o"},
            {"id": "o3-mini"},
            {"id": "o4-mini"},        # 目標：不應被過濾掉
            {"id": "whisper-1"},      # 應被過濾掉（非 chat 模型）
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = api_response

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.models.fetcher.httpx.AsyncClient", return_value=mock_client):
        results = await fetch_openai_models("sk-test", blacklist_checker=lambda _: False)

    result_ids = [m["id"] for m in results]
    assert "o4-mini" in result_ids, f"o4-mini 應在結果中，但只有：{result_ids}"
    assert "gpt-4o" in result_ids
    assert "whisper-1" not in result_ids


def test_main_does_not_read_enable_dynamic_models_from_os_environ():
    """main.py 必須改用 config.ENABLE_DYNAMIC_MODELS，不得直接以 os.environ 讀取此旗標。

    根本原因：pydantic-settings 的 env_file 載入不會寫回 os.environ，
    因此 os.environ.get("ENABLE_DYNAMIC_MODELS") 永遠讀到預設值 "false"，
    導致 .env 中的設定被靜默忽略。
    """
    source = MAIN_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 偵測 os.environ.get("ENABLE_DYNAMIC_MODELS", ...)
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
        ):
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "ENABLE_DYNAMIC_MODELS"
        ):
            pytest.fail(
                "main.py 仍以 os.environ.get('ENABLE_DYNAMIC_MODELS') 讀取旗標。"
                "請改用 config.ENABLE_DYNAMIC_MODELS。"
            )


@pytest.mark.asyncio
async def test_gemini_fetcher_version_filtering():
    """Gemini fetcher 應以正則篩選，放行 >= 2.0 版本的模型，過濾 1.5 及以下之模型。"""
    api_response = {
        "models": [
            {
                "name": "models/gemini-2.0-flash",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            },
            {
                "name": "models/gemini-2.5-flash",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            },
            {
                "name": "models/gemini-1.5-flash",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            },
            {
                "name": "models/gemini-3.0-ultra",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            },
            {
                "name": "models/gemini-1.0-pro",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            },
            {
                "name": "models/gemini-2.0-pro-exp-02-05",
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            },
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = api_response

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.services.models.fetcher.httpx.AsyncClient", return_value=mock_client):
        results = await fetch_gemini_models("AIzaSy-test", blacklist_checker=lambda _: False)

    result_ids = [m["id"] for m in results]
    assert "gemini-2.0-flash" in result_ids
    assert "gemini-2.5-flash" in result_ids
    assert "gemini-3.0-ultra" in result_ids
    assert "gemini-2.0-pro-exp-02-05" in result_ids
    assert "gemini-1.5-flash" not in result_ids
    assert "gemini-1.0-pro" not in result_ids


# ---------------------------------------------------------------------------
# E22（ADR-0033，批⑤-A）：fetcher SSRF follow_redirects=False —— 驅動真 client 構造
#
# 既有三守衛都測不到「值為 False」：
#   1. test_batch3_security 的 inspect.getsource 字串鎖（`follow_redirects=True`
#      帶空白即繞、且只掃 fetcher.py 原始碼字面）。
#   2. 本檔 test_all_native_fetchers_set_no_follow_redirects 的 AST 只查 kwarg 是否
#      「存在」（`if "follow_redirects" not in kw_names`），不查其值。
#   3. test_gemini_key_header_security 有查值但 scoped 只 gemini/google client。
# → openai/grok/anthropic/deepseek/generic 五者的 value 從無行為斷言。以下驅動每個
#   fetcher 真的建構 httpx.AsyncClient，斷言 follow_redirects is False；並以 MockTransport
#   演示 302→metadata 不被跟隨（威脅級）。
# ---------------------------------------------------------------------------


class _CaptureClient:
    """假 httpx.AsyncClient：記錄建構 kwargs，get() 回 200 空清單（不觸網）。"""

    captured_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).captured_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        return httpx.Response(200, json={"data": [], "models": []})


_FETCHER_INVOCATIONS = [
    ("gemini", lambda: fetch_gemini_models("k", lambda _: False)),
    ("openai", lambda: fetch_openai_models("k", lambda _: False)),
    ("grok", lambda: fetch_grok_models("k", lambda _: False)),
    ("anthropic", lambda: fetch_anthropic_models("k", lambda _: False)),
    ("deepseek", lambda: fetch_deepseek_models("k", lambda _: False)),
    (
        "openai_compatible",
        lambda: fetch_openai_compatible_models(
            "http://localhost:11434", "k", "ollama", lambda _: False
        ),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,invoke", _FETCHER_INVOCATIONS, ids=[c[0] for c in _FETCHER_INVOCATIONS]
)
async def test_fetcher_constructs_client_with_follow_redirects_false(name, invoke, monkeypatch):
    """每個 fetcher 建構 httpx.AsyncClient 時 follow_redirects 必須 is False（value 級）。

    改任一 fetcher 的 follow_redirects=False→True（或帶空白繞字串鎖）→ 捕獲值非 False → 紅。
    """
    _CaptureClient.captured_kwargs = {}
    monkeypatch.setattr("src.services.models.fetcher.httpx.AsyncClient", _CaptureClient)
    await invoke()
    assert _CaptureClient.captured_kwargs.get("follow_redirects") is False, (
        f"{name} fetcher 的 httpx.AsyncClient 缺 follow_redirects=False"
        f"（實得 {_CaptureClient.captured_kwargs.get('follow_redirects')!r}）"
    )


@pytest.mark.asyncio
async def test_openai_fetcher_does_not_follow_redirect_to_metadata(monkeypatch):
    """威脅級：302 重導到雲端 metadata 時 fetcher 不得跟隨——follow_redirects=False 使
    302 被當非 200（→ DiscoveryError），第二段 metadata URL 從不被請求。
    改 openai fetcher 為 follow_redirects=True → httpx 自動跟隨 → 請求 metadata → 無
    DiscoveryError（回傳偽造模型）→ 兩處斷言皆紅。"""
    requested: list[str] = []
    metadata_host = "169.254.169.254"

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if metadata_host in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "gpt-leaked"}]})
        return httpx.Response(
            302, headers={"Location": f"http://{metadata_host}/latest/meta-data/"}
        )

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client_with_mock(**kwargs):
        kwargs["transport"] = transport
        return orig_client(**kwargs)

    monkeypatch.setattr("src.services.models.fetcher.httpx.AsyncClient", _client_with_mock)

    with pytest.raises(DiscoveryError):
        await fetch_openai_models("sk", blacklist_checker=lambda _: False)
    assert not any(metadata_host in u for u in requested), (
        "follow_redirects 被繞過：fetcher 跟隨 302 請求了 metadata 端點"
    )

