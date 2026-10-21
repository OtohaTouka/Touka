"""Mutation-killing unit tests for PermissionService (the access-control SSOT).

These tests inject a KNOWN config directly into a constructed PermissionService
instance (auto-reload disabled) so the authorization logic is exercised against a
deterministic RBAC/identity/policy fixture rather than on-disk config. Each test
pins exact behavioural boundaries (True/False, exact sets, exact action strings)
to kill surviving mutants.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from src.models.identity import (
    AgentIdentity,
    AgentOverride,
    IdentityMapConfig,
    IdentityRule,
)
from src.models.permissions import PermissionsConfig, RoleConfig, SystemConfig
from src.models.policy import PolicyContext, PolicyDecision
from src.models.safety import AuthPoliciesConfig, SafetyRules, ToolRiskConfig
from src.services.permission_service import (
    DEFAULT_AUTH_POLICIES,
    DEFAULT_IDENTITY_MAP,
    DEFAULT_PERMISSIONS_MODEL,
    PermissionService,
)
from src.utils.permission_grants import McpGrant, NameGrant, RoleGrants


@pytest.fixture
def svc():
    """Construct a PermissionService with a KNOWN injected config (no disk)."""
    PermissionService.reset_singleton()
    service = PermissionService()
    service.disable_auto_reload()

    service.roles_config = PermissionsConfig(
        roles={
            "Owner": RoleConfig(
                desc="Superuser",
                allowed_tools=["*"],
                denied_tools=["self_destruct"],
                allowed_mcp_servers=["*"],
                allowed_skills=["*"],
            ),
            "Editor": RoleConfig(
                desc="Editor",
                inherits=["Reader"],
                allowed_tools=["write_file"],
                denied_tools=["delete_all"],
                allowed_mcp_servers=["editor_mcp"],
                allowed_skills=["edit_skill"],
            ),
            "Reader": RoleConfig(
                desc="Reader",
                allowed_tools=["read_file", "secret_tool"],
                denied_tools=["secret_tool"],
                allowed_mcp_servers=["web_search"],
                allowed_skills=["read_skill"],
            ),
        },
        system_config=SystemConfig(default_role="Reader"),
        agent_mappings={},
        mcp_servers=[],
    )

    service.identity_map = IdentityMapConfig(
        tag_rules={
            "is_toukasoul": IdentityRule(role="Owner", capabilities=["soul_cap"]),
            "is_custom": IdentityRule(role="Editor", capabilities=["custom_cap"]),
        },
        agent_overrides={
            "Alice": AgentOverride(role="Owner", capabilities=["override_cap"]),
        },
        default_role="Reader",
    )

    service.policies = AuthPoliciesConfig(
        policies=SafetyRules(safe_whitelist=[], critical_blacklist=[]),
        tool_risk=ToolRiskConfig(default="dangerous", tools={"research": "read_only"}),
    )

    # R-2a：靈魂旗標要經原始 ID 確認；這幾個合成 ID 宣告為靈魂。
    with patch("src.services.permission_service.soul_service") as souls:
        souls.is_toukasoul.side_effect = {"Alice", "unknown", "ghost", "nobody"}.__contains__
        yield service

    PermissionService.reset_singleton()


# --------------------------------------------------------------------------- #
# is_authorized
# --------------------------------------------------------------------------- #
def test_is_authorized_owner_short_circuits_but_denied_tool_blocked(svc):
    owner = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    # Owner / "*" short-circuit grants everything...
    assert svc.is_authorized(owner, "anything_random", {}, {}) is True
    # ...EXCEPT a tool that is explicitly in denied_tools.
    assert svc.is_authorized(owner, "self_destruct", {}, {}) is False


def test_is_authorized_allowed_and_not_denied_true(svc):
    # Reader allows read_file (not denied) -> True.
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    assert svc.is_authorized(reader, "read_file", {}, {}) is True


def test_is_authorized_allowed_but_denied_is_false(svc):
    # secret_tool is in BOTH allowed_tools and denied_tools -> denied wins.
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    assert svc.is_authorized(reader, "secret_tool", {}, {}) is False


def test_is_authorized_mcp_fallback_true_when_tool_missing(svc):
    # Tool not in allowed list, but mcp_server in allowed_mcp -> True (fallback).
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    assert svc.is_authorized(reader, "unknown_tool", {}, {"mcp_server": "web_search"}) is True


def test_is_authorized_default_deny_false(svc):
    # Tool missing AND mcp_server not allowed -> default deny.
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    assert svc.is_authorized(reader, "unknown_tool", {}, {"mcp_server": "secret_mcp"}) is False
    # No mcp_server at all -> also deny.
    assert svc.is_authorized(reader, "unknown_tool", {}, {}) is False


# --------------------------------------------------------------------------- #
# get_allowed_tools
# --------------------------------------------------------------------------- #
def test_get_allowed_tools_owner_returns_star(svc):
    owner = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    assert svc.get_allowed_tools(owner) == NameGrant.from_lists(["*"], ["self_destruct"])


def test_get_allowed_tools_subtracts_denied(svc):
    # Reader: allowed=[read_file, secret_tool], denied=[secret_tool] -> {read_file}.
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    grant = svc.get_allowed_tools(reader)
    assert grant.allows("read_file") is True
    assert grant.allows("secret_tool") is False


# --------------------------------------------------------------------------- #
# is_mcp_server_allowed
# --------------------------------------------------------------------------- #
def test_is_mcp_server_allowed_membership_and_deny(svc):
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    assert svc.is_mcp_server_allowed("web_search", reader) is True
    assert svc.is_mcp_server_allowed("not_listed", reader) is False
    # Owner wildcard -> always True.
    owner = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    assert svc.is_mcp_server_allowed("anything", owner) is True


# --------------------------------------------------------------------------- #
# can_use_skill
# --------------------------------------------------------------------------- #
def test_can_use_skill_membership_and_wildcard(svc):
    reader = AgentIdentity.for_role(agent_id="bob", role="Reader")
    assert svc.can_use_skill(reader, "read_skill") is True
    assert svc.can_use_skill(reader, "ghost_skill") is False
    owner = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    assert svc.can_use_skill(owner, "any_skill") is True


# --------------------------------------------------------------------------- #
# is_tool_allowed
# --------------------------------------------------------------------------- #
def test_is_tool_allowed_membership_and_wildcard(svc):
    explicit = NameGrant.from_lists(["foo", "bar"])
    assert svc.is_tool_allowed("foo", explicit) is True
    assert svc.is_tool_allowed("baz", explicit) is False
    assert svc.is_tool_allowed("anything", NameGrant.from_lists(["*"])) is True


# --------------------------------------------------------------------------- #
# get_role_key
# --------------------------------------------------------------------------- #
def test_get_role_key_override_wins_first(svc):
    # Alice has an override to Owner; even with is_toukasoul/is_custom flags,
    # the override is checked first.
    assert svc.get_role_key("Alice", is_toukasoul=True, is_custom=True) == "Owner"


def test_get_role_key_tag_rules_and_default(svc):
    # is_toukasoul rule -> Owner; default Reader only for souls and system agents.
    # ADR-0245: other IDs ignore the is_custom rule and the default -> Base.
    assert svc.get_role_key("unknown", is_toukasoul=True) == "Owner"
    # R-2a: the soul flag alone does not make a non-soul ID a soul.
    assert svc.get_role_key("stranger", is_toukasoul=True) == "Base"
    assert svc.get_role_key("DispatchAgent") == "Reader"
    assert svc.get_role_key("unknown", is_custom=True) == "Base"
    assert svc.get_role_key("unknown") == "Base"


def test_get_role_key_capability_derived_toukasoul(svc):
    # Capability "is_toukasoul" must resolve via the tag rule even when the
    # is_toukasoul ARGUMENT is False (derived from capabilities).
    ident = AgentIdentity.for_role(agent_id="ghost", role="Reader", capabilities=["is_toukasoul"])
    assert svc.get_role_key(ident, is_toukasoul=False) == "Owner"


# --------------------------------------------------------------------------- #
# get_capabilities_by_id
# --------------------------------------------------------------------------- #
def test_get_capabilities_by_id_override_tag_and_unknown(svc):
    # Override caps (Alice judged a soul by the caller).
    assert svc.get_capabilities_by_id("Alice", is_toukasoul=True) == ["override_cap"]
    # is_toukasoul tag caps.
    assert svc.get_capabilities_by_id("nobody", is_toukasoul=True) == ["soul_cap"]
    assert svc.get_capabilities_by_id("stranger", is_toukasoul=True) == []
    # ADR-0245: non-soul IDs get no caps from overrides or the is_custom rule.
    assert svc.get_capabilities_by_id("Alice") == []
    assert svc.get_capabilities_by_id("nobody", is_custom=True) == []
    # Unknown -> empty list.
    assert svc.get_capabilities_by_id("nobody") == []


# --------------------------------------------------------------------------- #
# resolve_grants（唯一解析器：繼承、self-only deny、全域地板）
# --------------------------------------------------------------------------- #
def test_resolve_grants_merges_parent_allows_but_not_denials(svc):
    grants = svc.resolve_grants("Editor")
    assert grants.tools.allows("write_file") is True  # own allow
    assert grants.tools.allows("read_file") is True  # inherited allow
    assert grants.tools.allows("secret_tool") is True  # Reader deny 不往 Editor 傳播


def test_resolve_grants_missing_role_is_empty(svc):
    assert svc.resolve_grants("DoesNotExist") == RoleGrants()


def test_global_denial_floor_binds_every_role():
    PermissionService.reset_singleton()
    service = PermissionService()
    service.disable_auto_reload()
    service.roles_config = PermissionsConfig(
        roles={
            "Owner": RoleConfig(
                allowed_tools=["*"],
                allowed_mcp_servers=["*"],
                allowed_skills=["*"],
            ),
            "Subagent": RoleConfig(allowed_tools=["read"]),
        },
        global_denied_tools=["execute_command"],
        global_denied_mcp_tools=["stripe/create_refund"],
    )

    owner = service.resolve_grants("Owner")
    guest = service.resolve_grants("Subagent")
    assert owner.tools.allows("execute_command") is False
    assert guest.tools.allows("execute_command") is False
    assert owner.mcp.allows("stripe", "create_refund") is False
    assert owner.mcp.allows("stripe", "list_customers") is True
    PermissionService.reset_singleton()


def test_owner_is_data_not_a_hardcoded_bypass():
    PermissionService.reset_singleton()
    service = PermissionService()
    service.disable_auto_reload()
    service.roles_config = PermissionsConfig(roles={"Owner": RoleConfig()})
    owner = AgentIdentity.for_role(agent_id="narrow-owner", role="Owner")

    assert service.get_allowed_tools(owner) == NameGrant()
    assert service.is_authorized(owner, "read", {}, {}) is False
    assert service.is_mcp_server_allowed("stripe", owner) is False
    assert service.is_mcp_tool_allowed("stripe", "list_customers", owner) is False
    assert service.can_use_skill(owner, "future-skill") is False
    PermissionService.reset_singleton()


def test_all_permission_entry_points_delegate_to_resolve_grants(svc, monkeypatch):
    grants = RoleGrants(
        tools=NameGrant.from_lists(["read"]),
        mcp=McpGrant.from_lists(["stripe/list_customers"]),
        skills=NameGrant.from_lists(["deep_research"]),
    )
    calls: list[str | None] = []

    def _resolve(role_key: str | None) -> RoleGrants:
        calls.append(role_key)
        return grants

    monkeypatch.setattr(svc, "resolve_grants", _resolve)
    identity = AgentIdentity.for_role(agent_id="probe", role="Reader")

    assert svc.get_allowed_tools(identity) is grants.tools
    assert svc.is_authorized(identity, "read", {}, {}) is True
    assert svc.is_mcp_server_allowed("stripe", identity) is True
    assert svc.is_mcp_tool_allowed("stripe", "list_customers", identity) is True
    assert svc.can_use_skill(identity, "deep_research") is True
    assert calls == ["Reader"] * 5


# --------------------------------------------------------------------------- #
# can_awaken
# --------------------------------------------------------------------------- #
def test_can_awaken_requires_capability(svc):
    with_cap = AgentIdentity.for_role(
        agent_id="h", role="Reader", capabilities=["heartbeat_awakening"]
    )
    without_cap = AgentIdentity.for_role(agent_id="h2", role="Reader", capabilities=["other"])
    assert svc.can_awaken(with_cap) is True
    assert svc.can_awaken(without_cap) is False


# --------------------------------------------------------------------------- #
# _check_pattern
# --------------------------------------------------------------------------- #
def test_check_pattern_empty_match_and_invalid_regex(svc):
    # Empty / None text -> False.
    assert svc._check_pattern("", ["foo"]) is False  # noqa: SLF001
    assert svc._check_pattern(None, ["foo"]) is False  # noqa: SLF001
    # Matching regex -> True (case-insensitive).
    assert svc._check_pattern("rm -RF /", ["rm -rf"]) is True  # noqa: SLF001
    # Non-matching -> False.
    assert svc._check_pattern("hello world", ["goodbye"]) is False  # noqa: SLF001
    # Invalid regex must NOT raise and yields False (resilient).
    assert svc._check_pattern("anything", ["[unclosed"]) is False  # noqa: SLF001


# --------------------------------------------------------------------------- #
# evaluate_policy
# --------------------------------------------------------------------------- #
def test_evaluate_policy_none_policies_returns_deny_and_audits(svc):
    """Tier 1 fail-safe：政策未載入時預設 DENY（設定錯誤不得偽裝成正常審批）。"""
    svc.policies = None
    ctx = PolicyContext(
        actor=AgentIdentity.for_role(agent_id="bob", role="Reader"),
        resource="ls",
        action="tool_call",
    )
    with patch(
        "src.services.permission_service.audit_service.log_decision"
    ) as mock_log:
        decision = svc.evaluate_policy(ctx)
    assert decision.effect == "DENY"
    assert decision.reason == "Policy not loaded"
    mock_log.assert_called_once()
    # The decision passed to the auditor is the same fallback decision.
    assert mock_log.call_args.args[1].effect == "DENY"


# --------------------------------------------------------------------------- #
# get_execution_strategy (PolicyContext construction)
# --------------------------------------------------------------------------- #
def test_get_execution_strategy_terminal_action_and_resource(svc):
    captured = {}

    def fake_eval(context: PolicyContext):
        captured["ctx"] = context
        return PolicyDecision(effect="CONFIRM", reason="stub")

    agent = AgentIdentity.for_role(agent_id="bob", role="Reader")
    with patch.object(svc, "evaluate_policy", side_effect=fake_eval):
        effect = svc.get_execution_strategy(
            "execute_terminal", {"command": "rm file"}, agent, {}
        )
    assert effect == "CONFIRM"
    assert captured["ctx"].action == "terminal_execute"
    assert captured["ctx"].resource == "rm file"


def test_get_execution_strategy_tool_call_action_and_resource(svc):
    captured = {}

    def fake_eval(context: PolicyContext):
        captured["ctx"] = context
        return PolicyDecision(effect="ALLOW", reason="stub")

    agent = AgentIdentity.for_role(agent_id="bob", role="Reader")
    with patch.object(svc, "evaluate_policy", side_effect=fake_eval):
        effect = svc.get_execution_strategy("query_memory", {}, agent, {})
    assert effect == "ALLOW"
    assert captured["ctx"].action == "tool_call"
    # Without "command", resource falls back to the tool_name.
    assert captured["ctx"].resource == "query_memory"


# --------------------------------------------------------------------------- #
# Config loaders fall back to DEFAULT_* on empty / "{}" / missing
# --------------------------------------------------------------------------- #
def test_load_config_fallbacks(svc):
    # Empty text -> DEFAULT_PERMISSIONS_MODEL.
    with patch.object(type(svc.config_path), "exists", return_value=True), patch(
        "src.services.permission_service.resource_manager.read_file_text",
        return_value="",
    ):
        svc._load_config(startup=False)  # noqa: SLF001
    assert svc.roles_config is DEFAULT_PERMISSIONS_MODEL

    # "{}" -> DEFAULT.
    svc.roles_config = None
    with patch.object(type(svc.config_path), "exists", return_value=True), patch(
        "src.services.permission_service.resource_manager.read_file_text",
        return_value="{}",
    ):
        svc._load_config(startup=False)  # noqa: SLF001
    assert svc.roles_config is DEFAULT_PERMISSIONS_MODEL

    # Missing file -> DEFAULT.
    svc.roles_config = None
    with patch.object(type(svc.config_path), "exists", return_value=False):
        svc._load_config(startup=False)  # noqa: SLF001
    assert svc.roles_config is DEFAULT_PERMISSIONS_MODEL


# --------------------------------------------------------------------------- #
# 軌B 生產保真：DebugAgent → Debugger 必須拿到 Owner 等級全工具面
# --------------------------------------------------------------------------- #
def test_debugger_identity_resolves_to_full_toolface():
    """DebugAgent（identity_map→Debugger，繼承 Owner）必須拿到 ["*"] 全工具面。

    這是計分卡能真實調用工具的前提：Debugger 角色若未定義會退回空權限 →
    ToolLayer 過濾掉全部工具 → 模型一個工具都收不到（called: []）。
    """
    PermissionService.reset_singleton()
    service = PermissionService()
    service.disable_auto_reload()
    service.roles_config = PermissionsConfig(
        roles={
            "Owner": RoleConfig(
                desc="Owner", allowed_tools=["*"], allowed_mcp_servers=["*"], allowed_skills=["*"]
            ),
            "Base": RoleConfig(desc="Base", allowed_tools=[], denied_tools=["*"]),
            "Debugger": RoleConfig(desc="Debugger", inherits=["Owner"]),
        },
        system_config=SystemConfig(default_role="Base"),
    )
    service.identity_map = IdentityMapConfig(
        tag_rules={},
        agent_overrides={"DebugAgent": AgentOverride(role="Debugger", capabilities=[])},
        default_role="Base",
    )
    # 全鏈：DebugAgent → Debugger
    assert service.get_role_key("DebugAgent") == "Debugger"
    debugger = AgentIdentity.for_role(agent_id="DebugAgent", role="Debugger")
    # Debugger 繼承 Owner → 工具面收斂為 ["*"]
    assert service.get_allowed_tools(debugger) == NameGrant.from_lists(["*"])
    # ToolLayer Gate-1 過濾下，記憶工具會被放行
    assert service.is_tool_allowed("memory_tools", service.get_allowed_tools(debugger)) is True
    # 繼承 Owner 的 allowed_skills=["*"] → 可用任意技能（docgen 計分卡情境的前提）
    assert service.can_use_skill(debugger, "document-generator") is True
    PermissionService.reset_singleton()


def test_load_identity_map_fallbacks(svc):
    svc.identity_map = None
    with patch.object(type(svc.identity_path), "exists", return_value=True), patch(
        "src.services.permission_service.resource_manager.read_file_text",
        return_value="{}",
    ):
        svc._load_identity_map()  # noqa: SLF001
    assert svc.identity_map is DEFAULT_IDENTITY_MAP

    svc.identity_map = None
    with patch.object(type(svc.identity_path), "exists", return_value=False):
        svc._load_identity_map()  # noqa: SLF001
    assert svc.identity_map is DEFAULT_IDENTITY_MAP


def test_load_policies_fallbacks(svc):
    svc.policies = None
    with patch.object(type(svc.policy_path), "exists", return_value=True), patch(
        "src.services.permission_service.resource_manager.read_file_text",
        return_value="",
    ):
        svc._load_policies()  # noqa: SLF001
    assert svc.policies is DEFAULT_AUTH_POLICIES

    svc.policies = None
    with patch.object(type(svc.policy_path), "exists", return_value=False):
        svc._load_policies()  # noqa: SLF001
    assert svc.policies is DEFAULT_AUTH_POLICIES
