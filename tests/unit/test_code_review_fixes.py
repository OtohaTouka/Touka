"""2026-06-11 ToukaProxy Code Review 修復 — TDD 結構性與行為測試。

涵蓋九項已確認問題（依審查報告編號）：
  #1 AgentIdentity 欄位字串預設值必須經驗證（heartbeat / debug 請求模型）
  #2 openai_handler 防幻覺 join 對 content=None 必須安全
  #3 三家 handler 的 System 身分豁免必須一致
  #4 Gemini→OpenAI finish_reason 對映必須符合 OpenAI 合約（含 model 透傳）
  #5 ModelHealthResponse.model_id 必須是 str 而非 AgentIdentity
  #6 reset_quota 必須持 _autonomy_lock
  #7 dispatcher 非串流重組對空 choices 必須防禦
  #8 MCPHubService 對 hub JSON 缺鍵條目必須跳過/拒絕而非 KeyError
  #9 AuthSecretManager.get_secret 首次生成必須執行緒安全
  #10（次要觀察修復）custom_models 變更端點必須以 per-model-id 鎖串行化（TOCTOU）
  #11（次要觀察修復）MCP Hub npm 下載量誠實化：失敗回 None 不偽造、真 0 保留、
      無數據省略欄位（前端顯示「精選」）、排序退回 popularity
"""
import asyncio
import threading
from pathlib import Path

import pytest

from src.adapters.gemini_to_openai import convert_gemini_to_openai
from src.core import security as security_mod
from src.handlers.adapters.openai_adapter import OpenAIAdapter
from src.models.custom_models import CustomModelRenameRequest, CustomModelUpdateRequest
from src.models.identity import AgentIdentity
from src.routers import custom_models as custom_models_router
from src.routers import mcp as mcp_router
from src.routers.debug import ToolExecuteRequest
from src.routers.heartbeat import HeartbeatAwakenRequest
from src.routers.models import ModelHealthResponse
from src.services.autonomy import autonomy_service
from src.services.dispatcher import _reassemble_final_json
from src.services.mcp_hub_service import MCPHubService

ROOT = Path(__file__).parent.parent.parent
HANDLERS_DIR = ROOT / "ToukaProxy" / "src" / "handlers"
SECURITY_PY = ROOT / "ToukaProxy" / "src" / "core" / "security.py"

# ADR-0146：舊「System 身分豁免消耗配額」守衛隨 handler 端配額消耗一併退場
# （配額只在 heartbeat pulse 派發時扣）。後繼跨場景一致性不變式：三家 handler
# 都以同一資源解析器初始化工具輪預算、且不得在 handler 內扣每日配額。
ROUNDS_INIT_GUARD = "rhythm_service.get_effective_rounds("


# ---------------------------------------------------------------------------
# #1 AgentIdentity 字串預設值必須經驗證（validate_default）
# ---------------------------------------------------------------------------

def test_heartbeat_awaken_request_default_agent_id_is_identity_object():
    """省略 agent_id 時，預設值必須被驗證為 AgentIdentity 物件（而非裸字串）。"""
    req = HeartbeatAwakenRequest(token="t")
    assert isinstance(req.agent_id, AgentIdentity), (
        "HeartbeatAwakenRequest.agent_id 預設值未經驗證，仍是 str —— "
        "awaken 端點將在 req.agent_id.agent_id 處 AttributeError"
    )
    assert req.agent_id.agent_id == "DebugAgent"


def test_debug_tool_execute_request_default_agent_id_is_identity_object():
    """ToolExecuteRequest 省略 agent_id 時同樣必須得到 AgentIdentity 物件。"""
    req = ToolExecuteRequest(tool_name="noop")
    assert isinstance(req.agent_id, AgentIdentity)
    assert req.agent_id.agent_id == "DebugAgent"


# ---------------------------------------------------------------------------
# #2 防幻覺 join 對 content=None 安全
# ---------------------------------------------------------------------------

def test_extract_response_text_handles_none_content():
    """OpenAI 規格：assistant 帶 tool_calls 時 content 為 null —— join 不得 TypeError。

    E1 後邏輯歸 OpenAIAdapter.extract_final_text（openai_handler＝薄殼）。
    """
    final_response = {
        "choices": [
            {"message": {"content": None, "tool_calls": [{"id": "c1"}]}},
        ]
    }
    assert OpenAIAdapter().extract_final_text(None, final_response) == ""


