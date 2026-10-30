"""刪除靈魂時清掉它在隔離暫存區的 HOME／TMP（ADR-0272 已知限制 26）.

家目錄依 agent ID 放在 ``touka-executor-state``（執行器）與 ``touka-sandbox-state``（proxy
本機那一層），刪除靈魂原本不碰：同 ID 的新靈魂會沿用舊靈魂留在 ``~`` 的檔案（例如
命令列工具的權杖）。現在刪除前先清這兩處：proxy 那一層直接刪（唯讀的也刪、連結不跟）；
執行器那一層 proxy 碰不到，走既有 START 跑固定的清除指令。預期是執行器卻清不掉（不在
READY、指令失敗、逾時）就 503、什麼都不刪。

全部 sandbox／mock：執行器、runner 與 spawn 都是替身；真執行器的端到端在
``tests/integration/test_isolation_linux.py``。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from memory_server.utils.zone_paths import Anchor
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import souls
from src.services.agent_reset.primitives import RETAINED_STORES
from src.services.auth_service import AuthService
from src.services.identity_router import get_verified_identity
from src.services.isolation import home_wipe
from src.services.isolation.profiles import current_layout, state_dirs
from src.services.isolation.runners import Acquired, IsolationUnavailableError
from src.services.resource_manager import resource_manager
from src.tools import terminal

from tests.support.links import CHANGED_ANYWAY, LINK_FOLLOWED, make_dir_link
from tests.support.zone_roots import point_zone_roots

if sys.platform == "win32":
    import _winapi

if TYPE_CHECKING:
    from collections.abc import Iterator

SOUL = "Jin"
ADMIN_PW = "WipePass123!"
STATE_ROOT = "/x/executor_state"


def _link_dir(link: Path, target: Path) -> None:
    """不需要管理員權限的資料夾連結：Windows 用 junction，其他用符號連結."""
    if sys.platform == "win32":
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _proxy_owner(agent_id: str) -> Path:
    home, _tmp = state_dirs(home_wipe.current_layout(), "proxy", "agents", agent_id)
    (home / "token").write_text("old soul secret", encoding="utf-8")
    return home.parent


class _Runner:
    def __init__(self, failure: str | None = None) -> None:
        self.requests: list = []
        self.plan_obj = SimpleNamespace(
            argv=("relay",), cwd=Path(), env={}, pass_fds=(), release=Mock(),
            read_status=Mock(return_value=b""),
        )
        self._failure = failure

    def plan(self, request):  # noqa: ANN001, ANN201
        self.requests.append(request)
        return self.plan_obj

    def failure(self, _status: bytes):  # noqa: ANN201
        return SimpleNamespace(code=self._failure) if self._failure else None


class _Broker:
    def __init__(self, runner: _Runner | None = None, error: str | None = None) -> None:
        self.runner, self.error, self.sources = runner or _Runner(), error, []

    async def acquire_maintenance(self, source) -> Acquired:  # noqa: ANN001
        self.sources.append(source)
        if self.error:
            raise IsolationUnavailableError(self.error)
        return Acquired(self.runner, await source.build("executor", STATE_ROOT))


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN201
    """預期是執行器的部署：替身 monitor、broker 與 spawn（結束碼可改）；ST 是這個測試自己的."""
    # ST 換成這個測試自己的；版面與 RM 用同一個（ADR-0293 P7）
    point_zone_roots(monkeypatch, Path(resource_manager.get_base_path()), tmp_path / "st")
    layout = current_layout()
    monkeypatch.setattr(home_wipe, "current_layout", lambda: layout)
    monitor = SimpleNamespace(
        ensure_ready=AsyncMock(return_value=SimpleNamespace(expected="executor")),
        block_ports=Mock(return_value=()),
        executor_scope_issue=Mock(return_value=None),
        report_failure=Mock(),
    )
    proc = SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0)
    spawn = AsyncMock(return_value=proc)
    broker = _Broker()
    monkeypatch.setattr(home_wipe, "isolation_monitor", monitor)
    monkeypatch.setattr(home_wipe, "runner_broker", broker)
    monkeypatch.setattr(home_wipe, "spawn_exec", spawn)
    return SimpleNamespace(monitor=monitor, broker=broker, spawn=spawn, proc=proc)


# ── 清除本身 ──────────────────────────────────────────────────────────────────


async def test_executor_wipe_runs_the_fixed_script_for_this_soul_only(wiring) -> None:
    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    (request,) = wiring.broker.runner.requests
    profile = request.profile
    home, tmp = profile.env_set["HOME"], profile.env_set["TMPDIR"]
    assert request.argv == ("/bin/sh", "-c", home_wipe.WIPE_SCRIPT, "touka-home-wipe", home, tmp)
    assert Path(home) == Path(STATE_ROOT) / "agents" / SOUL / "home"
    assert dict(request.env) == {}  # 不帶 proxy 的環境
    assert profile.owner == SOUL and profile.project_root is None
    drive = Path(resource_manager.get_base_path()) / "personal_drive" / SOUL
    assert profile.cwd.resolve() == drive.resolve() and drive.is_dir()  # START 的 cwd
    (source,) = wiring.broker.sources
    assert source.executor_issue is None
    wiring.spawn.assert_awaited_once()
    assert wiring.spawn.await_args.args == ("relay",)
    wiring.broker.runner.plan_obj.release.assert_called_once()


async def test_proxy_state_of_this_soul_is_removed_and_others_are_kept(wiring) -> None:
    mine, other = _proxy_owner(SOUL), _proxy_owner("Bob")

    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert not mine.exists()
    assert (other / "home" / "token").read_text(encoding="utf-8") == "old soul secret"


async def test_without_an_executor_only_the_proxy_state_is_cleared(wiring) -> None:
    wiring.monitor.ensure_ready.return_value = SimpleNamespace(expected="landlock")
    mine = _proxy_owner(SOUL)

    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert not mine.exists()
    assert wiring.broker.sources == []
    wiring.spawn.assert_not_awaited()


async def test_executor_not_ready_refuses_before_touching_anything(wiring) -> None:
    wiring.broker.error = "executor_unreachable"
    mine = _proxy_owner("Unready")

    with pytest.raises(home_wipe.HomeWipeError) as exc:
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id="Unready"))

    assert exc.value.reason == "executor_unreachable"
    assert (mine / "home" / "token").exists()
    wiring.spawn.assert_not_awaited()
    drive = Path(resource_manager.get_base_path()) / "personal_drive" / "Unready"
    assert not drive.exists()  # 拿不到執行器就不在 data 留空資料夾


async def test_read_only_folders_in_the_proxy_state_are_still_removed(wiring) -> None:
    """Go 模組快取（0555）、代理人自己 chmod 000 的資料夾、Windows 的唯讀檔都刪得掉."""
    wiring.monitor.ensure_ready.return_value = SimpleNamespace(expected="landlock")
    mine = _proxy_owner(SOUL)
    cache = mine / "home" / "go" / "pkg"
    cache.mkdir(parents=True)
    (cache / "mod.txt").write_text("x", encoding="utf-8")
    (cache / "mod.txt").chmod(stat.S_IREAD)
    sealed = mine / "home" / "sealed"
    (sealed / "inner").mkdir(parents=True)
    cache.chmod(0o555)
    sealed.chmod(0)
    try:
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))
    finally:
        for path in (sealed, cache, cache / "mod.txt"):
            with contextlib.suppress(OSError):
                path.chmod(stat.S_IRWXU)  # 失敗時讓 tmp_path 收得掉

    assert not mine.exists()


async def test_unlocking_stays_inside_the_tree_and_does_not_follow_links(
    wiring, tmp_path: Path
) -> None:
    """連結放在唯讀資料夾裡：POSIX 上刪連結會失敗、補權限時拿連結當目標，不能改到外面."""
    wiring.monitor.ensure_ready.return_value = SimpleNamespace(expected="landlock")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    outside.chmod(0o755)
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (outside / "keep.txt").chmod(stat.S_IREAD)
    before = stat.S_IMODE(outside.stat().st_mode)
    mine = _proxy_owner(SOUL)
    sealed = mine / "home" / "sealed"
    sealed.mkdir()
    _link_dir(sealed / "out", outside)
    (sealed / "ro.txt").write_text("x", encoding="utf-8")
    (sealed / "ro.txt").chmod(stat.S_IREAD)
    sealed.chmod(0o555)

    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert not mine.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not os.access(outside / "keep.txt", os.W_OK)  # 外面的權限沒被改
    assert stat.S_IMODE(outside.stat().st_mode) == before


@pytest.mark.parametrize("calls", [("rmdir",), ("scandir", "open")])
async def test_a_permission_error_that_stays_is_refused_not_retried_forever(
    wiring, monkeypatch, calls: tuple[str, ...]
) -> None:
    """補過權限還是刪不掉或列不出來（例如 Windows 被別的行程開著）：回清不掉，不無限重試."""
    wiring.monitor.ensure_ready.return_value = SimpleNamespace(expected="landlock")
    mine = _proxy_owner(SOUL)
    (mine / "home" / "stuck").mkdir()

    def busy(real):  # noqa: ANN001, ANN202
        def call(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if isinstance(path, str | os.PathLike) and Path(path).name == "stuck":
                raise PermissionError(13, "in use", str(path))
            return real(path, *args, **kwargs)

        return call

    for name in calls:
        monkeypatch.setattr(os, name, busy(getattr(os, name)))
    with pytest.raises(home_wipe.HomeWipeError) as exc:
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert exc.value.reason == "proxy_state_remove_failed"


async def test_an_id_that_is_not_a_safe_segment_has_nothing_to_wipe(wiring) -> None:
    """profile 一律拒絕這種名稱，所以不會有家目錄；照常讓刪除進行，不當成執行器不可用."""
    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id="Jin "))

    assert wiring.broker.sources == []
    wiring.spawn.assert_not_awaited()


async def test_cancelled_request_also_stops_the_relay(wiring, monkeypatch) -> None:
    async def cancelled() -> int:
        raise asyncio.CancelledError

    wiring.proc.wait = cancelled
    reap = AsyncMock()
    monkeypatch.setattr(home_wipe, "reap_cancelled_process", reap)

    with pytest.raises(asyncio.CancelledError):
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    reap.assert_awaited_once()


def test_reset_report_says_the_sandbox_home_is_kept() -> None:
    """重置不清隔離家目錄（刪除靈魂時才清）：報告的保留清單要照實列出，clean 不被誤讀."""
    assert "sandbox_home" in {name for name, _reason in RETAINED_STORES}


def test_only_home_wipe_uses_the_maintenance_runner() -> None:
    """不看同意的 ``acquire_maintenance`` 只給 home_wipe 的固定清除指令用（禁令 66 ①④）."""
    src = Path(home_wipe.__file__).resolve().parents[2]
    allowed = {"services/isolation/runners.py", "services/isolation/home_wipe.py"}
    users = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if any(
            isinstance(node, ast.Attribute) and node.attr == "acquire_maintenance"
            or isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "acquire_maintenance"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert users == allowed


@pytest.mark.parametrize(("exit_code", "failure"), [(3, None), (0, "spec_invalid")])
async def test_failed_wipe_refuses(wiring, exit_code: int, failure: str | None) -> None:
    wiring.proc.wait.return_value = wiring.proc.returncode = exit_code
    wiring.broker.runner = _Runner(failure)
    mine = _proxy_owner(SOUL)

    with pytest.raises(home_wipe.HomeWipeError):
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert (mine / "home" / "token").exists()
    if failure:
        wiring.monitor.report_failure.assert_called_once_with(failure)


async def test_timeout_kills_the_relay_and_refuses(wiring, monkeypatch) -> None:
    async def never() -> int:
        raise TimeoutError

    wiring.proc.wait = never
    reap = AsyncMock()
    monkeypatch.setattr(home_wipe, "reap_process_tree", reap)

    with pytest.raises(home_wipe.HomeWipeError) as exc:
        await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert exc.value.reason == "timeout"
    reap.assert_awaited_once()


async def test_a_linked_proxy_owner_only_loses_the_link(wiring, tmp_path: Path) -> None:
    """``ST/proxy/agents/Jin`` 本身是連到外面的連結：只刪連結，不跟過去（ADR-0293 PR 2）."""
    outside = tmp_path / "outside"
    (outside / "home").mkdir(parents=True)
    (outside / "home" / "keep.txt").write_text("keep", encoding="utf-8")
    owner = _proxy_owner("Other").parent / SOUL
    _link_dir(owner, outside)

    await home_wipe.wipe_agent_homes(AgentIdentity(agent_id=SOUL))

    assert not os.path.lexists(owner)
    assert (outside / "home" / "keep.txt").read_text(encoding="utf-8") == "keep"


# ── 刪除靈魂的端點 ────────────────────────────────────────────────────────────


@pytest.fixture
def admin(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    slug = "".join(c for c in request.node.name if c.isalnum())
    user = AuthService().create_user(f"wipe-{slug}@test.com", ADMIN_PW, role="admin")
    identity = AgentIdentity.for_role(user.id, "admin", is_human=True, owned_agents=[])
    monkeypatch.setattr(souls, "assert_memory_cleared", AsyncMock())
    monkeypatch.setattr(souls, "assert_timeline_cleared", AsyncMock())
    app.dependency_overrides[get_verified_identity] = lambda: identity
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _soul_dir() -> Path:
    path = Path(resource_manager.get_base_path()) / "soul" / SOUL
    path.mkdir(parents=True, exist_ok=True)
    return path


def _delete(client: TestClient, password: str = ADMIN_PW):  # noqa: ANN202
    return client.request("DELETE", f"/v1/souls/{SOUL}", json={"admin_password": password})


def test_delete_clears_the_homes_before_deleting_the_soul(admin, monkeypatch) -> None:
    soul_dir = _soul_dir()
    seen: list[tuple[str, bool]] = []

    async def stop(identity: AgentIdentity) -> int:
        seen.append((f"stop:{identity.agent_id}", soul_dir.exists()))
        return 0

    async def wipe(identity: AgentIdentity) -> None:
        seen.append((identity.agent_id, soul_dir.exists()))
        # 執行器範圍看 personal vault：沒有角色時 soul/ 不在就會解析到 custom 的 drive
        drive = resource_manager.agent_drive(identity)
        assert drive is not None and drive.anchor is Anchor.PERSONAL_DRIVE

    monkeypatch.setattr(terminal, "stop_agent_commands", stop)
    monkeypatch.setattr(souls, "wipe_agent_homes", wipe)
    resp = _delete(admin)

    assert resp.status_code == 200, resp.text
    # 先停這個靈魂的指令（R11），再清家目錄，那時靈魂資料都還在
    assert seen == [(f"stop:{SOUL}", True), (SOUL, True)]
    assert not soul_dir.exists()


def test_deleting_an_orphan_still_clears_its_personal_drive_home(admin, monkeypatch) -> None:
    """``soul/<id>`` 已不在（建立失敗留下的孤兒）也照樣清：家目錄跟著 personal_drive 走."""
    seen: list[str] = []

    async def wipe(identity: AgentIdentity) -> None:
        drive = resource_manager.agent_drive(identity)
        seen.append(drive.anchor.name if drive else "")

    monkeypatch.setattr(souls, "wipe_agent_homes", wipe)
    assert _delete(admin).status_code == 200
    assert seen == [Anchor.PERSONAL_DRIVE.name]


def test_a_linked_personal_drive_refuses_the_delete(
    zone_data_root: Path, admin, monkeypatch, tmp_path: Path
) -> None:
    """個人空間根是連結：整個刪除失敗、什麼都不刪，連結另一端也沒動（ADR-0293 R11）."""
    fake = tmp_path.resolve() / "fake_config"
    fake.mkdir()
    (fake / "users.json").write_text("{}", encoding="utf-8")
    soul_dir = _soul_dir()
    (zone_data_root / "personal_drive").mkdir()
    make_dir_link(zone_data_root / "personal_drive" / SOUL, fake)
    wipe, stop = AsyncMock(), AsyncMock(return_value=0)
    monkeypatch.setattr(souls, "wipe_agent_homes", wipe)
    monkeypatch.setattr(terminal, "stop_agent_commands", stop, raising=False)

    resp = _delete(admin)

    assert (fake / "users.json").read_text(encoding="utf-8") == "{}", LINK_FOLLOWED
    assert soul_dir.exists(), CHANGED_ANYWAY
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "AGENT_DRIVE_LINKED"
    wipe.assert_not_awaited()
    stop.assert_not_awaited()
    assert os.path.lexists(zone_data_root / "personal_drive" / SOUL)


def test_delete_is_refused_when_the_homes_cannot_be_cleared(admin, monkeypatch) -> None:
    soul_dir = _soul_dir()
    monkeypatch.setattr(
        souls, "wipe_agent_homes", AsyncMock(side_effect=home_wipe.HomeWipeError("timeout"))
    )
    resp = _delete(admin)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "SOUL_SANDBOX_HOME_NOT_CLEARED"
    assert soul_dir.exists()  # 什麼都沒刪


def test_wrong_password_does_not_reach_the_wipe(admin, monkeypatch) -> None:
    wipe = AsyncMock()
    monkeypatch.setattr(souls, "wipe_agent_homes", wipe)
    _soul_dir()

    assert _delete(admin, password="WRONG").status_code == 403
    wipe.assert_not_awaited()
