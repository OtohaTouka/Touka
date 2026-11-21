"""刪掉的靈魂的 SNS 機器人不給同名的新靈魂（ADR-0326 PR 2；禁令 20 對應安全測試）.

修補前：刪除靈魂不碰 ``bots.json``，也不碰正在跑的機器人。同名的新靈魂一建好，綁著舊靈魂的
Discord／Telegram 機器人就接到新靈魂，外人直接跟它聊，新靈魂的家原文與前情也進給外人的請求。

修補後：
- 刪除靈魂：放開破壞性操作鎖之後，綁著這個名字（兩種寫法、舊格式也算）的機器人一律關掉
  （``enabled: false``，token 不動），正在跑的標停用、移出執行清單、平行停（各自有期限），其他
  機器人不停、不重啟。寫不進去或讀不出來列進 ``skipped``。
- 建立靈魂（兩條路）：名字不屬於任何代理人時同樣停用；寫不進去或讀不出來就不建立。
- 標了停用的機器人轉來的訊息，閘道一律拒絕（停不下來時安全也不靠停下來）；還在啟動的不再啟動。
- ``bots.json`` 的讀改寫（設定頁的讀取遷移、存檔、停用）都在同一把程序內鎖裡。

不連網路、不建真的連線：正在跑的機器人用假的 adapter 放進執行清單。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from memory_server.services.mission_manager import mission_board_path
from src.adapters.discord_bot import DiscordAdapter
from src.adapters.telegram_bot import TelegramAdapter
from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.main import app
from src.models.identity import AgentIdentity
from src.models.sns import SNSIncomingEvent
from src.protocols.sns_base import BaseSNSAdapter
from src.routers import souls as souls_router
from src.services import agent_leftovers, destructive_ops
from src.services import bot_service as bot_service_module
from src.services.auth_service import AuthService
from src.services.bot_service import bot_service, disable_bound
from src.services.identity_router import get_verified_identity
from src.services.models import model_manager
from src.services.resource_manager import resource_manager
from src.services.sns_gateway import SNSGatewayService
from src.utils import async_db
from src.utils.agent_spellings import legacy_spellings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from src.models.user import User

ADMIN = AgentIdentity.for_role("bots-admin", "admin", is_human=True)
SOULS = ("GoneSoul", "OtherSoul", "NewSoul", "RepairSoul", "BusyBots", "BadBots")


# --------------------------------------------------------------------------- #
# 共用
# --------------------------------------------------------------------------- #


@pytest.fixture
async def sandbox() -> AsyncIterator[Path]:
    """靈魂路由要 conftest 沙盒的對話資料庫；本檔用到的名字在結束時清掉."""
    data = Path(resource_manager.get_base_path())
    await async_db.init_db_async()
    yield data
    for agent_id in SOULS:
        mission_board_path(agent_id).unlink(missing_ok=True)
        for base in ("soul", "personal_drive", "legacy", "stats", "custom"):
            shutil.rmtree(data / base / agent_id, ignore_errors=True)


@pytest.fixture(autouse=True)
def registry(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """models.json、agents_registry.json 從空的開始；沒有心跳位址（排程那步不連網路）."""
    _ = sandbox
    monkeypatch.setattr(agent_leftovers, "HEARTBEAT_EXTERNAL_URL", None)
    config = Path(get_config_dir())
    files = [config / "models.json", config / "agents_registry.json"]
    original = {f: (f.read_text(encoding="utf-8") if f.exists() else None) for f in files}
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    model_manager.load_initial_config()
    yield config / "agents_registry.json"
    for f, content in original.items():
        if content is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(content, encoding="utf-8")
    model_manager.load_initial_config()


@pytest.fixture(autouse=True)
def bots_file() -> Iterator[Path]:
    """``bots.json`` 每個測試從沒有開始，結束時還原."""
    path = Path(bot_service.config_path)
    before = path.read_bytes() if path.exists() else None
    path.unlink(missing_ok=True)
    yield path
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(before)


@pytest.fixture(autouse=True)
def running() -> Iterator[dict[str, dict[str, Any]]]:
    """執行清單換成空的，結束時還原（假 adapter 直接放進來）."""
    saved_bots, saved_tasks = bot_service._running_bots, bot_service._tasks
    bot_service._running_bots = {"discord": {}, "telegram": {}}
    bot_service._tasks = []
    yield bot_service._running_bots
    bot_service._running_bots, bot_service._tasks = saved_bots, saved_tasks


@pytest.fixture
def admin() -> Iterator[tuple[AgentIdentity, str]]:
    """真的 admin 帳號（刪除與用編輯建立靈魂要重新輸入密碼）；結束時還原 users.json."""
    pw = "BotsTakeoverPass123!"
    auth = AuthService()
    users = auth.users_file_path
    before = users.read_bytes() if users.exists() else None
    user = auth.create_user("bots-admin@test.com", pw, role="admin")
    yield AgentIdentity.for_role(user.id, "admin", is_human=True), pw
    if before is None:
        users.unlink(missing_ok=True)
    else:
        users.write_bytes(before)


def _send(method: str, url: str, body: dict[str, Any] | None, who: AgentIdentity = ADMIN) -> Any:
    app.dependency_overrides[get_verified_identity] = lambda: who
    try:
        return TestClient(app, raise_server_exceptions=False).request(method, url, json=body)
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)


def _code(response: Any) -> str:
    return response.json()["detail"]["code"]


def _create(agent_id: str, **config: Any) -> Any:
    return _send("POST", "/v1/souls", {"agent_id": agent_id, "config": config})


def _edit_create(agent_id: str, who: tuple[AgentIdentity, str]) -> Any:
    body = {"config": {"is_toukasoul": True}, "admin_password": who[1]}
    return _send("POST", f"/v1/souls/{agent_id}", body, who[0])


def _delete(agent_id: str, who: tuple[AgentIdentity, str]) -> Any:
    return _send("DELETE", f"/v1/souls/{agent_id}", {"admin_password": who[1]}, who[0])


def _bot(bot_id: str, agent_id: Any, *, enabled: bool = True) -> dict[str, Any]:
    return {"id": bot_id, "token": f"tok-{bot_id}", "agent_id": agent_id, "enabled": enabled}


def _put_bots(
    path: Path, *, discord: list[Any] | None = None, telegram: list[Any] | None = None
) -> None:
    path.write_text(
        json.dumps({"discord": discord or [], "telegram": telegram or []}), encoding="utf-8"
    )


def _enabled(path: Path) -> dict[str, bool]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {b["id"]: b["enabled"] for platform in ("discord", "telegram") for b in data[platform]}


def _tokens(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {b["id"]: b["token"] for platform in ("discord", "telegram") for b in data[platform]}


class FakeBot(BaseSNSAdapter):
    """不連網路的機器人：記錄停止的先後、停止當下自己的狀態；可以設成停不下來或丟例外."""

    def __init__(
        self,
        agent_id: Any,
        log: list[tuple[str, Any]] | None = None,
        *,
        hang: bool = False,
        fail: bool = False,
        on_stop: Callable[[], object] | None = None,
    ) -> None:
        super().__init__("synthetic-bot-token", agent_id)
        self.log = log if log is not None else []
        self.hang = hang
        self.fail = fail
        self.on_stop = on_stop
        self.stopped = False
        self.seen_at_stop: tuple[bool, bool] | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        listed = any(self in bots.values() for bots in bot_service._running_bots.values())
        self.seen_at_stop = (self.retired, listed)
        self.log.append(("enter", self.agent_id))
        if self.on_stop is not None:
            self.on_stop()
        if self.fail:
            raise RuntimeError
        if self.hang:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.log.append(("cancel", self.agent_id))
                raise
        self.stopped = True
        self.log.append(("stopped", self.agent_id))

    async def send_message(self, channel_id: str, response: Any) -> bool:
        return True

    async def show_typing(self, channel_id: str) -> None:
        return None


def _run(running: dict[str, dict[str, Any]], platform: str, key: str, bot: FakeBot) -> FakeBot:
    running[platform][key] = bot
    return bot


def _listed(running: dict[str, dict[str, Any]], bot: FakeBot) -> bool:
    return any(bot in bots.values() for bots in running.values())


# --------------------------------------------------------------------------- #
# 比對（純函式）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("agent_id", "enabled", "turned_off"),
    [
        ("GoneSoul", True, True),
        ("ToukaSoulGoneSoul", True, True),
        ({"agent_id": "GoneSoul", "agent_role": "Owner"}, True, True),
        ("gonesoul", True, False),
        ("GoneSoulTwo", True, False),
        ("GoneSoul", False, False),
    ],
)
def test_disable_bound_matches_both_spellings_only(
    agent_id: Any, enabled: bool, turned_off: bool
) -> None:
    entry = {"id": "x", "token": "tok", "agent_id": agent_id, "enabled": enabled, "extra": 1}
    config = {"telegram": [entry], "discord": [], "other": {"keep": True}}
    snapshot = json.dumps(config, sort_keys=True)
    result, count = disable_bound(config, legacy_spellings("GoneSoul"))
    assert json.dumps(config, sort_keys=True) == snapshot  # 傳入的不改
    assert count == (1 if turned_off else 0)
    (out,) = result["telegram"]
    assert out["enabled"] is (False if turned_off else enabled)
    assert {k: v for k, v in out.items() if k != "enabled"} == {
        k: v for k, v in entry.items() if k != "enabled"
    }
    assert result["other"] == {"keep": True}


def test_disable_bound_keeps_odd_entries() -> None:
    config = {"telegram": ["not-a-dict", {"id": "no-agent", "enabled": True}], "discord": []}
    result, count = disable_bound(config, legacy_spellings("GoneSoul"))
    assert count == 0
    assert result["telegram"] == config["telegram"]


# --------------------------------------------------------------------------- #
# 刪除
# --------------------------------------------------------------------------- #


async def test_deleting_a_soul_disables_its_bots_only(
    sandbox: Path, admin: tuple[AgentIdentity, str], bots_file: Path
) -> None:
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    _put_bots(
        bots_file,
        discord=[
            _bot("d1", {"agent_id": "GoneSoul", "agent_role": "Owner"}),
            _bot("d2", "OtherSoul"),
        ],
        telegram=[_bot("t1", "GoneSoul"), _bot("t2", "ToukaSoulGoneSoul"), _bot("t3", "gonesoul")],
    )
    tokens = _tokens(bots_file)
    response = _delete("GoneSoul", admin)
    assert response.status_code == 200, response.text
    assert _enabled(bots_file) == {"d1": False, "d2": True, "t1": False, "t2": False, "t3": True}
    assert _tokens(bots_file) == tokens
    assert "bots" not in response.json()["skipped"]


async def test_deleting_a_soul_stops_only_its_running_bots(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    mine = [
        _run(running, "telegram", "k1", FakeBot("GoneSoul")),
        _run(running, "telegram", "k2", FakeBot("ToukaSoulGoneSoul")),
        _run(running, "discord", "k3", FakeBot({"agent_id": "GoneSoul"})),
    ]
    others = [
        _run(running, "telegram", "k4", FakeBot("OtherSoul")),
        _run(running, "discord", "k5", FakeBot("gonesoul")),
    ]

    reloads: list[str] = []

    async def _reload() -> None:
        reloads.append("reload")

    monkeypatch.setattr(bot_service, "reload_bots", _reload)
    monkeypatch.setattr(bot_service, "start_bots", _reload)
    assert _delete("GoneSoul", admin).status_code == 200
    assert reloads == []
    for bot in mine:
        assert bot.stopped
        assert bot.retired
        assert not _listed(running, bot)
        # 標停用、移出清單在停之前（await 之前）
        assert bot.seen_at_stop == (True, False)
    for bot in others:
        assert not bot.stopped
        assert not bot.retired
        assert _listed(running, bot)


async def test_bots_are_disabled_after_the_destructive_lock(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    _put_bots(bots_file, telegram=[_bot("t1", "GoneSoul")])
    held: list[bool] = []
    real_write = resource_manager.write_json

    def _write(path: Any, data: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == bots_file:
            held.append(destructive_ops.is_held(AgentIdentity(agent_id="GoneSoul")))
        real_write(path, data, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "write_json", _write)
    assert _delete("GoneSoul", admin).status_code == 200
    assert held == [False]


async def test_incomplete_delete_still_disables_bots_and_reports_schedules(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登記表沒寫成（回應照樣 200）：名字看起來還有人用，機器人照停，排程沒清要看得出來."""
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    _put_bots(bots_file, telegram=[_bot("t1", "GoneSoul")])
    bot = _run(running, "telegram", "k1", FakeBot("GoneSoul"))
    monkeypatch.setattr(souls_router, "_update_agents_registry", lambda *_a, **_k: None)
    response = _delete("GoneSoul", admin)
    assert response.status_code == 200, response.text
    assert _enabled(bots_file) == {"t1": False}
    assert bot.stopped
    assert "schedules" in response.json()["skipped"]