def test_extract_response_text_normal_and_edge_cases():
    adapter = OpenAIAdapter()
    assert adapter.extract_final_text(None, {"choices": [{"message": {"content": "哈囉"}}]}) == "哈囉"
    assert adapter.extract_final_text(None, {"choices": []}) == ""
    assert adapter.extract_final_text(None, {"choices": [{}]}) == ""


# ---------------------------------------------------------------------------
# #3 三家 handler 的 System 身分豁免一致
# ---------------------------------------------------------------------------

# E1（react-core-extraction）後：openai 的預算初始化收進 react_core（單一實作＝
# 抽取目的）；claude/gemini 於 E2/E3 遷移後自本清單移除、終態只剩 react_core.py。
@pytest.mark.parametrize(
    "handler_file",
    ["react_core.py"],  # E3 後三家皆進 core＝預算初始化單源於此
)
def test_handlers_share_system_identity_guard(handler_file: str):
    """迴圈宿主的工具輪預算初始化寫法一致，且不得在其內扣每日配額
    （ADR-0146：配額只計無人 pulse；AGENTS §13 跨場景一致性）。"""
    content = (HANDLERS_DIR / handler_file).read_text(encoding="utf-8")
    assert ROUNDS_INIT_GUARD in content, (
        f"{handler_file} 缺少工具輪預算初始化：{ROUNDS_INIT_GUARD}"
    )
    assert "consume_quota" not in content, (
        f"{handler_file} 不得於 handler 內扣每日配額（配額只計 heartbeat pulse）"
    )


# ---------------------------------------------------------------------------
# #4 Gemini→OpenAI finish_reason 對映
# ---------------------------------------------------------------------------

def _gemini_response(finish_reason: str, parts: list[dict] | None = None) -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": parts if parts is not None else [{"text": "hi"}]},
                "finishReason": finish_reason,
            }
        ]
    }


def test_gemini_max_tokens_maps_to_length():
    result = convert_gemini_to_openai(_gemini_response("MAX_TOKENS"))
    assert result["choices"][0]["finish_reason"] == "length"


def test_gemini_safety_maps_to_content_filter():
    result = convert_gemini_to_openai(_gemini_response("SAFETY"))
    assert result["choices"][0]["finish_reason"] == "content_filter"


def test_gemini_unknown_reason_falls_back_to_stop():
    result = convert_gemini_to_openai(_gemini_response("BLOCKLIST"))
    assert result["choices"][0]["finish_reason"] == "stop"


def test_gemini_stop_with_tool_calls_maps_to_tool_calls():
    parts = [{"functionCall": {"name": "vault_tools", "args": {"action": "list"}}}]
    result = convert_gemini_to_openai(_gemini_response("STOP", parts))
    assert result["choices"][0]["finish_reason"] == "tool_calls"


def test_gemini_adapter_passes_through_model_name():
    """回應的 model 欄位必須反映實際模型，而非硬編碼字串。"""
    result = convert_gemini_to_openai(
        _gemini_response("STOP"), model_name="gemini-2.5-pro"
    )
    assert result["model"] == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# #5 ModelHealthResponse.model_id 必須是 str
# ---------------------------------------------------------------------------

def test_model_health_response_model_id_is_plain_string():
    """model_id 是模型識別碼而非身分；序列化必須是字串，不得展開成 AgentIdentity 物件。"""
    resp = ModelHealthResponse(model_id="gpt-4o", status="healthy")
    dumped = resp.model_dump()
    assert isinstance(dumped["model_id"], str), (
        f"model_id 序列化成 {type(dumped['model_id']).__name__}，"
        "API 合約應為純字串"
    )
    assert dumped["model_id"] == "gpt-4o"


# ---------------------------------------------------------------------------
# #6 reset_quota 必須持 _autonomy_lock
# ---------------------------------------------------------------------------

class _SpyLock:
    """記錄進入次數的鎖替身（context manager 介面）。"""

    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> "_SpyLock":
        self.entered += 1
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_reset_quota_acquires_autonomy_lock(monkeypatch):
    """reset_quota 的 load-modify-save 必須與其他配額方法共用 _autonomy_lock（P18）。"""
    spy = _SpyLock()
    monkeypatch.setattr("src.services.autonomy._autonomy_lock", spy)

    saved: dict = {}
    monkeypatch.setattr(
        autonomy_service, "_load_logs", lambda _i: {"daily_count": 3}
    )
    monkeypatch.setattr(
        autonomy_service,
        "_save_logs",
        lambda _i, logs: saved.update(logs),
    )

    autonomy_service.reset_quota(AgentIdentity(agent_id="TestAgent"))

    assert spy.entered >= 1, "reset_quota 未持 _autonomy_lock，與 consume_quota 競態"
    assert saved.get("daily_count") == 0


