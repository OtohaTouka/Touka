"""ADR-0250 A1／A4／L1：靈魂頁主要模型分成「你指定的」與「目前在用的」。

- 「你指定的」＝靈魂 config.json 的 ``base_model``；「目前在用的」＝``brain_settings`` 的
  active brain（模型自己可以換）。存檔時只有人類改了「你指定的」、或還沒有主要模型時，
  才把 active brain 換成新值；新值必須在模型清單裡（``is_selectable_brain``），不能用就
  回 404 ``MODEL_NOT_FOUND``，config.json 與 soul.md 都不寫。
- 決策表（plan §3.1）每列至少一個端點測試；判斷本身是純函式 ``should_set_active_brain``。
- 建立靈魂（``POST /v1/souls``）比照存檔先驗模型：不能用 404、只差大小寫分不出 409
  ``MODEL_NAME_AMBIGUOUS``，兩者都不建任何資料夾、不註冊；能用照舊建立並設成主要模型。
- 全程走 conftest 沙盒：模型清單用 ``model_manager.set_models`` 種入，不 patch 掉驗證。
  存檔用不跑 lifespan 的 TestClient，避免背景模型初始化和種入的清單互相覆寫。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from src.config import get_config_dir
from src.main import app
from src.models.identity import AgentIdentity
from src.services.auth_service import AuthService
from src.services.dispatchers.route_family import is_selectable_brain
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.models.routes import (
    CustomModelRoute,
    CustomRouteDisposition,
    CustomRouteResolution,
    ModelRouteSnapshot,
    ModelRouteSnapshotState,
)
from src.services.resource_manager import resource_manager
from src.utils.brain_persistence import (
    get_active_brain,
    set_active_brain,
    should_set_active_brain,
)

SOUL = "ChoiceSoul"
STEPUP_PW = "ChoicePass123!"
CHOSEN = "claude-chosen-y"  # 「你指定的」（在模型清單裡）
NEW = "claude-new-w"  # 人類改成的新值（在模型清單裡）
SWITCHED = "claude-switched-z"  # 模型自己換的（不必在清單裡）
RETIRED = "claude-4-5"  # 3a 拿掉的舊簡稱：不在模型清單


# ── 純函式：四條件（條件 4「新值能不能用」由呼叫端驗證）──────────────────────────

_DECISIONS = [
    pytest.param(True, CHOSEN, NEW, True, True, id="row1-human-changed-has-brain"),
    pytest.param(True, CHOSEN, CHOSEN, True, False, id="row2-human-same-has-brain"),
    pytest.param(True, CHOSEN, CHOSEN, False, True, id="row3-human-same-no-brain-rescue"),
    pytest.param(True, None, NEW, True, True, id="human-first-choice"),
    pytest.param(True, CHOSEN, NEW, False, True, id="human-changed-no-brain"),
    pytest.param(True, CHOSEN, None, True, False, id="row7-unspecified-has-brain"),
    pytest.param(True, CHOSEN, None, False, False, id="row7-unspecified-no-brain"),
    pytest.param(False, CHOSEN, NEW, True, False, id="row8-agent-changed"),
    pytest.param(False, CHOSEN, CHOSEN, False, False, id="row8-agent-no-brain"),
    pytest.param(False, None, NEW, False, False, id="row8-agent-first-choice"),
]


@pytest.mark.parametrize(("is_human", "old", "new", "has_brain", "expected"), _DECISIONS)
def test_should_set_active_brain_table(is_human, old, new, has_brain, expected):
    assert (
        should_set_active_brain(is_human=is_human, old=old, new=new, has_brain=has_brain)
        is expected
    )


# ── is_selectable_brain：只用 RouteCatalog 的兩個方法 ──────────────────────────────


class _Catalog:
    """RouteCatalog 替身：一般模型、靈魂、自訂模型、human、停用與啟用中的自訂連線。"""

    _ACTIVE = "Qwen/Qwen2.5-72B-Instruct"
    _INACTIVE = "retired-local"

    def get_models(self) -> list[dict]:
        # 一般名字會先轉小寫再比對，所以排除條件要用小寫 ID 才測得到（大寫 ID 本來就對不上）
        return [
            {"id": "claude-opus-4-8", "owned_by": "anthropic"},
            {"id": "gpt-4o", "owned_by": "openai"},
            {"id": "alice", "owned_by": "proxy", "is_agent": True},  # 靈魂
            {"id": "mycustom", "owned_by": "custom", "is_agent": True},  # L2 自訂模型
            {"id": "human", "owned_by": "human"},
            {"id": "toukasoulghost", "owned_by": "touka"},  # 舊靈魂路由 ID 的殘留
            {"id": self._INACTIVE, "owned_by": "custom"},
        ]

    def resolve_custom_route(self, model_name: str) -> CustomRouteResolution:
        if model_name == self._ACTIVE:
            route = CustomModelRoute(
                connection_id="c1", endpoint="http://127.0.0.1:1", api_key="", provider="X"
            )
            return CustomRouteResolution(CustomRouteDisposition.ACTIVE_CUSTOM, route=route)
        if model_name == self._INACTIVE:
            return CustomRouteResolution(CustomRouteDisposition.KNOWN_CUSTOM_INACTIVE)
        return CustomRouteResolution(CustomRouteDisposition.UNKNOWN)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("claude-opus-4-8", True),
        ("Claude-Opus-4-8", True),  # 官方 ID 不分大小寫（和 SoulLayer 一樣先轉小寫）
        ("gpt-4o", True),
        ("Qwen/Qwen2.5-72B-Instruct", True),  # 啟用中的自訂連線，原名
        (RETIRED, False),  # 舊簡稱不在清單
        ("gemini", False),  # 只寫家族名
        ("alice", False),  # 靈魂 ID
        ("Alice", False),
        ("mycustom", False),  # L2 自訂模型 ID
        ("human", False),
        ("toukasoulghost", False),  # 舊靈魂路由 ID
        ("ToukaSoulGhost", False),
        ("retired-local", False),  # 停用的自訂連線
        ("no-such-model", False),
    ],
)
def test_is_selectable_brain(name, expected):
    assert is_selectable_brain(name, manager=_Catalog()) is expected


# ── 端點：決策表逐列 ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _seeded_models():
    """沙盒模型清單種入 CHOSEN 與 NEW（teardown 由 conftest 的 reset_for_test 還原）。"""
    model_manager.set_models(
        [{"id": CHOSEN, "owned_by": "anthropic"}, {"id": NEW, "owned_by": "anthropic"}]
    )


@pytest.fixture
def human_admin() -> AgentIdentity:
    user = AuthService().create_user(f"choice-{uuid.uuid4().hex}@test.com", STEPUP_PW, "admin")
    return AgentIdentity.for_role(user.id, "admin", is_human=True, owned_agents=[])


def _soul_dir() -> Path:
    return Path(resource_manager.get_base_path()) / "soul" / SOUL


def _soul(base_model: str | None, brain: str | None) -> Path:
    soul_dir = _soul_dir()
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text("舊人格", encoding="utf-8")
    config: dict = {"is_toukasoul": True}
    if base_model is not None:
        config["base_model"] = base_model
    (soul_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if brain is not None:
        set_active_brain(AgentIdentity(agent_id=SOUL), brain)
    return soul_dir


def _save(identity: AgentIdentity, base_model: str | None, *, with_config: bool = True):
    app.dependency_overrides[get_verified_identity] = lambda: identity
    body: dict = {"persona": "新人格"}
    if with_config:
        body["config"] = {"is_toukasoul": True, "base_model": base_model}
    if identity.is_human:
        body["admin_password"] = STEPUP_PW
    return TestClient(app, raise_server_exceptions=False).post(f"/v1/souls/{SOUL}", json=body)


def _brain() -> str | None:
    return get_active_brain(AgentIdentity(agent_id=SOUL))


def _stored_model() -> str | None:
    return json.loads((_soul_dir() / "config.json").read_text(encoding="utf-8")).get("base_model")


def _persona() -> str:
    return (_soul_dir() / "soul.md").read_text(encoding="utf-8")


def test_row1_human_changes_choice_switches_brain(human_admin):
    """場景 2、H2 情境 4：舊值要在寫入前讀，否則新舊永遠相同、不會換。"""
    _soul(CHOSEN, CHOSEN)
    response = _save(human_admin, NEW)
    assert response.status_code == 200, response.text
    assert _brain() == NEW
    assert _stored_model() == NEW
    assert _persona() == "新人格"


def test_row2_unchanged_choice_keeps_brain_switched_by_model(human_admin):
    """場景 1、5（H2 情境 2，釘住）：只改人格、送回原本的「你指定的」，不蓋掉模型自己換的。"""
    _soul(CHOSEN, SWITCHED)
    response = _save(human_admin, CHOSEN)
    assert response.status_code == 200, response.text
    assert _brain() == SWITCHED
    assert _stored_model() == CHOSEN
    assert _persona() == "新人格"


def test_row3_unchanged_valid_choice_rescues_missing_brain(human_admin):
    """場景 3（H2 情境 3，釘住）：沒有主要模型時，沒改模型就存也會補設（p0 救援）。"""
    _soul(CHOSEN, None)
    response = _save(human_admin, CHOSEN)
    assert response.status_code == 200, response.text
    assert _brain() == CHOSEN


@pytest.mark.parametrize("invalid", [RETIRED, "no-such-model", SOUL, "未配置"])
@pytest.mark.parametrize("brain", [SWITCHED, None])
def test_row4_invalid_new_choice_is_rejected_without_writing(human_admin, invalid, brain):
    """場景 4：改成無效名字 → 404，人格與設定都沒被寫，主要模型不變。"""
    soul_dir = _soul(CHOSEN, brain)
    before = {name: (soul_dir / name).read_bytes() for name in ("config.json", "soul.md")}
    response = _save(human_admin, invalid)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    assert {name: (soul_dir / name).read_bytes() for name in before} == before
    assert not (soul_dir / "soul.md.bak").exists()
    assert _brain() == brain


def test_row5_invalid_unchanged_choice_without_brain_is_rejected(human_admin):
    """救援失敗要講清楚：沒有主要模型、送回的舊值本身無效 → 404，不寫。"""
    soul_dir = _soul(RETIRED, None)
    before = (soul_dir / "config.json").read_bytes(), (soul_dir / "soul.md").read_bytes()
    response = _save(human_admin, RETIRED)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    assert ((soul_dir / "config.json").read_bytes(), (soul_dir / "soul.md").read_bytes()) == before
    assert _brain() is None


def test_row6_old_invalid_choice_does_not_block_persona_save(human_admin):
    """舊的無效值沒改、已有主要模型：照存人格，不驗、不動主要模型。"""
    _soul(RETIRED, SWITCHED)
    response = _save(human_admin, RETIRED)
    assert response.status_code == 200, response.text
    assert _persona() == "新人格"
    assert _stored_model() == RETIRED
    assert _brain() == SWITCHED


@pytest.mark.parametrize("unspecified", [None, "", "   "])
@pytest.mark.parametrize("brain", [SWITCHED, None])
def test_row7_unspecified_choice_saves_empty_and_keeps_brain(human_admin, unspecified, brain):
    """沒指定（None、空字串、只有空白）：照存、base_model 存空、不動主要模型、不報錯。"""
    _soul(CHOSEN, brain)
    response = _save(human_admin, unspecified)
    assert response.status_code == 200, response.text
    assert _stored_model() is None
    assert _brain() == brain


def test_row7_persona_only_update_keeps_config_and_brain(human_admin):
    """沒送 config（只改人格）：config.json 不動、主要模型不動。"""
    _soul(CHOSEN, None)
    response = _save(human_admin, None, with_config=False)
    assert response.status_code == 200, response.text
    assert _stored_model() == CHOSEN
    assert _brain() is None


_AGENTS = [
    pytest.param(AgentIdentity.for_role(SOUL, "Owner", is_human=False), id="soul-itself"),
    pytest.param(  # admin 經 X-Agent-ID 代打進靈魂（identity_router 選項 A 的形狀）
        AgentIdentity(agent_id=SOUL, human_role="admin", is_human=False),
        id="admin-assumed-is-not-human",
    ),
]


@pytest.mark.parametrize("agent", _AGENTS)
@pytest.mark.parametrize("new", [NEW, RETIRED])
def test_row8_agent_save_never_touches_brain(agent, new):
    """代理人（含 admin 代打）存檔：照存、不驗、不動主要模型；是不是人類只看 is_human。"""
    _soul(CHOSEN, None)
    response = _save(agent, new)
    assert response.status_code == 200, response.text
    assert _stored_model() == new
    assert _brain() is None


# ── A4：GET /souls 沒設模型回 null ───────────────────────────────────────────────


def _listed(identity: AgentIdentity) -> dict:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    response = TestClient(app, raise_server_exceptions=False).get("/v1/souls")
    assert response.status_code == 200, response.text
    return next(soul for soul in response.json()["souls"] if soul["id"] == SOUL)


def test_list_souls_reports_missing_brain_as_null(human_admin):
    _soul(CHOSEN, None)
    listed = _listed(human_admin)
    assert listed["active_brain"] is None
    assert listed["config"]["base_model"] == CHOSEN


def test_list_souls_reports_brain_read_error_as_null(human_admin):
    _soul(CHOSEN, SWITCHED)
    with patch("src.routers.souls.get_active_brain", side_effect=RuntimeError("boom")):
        assert _listed(human_admin)["active_brain"] is None


def test_list_souls_reports_active_brain(human_admin):
    _soul(CHOSEN, SWITCHED)
    assert _listed(human_admin)["active_brain"] == SWITCHED


# ── L1：舊的 ToukaSoul 值當作沒設 ────────────────────────────────────────────────


@pytest.mark.parametrize("legacy", ["ToukaSoulAlice", "toukasoulalice"])
def test_legacy_soul_route_brain_reads_as_not_set(legacy):
    set_active_brain(AgentIdentity(agent_id=SOUL), legacy)
    assert _brain() is None


def test_legacy_soul_route_brain_makes_chat_ask_for_a_model(auth_client, global_test_sandbox):
    """舊值不偷換成寫死模型：聊天回 409 AGENT_BRAIN_NOT_SET，不分派。"""
    soul_dir = Path(global_test_sandbox) / "soul" / "DebugAgent"
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text("I am DebugAgent", encoding="utf-8")
    (soul_dir / "config.json").write_text('{"is_toukasoul": true}', encoding="utf-8")
    set_active_brain(AgentIdentity(agent_id="DebugAgent"), "ToukaSoulDebugAgent")
    dispatch = AsyncMock(return_value={"ok": True})
    with patch("src.services.proxy_service.dispatch_request", dispatch):
        response = auth_client.post(
            "/v1/chat/completions",
            json={"model": "DebugAgent", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Touka-Soul-ID": "DebugAgent"},
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "AGENT_BRAIN_NOT_SET"
    dispatch.assert_not_called()


# ── H9：建立靈魂比照存檔，先驗主要模型 ────────────────────────────────────────────

CREATED = "ChoiceNewSoul"


@pytest.fixture
def create_sandbox() -> Any:
    """建立會寫 models.json／agents_registry.json 與三個資料夾：先記下，結束時還原。"""
    config_dir = Path(get_config_dir())
    files = [config_dir / "models.json", config_dir / "agents_registry.json"]
    original = {path: path.read_bytes() if path.exists() else None for path in files}
    base = Path(resource_manager.get_base_path())
    dirs = [base / name / CREATED for name in ("soul", "personal_drive", "legacy")]
    yield original, dirs
    for path, content in original.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)
    for folder in dirs:
        shutil.rmtree(folder, ignore_errors=True)


def _create(identity: AgentIdentity, base_model: str | None):
    app.dependency_overrides[get_verified_identity] = lambda: identity
    body = {"agent_id": CREATED, "persona": "新靈魂", "config": {"base_model": base_model}}
    return TestClient(app, raise_server_exceptions=False).post("/v1/souls", json=body)


def _assert_nothing_created(create_sandbox: Any, human: AgentIdentity) -> None:
    original, dirs = create_sandbox
    assert [folder for folder in dirs if folder.exists()] == []
    for path, content in original.items():
        assert (path.read_bytes() if path.exists() else None) == content, path.name
    assert get_active_brain(AgentIdentity(agent_id=CREATED)) is None
    assert CREATED not in AuthService().get_user_by_id(human.agent_id).owned_agents


@pytest.mark.parametrize("invalid", [RETIRED, "no-such-model", "gemini"])
def test_create_with_unselectable_model_is_404_and_creates_nothing(
    human_admin, create_sandbox, invalid
):
    response = _create(human_admin, invalid)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "MODEL_NOT_FOUND"
    _assert_nothing_created(create_sandbox, human_admin)


def test_create_with_ambiguous_model_is_409_and_creates_nothing(
    human_admin, create_sandbox, monkeypatch
):
    """自訂連線同時有 Foo 和 foo、送 FOO：分不出是哪一個 → 409，不挑、不建。"""
    routes = {
        name: CustomModelRoute(
            connection_id=name, endpoint="http://127.0.0.1:1", api_key="", provider="X"
        )
        for name in ("Foo", "foo")
    }
    snapshot = ModelRouteSnapshot.create(
        generation=1,
        config_fingerprint="sandbox",
        active_routes=routes,
        known_custom_model_ids=frozenset(),
        state=ModelRouteSnapshotState.READY,
    )
    monkeypatch.setattr(model_manager, "_route_snapshot", snapshot)
    response = _create(human_admin, "FOO")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "MODEL_NAME_AMBIGUOUS"
    _assert_nothing_created(create_sandbox, human_admin)


def test_create_with_selectable_model_sets_it_as_brain(human_admin, create_sandbox):
    """釘住：清單裡的一般模型照舊建立，並設成主要模型。"""
    response = _create(human_admin, CHOSEN)
    assert response.status_code == 200, response.text
    assert get_active_brain(AgentIdentity(agent_id=CREATED)) == CHOSEN
    config_path = Path(resource_manager.get_base_path()) / "soul" / CREATED / "config.json"
    assert json.loads(config_path.read_text(encoding="utf-8"))["base_model"] == CHOSEN


def test_create_without_model_still_works(human_admin, create_sandbox):
    """釘住：沒指定模型照舊建立，不設主要模型。"""
    response = _create(human_admin, None)
    assert response.status_code == 200, response.text
    assert (Path(resource_manager.get_base_path()) / "soul" / CREATED).is_dir()
    assert get_active_brain(AgentIdentity(agent_id=CREATED)) is None


def _stored_created_model() -> str | None:
    config_path = Path(resource_manager.get_base_path()) / "soul" / CREATED / "config.json"
    return json.loads(config_path.read_text(encoding="utf-8")).get("base_model")


@pytest.mark.parametrize("unspecified", ["", "   "])
def test_create_with_blank_model_counts_as_unspecified(human_admin, create_sandbox, unspecified):
    """比照存檔（ADR-0250 A1）：空字串、只有空白都當沒指定，照舊建立、不設主要模型、不回 404。"""
    response = _create(human_admin, unspecified)
    assert response.status_code == 200, response.text
    assert get_active_brain(AgentIdentity(agent_id=CREATED)) is None
    assert _stored_created_model() is None


def test_create_with_padded_selectable_model_uses_trimmed_name(human_admin, create_sandbox):
    """比照存檔：先去掉前後空白再驗；能用就照去掉空白後的名字設成主要模型。"""
    response = _create(human_admin, f"  {CHOSEN} ")
    assert response.status_code == 200, response.text
    assert get_active_brain(AgentIdentity(agent_id=CREATED)) == CHOSEN
    assert _stored_created_model() == CHOSEN
