"""``GET /isolation``：隔離狀態只給已登入的人看，回應只有層級與原因代碼（iso-c2 U2h）.

- 沒登入（壞卡 401、沒有身分 403）一律拒絕。
- 執行器的 READY 帶 roots、state_root、digest、boot_id（路徑與版本指紋），回應一律不帶。
- 自癒計數（U2n ``PermissionRepairService.snapshot()``，monitor 放在 snapshot 的 ``repairs``）只有
  計數與旗標，不帶路徑與代理人 ID；沒有掛鉤（不是 Linux）時回 null。
- MEDIUM-B：``connections.json`` 的 apiKey 不會出現在 ``GET /isolation`` 與 ``/health`` 的回應。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.routers import observability
from src.sandbox_kernel import spec as kernel_spec
from src.sandbox_kernel.owner_repair import RepairOutcome
from src.services.identity_router import get_verified_identity
from src.services.isolation.executor_client import ExecutorReady, ExecutorRoot
from src.services.isolation.mcp_launch import McpLaunchBook, McpLaunchInfo
from src.services.isolation.profiles import SandboxLayout
from src.services.isolation.repair import PermissionRepairService
from src.services.isolation.status import IsolationMonitor, IsolationSnapshot, LandlockState
from src.services.mcp_isolation_store import MCP_ISOLATION_FILENAME
from src.utils.isolation_policy import landlock_features

from tests.support.isolation import PinnedMonitor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

OWNER = AgentIdentity.for_role(agent_id="AdminUser", role="admin", is_human=True)
FAKE_API_KEY = "sk-test-isolation-endpoint-fake-0123456789"
AGENT_MARKER = "SecretAgentNameU2h"


@pytest.mark.parametrize("broken", [False, True])
def test_mcp_summary_contains_only_counts_and_store_issue(tmp_path, monkeypatch, broken):
    now = [0.0]
    book = McpLaunchBook(clock=lambda: now[0])
    book.record("private-slow", McpLaunchInfo(
        "third_party", "manual", "ok", "waiting_probe", "none", reason="probe_timeout",
    ))
    book.record("private-self", McpLaunchInfo(
        "self", "creation", "ok", "waiting_isolation", "none",
    ))
    book.record("private-direct", McpLaunchInfo("third_party", "manual", "ok", "launched", "none"))
    now[0] = 660
    monkeypatch.setattr(observability, "mcp_launch_book", book)
    monkeypatch.setattr(observability, "get_config_dir", lambda: tmp_path)
    if broken:
        (tmp_path / MCP_ISOLATION_FILENAME).write_text("{private-credential", encoding="utf-8")
    response = _client().get("/v1/isolation")
    assert response.status_code == 200
    assert response.json()["mcp"] == {
        "unisolated": 1, "waiting": 1, "suspended": 1,
        "store_issue": "mcp_isolation_store_unreadable" if broken else None,
    }
    assert "private-" not in response.text and str(tmp_path) not in response.text


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def _client(identity: AgentIdentity | None = OWNER) -> TestClient:
    app.dependency_overrides[get_verified_identity] = lambda: identity
    return TestClient(app, raise_server_exceptions=False)


def test_unreadable_store_warning_only_repeats_after_recovery(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(observability, "get_config_dir", lambda: tmp_path)
    path = tmp_path / MCP_ISOLATION_FILENAME
    client = _client()
    for document, warning_count in [("{bad", 1), ("[]", 1), ("{}", 1), ("{bad", 2)]:
        path.write_text(document, encoding="utf-8")
        for _ in range(2):
            response = client.get("/v1/isolation")
            assert response.status_code == 200
            assert bool(response.json()["mcp"]["store_issue"]) == (document != "{}")
        warnings = [r for r in caplog.records if "is unreadable" in r.getMessage()]
        assert len(warnings) == warning_count


def _pin(monkeypatch: pytest.MonkeyPatch, snap: IsolationSnapshot) -> None:
    monkeypatch.setattr(observability, "isolation_monitor", PinnedMonitor(snap=snap))


def _ready(tmp_path: Path, *, net: str | None) -> ExecutorReady:
    roots = (
        ExecutorRoot(str(tmp_path / "data" / "personal_drive"), "rw"),
        ExecutorRoot(str(tmp_path / "config" / "skills"), "ro"),
    )
    return ExecutorReady(
        1, 6, True, net, ("fs", "tcp"), roots, str(tmp_path / "exec-state"), "k" * 64,
        "s" * 64, "bootid0123456789", "p" * 64, 1001,
    )


# --- 認證 ---------------------------------------------------------------------------


def test_an_invalid_token_is_401() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/v1/isolation", headers={"Authorization": "Bearer not.a.token"})
    assert response.status_code == 401


def test_no_verified_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, IsolationSnapshot("none", "landlock", "probe_pending", None,
                                        LandlockState("pending"), frozenset()))
    response = _client(None).get("/v1/isolation")
    assert response.status_code == 403
    assert "probe_pending" not in response.text


# --- 回應只有代碼 -------------------------------------------------------------------


def test_executor_status_carries_codes_but_no_paths_or_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = _ready(tmp_path, net="net_mptcp")
    local = LandlockState("available", abi=6)
    snap = IsolationSnapshot(
        "none", "executor", "net_mptcp", None, local, local.features, executor=ready,
        block_ports_issue="block_ports_incomplete",
    )
    _pin(monkeypatch, snap)
    response = _client().get("/v1/isolation")
    assert response.status_code == 200
    assert response.json() == {
        "level": "none",
        "expected": "executor",
        "reason": "net_mptcp",
        "detail": None,
        "features": sorted(landlock_features(6)),
        "local": {"status": "available", "abi": 6, "reason": None, "net": None},
        "local_issue": None,
        "executor": {"abi": 6, "net": "net_mptcp"},
        "block_ports_issue": "block_ports_incomplete",
        "repairs": None,
        "mcp": {"unisolated": 0, "waiting": 0, "suspended": 0, "store_issue": None},
    }
    for leaked in (str(tmp_path), "exec-state", "bootid0123456789", "k" * 64, "s" * 64, "p" * 64):
        assert leaked not in response.text


def test_local_issue_and_state_detail_are_reported_as_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = LandlockState("unavailable", reason="landlock_unavailable")
    snap = IsolationSnapshot(
        "none", "landlock", "sandbox_state_unconfigured", "not_writable", local, frozenset(),
        local_issue="sandbox_state_unconfigured",
    )
    _pin(monkeypatch, snap)
    body = _client().get("/v1/isolation").json()
    assert (body["reason"], body["detail"], body["local_issue"]) == (
        "sandbox_state_unconfigured", "not_writable", "sandbox_state_unconfigured",
    )
    assert body["local"] == {"status": "unavailable", "abi": 0, "reason": "landlock_unavailable",
                             "net": None}
    assert (body["executor"], body["features"]) == (None, [])


async def test_the_endpoint_waits_for_the_first_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """沒有任何指令跑過時也要開始檢查：端點走 ``ensure_ready``，不是只讀快取."""
    calls: list[str] = []

    class _Monitor(PinnedMonitor):
        async def ensure_ready(self) -> IsolationSnapshot:
            calls.append("ensure_ready")
            return self.snap

        def snapshot(self) -> IsolationSnapshot:
            pytest.fail("the endpoint must not read a never-started monitor")

    local = LandlockState("available", abi=7)
    monitor = _Monitor(snap=IsolationSnapshot("landlock", "landlock", None, None, local,
                                              local.features))
    monkeypatch.setattr(observability, "isolation_monitor", monitor)
    body = await observability.get_isolation_status(OWNER)
    assert (calls, body.level) == (["ensure_ready"], "landlock")


# --- 自癒計數 -----------------------------------------------------------------------


def _repair_layout(tmp_path: Path) -> SandboxLayout:
    return SandboxLayout(
        data_root=tmp_path / "data", config_dir=tmp_path / "config", install_root=tmp_path,
        proxy_root=tmp_path / "ToukaProxy", env_file=tmp_path / ".env",
        state_root=tmp_path / "state", kernel_dir=tmp_path / "kernel", executor_dir=None,
        mirror_roots=(), in_container=True,
    )


def _busy_repair_service(layout: SandboxLayout) -> PermissionRepairService:
    """真的 ``PermissionRepairService``：修好一次、白名單外拒絕一次，路徑都帶代理人 ID。"""
    service = PermissionRepairService(
        lambda: layout,
        is_protected=lambda _path: False,
        repair_chain=lambda _root, _target: RepairOutcome(changed=1),
    )
    agent_file = layout.data_root / "personal_drive" / AGENT_MARKER / "notes.md"
    outside = layout.data_root / "soul" / AGENT_MARKER / "persona.md"
    assert service.repair("read_file_text", (str(agent_file),), retry=True)
    assert not service.repair("read_file_text", (str(outside),), retry=True)
    return service


def _repairs_after_activity(tmp_path: Path) -> dict[str, Any]:
    return _busy_repair_service(_repair_layout(tmp_path)).snapshot()


def test_repairs_carry_only_counts_and_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repairs = {**_repairs_after_activity(tmp_path), "last_path": str(tmp_path / AGENT_MARKER)}
    local = LandlockState("available", abi=7)
    snap = IsolationSnapshot("landlock", "landlock", None, None, local, local.features,
                             repairs=repairs)
    _pin(monkeypatch, snap)
    response = _client().get("/v1/isolation")
    body = response.json()["repairs"]
    assert set(body) == {
        "attempted", "repaired", "refused_outside", "refused_protected", "refused_not_owner",
        "nothing_changed", "throttled", "retried", "retry_failed",
        "permission_relock", "denied_burst", "window_resets_at",
    }
    assert (body["repaired"], body["refused_outside"]) == (1, 1)
    assert (body["permission_relock"], body["denied_burst"]) == (False, False)
    assert isinstance(body["window_resets_at"], float)
    assert AGENT_MARKER not in response.text and str(tmp_path) not in response.text


def test_repairs_are_null_without_a_repair_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    local = LandlockState("available", abi=7)
    _pin(monkeypatch, IsolationSnapshot("landlock", "landlock", None, None, local, local.features))
    assert _client().get("/v1/isolation").json()["repairs"] is None


async def test_the_real_monitor_snapshot_repairs_reach_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monitor 的 ``snapshot().repairs`` 原樣（同一組鍵與計數）出現在回應；不帶路徑與代理人 ID."""
    layout = _monitor_layout(tmp_path)
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_selftest, platform="linux",
        start_background=False,
    )
    await monitor.ensure_ready()
    # 掛鉤只在真的 Linux 才會自己裝（repair.install）；這裡直接放進去
    monitor._repair = _busy_repair_service(layout)
    expected = monitor.snapshot().repairs
    assert expected is not None
    monkeypatch.setattr(observability, "isolation_monitor", monitor)
    response = _client().get("/v1/isolation")
    body = response.json()["repairs"]
    assert set(body) == set(expected)
    counts = {key: value for key, value in expected.items() if key != "window_resets_at"}
    assert {key: body[key] for key in counts} == counts
    assert (body["repaired"], body["refused_outside"]) == (1, 1)
    assert AGENT_MARKER not in response.text and str(tmp_path) not in response.text


