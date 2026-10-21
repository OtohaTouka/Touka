"""設定檔讀不到時 fail-closed：結果不比「檔案存在時」寬。

代理人的指令和 Touka 同帳號（uid 1001）時，Landlock 不管 chmod，指令可以把設定檔改成
讀不到。這裡在 tmp 設定上讓開檔丟 ``PermissionError(EACCES)``（stat 照常，等同真的
chmod 000），分啟動與熱重載兩種情況，涵蓋 permissions、identity、auth_policies、
autonomy、group whitelist、MCP registry、users 七個載入器，以及 MCP 隔離設定檔。

「不存在」走 ``.example`` 自癒（既有行為，不在此改）；「讀不到」不可拿 ``.example``
覆寫使用者的檔，也不可退回比檔案更寬的預設。
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from src.core.bootstrap import hydrate_config_examples
from src.core.exceptions import ErrorCode, ToukaException
from src.models.autonomy import AutoReviewConfig
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.protocols.registry import ProtocolRegistry
from src.routers import observability
from src.services import auto_reviewer as auto_reviewer_module
from src.services import autonomy as autonomy_module
from src.services import completion as completion_module
from src.services import group_router
from src.services.auth_service import AuthService
from src.services.auto_reviewer import AutoReviewer
from src.services.autonomy import AutonomyService
from src.services.completion import complete_once
from src.services.creation.engine import CreationEngine
from src.services.creation.mcp_kind import McpCreationKind
from src.services.creation.models import McpSpec
from src.services.mcp_hub_service import MCPHubService
from src.services.mcp_isolation_store import (
    MCP_ISOLATION_FILENAME,
    McpIsolationStore,
    assign,
    bound_of,
)
from src.services.permission_service import PermissionService
from src.services.rhythm_service import rhythm_service
from src.utils.bootstrap import ConfigValidationError, validate_live_configs

if TYPE_CHECKING:
    from collections.abc import Iterator

_EXAMPLES = Path(__file__).resolve().parents[2] / "ToukaProxy" / "config"
_REAL_OPEN = Path.open
_ACTOR = AgentIdentity.for_role(agent_id="ProbeAgent", role="Owner", is_human=False)

# 決定由嚴到寬
_EFFECT_RANK = {"DENY": 0, "HUMAN_ONLY": 1, "CONFIRM": 2, "REVIEW": 3, "ALLOW": 4}
# (action, resource, mode)：黑名單命中的指令、一般指令、唯讀工具、未列名工具
_POLICY_CASES = [
    ("terminal_execute", "sudo ls", "YOLO"),
    ("terminal_execute", "shutdown now", "YOLO"),
    ("terminal_execute", "drop table users", "Auto"),
    ("terminal_execute", "sudo ls", "Manual"),
    ("terminal_execute", "ls -la", "YOLO"),
    ("tool_call", "research", "Manual"),
    ("tool_call", "unlisted_tool", "YOLO"),
]
_PROBE_TOOLS = ["read", "write", "terminal", "memory_tools", "research", "bash"]


def _deny_reads(monkeypatch: pytest.MonkeyPatch, *targets: Path) -> set[Path]:
    """讓指定檔案「存在但讀不到」；回傳的集合清空即恢復可讀。"""
    denied = {Path(t).resolve() for t in targets}

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if denied and self.resolve() in denied:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return _REAL_OPEN(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    return denied


@pytest.fixture
def fresh_permission_service() -> Iterator[None]:
    PermissionService.reset_singleton()
    yield
    PermissionService.reset_singleton()


def _permission_service(
    tmp_path: Path,
    *,
    permissions: Path | None = None,
    identity: Path | None = None,
    policies: Path | None = None,
) -> PermissionService:
    PermissionService.reset_singleton()
    return PermissionService(
        config_path=str(permissions or tmp_path / "missing_permissions.json"),
        identity_path=str(identity or tmp_path / "missing_identity.json"),
        policy_path=str(policies or tmp_path / "missing_policies.json"),
    )


def _copy_example(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copyfile(_EXAMPLES / f"{name}.example", target)
    return target


def _decisions(service: PermissionService) -> list[str]:
    return [
        service.evaluate_policy(
            PolicyContext(
                actor=_ACTOR,
                resource=resource,
                action=action,
                environment={
                    "auth_level": mode,
                    "isolation": "executor",
                    "human_present": True,
                },
            )
        ).effect
        for action, resource, mode in _POLICY_CASES
    ]


def _assert_decisions_not_wider(got: list[str], baseline: list[str]) -> None:
    for effect, expected, case in zip(got, baseline, _POLICY_CASES, strict=True):
        assert _EFFECT_RANK[effect] <= _EFFECT_RANK[expected], (case, effect, expected)


def _assert_grants_not_wider(service: PermissionService, role: str, base_role: str) -> None:
    got = service.resolve_grants(role).tools
    baseline = service.resolve_grants(base_role).tools
    for tool in _PROBE_TOOLS:
        assert not got.allows(tool) or baseline.allows(tool), (tool, role, base_role)


# ── permissions：啟動拒絕、熱重載保留上一份 ────────────────────────────────────


@pytest.mark.usefixtures("fresh_permission_service")
def test_permissions_unreadable_at_startup_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permissions = _copy_example(tmp_path, "permissions.json")
    _deny_reads(monkeypatch, permissions)

    with pytest.raises(PermissionError):
        _permission_service(tmp_path, permissions=permissions)


@pytest.mark.usefixtures("fresh_permission_service")
def test_permissions_unreadable_on_hot_reload_keeps_last_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    permissions = _copy_example(tmp_path, "permissions.json")
    service = _permission_service(tmp_path, permissions=permissions)
    previous = service.roles_config

    _deny_reads(monkeypatch, permissions)
    assert service.reload_all() is False

    assert service.roles_config is previous
    assert service.permission_config_health.degraded is True


# ── identity：讀不到就全部 Base、沒有能力 ───────────────────────────────────────


@pytest.mark.usefixtures("fresh_permission_service")
@pytest.mark.parametrize("phase", ["startup", "hot_reload"])
def test_identity_unreadable_is_not_wider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    permissions = _copy_example(tmp_path, "permissions.json")
    identity = _copy_example(tmp_path, "identity_map.json")
    service = _permission_service(tmp_path, permissions=permissions, identity=identity)
    present_role = service.get_role_key("DebugAgent")
    present_caps = set(service.get_capabilities_by_id("DebugAgent"))
    assert present_role == "Debugger"
    assert "heartbeat_awakening" in present_caps

    _deny_reads(monkeypatch, identity)
    if phase == "startup":
        service = _permission_service(tmp_path, permissions=permissions, identity=identity)
    else:
        service.reload_all()

    caps = service.get_capabilities_by_id("DebugAgent")
    assert set(caps) <= present_caps
    assert caps == []
    _assert_grants_not_wider(service, service.get_role_key("DebugAgent"), present_role)


# ── auth_policies：啟動時讀不到就全部拒絕、標 degraded；熱重載保留上一份黑名單 ──


@pytest.mark.usefixtures("fresh_permission_service")
async def test_auth_policies_unreadable_at_startup_is_not_wider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policies = _copy_example(tmp_path, "auth_policies.json")
    baseline = _decisions(_permission_service(tmp_path, policies=policies))
    assert baseline[0] == "DENY"

    denied = _deny_reads(monkeypatch, policies)
    service = _permission_service(tmp_path, policies=policies)

    # 啟動時沒有上一份黑名單：不知道檔案擋了什麼，只能全部拒絕
    _assert_decisions_not_wider(_decisions(service), baseline)
    assert set(_decisions(service)) == {"DENY"}
    assert service.permission_config_health.degraded is True
    assert service.permission_config_health.reason == "auth_policies_unreadable"
    monkeypatch.setattr(observability, "permission_service", service)
    health = await observability.health_check()
    assert health.status == "degraded"
    assert health.permission_config_degraded_reason == "auth_policies_unreadable"

    # 權限修好後下一次重載恢復，且不需要改檔
    denied.clear()
    service.reload_all()
    assert _decisions(service) == baseline
    assert service.permission_config_health.degraded is False


@pytest.mark.usefixtures("fresh_permission_service")
def test_auth_policies_unreadable_audit_log_names_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    policies = _copy_example(tmp_path, "auth_policies.json")
    _deny_reads(monkeypatch, policies)
    with caplog.at_level(logging.WARNING):
        _permission_service(tmp_path, policies=policies)

    audit = [r.getMessage() for r in caplog.records if "AUDIT:" in r.getMessage()]
    assert any(
        str(policies) in m and "touka-init" in m and "restart touka-proxy" in m for m in audit
    ), audit


# ── auth_policies：熱重載保留上一份黑名單 ───────────────────────────────────────


@pytest.mark.usefixtures("fresh_permission_service")
def test_auth_policies_unreadable_on_hot_reload_is_not_wider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policies = _copy_example(tmp_path, "auth_policies.json")
    service = _permission_service(tmp_path, policies=policies)
    baseline = _decisions(service)
    assert baseline[0] == "DENY"

    _deny_reads(monkeypatch, policies)
    service.reload_all()

    _assert_decisions_not_wider(_decisions(service), baseline)


# ── autonomy：讀不到就停止自主執行、標 degraded ─────────────────────────────────

# 使用者可能寫出的最嚴設定：成本上限 0、每日配額 0、工具輪 1、不自動修復
_STRICTEST_AUTONOMY = {
    "quota": {"daily_max": 0},
    "react_loop": {"max_tool_rounds": 1},
    "cap": {"enabled": True, "max_total_cost_usd": 0.0},
    "self_repair": {"enabled": False},
}
_REPAIR_PROBE_SESSION = "u2i-repair-probe"


def _autonomy_limits(service: AutonomyService, identity: AgentIdentity) -> dict[str, float]:
    """自主執行的各項上限，值越大越寬（0 輪＝不限，換成無限大）。"""

    def _rounds(*, headless: bool) -> float:
        rounds = rhythm_service.get_effective_rounds(service.config, None, headless=headless)
        return math.inf if rounds == 0 else rounds

    repair = service.get_repair_instruction(_REPAIR_PROBE_SESSION, 1)
    service.reset_repair_attempts(_REPAIR_PROBE_SESSION)
    return {
        "cost_cap_open": float(not service.is_over_budget(identity)),
        "interactive_tool_rounds": _rounds(headless=False),
        "headless_tool_rounds": _rounds(headless=True),
        "daily_quota_remaining": service.get_daily_quota(identity)["remaining"],
        "self_repair": float(repair is not None),
    }


@pytest.mark.parametrize("phase", ["startup", "hot_reload"])
@pytest.mark.parametrize("contents", ["example", "strictest"])
def test_autonomy_unreadable_is_not_wider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_identity: Any,
    phase: str,
    contents: str,
) -> None:
    if contents == "example":
        config = _copy_example(tmp_path, "autonomy.json")
    else:
        config = tmp_path / "autonomy.json"
        config.write_text(json.dumps(_STRICTEST_AUTONOMY), encoding="utf-8")
    before = config.read_bytes()
    monkeypatch.setattr(autonomy_module, "CONFIG_FILE", config)
    identity = mock_identity(agent_id="U2iAutonomyProbe")
    service = AutonomyService()
    present = _autonomy_limits(service, identity)
    assert service.config_health.degraded is False

    denied = _deny_reads(monkeypatch, config)
    if phase == "startup":
        service = AutonomyService()
    else:
        service.reload_all()

    got = _autonomy_limits(service, identity)
    for key, value in got.items():
        assert value <= present[key], (key, value, present[key])
    # 不知道檔案寫了什麼：成本上限當作已到，自主執行停止
    assert service.is_over_budget(identity) is True
    assert service.config_health.degraded is True
    assert service.config_health.reason == "autonomy_config_unreadable"

    # 檔案沒被改寫；權限修好後重載恢復
    denied.clear()
    assert config.read_bytes() == before
    service.reload_all()
    assert _autonomy_limits(service, identity) == present
    assert service.config_health.degraded is False


@pytest.mark.usefixtures("fresh_permission_service")
async def test_health_reports_degraded_when_autonomy_config_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = _copy_example(tmp_path, "autonomy.json")
    monkeypatch.setattr(autonomy_module, "CONFIG_FILE", config)
    monkeypatch.setattr(observability, "permission_service", _permission_service(tmp_path))
    monkeypatch.setattr(observability, "autonomy_service", AutonomyService())
    assert (await observability.health_check()).status == "healthy"

    _deny_reads(monkeypatch, config)
    with caplog.at_level(logging.WARNING):
        monkeypatch.setattr(observability, "autonomy_service", AutonomyService())

    # 不新增 API 欄位：只反映在 status，權限欄位維持原意
    health = await observability.health_check()
    assert health.status == "degraded"
    assert health.permission_config_degraded is False
    assert (await observability.get_system_health()).status == "degraded"
    # AUDIT log 要寫出原因與恢復方法
    audit = [r.getMessage() for r in caplog.records if "AUDIT:" in r.getMessage()]
    assert any(str(config) in m and "touka-init" in m and "restart" in m for m in audit), audit


def test_autonomy_unreadable_keeps_reviewer_timeout_at_model_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """審查員逾時或失敗一律轉人，把逾時設短不會更安全，只會讓 Auto 全部轉人。"""
    config = _copy_example(tmp_path, "autonomy.json")
    monkeypatch.setattr(autonomy_module, "CONFIG_FILE", config)
    _deny_reads(monkeypatch, config)
    service = AutonomyService()
    assert service.config_health.degraded is True
    monkeypatch.setattr(auto_reviewer_module, "autonomy_service", service)

    reviewer_config = AutoReviewer()._config()

    assert reviewer_config is not None
    assert reviewer_config.timeout_s == AutoReviewConfig.model_fields["timeout_s"].default


_WORST_CASES_USD = (0.000001, 0.5)


@pytest.mark.parametrize(
    ("contents", "present"), [("example", [False, False]), ("strictest", [True, True])]
)
async def test_autonomy_unreadable_pre_send_estimate_is_not_wider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_identity: Any,
    contents: str,
    present: list[bool],
) -> None:
    """送前最壞估算（``would_exceed``）讀不到時也算超額，``complete_once`` 不送出。

    讀得到時上限閘沒開（範例檔）就回 False；讀不到時退回表把上限閘開在 0，已花＋任何
    正的最壞花費都超過，所以不比檔案在時寬。
    """
    if contents == "example":
        config = _copy_example(tmp_path, "autonomy.json")
    else:
        config = tmp_path / "autonomy.json"
        config.write_text(json.dumps(_STRICTEST_AUTONOMY), encoding="utf-8")
    monkeypatch.setattr(autonomy_module, "CONFIG_FILE", config)
    identity = mock_identity(agent_id="U2iAutonomyProbe")
    readable = AutonomyService()
    assert [readable.would_exceed(identity, w) for w in _WORST_CASES_USD] == present

    _deny_reads(monkeypatch, config)
    service = AutonomyService()
    assert service.config_health.degraded is True
    assert [service.would_exceed(identity, w) for w in _WORST_CASES_USD] == [True, True]

    sent: list[Any] = []

    async def _record_send(*args: Any) -> Any:
        sent.append(args)
        raise AssertionError

    monkeypatch.setattr(completion_module, "autonomy_service", service)
    monkeypatch.setattr(completion_module, "_send_once", _record_send)
    result = await complete_once(
        identity, "auto_reviewer", "claude-sonnet-4-5", "system", "user", 64, 5.0
    )
    assert result.error is not None
    assert (result.error.error_class, result.error.reason) == ("permission_denied", "over_budget")
    assert sent == []


def test_autonomy_saved_by_a_human_clears_the_unreadable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_identity: Any
) -> None:
    config = _copy_example(tmp_path, "autonomy.json")
    monkeypatch.setattr(autonomy_module, "CONFIG_FILE", config)
    _deny_reads(monkeypatch, config)
    service = AutonomyService()
    assert service.config_health.degraded is True

    saved = {"quota": {"daily_max": 5}, "react_loop": {"max_tool_rounds": 3}}
    assert service.update_config(saved) is True

    assert service.config_health.degraded is False
    assert service.is_over_budget(mock_identity(agent_id="U2iAutonomyProbe")) is False


# ── group whitelist：讀不到就沒有別名 ───────────────────────────────────────────


def test_group_whitelist_unreadable_resolves_no_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    whitelist = tmp_path / "group_whitelist.json"
    whitelist.write_text(json.dumps({"ToukaSoul": {"ali": "Alice"}}), encoding="utf-8")
    monkeypatch.setattr(group_router, "get_config_dir", lambda: tmp_path)
    present = group_router.load_whitelist()
    assert present == {"ali": "Alice"}

    # 每次點名都重讀，啟動與執行中是同一條路
    _deny_reads(monkeypatch, whitelist)
    got = group_router.load_whitelist()

    assert set(got.items()) <= set(present.items())
    assert got == {}


# ── MCP registry：讀不到就沒有伺服器 ────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["startup", "hot_reload"])
def test_mcp_registry_unreadable_loads_no_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    config = tmp_path / "server_config.json"
    config.write_text(
        json.dumps({"mcpServers": {"demo": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )
    registry = ProtocolRegistry(str(config))
    present = set(registry.get_all_servers())
    assert present == {"demo"}

    _deny_reads(monkeypatch, config)
    if phase == "startup":
        registry = ProtocolRegistry(str(config))
    else:
        registry.load_config()

    assert set(registry.get_all_servers()) <= present
    assert registry.get_all_servers() == {}


# ── users：讀不到就拒絕，不當成「尚未認領」 ───────────────────────────────────


def test_users_unreadable_fails_closed_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    users = tmp_path / "users.json"
    service = AuthService(users_file_path=str(users))
    service.create_user("owner@touka.io", "correct-horse-battery")
    assert service.any_claimed_user() is True
    before = users.read_bytes()

    denied = _deny_reads(monkeypatch, users)
    # 本機無卡領卡（LDH）只在「尚未認領」時開；讀不到不可當成尚未認領
    with pytest.raises(ToukaException):
        service.any_claimed_user()
    with pytest.raises(ToukaException):
        service.get_user_by_email("owner@touka.io")
    # 啟動時補 admin：不可因讀不到而新建 admin 或覆寫檔案
    with pytest.raises(ToukaException):
        service.ensure_admin_user([])

    denied.clear()
    assert users.read_bytes() == before


# ── MCP 寫入端：讀不到就拒絕，不把使用者的檔當成空的整份覆寫 ─────────────────

# 一台照常、一台被停用且關掉一個工具：覆寫會刪掉前者、讓後者復活（變寬）
_MCP_SERVERS = {
    "mcpServers": {
        "keep": {"command": "node", "args": ["a.js"]},
        "demo": {
            "command": "node",
            "args": ["d.js"],
            "enabled": False,
            "disabled_tools": ["drop_all"],
        },
    }
}
_CURATED_HUB = {"hub": [{"id": "curated-demo", "name": "demo", "source": "curated",
                         "command": "node", "args": ["d.js"]}]}


def _mcp_hub(tmp_path: Path) -> tuple[MCPHubService, Path, Path]:
    config = tmp_path / "server_config.json"
    config.write_text(json.dumps(_MCP_SERVERS), encoding="utf-8")
    hub = tmp_path / "mcp_hub.json"
    hub.write_text(json.dumps(_CURATED_HUB), encoding="utf-8")
    return MCPHubService(hub_path=str(hub), config_path=str(config)), config, hub


def _assert_persistence_error(raised: pytest.ExceptionInfo[ToukaException]) -> None:
    assert raised.value.code == ErrorCode.PERSISTENCE_ERROR


def test_mcp_register_server_refuses_unreadable_config_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _ = _mcp_hub(tmp_path)
    before = config.read_bytes()

    denied = _deny_reads(monkeypatch, config)
    with pytest.raises(ToukaException) as raised:
        service.register_server("demo", "node", ["d.js"])
    _assert_persistence_error(raised)

    denied.clear()
    assert config.read_bytes() == before


async def test_mcp_install_refuses_unreadable_config_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _ = _mcp_hub(tmp_path)
    before = config.read_bytes()

    denied = _deny_reads(monkeypatch, config)
    with pytest.raises(ToukaException) as raised:
        await service.install("curated-demo", {})
    _assert_persistence_error(raised)

    denied.clear()
    assert config.read_bytes() == before


async def test_mcp_hub_sync_refuses_unreadable_hub_without_dropping_curated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, hub = _mcp_hub(tmp_path)
    before = hub.read_bytes()

    async def _official() -> list[dict[str, Any]]:
        return [{"name": "Official One", "source": "official"}]

    monkeypatch.setattr(service, "fetch_official_servers", _official)
    monkeypatch.setattr(service, "fetch_registry_servers", _official)
    denied = _deny_reads(monkeypatch, hub)
    with pytest.raises(ToukaException) as raised:
        await service.sync()
    _assert_persistence_error(raised)

    denied.clear()
    assert hub.read_bytes() == before


async def test_mcp_creation_stops_at_validate_when_config_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, hub = _mcp_hub(tmp_path)
    before = config.read_bytes()

    class _Hub(MCPHubService):
        def __init__(self) -> None:
            super().__init__(hub_path=str(hub), config_path=str(config))

    engine = CreationEngine({"mcp": McpCreationKind(hub_factory=_Hub)})
    denied = _deny_reads(monkeypatch, config)
    # 看不到既有 id 時不能當成「沒有衝突」
    with pytest.raises(ToukaException) as raised:
        service.get_server_names()
    _assert_persistence_error(raised)
    with pytest.raises(ToukaException) as raised:
        await engine.create(
            "mcp",
            McpSpec(name="demo", command="node", args=["d.js"]),
            run_smoke=False,
            run_eval=False,
        )
    _assert_persistence_error(raised)

    denied.clear()
    assert config.read_bytes() == before


# ── 自癒：不存在才從 .example 補；讀不到不覆寫 ──────────────────────────────────


def test_self_heal_restores_missing_but_never_overwrites_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    examples = tmp_path / "examples"
    live = tmp_path / "live"
    examples.mkdir()
    live.mkdir()
    (examples / "autonomy.json.example").write_text('{"from": "example"}', encoding="utf-8")
    (examples / "bots.json.example").write_text('{"bots": []}', encoding="utf-8")
    owned = live / "autonomy.json"
    owned.write_text('{"from": "owner"}', encoding="utf-8")

    denied = _deny_reads(monkeypatch, owned)
    hydrate_config_examples(examples, live)

    denied.clear()
    assert owned.read_text(encoding="utf-8") == '{"from": "owner"}'
    assert (live / "bots.json").read_text(encoding="utf-8") == '{"bots": []}'


def test_startup_health_check_rejects_unreadable_live_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    autonomy = _copy_example(tmp_path, "autonomy.json")
    _deny_reads(monkeypatch, autonomy)

    with pytest.raises(ConfigValidationError, match="autonomy.json"):
        validate_live_configs(tmp_path)


def test_startup_health_check_rejects_unlistable_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """資料夾列不出來（例如 chmod 311，檔案仍可進入）時不可空過健檢。"""
    _copy_example(tmp_path, "autonomy.json")
    target = tmp_path.resolve()
    real_scandir, real_listdir = os.scandir, os.listdir

    def _refuse(real: Any) -> Any:
        def _call(path: Any = ".") -> Any:
            if Path(path).resolve() == target:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real(path)

        return _call

    monkeypatch.setattr(os, "scandir", _refuse(real_scandir))
    monkeypatch.setattr(os, "listdir", _refuse(real_listdir))

    with pytest.raises(ConfigValidationError, match="設定資料夾"):
        validate_live_configs(tmp_path)


# ── MCP 隔離設定檔（ADR-0275）：讀不到就不寫，也不改 server_config.json ──────


def _isolation_store(tmp_path: Path) -> Path:
    path = tmp_path / MCP_ISOLATION_FILENAME
    path.write_text(json.dumps({"version": 1, "servers": {}}), encoding="utf-8")
    return path


@pytest.mark.parametrize("write_point", ["hub_install", "register_server"])
async def test_mcp_isolation_store_unreadable_refuses_writes_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_point: str
) -> None:
    """讀不到來源紀錄時新增伺服器會失敗：沒辦法先寫來源，就不寫條目（否則被當成第三方）。"""
    service, config, _ = _mcp_hub(tmp_path)
    store = _isolation_store(tmp_path)
    before = {config: config.read_bytes(), store: store.read_bytes()}

    denied = _deny_reads(monkeypatch, store)
    with pytest.raises(ToukaException) as raised:
        if write_point == "hub_install":
            await service.install("curated-demo", {})
        else:
            service.register_server("made", "node", ["m.js"])
    _assert_persistence_error(raised)

    denied.clear()
    assert {path: path.read_bytes() for path in before} == before


def test_mcp_isolation_store_unreadable_reads_as_all_self_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """讀不到不是「沒有紀錄」：所有條目當自建、額外允許的資料夾一律不用。"""
    store_path = _isolation_store(tmp_path)
    store = McpIsolationStore(tmp_path)
    entry = {"command": "npx", "args": ["-y", "@acme/files"]}
    store.mutate(assign("files", "manual", bound_of(entry), fresh=True))

    denied = _deny_reads(monkeypatch, store_path)
    view = store.record_for("files", entry)

    assert view.records_unreadable is True
    assert view.effective_origin is None
    assert view.usable_sandbox_paths == ()
    denied.clear()


def test_self_heal_never_overwrites_an_unreadable_isolation_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    owned = live / MCP_ISOLATION_FILENAME
    owned.write_text('{"version": 1, "servers": {"x": {"bound": "b"}}}', encoding="utf-8")
    before = owned.read_bytes()

    denied = _deny_reads(monkeypatch, owned)
    hydrate_config_examples(_EXAMPLES, live)

    denied.clear()
    assert owned.read_bytes() == before
