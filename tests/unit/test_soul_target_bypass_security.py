"""靈魂目標檢查的繞法（U2-SOUL 第 2 輪；R-2a ① 再審；禁令 20 對應安全測試）。

第 1 輪讓 MonitorLayer 在解析出靈魂目標後驗呼叫者，但還有幾條路繞過或打壞它：

1. **同名自訂模型**：標頭 `X-Touka-Soul-ID` 指自己的靈魂（通過檢查），`model` 指和別人的
   靈魂同名的自訂模型；CustomModelLayer 把 `ctx["agent_id"]` 改成 model，SoulLayer 就照它
   套別人的人設與家對話。
2. **路徑穿越**：`model="../soul/<別人>"`（Windows 另有 `..\\soul\\<別人>`），`custom_soul`
   路徑沒擋穿越，靈魂目錄的 config 被當成自訂模型。
3. **建立／改名自訂模型**：不擋和靈魂撞名、也不擋含 `..` 的 ID，第 1 點因此做得出來。
4. **子代理**繼承父請求的標頭：ToukaChat 靈魂對話裡的子代理被認成父靈魂而全部 403。
5. **計分卡**在 pipeline 裡以 DebugAgent 跑：`model=<別人的靈魂>` 被 DebugAgent 例外放行。

全部離線：上游與計分卡 runner 以假的取代；靈魂與自訂模型只寫到測試沙盒。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import src.routers.debug as debug_router
from fastapi.testclient import TestClient
from src.adapters.schemas import OpenAIChatRequest
from src.core.exceptions import ToukaException
from src.main import app
from src.middleware.layers.custom_model_layer import CustomModelLayer
from src.middleware.layers.soul import SoulLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.services import proxy_service
from src.services.identity_router import get_verified_identity
from src.services.resource_manager import resource_manager
from src.services.scorecard.models import RunOutcome
from src.services.soul_service import soul_service
from src.tools import subagent_tools
from src.utils.brain_persistence import set_active_brain

OWN_SOUL = "U2sOwnSoul"
VICTIM = "U2sVictim"
SOULS = {OWN_SOUL, VICTIM}
_PATHS = ["/v1/messages", "/v1/chat/completions"]
_TRAVERSALS = [f"../soul/{VICTIM}", f"..\\soul\\{VICTIM}"]


def _base() -> Path:
    return Path(resource_manager.get_base_path())


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """OWN_SOUL／VICTIM 被認成靈魂；記錄人設、家對話與上游呼叫。"""
    seen: dict[str, Any] = {"persona": [], "home": [], "dispatch": AsyncMock(return_value={})}

    def _apply_persona(_request: Any, agent_id: Any) -> None:
        seen["persona"].append(agent_id)

    async def _inject_home(_request: Any, agent_id: Any) -> None:
        seen["home"].append(agent_id)

    async def _no_summary(*_args: Any) -> None:
        return None

    monkeypatch.setattr(
        soul_service, "is_toukasoul", lambda who: getattr(who, "agent_id", who) in SOULS
    )
    monkeypatch.setattr(soul_service, "get_soul_data", lambda _agent_id: ("PERSONA", {}))
    monkeypatch.setattr(soul_service, "apply_soul_to_request", _apply_persona)
    monkeypatch.setattr(soul_service, "inject_home_messages", _inject_home)
    monkeypatch.setattr(soul_service, "inject_context_summary", _no_summary)
    monkeypatch.setattr(
        "src.middleware.layers.soul.get_active_brain", lambda _identity: "claude-probe"
    )
    monkeypatch.setattr(proxy_service, "dispatch_request", seen["dispatch"])
    return seen


@pytest.fixture
def victim_dirs() -> Any:
    """沙盒裡建一個真的靈魂目錄 VICTIM 與同名自訂模型；結束時刪掉。"""
    soul_dir = _base() / "soul" / VICTIM
    custom_dir = _base() / "custom" / VICTIM
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text("VICTIM PERSONA", encoding="utf-8")
    soul_config = {"is_toukasoul": True, "base_model": "claude-victim", "max_tokens": 77}
    (soul_dir / "config.json").write_text(json.dumps(soul_config), encoding="utf-8")
    yield {"soul": soul_dir, "custom": custom_dir}
    shutil.rmtree(soul_dir, ignore_errors=True)
    shutil.rmtree(custom_dir, ignore_errors=True)


def _make_same_name_custom_model(custom_dir: Path) -> None:
    custom_dir.mkdir(parents=True, exist_ok=True)
    (custom_dir / "config.json").write_text(json.dumps({"base_model": "claude-c"}), "utf-8")
    (custom_dir / "system_prompt.md").write_text("CUSTOM", encoding="utf-8")


def _owner() -> AgentIdentity:
    return AgentIdentity.for_role("u2s-owner", "normal", is_human=True, owned_agents=[OWN_SOUL])


def _chat(path: str, model: str) -> Any:
    app.dependency_overrides[get_verified_identity] = _owner
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if path.endswith("/messages"):
        body["max_tokens"] = 64
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(path, json=body, headers={"X-Touka-Soul-ID": OWN_SOUL})


def _assert_only_own_soul(resp: Any, seen: dict[str, Any]) -> None:
    assert seen["persona"] == [OWN_SOUL], f"{resp.status_code} {resp.text} | {seen['persona']}"
    assert seen["home"] == [OWN_SOUL]
    assert resp.status_code == 200, resp.text
    assert seen["dispatch"].await_args.args[2].agent_id == OWN_SOUL


# --------------------------------------------------------------------------- #
# 1. 標頭指自己的靈魂＋model 指別人的同名自訂模型
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _PATHS)
def test_same_name_custom_model_cannot_swap_in_another_soul(
    path: str, probe: dict[str, Any], victim_dirs: dict[str, Path]
) -> None:
    """擁有 OWN_SOUL 的人：標頭 OWN_SOUL＋model=VICTIM（同名自訂模型）→ 只套 OWN_SOUL。"""
    _make_same_name_custom_model(victim_dirs["custom"])
    _assert_only_own_soul(_chat(path, VICTIM), probe)


# --------------------------------------------------------------------------- #
# 2. 路徑穿越 ../soul/<別人>（POSIX 與 Windows 寫法）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("model", _TRAVERSALS)
@pytest.mark.parametrize("path", _PATHS)
def test_traversal_model_cannot_swap_in_another_soul(
    path: str, model: str, probe: dict[str, Any], victim_dirs: dict[str, Path]
) -> None:
    """標頭 OWN_SOUL＋model 穿越到 VICTIM 的靈魂目錄 → 只套 OWN_SOUL。"""
    _assert_only_own_soul(_chat(path, model), probe)


@pytest.mark.parametrize("model", _TRAVERSALS)
def test_custom_soul_path_never_leaves_custom_dir(
    model: str, victim_dirs: dict[str, Path]
) -> None:
    """custom_soul 路徑不得穿越出 custom/（禁令 17）。"""
    resolved = resource_manager.get_vault_path("custom_soul", AgentIdentity(agent_id=model))
    custom_root = (_base() / "custom").resolve()
    assert resolved is None or Path(resolved).resolve().is_relative_to(custom_root)


@pytest.mark.parametrize("model", _TRAVERSALS)
async def test_custom_model_layer_does_not_load_a_soul_dir(
    model: str, victim_dirs: dict[str, Path]
) -> None:
    """沒有靈魂標頭時，穿越 ID 也不會把靈魂目錄的設定當成自訂模型套用。"""
    request = OpenAIChatRequest(model=model, messages=[{"role": "user", "content": "hi"}])
    ctx = PipelineContext()
    await CustomModelLayer().process(request, ctx)
    assert request.model == model
    assert ctx.get("agent_id") is None
    assert ctx.get("is_custom") in (None, False)


async def test_soul_layer_rechecks_the_soul_it_injects(probe: dict[str, Any]) -> None:
    """注入前再驗一次：ctx 身分是 OWN_SOUL，agent_id 卻被改成 VICTIM → 403、不套人設。"""
    request = OpenAIChatRequest(model="x", messages=[{"role": "user", "content": "hi"}])
    ctx = PipelineContext(
        identity=AgentIdentity.for_role(OWN_SOUL, "Owner", is_human=False), agent_id=VICTIM
    )
    ctx["is_toukasoul"] = True
    raised: ToukaException | None = None
    try:
        await SoulLayer().process(request, ctx)
    except ToukaException as exc:
        raised = exc
    assert probe["persona"] == []
    assert probe["home"] == []
    assert raised is not None
    assert raised.detail["code"] == "AUTH_PERMISSION_DENIED"


# --------------------------------------------------------------------------- #
# 3. 建立／改名自訂模型：不能和靈魂撞名、不能含路徑字元
# --------------------------------------------------------------------------- #

_OWN_AGENT = AgentIdentity.for_role(OWN_SOUL, "Owner", is_human=False)
_ADMIN = AgentIdentity.for_role("u2s-admin", "admin", is_human=True)
_MODEL_BODY = {"name": "x", "description": "", "systemPrompt": "CUSTOM", "config": {}}


@pytest.fixture
def owned_list(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """擁有清單的寫入改成假的（不碰沙盒 users.json）。"""
    fake = MagicMock()
    monkeypatch.setattr("src.routers.custom_models.user_service", fake)
    return fake


def _models_post(url: str, caller: AgentIdentity, body: dict[str, Any]) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: caller
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post(url, json=body)


def test_create_custom_model_named_like_a_soul_is_rejected(
    probe: dict[str, Any], victim_dirs: dict[str, Path], owned_list: MagicMock
) -> None:
    """靈魂代理建立和別人的靈魂同名的自訂模型 → 409，沒有建出目錄。"""
    resp = _models_post(f"/v1/ui/custom_models/{VICTIM}", _OWN_AGENT, _MODEL_BODY)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
    assert not victim_dirs["custom"].exists()


@pytest.mark.parametrize(
    "alias",
    [f"ToukaSoul{VICTIM}", f"@{VICTIM}", f"toukasoul-{VICTIM}", f"toukasoul{VICTIM}"],
)
def test_create_custom_model_with_soul_alias_is_rejected(
    alias: str, probe: dict[str, Any], victim_dirs: dict[str, Path], owned_list: MagicMock
) -> None:
    """MonitorLayer 會認成靈魂的寫法（前綴、@、toukasoul-）都算撞名 → 409，不建目錄。"""
    prefixed = _base() / "custom" / alias
    try:
        resp = _models_post(f"/v1/ui/custom_models/{alias}", _OWN_AGENT, _MODEL_BODY)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "MODEL_ALREADY_EXISTS"
        assert not prefixed.exists()
    finally:
        shutil.rmtree(prefixed, ignore_errors=True)


@pytest.mark.parametrize("model_id", ["..%5Csoul%5CU2sVictim", "a..b"])
def test_create_custom_model_with_path_characters_is_rejected(
    model_id: str, probe: dict[str, Any], victim_dirs: dict[str, Path], owned_list: MagicMock
) -> None:
    """ID 含 `\\` 或 `..` → 422，靈魂目錄的設定沒被改寫。"""
    before = (victim_dirs["soul"] / "config.json").read_bytes()
    resp = _models_post(f"/v1/ui/custom_models/{model_id}", _ADMIN, _MODEL_BODY)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "VALIDATION_ERROR"
    assert (victim_dirs["soul"] / "config.json").read_bytes() == before
    assert not (victim_dirs["soul"] / "system_prompt.md").exists()


@pytest.mark.parametrize(
    ("new_id", "status", "code"),
    [(VICTIM, 409, "MODEL_ALREADY_EXISTS"), ("..", 422, "VALIDATION_ERROR")],
)
def test_rename_custom_model_to_soul_or_dotdot_is_rejected(
    new_id: str,
    status: int,
    code: str,
    probe: dict[str, Any],
    victim_dirs: dict[str, Path],
    owned_list: MagicMock,
) -> None:
    """改名成靈魂名稱 → 409；改成 `..` → 422；原本的自訂模型還在。"""
    mine = _base() / "custom" / "U2sMine"
    try:
        created = _models_post("/v1/ui/custom_models/U2sMine", _ADMIN, _MODEL_BODY)
        assert created.status_code == 200, created.text
        url = "/v1/ui/custom_models/U2sMine/rename"
        resp = _models_post(url, _ADMIN, {"new_id": new_id})
        assert resp.status_code == status, resp.text
        assert resp.json()["detail"]["code"] == code
        assert (mine / "config.json").exists()
        assert not victim_dirs["custom"].exists()
    finally:
        shutil.rmtree(mine, ignore_errors=True)


def test_create_plain_custom_model_still_works(
    probe: dict[str, Any], owned_list: MagicMock
) -> None:
    """一般名稱照舊建立。"""
    mine = _base() / "custom" / "U2sPlain"
    try:
        resp = _models_post("/v1/ui/custom_models/U2sPlain", _OWN_AGENT, _MODEL_BODY)
        assert resp.status_code == 200, resp.text
        assert (mine / "config.json").exists()
    finally:
        shutil.rmtree(mine, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. 子代理不繼承父請求的目標標頭
# --------------------------------------------------------------------------- #

_PARENT_HEADERS = {
    "x-touka-soul-id": OWN_SOUL,
    "x-touka-session-id": "u2s-parent-session",
    "x-toukachat-session-id": "u2s-parent-session",
    "x-touka-skills": "",
}


def _sub_identity() -> AgentIdentity:
    return AgentIdentity.for_role("__subagent_u2s0001__", "Subagent")


async def test_subagent_in_soul_chat_assembles_with_plain_model(probe: dict[str, Any]) -> None:
    """父 ctx 帶靈魂標頭（ToukaChat 靈魂對話）、子代理用一般模型 → 照常組裝、不套人設。"""
    parent = PipelineContext(headers=dict(_PARENT_HEADERS))
    request = await subagent_tools._assemble_subagent_request(
        goal="整理資料", sub_identity=_sub_identity(), base_ctx=parent, model="claude-x"
    )
    assert request.model == "claude-x"
    assert probe["persona"] == []
    assert probe["home"] == []
    assert parent["headers"] == _PARENT_HEADERS


async def test_subagent_does_not_touch_the_parent_session(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """父 ctx 的 session 標頭不帶進子代理：不清父 session 的中斷旗標、不從 session 連結認目標。"""
    cleared: list[str] = []
    monkeypatch.setattr(
        "src.middleware.layers.monitor.rhythm_service.clear_interrupt", cleared.append
    )
    session_only = {"x-touka-session-id": "u2s-parent", "x-toukachat-session-id": "u2s-parent"}
    parent = PipelineContext(headers=session_only)
    await subagent_tools._assemble_subagent_request(
        goal="整理資料", sub_identity=_sub_identity(), base_ctx=parent, model="claude-x"
    )
    assert cleared == []


async def test_subagent_in_soul_chat_still_cannot_take_a_soul(probe: dict[str, Any]) -> None:
    """父 ctx 帶靈魂標頭、子代理 sub_model=靈魂 → 一樣 403。"""
    parent = PipelineContext(headers=dict(_PARENT_HEADERS))
    with pytest.raises(ToukaException) as exc_info:
        await subagent_tools._assemble_subagent_request(
            goal="整理資料", sub_identity=_sub_identity(), base_ctx=parent, model=VICTIM
        )
    assert exc_info.value.detail["code"] == "AUTH_PERMISSION_DENIED"
    assert probe["persona"] == []


@pytest.mark.parametrize("headers", [_PARENT_HEADERS, {}], ids=["soul_chat", "heartbeat"])
async def test_subagent_without_model_runs_on_the_parent_brain(
    headers: dict[str, str], probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """父靈魂大腦是 Claude 系、沒指定 sub_model（Claude 路由不寫 ctx model）→ 用父的大腦。"""
    parent_soul = AgentIdentity.for_role(OWN_SOUL, "Owner", is_human=False)
    set_active_brain(parent_soul, "Claude-U2s-Brain")
    routed: list[str] = []

    def _fake_claude(request: Any, _context: Any) -> Any:
        routed.append(request.model)

        async def _gen() -> Any:
            yield 'data: {"choices": [{"delta": {"content": "嗨"}, "index": 0}]}\n\n'

        return _gen()

    monkeypatch.setattr("src.services.dispatchers.factory.handle_claude_request", _fake_claude)
    text = await subagent_tools._run_subagent(
        goal="整理資料",
        parent_identity=parent_soul,
        parent_request_id="req-u2s",
        base_ctx=PipelineContext(headers=dict(headers)),
        model=None,
    )
    assert routed == ["claude-u2s-brain"]
    assert text == "嗨"
    assert probe["persona"] == []
    assert probe["home"] == []


# --------------------------------------------------------------------------- #
# 5. 計分卡：model 是靈魂時先驗真正的呼叫者
# --------------------------------------------------------------------------- #


async def _fake_runner(_model: str, _scenario: Any) -> RunOutcome:
    return RunOutcome(text="ok", tokens=5, cost_usd=0.001, latency_ms=3)


def _scorecard(model: str, caller: AgentIdentity, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(debug_router, "_scorecard_runner", _fake_runner)
    app.dependency_overrides[get_verified_identity] = lambda: caller
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post("/v1/debug/scorecard/run", json={"model": model, "budget_usd": 0.5})


@pytest.mark.parametrize("model", [VICTIM, f"@{VICTIM}", f"ToukaSoul-{VICTIM}"])
def test_scorecard_on_another_soul_is_denied(
    model: str, probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """擁有者用 X-Agent-ID 切進自己的靈魂後，對別人的靈魂跑計分卡 → 403。"""
    resp = _scorecard(model, _OWN_AGENT, monkeypatch)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "AUTH_PERMISSION_DENIED"


@pytest.mark.parametrize("model", [OWN_SOUL, "deepseek-chat"])
def test_scorecard_on_own_soul_or_plain_model_still_runs(
    model: str, probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """自己的靈魂、一般模型 → 照舊開跑。"""
    resp = _scorecard(model, _OWN_AGENT, monkeypatch)
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"].startswith("sc_")