# ---------------------------------------------------------------------------
# #7 dispatcher 非串流重組對空 choices 防禦
# ---------------------------------------------------------------------------

def test_reassemble_final_json_with_empty_choices_does_not_raise():
    """供應商終端 chunk 可能是 "choices": []（如 usage chunk）—— 不得 IndexError。"""
    last_json = {"choices": [], "usage": {"total_tokens": 5}}
    result = _reassemble_final_json(last_json, "full text")
    assert result["choices"] == []


def test_reassemble_final_json_normal_path():
    last_json = {"choices": [{"delta": {"content": "x"}}]}
    result = _reassemble_final_json(last_json, "整合後內容")
    assert result["choices"][0]["message"] == {
        "role": "assistant",
        "content": "整合後內容",
    }
    assert "delta" not in result["choices"][0]


def test_reassemble_final_json_without_choices_key():
    assert _reassemble_final_json({"type": "ping"}, "x") == {"type": "ping"}


# ---------------------------------------------------------------------------
# #8 MCPHubService 對缺鍵條目防禦
# ---------------------------------------------------------------------------

class _FakeResourceManager:
    """路徑感知的 resource_manager 替身：以路徑字串對映 JSON 內容。"""

    def __init__(self, files: dict[str, dict]) -> None:
        self.files = files
        self.written: dict[str, dict] = {}

    def read_json(self, path: str) -> dict | None:
        return self.files.get(str(path))

    def write_json(self, path: str, data: dict) -> None:
        self.written[str(path)] = data

    def make_dir(self, _path: object) -> None:
        return None


@pytest.mark.asyncio
async def test_hub_sync_skips_entries_missing_name(monkeypatch):
    """sync 合併時遇到缺 name 的條目必須跳過該筆，不得 KeyError 使整批失敗。"""
    fake_rm = _FakeResourceManager(
        {
            "hub.json": {
                "hub": [
                    {"source": "curated"},  # 缺 name 的損毀條目
                    {"name": "Good Curated", "source": "curated"},
                ]
            }
        }
    )
    monkeypatch.setattr("src.services.mcp_hub_service.resource_manager", fake_rm)

    svc = MCPHubService(hub_path="hub.json", config_path="cfg.json")

    async def _official() -> list[dict]:
        return [{"name": "Official One", "source": "official"}]

    async def _registry() -> list[dict]:
        return []

    monkeypatch.setattr(svc, "fetch_official_servers", _official)
    monkeypatch.setattr(svc, "fetch_registry_servers", _registry)

    result = await svc.sync()

    assert result["status"] == "success"
    names = [s["name"] for s in fake_rm.written["hub.json"]["hub"]]
    assert "Good Curated" in names
    assert "Official One" in names


@pytest.mark.asyncio
async def test_hub_install_rejects_entry_missing_command(monkeypatch):
    """install 目標缺 command/args 時必須回傳 invalid_entry 且不寫入設定檔。"""
    fake_rm = _FakeResourceManager(
        {"hub.json": {"hub": [{"id": "bad-1", "name": "Broken Entry"}]}}
    )
    monkeypatch.setattr("src.services.mcp_hub_service.resource_manager", fake_rm)

    svc = MCPHubService(hub_path="hub.json", config_path="cfg.json")
    result = await svc.install("bad-1", {})

    assert result["status"] == "invalid_entry"
    assert "cfg.json" not in fake_rm.written


# ---------------------------------------------------------------------------
# #9 AuthSecretManager.get_secret 執行緒安全
# ---------------------------------------------------------------------------

def test_get_secret_uses_class_lock():
    """get_secret 的延遲生成必須以鎖保護（double-checked locking）。"""
    content = SECURITY_PY.read_text(encoding="utf-8")
    assert "with cls._lock" in content, (
        "AuthSecretManager.get_secret 未持鎖：dev 模式下並行首呼可能各生成不同密鑰"
    )


