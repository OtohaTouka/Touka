"""清除連備份：重置端點的誤觸發防護（E 組，對照設計 §1-1、§11、§12-2）.

端點函式直接呼叫（比照 test_per_agent_reset）；請求模型的格式檢查另外驗。任何被拒絕的情況都不得
呼叫到重置。只用合成 ID。
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from src.core.exceptions import ErrorCode, ToukaException
from src.models.identity import AgentIdentity
from src.models.system_restore import RestoreStatus
from src.routers import admin as A
from src.routers import debug as debug_router
from src.routers import system as system_router
from src.services import memory_scrub
from src.services.agent_reset import orchestrator as O
from src.services.agent_reset import primitives as P

from tests.helpers.memory_scrub import TARGET

ROOT = Path(__file__).resolve().parents[2]
HUMAN_ADMIN = AgentIdentity.for_role(agent_id="zz-admin-h", role="admin", is_human=True)
ASSUMED_ADMIN = AgentIdentity.for_role(agent_id="zz-admin-x", role="admin")  # X-Agent-ID 代打的形狀
USER = SimpleNamespace(role="admin", hashed_password="h", id="zz-admin-h")


def _request(**overrides: object) -> A.AgentResetRequest:
    body = {
        "confirm": True,
        "confirm_agent_id": TARGET,
        "current_password": "pw",
        "scrub_backups": True,
        "scrub_acknowledged": True,
    }
    body.update(overrides)
    return A.AgentResetRequest(**body)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    async def fake_reset(target: str, actor: AgentIdentity, **kwargs: object) -> O.ResetReport:
        seen.append({"target": target, "actor": actor.agent_id, **kwargs})
        return O.ResetReport(agent=target, clean=True)

    monkeypatch.setattr(A, "reset_agent_data", fake_reset)
    monkeypatch.setattr(A, "verify_origin", lambda _r: None)
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="idle"))
    return seen


def _rejected(caplog: pytest.LogCaptureFixture) -> list[str]:
    """勾選清除卻被擋下的稽核行，依序取出原因代碼."""
    return [
        record.getMessage().rsplit("reason=", 1)[1]
        for record in caplog.records
        if record.getMessage().startswith("AUDIT: memory-scrub rejected")
    ]


def _auth(ok: bool = True) -> Mock:
    auth = Mock()
    auth.verify_password.return_value = ok
    return auth


async def _call(
    request: A.AgentResetRequest, actor: AgentIdentity = HUMAN_ADMIN, auth: Mock | None = None
) -> dict:
    return await A.reset_agent(TARGET, request, None, actor, USER, auth or _auth())


async def test_e01_without_the_checkbox_nothing_is_scrubbed(calls: list[dict]) -> None:
    await _call(_request(scrub_backups=False, scrub_acknowledged=False))
    assert calls == [{"target": TARGET, "actor": HUMAN_ADMIN.agent_id, "scrub": False}]


async def test_e02_checkbox_without_second_confirmation(
    calls: list[dict], caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ToukaException) as exc:
        await _call(_request(scrub_acknowledged=False))
    assert exc.value.code is ErrorCode.MEMORY_SCRUB_NOT_ACKNOWLEDGED
    assert calls == []
    assert "AUDIT: memory-scrub rejected" in caplog.text and "not_acknowledged" in caplog.text


async def test_e03_wrong_password(calls: list[dict], caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ToukaException) as exc:
        await _call(_request(), auth=_auth(ok=False))
    assert exc.value.code is ErrorCode.AUTH_REAUTH_FAILED
    assert calls == []
    assert "bad_password" in caplog.text


async def test_e04_bad_origin(
    calls: list[dict], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_request: object) -> None:
        raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

    monkeypatch.setattr(A, "verify_origin", refuse)
    with pytest.raises(ToukaException) as exc:
        await _call(_request())
    assert exc.value.code is ErrorCode.AUTH_PERMISSION_DENIED
    assert calls == []
    assert _rejected(caplog) == ["bad_origin"]


async def test_e05_assumed_admin_cannot_scrub_and_is_not_asked_for_a_password(
    calls: list[dict], caplog: pytest.LogCaptureFixture
) -> None:
    auth = _auth()
    with pytest.raises(ToukaException) as exc:
        await _call(_request(), actor=ASSUMED_ADMIN, auth=auth)
    assert exc.value.code is ErrorCode.AUTH_ADMIN_REQUIRED
    assert calls == []
    auth.verify_password.assert_not_called()
    assert "not_human" in caplog.text
    await _call(_request(scrub_backups=False, scrub_acknowledged=False), actor=ASSUMED_ADMIN)
    assert calls == [{"target": TARGET, "actor": ASSUMED_ADMIN.agent_id, "scrub": False}]


def test_e06_non_admin_is_refused_by_the_dependency() -> None:
    with pytest.raises(ToukaException) as exc:
        A.require_admin(AgentIdentity.for_role(agent_id="zz-user", role="normal", is_human=True))
    assert exc.value.code is ErrorCode.AUTH_ADMIN_REQUIRED


async def test_e07_confirm_mismatch(calls: list[dict], caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ToukaException):
        await _call(_request(confirm_agent_id="zz-someone-else"))
    assert calls == []
    assert _rejected(caplog) == ["not_confirmed"]
    caplog.clear()
    with pytest.raises(ToukaException):
        await _call(_request(confirm_agent_id="zz-someone-else", scrub_backups=False))
    assert _rejected(caplog) == []  # 沒勾清除就沒有清除的稽核行


@pytest.mark.parametrize(
    "body",
    [
        {
            "confirm": True,
            "confirm_agent_id": TARGET,
            "current_password": "pw",
            "scrub_backup": True,
        },
        {
            "confirm": True,
            "confirm_agent_id": TARGET,
            "current_password": "pw",
            "scrub_backups": "true",
        },
        {
            "confirm": True,
            "confirm_agent_id": TARGET,
            "current_password": "pw",
            "scrub_acknowledged": 1,
        },
    ],
    ids=["typo", "string-true", "int-one"],
)
def test_e08_e16_request_shape_is_strict(body: dict) -> None:
    with pytest.raises(ValidationError):
        A.AgentResetRequest.model_validate(body)


async def test_e09_debug_reset_cannot_scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    async def fake_debug_reset(*args: object, **kwargs: object) -> O.ResetReport:
        seen.append(kwargs)
        return O.ResetReport(agent="DebugAgent", clean=True)

    monkeypatch.setattr(debug_router, "reset_debug_agent_data", fake_debug_reset)
    payload = debug_router.DebugResetRequest.model_validate(
        {
            "confirm": True,
            "confirm_agent_id": "DebugAgent",
            "scrub_backups": True,
            "scrub_acknowledged": True,
        }
    )
    await debug_router.debug_reset(payload, HUMAN_ADMIN)
    assert seen == [{}]


# --- E10 不做成工具：允許清單 -----------------------------------------------------

_SCAN_ROOTS = ("ToukaProxy/src", "ToukaMemory/src", "ToukaDispatch", "ToukaHeartbeat")
_MEMORY_SCRUB = "ToukaProxy/src/services/memory_scrub/"
_RESET_ORCHESTRATOR = "ToukaProxy/src/services/agent_reset/orchestrator.py"
_PRIMITIVES = "ToukaProxy/src/services/agent_reset/primitives.py"
# 名稱 → 只能出現在這些地方（屬性存取、直接名稱、from-import 的名稱或別名都算）
_ALLOWED_USES = {
    "scrub_store_image": {_MEMORY_SCRUB},
    "inspect_store_image": {_MEMORY_SCRUB},
    "compact_after_reset": {_MEMORY_SCRUB},
    "upgrade_backup_lock": {_MEMORY_SCRUB},
    "rewrite_archive": {_MEMORY_SCRUB},
    "delete_points": {_PRIMITIVES},
    "count_points_outside": {
        _PRIMITIVES,
        "ToukaProxy/src/services/agent_reset/delete_guard.py",
    },
    "agent_fact_ids": {_PRIMITIVES, "ToukaProxy/src/services/memory_collection_guard.py"},
    "_execute_reset": {_RESET_ORCHESTRATOR},
    "run_after_reset": {_MEMORY_SCRUB, _RESET_ORCHESTRATOR},
    "memory_collection_guard": {
        "ToukaProxy/src/services/memory_collection_guard.py",
        _PRIMITIVES,
        "ToukaProxy/src/services/agent_reset/delete_guard.py",
        _RESET_ORCHESTRATOR,
        _MEMORY_SCRUB,
        "ToukaProxy/src/routers/souls.py",
        "ToukaProxy/src/routers/custom_models.py",
    },
}
# 定義這些方法的檔案（memory_scrub 以外）
_DEFINITIONS = {
    "ToukaMemory/src/memory_server/factual.py",
    "ToukaMemory/src/memory_server/vector.py",
    "ToukaProxy/src/services/resource_manager.py",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in _SCAN_ROOTS:
        files.extend(
            p
            for p in (ROOT / root).rglob("*.py")
            if "node_modules" not in p.parts and ".venv" not in p.parts
        )
    return files


def _allowed(relative: str, prefixes: set[str]) -> bool:
    return any(relative == p or relative.startswith(p) for p in prefixes)


def _names_used(node: ast.AST) -> list[str]:
    """這個節點用到的名稱：屬性、直接名稱、匯入的名稱與別名."""
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        found = []
        for alias in node.names:
            found.append(alias.name.rsplit(".", 1)[-1])
            if alias.asname:
                found.append(alias.asname)
        return found
    return []


def _offenses(relative: str, node: ast.AST) -> list[str]:
    found = [
        f"{relative}:{node.lineno} {name}"
        for name in _names_used(node)
        if name in _ALLOWED_USES
        and relative not in _DEFINITIONS
        and not _allowed(relative, _ALLOWED_USES[name])
    ]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        names = [alias.name for alias in node.names]
        touches = (
            (module or "").startswith("src.services.memory_scrub")
            or any(n.startswith("src.services.memory_scrub") for n in names)
            or (module == "src.services" and "memory_scrub" in names)
        )
        allowed = (
            relative.startswith(_MEMORY_SCRUB)
            or relative == _RESET_ORCHESTRATOR
            or (relative == "ToukaProxy/src/routers/system.py" and names == ["in_progress"])
        )
        if touches and not allowed:
            found.append(f"{relative}:{node.lineno} import memory_scrub")
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "begin"
        and getattr(node.value, "id", "") == "memory_scrub"
        and relative != _RESET_ORCHESTRATOR
    ):
        found.append(f"{relative}:{node.lineno} memory_scrub.begin")
    if isinstance(node, ast.Call) and (
        getattr(node.func, "id", getattr(node.func, "attr", "")) == "reset_agent_data"
    ):
        scrub = any(k.arg == "scrub" for k in node.keywords)
        spread = any(k.arg is None for k in node.keywords)  # **kwargs 也可能帶 scrub
        if spread or (scrub and relative != "ToukaProxy/src/routers/admin.py"):
            found.append(f"{relative}:{node.lineno} reset_agent_data(scrub=)")
    return found


def test_e10_scrub_entry_points_only_appear_where_allowed() -> None:
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.extend(_offenses(relative, node))
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "from memory_server.factual import scrub_store_image\nscrub_store_image(b'', None, None)",
        "from memory_server.factual import upgrade_backup_lock as lock\n",
        "from src.services.agent_reset.orchestrator import _execute_reset\n",
        "reset_agent_data('x', actor, **{'scrub': True})",
        "from src.services import memory_scrub\nmemory_scrub.begin()",
        "from src.services import memory_collection_guard\n",
        "orchestrator.run_after_reset(identity)",
    ],
)
def test_e10b_the_scan_catches_indirect_uses(source: str) -> None:
    relative = "ToukaProxy/src/tools/some_tool.py"
    offenders = [o for n in ast.walk(ast.parse(source)) for o in _offenses(relative, n)]
    assert offenders, source


# --- E11 守衛 -----------------------------------------------------------------


@pytest.mark.parametrize("target", ["DebugAgent", "zz-does-not-exist"])
async def test_e11_debugagent_and_unknown_targets_are_refused(
    target: str, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ToukaException):
        await O.reset_agent_data(target, HUMAN_ADMIN, scrub=True)
    assert _rejected(caplog) == ["not_resettable"]


async def test_e11b_coordinator_requires_a_human_admin_for_scrub(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def must_not_run(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("the target check must not run for a refused actor")

    monkeypatch.setattr(O, "assert_agent_resettable", must_not_run)
    for actor in (
        ASSUMED_ADMIN,
        AgentIdentity.for_role(agent_id="zz-user", role="normal", is_human=True),
    ):
        with pytest.raises(ToukaException):
            await O.reset_agent_data(TARGET, actor, scrub=True)
    assert _rejected(caplog) == ["not_human", "not_human"]


# --- E12 還原進行中、E14／E15 清除或重置進行中 -----------------------------------------


@pytest.mark.parametrize("scrub", [True, False])
async def test_e12_restore_in_progress_blocks_every_reset(
    calls: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    scrub: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="running"))
    request = _request() if scrub else _request(scrub_backups=False, scrub_acknowledged=False)
    with pytest.raises(ToukaException) as exc:
        await _call(request)
    assert exc.value.code is ErrorCode.SYSTEM_RESTORE_IN_PROGRESS
    assert calls == []
    assert _rejected(caplog) == (["restore_in_progress"] if scrub else [])


async def test_e14_restore_is_refused_while_a_scrub_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    session = memory_scrub.begin(AgentIdentity(agent_id=TARGET))
    try:
        auth = _auth()
        request = system_router.RestoreRequest(filename="Touka_Backup_x.zip", current_password="pw")
        user = SimpleNamespace(role="admin", hashed_password="h")
        monkeypatch.setattr(system_router, "verify_origin", lambda _r: None)
        with pytest.raises(ToukaException) as exc:
            await system_router.run_restore(request, Mock(), None, user, auth)
        assert exc.value.code is ErrorCode.MEMORY_SCRUB_IN_PROGRESS
    finally:
        session.end()
    assert not memory_scrub.in_progress()


async def test_e15_another_reset_is_refused_while_a_scrub_or_reset_runs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(P, "_identity", lambda candidate, guard: AgentIdentity(agent_id=candidate))
    session = memory_scrub.begin(AgentIdentity(agent_id=TARGET))
    try:
        with pytest.raises(ToukaException) as exc:
            await O._execute_reset(
                TARGET, lambda c: c, do_backup=False, scrub=True, actor_label="x"
            )
        assert exc.value.code is ErrorCode.AGENT_RESET_IN_PROGRESS
        assert _rejected(caplog) == ["reset_in_progress"]
    finally:
        session.end()
    async with O._reset_lock:
        with pytest.raises(ToukaException) as exc:
            await O._execute_reset(TARGET, lambda c: c, do_backup=False)
        assert exc.value.code is ErrorCode.AGENT_RESET_IN_PROGRESS
    await asyncio.sleep(0)


# --- E19／E20 還原與重置雙向互斥 -------------------------------------------------


async def test_e19_restore_is_refused_while_a_reset_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    request = system_router.RestoreRequest(filename="Touka_Backup_x.zip", current_password="pw")
    user = SimpleNamespace(role="admin", hashed_password="h")
    monkeypatch.setattr(system_router, "verify_origin", lambda _r: None)
    async with O._reset_lock:
        with pytest.raises(ToukaException) as exc:
            await system_router.run_restore(request, Mock(), None, user, _auth())
    assert exc.value.code is ErrorCode.AGENT_RESET_IN_PROGRESS
    assert system_router.restore_in_progress() is False


async def test_e20_debug_reset_is_refused_while_a_restore_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    async def fake_debug_reset(*args: object, **kwargs: object) -> O.ResetReport:
        seen.append(kwargs)
        return O.ResetReport(agent="DebugAgent", clean=True)

    monkeypatch.setattr(debug_router, "reset_debug_agent_data", fake_debug_reset)
    monkeypatch.setattr(system_router, "_last_restore", RestoreStatus(state="running"))
    payload = debug_router.DebugResetRequest.model_validate(
        {"confirm": True, "confirm_agent_id": "DebugAgent"}
    )
    with pytest.raises(ToukaException) as exc:
        await debug_router.debug_reset(payload, HUMAN_ADMIN)
    assert exc.value.code is ErrorCode.SYSTEM_RESTORE_IN_PROGRESS
    assert seen == []


# --- nginx 範例：只有重置端點拉長讀取逾時（R7 後半） -----------------------------------

_NGINX = ROOT / "nginx" / "nginx.conf.example"
_RESET_LOCATION = "location ~ ^/(v1/)?admin/agents/[^/]+/reset$"


def _location_block(text: str, header: str) -> list[str]:
    start = text.index(header + " {")
    end = text.index("\n    }", start)
    return [line.strip() for line in text[start:end].splitlines()[1:] if line.strip()]


def _api_server(text: str) -> str:
    return text[text.index("server_name api.yourdomain.com;") :]


def test_nginx_reset_location_only_extends_the_read_timeout() -> None:
    api = _api_server(_NGINX.read_text(encoding="utf-8"))
    reset = _location_block(api, _RESET_LOCATION)
    rest = _location_block(api, "location /")
    directives = [line for line in reset if not line.startswith("#")]
    rest_directives = [line for line in rest if not line.startswith("#")]
    assert "proxy_read_timeout 1800s;" in directives
    # 其餘設定照抄 REST 的 location /（header、upstream 都一樣，沒有漏）
    assert [d for d in directives if d != "proxy_read_timeout 1800s;"] == rest_directives
    assert not any(d.startswith("proxy_pass") and "8000/" in d for d in directives)  # 不改寫路徑
    assert not any("proxy_read_timeout" in d for d in rest_directives)  # 其他 REST 仍是預設 60 秒
    pattern = re.compile(_RESET_LOCATION.split(" ", 2)[2])
    for path in ("/v1/admin/agents/Jin/reset", "/admin/agents/zz-a/reset"):
        assert pattern.match(path), path
    for path in ("/v1/admin/agents/Jin", "/v1/admin/users", "/v1/admin/agents/a/b/reset"):
        assert not pattern.match(path), path