async def test_schedules_are_judged_before_waiting_for_bots(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排程那步（判斷與清除）在等機器人停下來之前，判斷的時間點和 PR 1 相同."""
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    log: list[tuple[str, Any]] = []
    _run(running, "telegram", "k1", FakeBot("GoneSoul", log))
    real_is_unclaimed = agent_leftovers.mission_boards.is_unclaimed

    def _is_unclaimed(identity: AgentIdentity) -> bool:
        log.append(("judge", identity.agent_id))
        return real_is_unclaimed(identity)

    async def _schedules(identity: AgentIdentity) -> int:
        log.append(("schedules", identity.agent_id))
        return 0

    monkeypatch.setattr(agent_leftovers.mission_boards, "is_unclaimed", _is_unclaimed)
    monkeypatch.setattr(agent_leftovers, "_remove_schedules", _schedules)
    response = _delete("GoneSoul", admin)
    assert response.status_code == 200, response.text
    order = [step for step, _ in log]
    assert order.index("judge") < order.index("schedules") < order.index("enter")


@pytest.mark.parametrize("refusal", ["delete", "create"])
async def test_refused_requests_leave_bots_alone(
    refusal: str,
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = sandbox
    if refusal == "delete":
        assert _create("BusyBots").status_code == 200

        async def _has_memory(*_args: Any, **_kwargs: Any) -> None:
            raise ToukaException(ErrorCode.SOUL_MEMORY_NOT_RESET)

        monkeypatch.setattr(souls_router, "assert_memory_cleared", _has_memory)
    name = "BusyBots" if refusal == "delete" else "BadBots"
    _put_bots(bots_file, telegram=[_bot("t1", name)])
    before = bots_file.read_bytes()
    bot = _run(running, "telegram", "k1", FakeBot(name))
    response = _delete(name, admin) if refusal == "delete" else _create(name, base_model="no-such")
    assert response.status_code in {404, 409}, response.text
    assert bots_file.read_bytes() == before
    assert not bot.stopped
    assert not bot.retired
    assert _listed(running, bot)


async def test_cancelled_cleanup_still_stops_the_disabled_bots(
    running: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _run(running, "telegram", "k1", FakeBot("GoneSoul"))

    async def _stall(_identity: AgentIdentity) -> int:
        await asyncio.sleep(3600)
        return 0

    monkeypatch.setattr(agent_leftovers, "_remove_schedules", _stall)
    task = asyncio.ensure_future(
        agent_leftovers.clean_up_after_delete(AgentIdentity(agent_id="GoneSoul"))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bot.retired
    assert bot.stopped


def _corrupt(path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text("{ not json", encoding="utf-8")


def _unwritable(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = resource_manager.write_json

    def _write(target: Any, data: Any, *args: Any, **kwargs: Any) -> None:
        if Path(target) == path:
            raise OSError
        real_write(target, data, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "write_json", _write)


def _unexpected(_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError

    monkeypatch.setattr(bot_service_module, "disable_bound", _boom)


def _bad_shape(path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON 讀得出來，但平台的值不是清單：也不能當成沒有機器人."""
    path.write_text(json.dumps({"discord": [], "telegram": "x"}), encoding="utf-8")


@pytest.mark.parametrize("breakage", [_corrupt, _unwritable, _unexpected, _bad_shape])
async def test_bots_failure_on_delete_is_listed_in_skipped(
    breakage: Callable[[Path, pytest.MonkeyPatch], None],
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    _put_bots(bots_file, telegram=[_bot("t1", "GoneSoul")])
    breakage(bots_file, monkeypatch)
    before = bots_file.read_bytes()
    bot = _run(running, "telegram", "k1", FakeBot("GoneSoul"))
    response = _delete("GoneSoul", admin)
    assert response.status_code == 200, response.text
    assert "bots" in response.json()["skipped"]
    assert bot.stopped
    assert bot.retired
    if breakage is not _unexpected:
        assert bots_file.read_bytes() == before


async def test_unclaimed_check_error_lists_schedules_and_still_disables_bots(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = sandbox
    assert _create("GoneSoul").status_code == 200
    _put_bots(bots_file, telegram=[_bot("t1", "GoneSoul")])

    def _broken(_identity: AgentIdentity) -> bool:
        raise RuntimeError

    monkeypatch.setattr(agent_leftovers.mission_boards, "is_unclaimed", _broken)
    response = _delete("GoneSoul", admin)
    assert response.status_code == 200, response.text
    assert "schedules" in response.json()["skipped"]
    assert _enabled(bots_file) == {"t1": False}


# --------------------------------------------------------------------------- #
# 建立
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["create", "edit_create"])
async def test_creating_a_soul_disables_leftover_bots(
    path: str,
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    running: dict[str, dict[str, Any]],
) -> None:
    _put_bots(bots_file, telegram=[_bot("t1", "NewSoul"), _bot("t2", "OtherSoul")])
    bot = _run(running, "telegram", "k1", FakeBot("ToukaSoulNewSoul"))
    other = _run(running, "telegram", "k2", FakeBot("OtherSoul"))
    response = _create("NewSoul") if path == "create" else _edit_create("NewSoul", admin)
    assert response.status_code == 200, response.text
    assert (sandbox / "soul" / "NewSoul").exists()
    assert _enabled(bots_file) == {"t1": False, "t2": True}
    assert bot.stopped
    assert bot.retired
    assert not other.stopped


@pytest.mark.parametrize("breakage", [_corrupt, _unwritable, _bad_shape])
async def test_create_refuses_when_bots_cannot_be_disabled(
    breakage: Callable[[Path, pytest.MonkeyPatch], None],
    sandbox: Path,
    bots_file: Path,
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _put_bots(bots_file, telegram=[_bot("t1", "NewSoul")])
    breakage(bots_file, monkeypatch)
    bot = _run(running, "telegram", "k1", FakeBot("NewSoul"))
    response = _create("NewSoul")
    assert response.status_code == 500, response.text
    assert _code(response) == ErrorCode.SYSTEM_ERROR.value
    assert not (sandbox / "soul" / "NewSoul").exists()
    assert bot.stopped


async def test_schedule_refusal_on_create_leaves_bots_alone(
    sandbox: Path,
    bots_file: Path,
    running: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建立時排程第一個：心跳服務連不上而拒絕時，機器人還沒動（決定 3）."""

    async def _unreachable(_identity: AgentIdentity) -> int:
        raise ToukaException(ErrorCode.HEARTBEAT_UPSTREAM_FAILED)

    monkeypatch.setattr(agent_leftovers, "_remove_schedules", _unreachable)
    _put_bots(bots_file, telegram=[_bot("t1", "NewSoul")])
    before = bots_file.read_bytes()
    bot = _run(running, "telegram", "k1", FakeBot("NewSoul"))
    response = _create("NewSoul")
    assert response.status_code == 502, response.text
    assert not (sandbox / "soul" / "NewSoul").exists()
    assert bots_file.read_bytes() == before
    assert not bot.retired
    assert not bot.stopped
    assert _listed(running, bot)


async def test_repairing_a_soul_keeps_its_bots(
    sandbox: Path,
    admin: tuple[AgentIdentity, str],
    bots_file: Path,
    running: dict[str, dict[str, Any]],
) -> None:
    """用編輯建立時名字還在擁有清單裡：是在修復既有靈魂，它的機器人不動."""
    _ = sandbox

    def _own(users: list[User]) -> None:
        users[0].owned_agents.append("RepairSoul")

    AuthService()._atomic_users_write(_own)
    _put_bots(bots_file, telegram=[_bot("t1", "RepairSoul")])
    bot = _run(running, "telegram", "k1", FakeBot("RepairSoul"))
    response = _edit_create("RepairSoul", admin)
    assert response.status_code == 200, response.text
    assert _enabled(bots_file) == {"t1": True}
    assert not bot.stopped
    assert not bot.retired


# --------------------------------------------------------------------------- #
# 閘道、停止、啟動
# --------------------------------------------------------------------------- #


async def test_retired_bot_messages_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = FakeBot("Alice")
    bot.retired = True
    identify = MagicMock(side_effect=AssertionError)
    opened = AsyncMock(side_effect=AssertionError)
    monkeypatch.setattr("src.services.sns_gateway.agent_service.identify_agent", identify)
    monkeypatch.setattr("src.services.sns_gateway.session_service.open_channel_session", opened)
    event = SNSIncomingEvent(platform="telegram", sender_id="u1", channel_id="c1", content="hi")
    with pytest.raises(ToukaException) as caught:
        await SNSGatewayService().on_event(bot, event)
    assert caught.value.code == ErrorCode.SESSION_SCOPE_MISMATCH
    identify.assert_not_called()
    opened.assert_not_called()


def test_every_adapter_starts_not_retired() -> None:
    assert FakeBot("Alice").retired is False
    assert BaseSNSAdapter.retired is False


async def test_bots_that_do_not_stop_are_stopped_in_parallel(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(bot_service_module, "BOT_STOP_SECONDS", 0.05)
    log: list[tuple[str, Any]] = []
    first, second = FakeBot("A", log, hang=True), FakeBot("B", log, hang=True)
    await bot_service.stop_adapters([first, second])
    assert [step for step, _ in log[:2]] == ["enter", "enter"]
    assert sorted(step for step, _ in log[2:]) == ["cancel", "cancel"]
    assert "synthetic-bot-token" not in caplog.text
    assert "synth" not in caplog.text
    assert "did not stop" in caplog.text


async def test_stop_bots_retires_everything_even_when_disable_runs_meanwhile(
    running: dict[str, dict[str, Any]], bots_file: Path
) -> None:
    """整批重載停到一半，停用改了執行清單：不丟例外，兩邊結果都對."""
    _put_bots(bots_file, telegram=[_bot("t1", "Later")])
    later = FakeBot("Later")
    first = FakeBot(
        "First",
        on_stop=lambda: bot_service.disable_bots_for(AgentIdentity(agent_id="Later")),
    )
    _run(running, "telegram", "k1", first)
    _run(running, "telegram", "k2", later)
    await bot_service.stop_bots()
    assert first.stopped
    assert later.stopped
    assert first.retired
    assert later.retired
    # 先換成空清單、標停用，才開始停（停完才換的話，重疊的重載會留下沒停的機器人）
    assert first.seen_at_stop == (True, False)
    assert later.seen_at_stop == (True, False)
    assert bot_service._running_bots == {"discord": {}, "telegram": {}}
    assert _enabled(bots_file) == {"t1": False}


async def test_stop_bots_keeps_going_past_broken_bots(
    running: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_service_module, "BOT_STOP_SECONDS", 0.05)
    broken = _run(running, "telegram", "k1", FakeBot("A", fail=True))
    stuck = _run(running, "telegram", "k2", FakeBot("B", hang=True))
    fine = _run(running, "discord", "k3", FakeBot("C"))
    await bot_service.stop_bots()
    assert fine.stopped
    assert broken.retired
    assert stuck.retired


def _fake_application() -> MagicMock:
    application = MagicMock()
    for name in ("initialize", "start", "stop", "shutdown"):
        setattr(application, name, AsyncMock())
    application.updater.start_polling = AsyncMock()
    application.updater.stop = AsyncMock()
    return application


async def test_telegram_bot_retired_while_starting_stops_itself() -> None:
    adapter = TelegramAdapter(token="123:synthetic", agent_id="Alice")  # noqa: S106
    application = _fake_application()

    async def _retire_meanwhile(*_args: Any, **_kwargs: Any) -> None:
        adapter.retired = True

    application.updater.start_polling.side_effect = _retire_meanwhile
    adapter.application = application
    await adapter.start()
    application.stop.assert_awaited()
    application.shutdown.assert_awaited()


async def test_telegram_bot_retired_before_starting_does_not_start() -> None:
    adapter = TelegramAdapter(token="123:synthetic", agent_id="Alice")  # noqa: S106
    application = _fake_application()
    adapter.application = application
    adapter.retired = True
    await adapter.start()
    application.initialize.assert_not_awaited()
    application.updater.start_polling.assert_not_awaited()


@pytest.fixture
def offline_discord(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """discord.Client 換成假的：就算 adapter 照樣啟動也不連網路."""
    client = MagicMock()
    client.start = AsyncMock()
    client.close = AsyncMock()
    client.is_closed = MagicMock(return_value=True)
    factory = MagicMock(return_value=client)
    monkeypatch.setattr("src.adapters.discord_bot.discord.Client", factory)
    return factory


async def _settle(adapter: DiscordAdapter) -> None:
    if adapter._task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await adapter._task


async def test_discord_bot_retired_before_starting_does_not_start(
    offline_discord: MagicMock,
) -> None:
    adapter = DiscordAdapter(token="synthetic", agent_id="Alice")  # noqa: S106
    adapter.retired = True
    await adapter.start()
    started = adapter._task is not None
    await _settle(adapter)
    assert not started
    assert adapter.client is None
    offline_discord.assert_not_called()


async def test_discord_bot_retired_before_its_task_runs_does_not_connect(
    offline_discord: MagicMock,
) -> None:
    adapter = DiscordAdapter(token="synthetic", agent_id="Alice")  # noqa: S106
    await adapter.start()
    adapter.retired = True
    assert adapter._task is not None
    await _settle(adapter)
    assert adapter.client is None
    offline_discord.assert_not_called()


# --------------------------------------------------------------------------- #
# 鎖
# --------------------------------------------------------------------------- #


async def test_every_bots_json_read_modify_write_holds_the_lock(
    bots_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, bool]] = []
    real_read, real_write = resource_manager.read_json, resource_manager.write_json

    def _read(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == bots_file:
            seen.append(("read", bot_service.locked().locked()))
        return real_read(path, *args, **kwargs)

    def _write(path: Any, data: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == bots_file:
            seen.append(("write", bot_service.locked().locked()))
        real_write(path, data, *args, **kwargs)

    monkeypatch.setattr(resource_manager, "read_json", _read)
    monkeypatch.setattr(resource_manager, "write_json", _write)
    monkeypatch.setattr(bot_service, "reload_bots", AsyncMock())

    # GET 的一次性遷移（條目沒有 id）
    _put_bots(bots_file, telegram=[{"token": "tok", "agent_id": "Alice", "enabled": True}])
    assert _send("GET", "/v1/bots", None).status_code == 200
    # 設定頁存檔
    body = {"discord": [], "telegram": [_bot("t1", "Alice")]}
    assert _send("POST", "/v1/bots", body).status_code == 200
    # 停用
    bot_service.disable_bots_for(AgentIdentity(agent_id="Alice"))

    assert [op for op, _ in seen].count("read") >= 3
    assert [op for op, _ in seen].count("write") >= 3
    assert all(held for _, held in seen), seen
