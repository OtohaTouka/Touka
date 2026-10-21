"""R-3a 修正：anthropic SDK 讀的環境變數不得改寫 Touka 的請求（ADR-0247；SECURITY SDK3）。

SDK 1.11 建構 client（含 ``with_options`` 換金鑰的副本）時讀 ``ANTHROPIC_CUSTOM_HEADERS``，能蓋掉
``x-api-key``、加任意標頭；``import anthropic`` 時讀 ``ANTHROPIC_LOG``，``debug`` 會把完整請求寫進
日誌。``src.config`` 載入時清掉這批變數；ToukaMemory 匯入時會以 ``load_dotenv`` 把 ``.env`` 再灌回
``os.environ``，所以三個建構點（主迴圈、factory 換金鑰、``complete_once``）送出前再清一次。
以下用 httpx2 MockTransport 看實際送出的請求（不連網）。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from src import config
from src import main as proxy_main
from src.adapters.schemas import OpenAIChatRequest, OpenAIMessage
from src.models.identity import AgentIdentity
from src.services import completion
from src.services.completion import complete_once
from src.services.dispatchers import factory
from src.services.models.manager import ModelManager
from src.services.models.routes import CustomRouteDisposition, CustomRouteResolution

# 字面清單（不讀產品常數）：SDK 1.11 原始碼裡會改寫目的地、憑證、標頭或日誌的變數。
_STRIPPED = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG",
    "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_SCOPE",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL", "ANTHROPIC_AWS_BASE_URL",
)
_HEADERS = "X-Api-Key: env-key\nanthropic-beta: env-beta\nx-gateway-secret: s3cr3t"
_TOUKA_URL = "https://api.anthropic.com/v1/messages"
_ADMIN = AgentIdentity.for_role(agent_id="DebugAgent", role="admin", is_human=True, capabilities=[])
_MESSAGE = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "好"}], "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


class _Wire:
    """取代 ``DefaultAsyncHttpxClient``：傳輸換成 MockTransport，記下送出的請求。"""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []

    def http_client(self, **kwargs: Any) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            return httpx2.Response(200, json=_MESSAGE)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle), **kwargs)


def _hijack(monkeypatch) -> None:
    """模擬 config 載入之後環境又被灌回（ToukaMemory 的 load_dotenv、使用者 shell）。"""
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", _HEADERS)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env-hijack.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "env-bearer")


def _touka_only(request: httpx2.Request) -> tuple:
    """(目的地, x-api-key 全部值, 有無 env 加的三個標頭)。"""
    headers = request.headers
    return (str(request.url), headers.get_list("x-api-key"), "x-gateway-secret" in headers,
            "anthropic-beta" in headers, "authorization" in headers)


async def _send(client: AsyncAnthropic, text: str = "好") -> None:
    await client.messages.create(
        model="claude-sonnet-4-5", max_tokens=16, messages=[{"role": "user", "content": text}]
    )


def test_strip_pops_sdk_overrides_and_keeps_the_api_key():
    environ = dict.fromkeys(_STRIPPED, "x")
    environ |= {"ANTHROPIC_BASE_URL": "", "ANTHROPIC_API_KEY": "fake-key", "PATH": "p"}
    assert config.strip_anthropic_sdk_env_overrides(environ) == _STRIPPED
    assert environ == {"ANTHROPIC_API_KEY": "fake-key", "PATH": "p"}  # 空字串也拿掉、不留空值
    assert config.strip_anthropic_sdk_env_overrides(environ) == ()



def test_each_stripped_name_is_logged_once(monkeypatch, caplog):
    """ToukaMemory 會反覆把 .env 灌回；同一個名字只記一次 info，避免洗版。"""
    monkeypatch.setattr(config, "_announced_sdk_env_overrides", set())
    with caplog.at_level(logging.INFO, logger="src.config"):
        for environ in ({"ANTHROPIC_SCOPE": "x"}, {"ANTHROPIC_SCOPE": "x"},
                        {"ANTHROPIC_SCOPE": "x", "ANTHROPIC_PROFILE": "p"}):
            config.strip_anthropic_sdk_env_overrides(environ)
    assert [r.getMessage() for r in caplog.records if r.name == "src.config"] == [
        "Ignored anthropic SDK environment overrides: ANTHROPIC_SCOPE",
        "Ignored anthropic SDK environment overrides: ANTHROPIC_PROFILE",
    ]

def test_config_load_already_cleared_the_process_env():
    """conftest 在任何測試前就 import src.config（與 src.main）；shell 帶著這些變數時已被清掉。"""
    assert [name for name in _STRIPPED if name in config.get_full_env()] == []


def test_config_module_strips_right_after_loading_the_env_file():
    """模組層呼叫順序（AST）：先把 .env 灌進環境，再清除；不能只靠 main.py 建 client 時那次。"""
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    calls = [node.value.func.id for node in tree.body
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
             and isinstance(node.value.func, ast.Name)]
    loaded = calls.index("load_env_file_into_environ")
    assert calls[loaded:loaded + 2] == ["load_env_file_into_environ",
                                        "strip_anthropic_sdk_env_overrides"]


async def test_control_custom_headers_replace_the_key_without_the_strip(monkeypatch):
    _hijack(monkeypatch)
    wire = _Wire()
    client = AsyncAnthropic(api_key="fake-key", base_url=config.ANTHROPIC_API_BASE_URL,
                            max_retries=0, http_client=wire.http_client(follow_redirects=False))
    try:
        await _send(client)
    finally:
        await client.close()
    assert _touka_only(wire.seen[0]) == (_TOUKA_URL, ["env-key"], True, True, False)


async def test_main_loop_and_live_key_copy_send_only_touka_headers(monkeypatch):
    wire = _Wire()
    monkeypatch.setattr(proxy_main, "DefaultAsyncHttpxClient", wire.http_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-boot-key")
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-live-key", raising=False)

    async def _noop_stream(_req: Any, _ctx: Any):  # pragma: no cover - 佔位用的 async generator
        if False:
            yield ""

    monkeypatch.setattr(factory, "handle_claude_request", _noop_stream)
    _hijack(monkeypatch)
    boot = proxy_main._build_anthropic_client()
    ctx: dict = {"anthropic_client": boot}
    request = OpenAIChatRequest(
        model="claude-sonnet-4-5", messages=[OpenAIMessage(role="user", content="好")]
    )
    try:
        await _send(boot)
        _hijack(monkeypatch)
        await factory.get_stream_generator("claude-sonnet-4-5", request, _ADMIN, "rid", 0.0, ctx)
        await _send(ctx["anthropic_client"])
    finally:
        await boot.close()
    assert [_touka_only(sent) for sent in wire.seen] == [
        (_TOUKA_URL, ["fake-boot-key"], False, False, False),
        (_TOUKA_URL, ["fake-live-key"], False, False, False),
    ]


async def test_complete_once_sends_only_touka_headers(monkeypatch):
    wire = _Wire()
    monkeypatch.setattr(completion, "DefaultAsyncHttpxClient", wire.http_client)
    monkeypatch.setattr(ModelManager, "get_models", lambda _self: [])
    monkeypatch.setattr(ModelManager, "resolve_custom_route", lambda _self, _m:
                        CustomRouteResolution(disposition=CustomRouteDisposition.UNKNOWN))
    monkeypatch.setattr("src.config.ANTHROPIC_API_KEY", "fake-anthropic", raising=False)
    monkeypatch.setattr(completion, "autonomy_service",
                        SimpleNamespace(is_over_budget=lambda _identity: False,
                                        would_exceed=lambda _identity, _worst: False))
    monkeypatch.setattr(completion, "accumulate_token_usage", lambda *_a, **_k: None)
    monkeypatch.setattr(completion, "log_usage_audit", lambda *_a, **_k: None)
    _hijack(monkeypatch)

    result = await complete_once(AgentIdentity(agent_id="DebugAgent"), "reviewer",
                                 "claude-sonnet-4-5", "審查員", "判斷", 256, 5.0)

    assert result.ok
    assert [_touka_only(sent) for sent in wire.seen] == [
        (_TOUKA_URL, ["fake-anthropic"], False, False, False)
    ]


async def test_anthropic_log_debug_cannot_dump_request_bodies(monkeypatch):
    """SDK 先於 config 載入（main.py 的匯入順序）時已依 ANTHROPIC_LOG 調到 DEBUG；清除時 anthropic
    還原、httpx2 釘在 WARNING（3b-U4a，ADR-0248 D4）。"""
    sdk_loggers = [logging.getLogger("anthropic"), logging.getLogger("httpx2")]
    original = [lg.level for lg in sdk_loggers]
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append  # type: ignore[method-assign]
    sdk_loggers[0].addHandler(handler)
    wire = _Wire()
    monkeypatch.setattr(proxy_main, "DefaultAsyncHttpxClient", wire.http_client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-boot-key")
    try:
        monkeypatch.setenv("ANTHROPIC_LOG", "debug")
        for lg in sdk_loggers:
            lg.setLevel(logging.DEBUG)  # 等同 anthropic._utils._logs.setup_logging() 的效果
        assert "ANTHROPIC_LOG" in config.strip_anthropic_sdk_env_overrides()
        levels_after_strip = [lg.level for lg in sdk_loggers]
        client = proxy_main._build_anthropic_client()
        try:
            await _send(client, "INCOGNITO-SECRET-TEXT")
        finally:
            await client.close()
    finally:
        sdk_loggers[0].removeHandler(handler)
        for lg, level in zip(sdk_loggers, original, strict=True):
            lg.setLevel(level)
    assert len(wire.seen) == 1
    assert "ANTHROPIC_LOG" not in config.get_full_env()
    assert levels_after_strip == [logging.NOTSET, logging.WARNING]
    assert not any("INCOGNITO-SECRET-TEXT" in record.getMessage() for record in records)