def test_get_secret_concurrent_first_call_yields_single_secret(monkeypatch):
    """16 執行緒同時首呼 get_secret，必須拿到同一把密鑰。"""
    original = security_mod.AuthSecretManager._secret
    monkeypatch.setattr(security_mod, "TOUKA_AUTH_SECRET", "")
    security_mod.AuthSecretManager._secret = None
    try:
        barrier = threading.Barrier(16)
        results: list[str] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            secret = security_mod.AuthSecretManager.get_secret()
            with results_lock:
                results.append(secret)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 1, "並行首呼產生了多把不同密鑰"
    finally:
        security_mod.AuthSecretManager._secret = original


# ---------------------------------------------------------------------------
# #10（次要觀察修復）custom_models 變更端點的 per-model-id 鎖（TOCTOU）
# ---------------------------------------------------------------------------

def _patch_custom_model_io(monkeypatch, calls: list[str]):
    """以無副作用替身隔離 custom_models 端點的檔案與使用者操作。"""
    monkeypatch.setattr(
        custom_models_router.custom_io, "custom_file_exists", lambda *_a: False
    )
    monkeypatch.setattr(
        custom_models_router.custom_model_service,
        "save_model",
        lambda *_a, **_k: calls.append("save"),
    )
    monkeypatch.setattr(
        custom_models_router.user_service,
        "claim_owned_agent",
        lambda *_a, **_k: calls.append("own"),
    )


def test_model_lock_is_per_model_id():
    """同一 model_id 必須拿到同一把鎖；不同 model_id 互不阻塞。"""
    custom_models_router._model_locks.clear()
    lock_a1 = custom_models_router._get_model_lock("model-a")
    lock_a2 = custom_models_router._get_model_lock("model-a")
    lock_b = custom_models_router._get_model_lock("model-b")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