# --- MEDIUM-B：connections.json 的金鑰不會出現在回應 --------------------------------


def _monitor_layout(tmp_path: Path) -> SandboxLayout:
    data = tmp_path / "data"
    (data / "mcp_servers").mkdir(parents=True)
    for kernel in (tmp_path / "kernel", tmp_path / "ToukaProxy" / "src" / "sandbox_kernel"):
        kernel.mkdir(parents=True)
        (kernel / "launcher.py").write_text("# kernel\n", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()
    return SandboxLayout(
        data_root=data, config_dir=tmp_path / "config", install_root=tmp_path,
        proxy_root=tmp_path / "ToukaProxy", env_file=tmp_path / ".env",
        state_root=tmp_path / "state", kernel_dir=tmp_path / "kernel", executor_dir=None,
        mirror_roots=(), in_container=True,
    )


async def _selftest(_kernel_dir: Path, _test_dir: Path) -> kernel_spec.SelfTestResult:
    return kernel_spec.SelfTestResult(ok=True, abi=6)


_KEY_NUMBER = 987654321123
_SECRET_CASES = {
    "api_key_int": (json.dumps({"connections": [{"apiKey": _KEY_NUMBER}]}), str(_KEY_NUMBER)),
    "broken_near_key": (
        '{"connections": [{"url": "http://x:1", "apiKey": "' + FAKE_API_KEY + '",,]}',
        FAKE_API_KEY,
    ),
    "good": (
        json.dumps({"connections": [{"url": "http://localhost:4100", "apiKey": FAKE_API_KEY}]}),
        FAKE_API_KEY,
    ),
}


@pytest.mark.parametrize("case", list(_SECRET_CASES))
async def test_connection_secrets_never_reach_isolation_or_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    text, secret = _SECRET_CASES[case]
    layout = _monitor_layout(tmp_path)
    (layout.config_dir / "connections.json").write_text(text, encoding="utf-8")
    monitor = IsolationMonitor(
        layout_provider=lambda: layout, selftest=_selftest, platform="linux",
        start_background=False,
    )
    await monitor.ensure_ready()
    await monitor.refresh()
    expected_issue = None if case == "good" else "block_ports_incomplete"
    assert monitor.snapshot().block_ports_issue == expected_issue
    monkeypatch.setattr(observability, "isolation_monitor", monitor)
    client = _client()
    isolation = client.get("/v1/isolation")
    health = client.get("/v1/health")
    assert (isolation.status_code, health.status_code) == (200, 200)
    assert isolation.json()["block_ports_issue"] == expected_issue
    assert secret not in isolation.text and secret not in health.text
