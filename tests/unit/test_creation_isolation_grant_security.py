"""創建試跑的授權、來源與隔離傳遞；只用 sandbox 與連線替身。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.protocols.registry import ProtocolServerConfig
from src.services.creation import api, creation_engine, kinds, mcp_kind, smoke
from src.services.creation.models import McpScaffoldSpec, McpSpec, ToolSpec
from src.services.identity_router import get_verified_identity
from src.services.isolation import policy
from src.services.isolation.consent import (
    Consent,
    ConsentKey,
    ExecutionGrant,
    consent_ledger,
    consent_subject,
)
from src.services.isolation.mcp_launch import McpLauncher, mcp_launcher
from src.services.isolation.runners import RunnerBroker
from src.services.isolation.status import LevelDecision, isolation_monitor
from src.services.mcp_isolation_store import IsolationRecord, IsolationSnapshot, bound_of
from src.tools.builtin import creation
from src.utils.mcp_sentinels import APPDATA_SENTINEL

from tests.support.isolation import (
    PinnedMonitor,
    fake_kernel_layout,
    fixed_interpreter,
    match_state_root,
    pinned_snapshot,
)

ACTOR = AgentIdentity.for_role("CreationProbe", "Owner")


async def test_smoke_without_context_never_acquires_or_connects(monkeypatch):
    candidate = AsyncMock()
    monkeypatch.setattr(smoke.mcp_launcher, "candidate_launch", candidate)
    connect, _ = _connection(monkeypatch)
    report = await smoke.smoke_mcp_server("python", ["server.py"])
    assert not report.ok and "isolation_unavailable" in report.detail
    candidate.assert_not_awaited()
    connect.assert_not_awaited()


def _connection(monkeypatch):
    client = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(name="ping", description="", input_schema={}),
                ]
            )
        )
    )
    stack = SimpleNamespace(aclose=AsyncMock())
    connect = AsyncMock(return_value=(client, stack))
    monkeypatch.setattr(smoke, "connect_mcp_server", connect)
    return connect, stack


def _launcher(monkeypatch, tmp_path, monitor):
    layout = fake_kernel_layout(tmp_path)
    match_state_root(monkeypatch, layout.state_root)
    launcher = McpLauncher(
        monitor=monitor,
        broker=RunnerBroker(
            monitor=monitor, layout_provider=lambda: layout, interpreter=fixed_interpreter
        ),
        layout_provider=lambda: layout,
        base_dir=lambda: layout.proxy_root,
    )
    monkeypatch.setattr(mcp_launcher, "candidate_launch", launcher.candidate_launch)
    return launcher, layout


@pytest.mark.parametrize("tool,origin", [("create_mcp", "creation"), ("scaffold_mcp", "scaffold")])
async def test_one_grant_shared_by_smoke_eval_and_commit(monkeypatch, tool, origin):
    args = {
        "name": f"grant-{tool}",
        "command": "npx",
        "args": ["pkg"],
        "tools": [{"name": "ping"}],
        "origin": "hub",
        "context": {"origin": "hub"},
        "_creation_grant": {"policy_level": "executor"},
    }
    key = ConsentKey.of(ACTOR, "s", "c")
    consent_ledger.record_policy(key, level="landlock", subject=consent_subject(tool, args))
    grants = []
    real_take = consent_ledger.take

    def take_once(*args, **kwargs):
        grant = real_take(*args, **kwargs)
        grants.append(grant)
        return grant

    take = Mock(side_effect=take_once)
    monkeypatch.setattr(consent_ledger, "take", take)
    trial = AsyncMock(return_value=smoke.SmokeReport(ok=True, tool_count=1))
    probe = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_kind, "smoke_mcp_server", trial)
    monkeypatch.setattr(mcp_kind, "list_mcp_tools", probe)
    register = Mock()
    monkeypatch.setattr(mcp_kind.MCPHubService, "register_server", register)
    result = await getattr(creation, tool)(identity=ACTOR, session_id="s", _call_id="c", **args)
    assert result.startswith("✅")
    assert take.call_count == 1
    context = trial.call_args.kwargs["context"]
    assert context.grant is grants[0]
    assert context is probe.call_args.kwargs["context"]
    assert context.grant.policy_level == "landlock"
    assert context.origin == origin
    assert register.call_args.kwargs["origin"] == origin
    assert consent_ledger.pending(key) is None


@pytest.mark.parametrize("origin,profile", [("creation", "mcp_self"), ("scaffold", "mcp_scaffold")])
async def test_trial_transport_and_profile_match_production(monkeypatch, tmp_path, origin, profile):
    launcher, layout = _launcher(monkeypatch, tmp_path, PinnedMonitor())
    (layout.data_root / "personal_drive").mkdir(parents=True)
    args = ["pkg"]
    if origin == "scaffold":
        server = layout.config_dir / "mcp_servers" / "trial" / "server.py"
        server.parent.mkdir(parents=True)
        server.write_text("# stub", encoding="utf-8")
        args = [str(server), APPDATA_SENTINEL]
    env = {"DATA": APPDATA_SENTINEL, "API_KEY": "candidate-key"}
    entry = ProtocolServerConfig(command="npx", args=args, env=env)
    store = IsolationSnapshot(
        "ok",
        {
            "trial": IsolationRecord(
                origin=origin,
                bound=bound_of(
                    {
                        "command": "npx",
                        "args": args,
                    }
                ),
            )
        },
    )
    reader = SimpleNamespace(record_for=store.view)
    production = await launcher.plan_server_launch(
        "trial", entry, reader, tool_base_url="http://tool"
    )
    context = kinds.CreationLaunchContext(ExecutionGrant("landlock"), origin)
    connect, stack = _connection(monkeypatch)
    report = await smoke.smoke_mcp_server("npx", args, env, name="trial", context=context)
    assert report.ok, report.detail
    launch = connect.call_args.kwargs["isolated"].launch
    assert connect.call_args.kwargs["isolated"].on_eof == "close"
    assert launch.acquired.profile.name == profile
    assert launch.acquired.profile == production.acquired.profile
    assert launch.exec_env == env
    assert production.exec_env["DATA"] != APPDATA_SENTINEL
    assert production.exec_env["TOOL_BASE_URL"] == "http://tool"
    if origin == "scaffold":
        assert launch.argv[-1] == str(layout.data_root / "personal_drive")
    stack.aclose.assert_awaited_once()
    connect.reset_mock()
    tools = await smoke.list_mcp_tools("npx", args, env, name="trial", context=context)
    assert len(tools) == 1
    assert connect.call_args.kwargs["isolated"].launch.acquired.profile == launch.acquired.profile
    assert connect.call_args.kwargs["isolated"].on_eof == "close"


@pytest.mark.parametrize(
    "tool,expected,eligible",
    [
        ("create_mcp", "executor", True),
        ("scaffold_mcp", "landlock", False),
    ],
)
async def test_policy_uses_creation_scope(monkeypatch, tool, expected, eligible):
    monitor = PinnedMonitor(snap=pinned_snapshot("executor"), scope_issue="agent_not_in_executor")
    monkeypatch.setattr(policy, "isolation_monitor", monitor)
    result = await policy.policy_level(ACTOR, {"identity": ACTOR}, "s", "c", tool=tool, args={})
    assert result == LevelDecision(expected)
    assert consent_ledger.pending(ConsentKey.of(ACTOR, "s", "c")).executor_eligible is eligible


@pytest.mark.parametrize(
    "route,payload",
    [
        ("mcp", {"name": "http-none", "command": "npx"}),
        ("scaffold", {"name": "http-none", "tools": [{"name": "ping"}]}),
    ],
)
@pytest.mark.parametrize("run_smoke", [True, False])
def test_http_none_never_runs_a_trial(monkeypatch, tmp_path, route, payload, run_smoke):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", local_status="unavailable", abi=0))
    _launcher(monkeypatch, tmp_path, monitor)
    monkeypatch.setattr(isolation_monitor, "snapshot", monitor.snapshot)
    monkeypatch.setattr(isolation_monitor, "local_level", monitor.local_level)
    connect, _stack = _connection(monkeypatch)
    app.dependency_overrides[get_verified_identity] = lambda: ACTOR
    with TestClient(app) as client:
        response = client.post(
            f"/v1/creation/{route}",
            json={
                **payload,
                "name": f"http-{route}-{str(run_smoke).lower()}",
                "run_smoke": run_smoke,
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["committed"] is (not run_smoke)
    if run_smoke:
        assert "isolation_unavailable" in body["errors"][0]
    connect.assert_not_awaited()


async def test_scaffold_api_uses_local_level(monkeypatch):
    monkeypatch.setattr(isolation_monitor, "snapshot", lambda: pinned_snapshot("executor"))
    monkeypatch.setattr(
        isolation_monitor, "local_level", lambda: LevelDecision("none", "net_mptcp")
    )
    create = AsyncMock()
    monkeypatch.setattr(api.creation_engine, "create", create)
    await api.scaffold_and_create_mcp(
        McpScaffoldSpec(name="local-srv", tools=[ToolSpec(name="ping")])
    )
    context = create.call_args.kwargs["context"]
    assert context.grant == ExecutionGrant("none")
    assert context.origin == "scaffold"


async def test_direct_consent_is_shared_and_cannot_be_replayed(monkeypatch, tmp_path):
    monitor = PinnedMonitor(snap=pinned_snapshot("none", local_status="unavailable", abi=0))
    _launcher(monkeypatch, tmp_path, monitor)
    monkeypatch.setattr(isolation_monitor, "snapshot", monitor.snapshot)
    connect, stack = _connection(monkeypatch)
    monkeypatch.setattr(mcp_kind.MCPHubService, "register_server", Mock())
    args = {"name": "direct-consent", "command": "npx"}
    key = ConsentKey.of(ACTOR, "s", "c")
    consent_ledger.record_policy(key, level="none", subject=consent_subject("create_mcp", args))
    assert consent_ledger.grant(key, fallback="direct")
    result = await creation.create_mcp(identity=ACTOR, session_id="s", _call_id="c", **args)
    assert result.startswith("✅")
    assert connect.await_count == stack.aclose.await_count == 2
    assert all("isolated" not in call.kwargs for call in connect.call_args_list)
    connect.reset_mock()
    result = await creation.create_mcp(identity=ACTOR, session_id="s", _call_id="c", **args)
    assert "isolation_unavailable" in result
    connect.assert_not_awaited()


async def test_synthetic_spec_origin_cannot_change_commit_origin(monkeypatch):
    register = Mock()
    monkeypatch.setattr(mcp_kind.MCPHubService, "register_server", register)
    spec = SimpleNamespace(name="untrusted-origin", command="npx", args=[], env={}, origin="hub")
    context = kinds.CreationLaunchContext(ExecutionGrant("none", Consent("x", "direct")))
    mcp_kind.McpCreationKind().commit(spec, context)
    assert register.call_args.kwargs["origin"] == "creation"


@pytest.mark.parametrize("run_smoke", [True, False])
async def test_engine_without_launch_context_never_runs_a_trial(monkeypatch, run_smoke):
    connect, _stack = _connection(monkeypatch)
    lookup = AsyncMock(wraps=kinds.launch_context)
    monkeypatch.setattr(kinds, "launch_context", lookup)
    register = Mock()
    monkeypatch.setattr(mcp_kind.MCPHubService, "register_server", register)
    result = await creation_engine.create(
        "mcp", McpSpec(name="missing-context", command="npx"), run_smoke=run_smoke,
    )
    connect.assert_not_awaited()
    lookup.assert_not_awaited()
    assert result.committed is (not run_smoke)
    if run_smoke:
        assert result.smoke is not None and not result.smoke.ok
        assert "isolation_unavailable" in result.smoke.detail
        register.assert_not_called()
    else:
        register.assert_called_once()
        assert result.evaluation is not None and result.evaluation.tasks == []