@pytest.mark.asyncio
async def test_update_custom_model_respects_model_lock(monkeypatch):
    """外部持鎖期間，update_custom_model 的「查存在→寫入→綁定」必須阻塞等待。

    這保證兩個使用者毫秒級搶建同一新 ID 時，check-then-act 不會交錯
    （後到者會看到 exists=True 並走擁有權驗證）。
    """
    calls: list[str] = []
    _patch_custom_model_io(monkeypatch, calls)
    custom_models_router._model_locks.clear()

    ident = AgentIdentity.for_role(agent_id="human1", role="admin", is_human=True)
    data = CustomModelUpdateRequest(systemPrompt="persona")

    lock = custom_models_router._get_model_lock("race-model")
    await lock.acquire()
    try:
        task = asyncio.create_task(
            custom_models_router.update_custom_model("race-model", data, ident)
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "update_custom_model 未受 per-model-id 鎖串行化"
        assert calls == [], "持鎖期間不得進入臨界區"
    finally:
        lock.release()

    result = await task
    assert result["status"] == "success"
    assert calls == ["save", "own"]


@pytest.mark.asyncio
async def test_delete_custom_model_respects_model_lock(monkeypatch):
    """刪除端點同樣必須持同一把 per-model-id 鎖（與建立互相串行）。"""
    monkeypatch.setattr(
        custom_models_router.resource_manager, "is_owner", lambda *_a: True
    )
    monkeypatch.setattr(
        custom_models_router.custom_model_service, "delete_model", lambda *_a: True
    )
    monkeypatch.setattr(
        custom_models_router.user_service,
        "remove_owned_agent",
        lambda *_a, **_k: None,
    )
    custom_models_router._model_locks.clear()

    ident = AgentIdentity.for_role(agent_id="human1", role="admin", is_human=True)
    lock = custom_models_router._get_model_lock("doomed-model")
    await lock.acquire()
    try:
        task = asyncio.create_task(
            custom_models_router.delete_custom_model("doomed-model", ident)
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "delete_custom_model 未受 per-model-id 鎖串行化"
    finally:
        lock.release()

    result = await task
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_rename_custom_model_locks_both_ids(monkeypatch):
    """改名涉及兩個 ID（舊與新），必須兩把鎖都取得（排序取得防死鎖）。"""
    monkeypatch.setattr(
        custom_models_router.resource_manager, "is_owner", lambda *_a: True
    )
    monkeypatch.setattr(
        custom_models_router.custom_io, "list_custom_ids", lambda: ["old-name"]
    )
    monkeypatch.setattr(
        custom_models_router.custom_model_service,
        "rename_model",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        custom_models_router.user_service,
        "rename_owned_agent",
        lambda *_a, **_k: None,
    )
    custom_models_router._model_locks.clear()

    ident = AgentIdentity.for_role(agent_id="human1", role="admin", is_human=True)
    data = CustomModelRenameRequest(new_id="new-name")

    # 只持「新 ID」的鎖：rename 也必須被擋住（防止 rename 與搶建新 ID 競態）
    lock_new = custom_models_router._get_model_lock("new-name")
    await lock_new.acquire()
    try:
        task = asyncio.create_task(
            custom_models_router.rename_custom_model("old-name", data, ident)
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "rename_custom_model 未取得新 ID 的鎖"
    finally:
        lock_new.release()

    result = await task
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# #11（次要觀察修復）MCP Hub npm 下載量誠實化
# ---------------------------------------------------------------------------

def _make_fake_async_client(response: object = None, exc: Exception | None = None):
    """產生 httpx.AsyncClient 替身類別（async context manager + get）。"""

    class _FakeClient:
        def __init__(self, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def get(self, _url: str) -> object:
            if exc is not None:
                raise exc
            return response

    return _FakeClient


class _FakeNpmResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_fetch_npm_downloads_returns_real_counts(monkeypatch):
    """200 回應的真實數字（含真 0）必須原樣回傳。"""
    monkeypatch.setattr(
        mcp_router.httpx,
        "AsyncClient",
        _make_fake_async_client(_FakeNpmResponse(200, {"downloads": 1234})),
    )
    assert await mcp_router.fetch_npm_downloads("pkg-real") == 1234

    monkeypatch.setattr(
        mcp_router.httpx,
        "AsyncClient",
        _make_fake_async_client(_FakeNpmResponse(200, {"downloads": 0})),
    )
    assert await mcp_router.fetch_npm_downloads("pkg-zero") == 0


@pytest.mark.asyncio
async def test_fetch_npm_downloads_returns_none_when_unknown(monkeypatch):
    """非 200／網路例外／非法套件名 → None（未知），不得與真 0 混淆。"""
    monkeypatch.setattr(
        mcp_router.httpx,
        "AsyncClient",
        _make_fake_async_client(_FakeNpmResponse(404)),
    )
    assert await mcp_router.fetch_npm_downloads("pkg-404") is None

    monkeypatch.setattr(
        mcp_router.httpx,
        "AsyncClient",
        _make_fake_async_client(exc=RuntimeError("network down")),
    )
    assert await mcp_router.fetch_npm_downloads("pkg-err") is None

    assert await mcp_router.fetch_npm_downloads("../evil") is None


@pytest.mark.asyncio
async def test_hub_endpoint_does_not_fabricate_downloads(monkeypatch):
    """Hub 端點：失敗不偽造 100、真 0 保留、非 npm 條目不把 popularity 冒充下載量。"""
    hub = [
        {"id": "a", "name": "A", "command": "npx", "args": [], "npm_package": "pkg-a", "popularity": 10},
        {"id": "b", "name": "B", "command": "npx", "args": [], "npm_package": "pkg-b", "popularity": 50000},
        {"id": "c", "name": "C", "command": "npx", "args": [], "popularity": 999999},
        {"id": "d", "name": "D", "command": "npx", "args": [], "npm_package": "pkg-d", "popularity": 5},
    ]
    monkeypatch.setattr(
        mcp_router.resource_manager, "read_json", lambda _p: {"hub": hub}
    )
    monkeypatch.setattr(mcp_router, "NPM_CACHE", {})
    monkeypatch.setattr(mcp_router, "NPM_CACHE_TIME", 0)

    async def fake_fetch(package: str) -> int | None:
        return {"pkg-a": 1234, "pkg-b": None, "pkg-d": 0}[package]

    monkeypatch.setattr(mcp_router, "fetch_npm_downloads", fake_fetch)

    result = await mcp_router.get_mcp_hub(AgentIdentity.for_role(agent_id="admin1", role="admin"))
    by_id = {item["id"]: item for item in result["hub"]}

    assert by_id["a"]["downloads"] == 1234, "真實 npm 數據必須保留"
    assert by_id["d"]["downloads"] == 0, "真 0 必須誠實保留（不得變 100）"
    assert "downloads" not in by_id["b"], "抓取失敗不得偽造下載量"
    assert "downloads" not in by_id["c"], "非 npm 條目不得把 popularity 冒充下載量"

    order = [item["id"] for item in result["hub"]]
    assert order == ["c", "b", "a", "d"], "排序：有 downloads 用 downloads、無數據退回 popularity"
