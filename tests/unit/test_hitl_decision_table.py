"""H1 — HITL 三模式 × 風險分類決策表（hitl-unification 提案 H1；ADR-0246 改三模式）。

診斷（2026-07-11）：Manual 下 evaluate() 對一切回 CONFIRM，但 tool_executor.py:250-251
對無 ``command`` 參數的工具「log 後照常執行」＝非終端工具零審批 fail-open；auth_policies
的 read_only_tools 只有 research，其餘工具無風險分級可言。本檔把新語意（``tool_risk``
分類 × 四級決策表）釘死於引擎層（AuthPoliciesConfig 純模型，不碰 singleton／磁碟）。

表（黑名單恆 DENY，凌駕一切、含 YOLO）::

                read_only   safe      dangerous
    Manual      ALLOW       CONFIRM   CONFIRM
    Auto        ALLOW       ALLOW     CONFIRM（U2c-3 前的佔位）
    YOLO        ALLOW       ALLOW     ALLOW

終端（action=terminal_execute）：黑名單＝DENY、其餘一律 dangerous（safe_whitelist 已拿掉，
舊檔留著這個鍵也不再生效）。已退役的舊名稱（ReadOnly、SafeAuto、FullYOLO）走到這裡一律
當看不懂的值＝CONFIRM。聚合多動詞工具按 action 細分：廣告
（schema_injection）取最低風險（模型能看到、能用其唯讀動詞）、執行取實際 action 的類、
action 缺失／未知＝dangerous（fail-safe）；未列名工具＝default dangerous（MCP/runtime）。
read_only 類＝全等級 ALLOW 免 HITL（ADR-0089 語意由 tool_risk 承接）。
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import (
    AuthPoliciesConfig,
    SafetyRules,
    ToolRiskConfig,
    ToolRiskEntry,
)

_ACTOR = AgentIdentity.for_role(agent_id="HitlProbe", role="Owner", is_human=False)
_LEVELS = ("Manual", "Auto", "YOLO")


def _config() -> AuthPoliciesConfig:
    return AuthPoliciesConfig(
        policies=SafetyRules(
            safe_whitelist=[r"^ls(\s+.*)?$", r"^git\s+status$"],
            critical_blacklist=[r"rm\s+-rf\s+/", r"sudo\s+", r"^evil_tool$"],
        ),
        tool_risk=ToolRiskConfig(
            default="dangerous",
            tools={
                "research": "read_only",
                "read": "read_only",
                "write": "safe",
                "send_message": "dangerous",
                "evil_tool": "read_only",  # 黑名單優先權對釘用
                "memory_tools": ToolRiskEntry(
                    action_param="action",
                    actions={"search": "read_only", "query": "read_only", "save": "safe"},
                ),
            },
        ),
    )


def _tool_decision(
    level: str,
    tool: str,
    *,
    action: str | None = None,
    policy_action: str = "tool_call",
):
    env: dict = {"auth_level": level}
    if action is not None:
        env["tool_action"] = action
    return _config().evaluate(
        PolicyContext(actor=_ACTOR, resource=tool, action=policy_action, environment=env)
    )


def _terminal_decision(level: str, command: str):
    return _config().evaluate(
        PolicyContext(
            actor=_ACTOR,
            resource=command,
            action="terminal_execute",
            environment={"auth_level": level, "isolation": "landlock"},
        )
    )


# ── 核心矩陣：3 模式 × 3 類（以代表工具求值）───────────────────────────────────

_MATRIX = {
    # (level, tool) -> expected effect
    ("Manual", "read"): "ALLOW",
    ("Manual", "write"): "CONFIRM",
    ("Manual", "send_message"): "CONFIRM",
    ("Auto", "read"): "ALLOW",
    ("Auto", "write"): "ALLOW",
    ("Auto", "send_message"): "HUMAN_ONLY",  # F6 對外發言：Auto 下只有人能核准（決定 6）
    ("YOLO", "read"): "ALLOW",
    ("YOLO", "write"): "ALLOW",
    ("YOLO", "send_message"): "ALLOW",
}


@pytest.mark.parametrize(
    ("level", "tool", "expected"),
    [(lv, t, e) for (lv, t), e in _MATRIX.items()],
    ids=[f"{lv}-{t}" for (lv, t) in _MATRIX],
)
def test_level_by_class_matrix(level: str, tool: str, expected: str):
    assert _tool_decision(level, tool).effect == expected


def test_read_only_class_allows_at_all_levels_adr_0089_semantic():
    """research（read_only 類）三模式一律 ALLOW 免 HITL——ADR-0089 語意由 tool_risk 承接。"""
    for level in _LEVELS:
        assert _tool_decision(level, "research").effect == "ALLOW", level


# ── 終端：黑名單=DENY、其餘=dangerous（設定檔的 safe_whitelist 不再生效）──────────

@pytest.mark.parametrize(
    ("level", "command", "expected"),
    [
        # 舊白名單指令：Auto 不再自動放行，交給審查員（U2c-3；無人在場不送審）
        ("Manual", "ls -la", "CONFIRM"),
        ("Auto", "ls -la", "REVIEW"),
        ("YOLO", "ls -la", "ALLOW"),
        ("Manual", "curl http://x", "CONFIRM"),
        ("Auto", "curl http://x", "REVIEW"),
        ("YOLO", "curl http://x", "ALLOW"),
    ],
)
def test_terminal_command_matrix(level: str, command: str, expected: str):
    assert _terminal_decision(level, command).effect == expected


def test_terminal_blacklist_denies_even_under_fullyolo():
    for level in _LEVELS:
        assert _terminal_decision(level, "rm -rf / --no-preserve-root").effect == "DENY", level


def test_terminal_command_named_research_not_auto_allowed():
    """bash 指令字串剛好叫 research 不得吃到 read_only 類豁免（ADR-0089 回歸鎖）。"""
    assert _terminal_decision("Manual", "research").effect == "CONFIRM"


# ── 聚合多動詞工具：廣告取最低風險、執行取實際 action、未知 action fail-safe ────

def test_aggregate_execution_uses_action_class():
    assert _tool_decision("Manual", "memory_tools", action="search").effect == "ALLOW"
    assert _tool_decision("Auto", "memory_tools", action="save").effect == "ALLOW"
    assert _tool_decision("Manual", "memory_tools", action="save").effect == "CONFIRM"


def test_aggregate_unknown_or_missing_action_is_dangerous():
    """action 缺失／未知＝dangerous（fail-safe）：Manual 下 CONFIRM、Auto 下交給審查員。"""
    assert _tool_decision("Auto", "memory_tools").effect == "REVIEW"
    assert _tool_decision("Auto", "memory_tools", action="wipe_all").effect == "REVIEW"
    assert _tool_decision("Manual", "memory_tools", action="wipe_all").effect == "CONFIRM"


def test_aggregate_advertisement_uses_min_risk_class():
    """廣告（schema_injection）取最低風險類：Manual 下 memory_tools 仍可見（其唯讀
    動詞可用）；執行面各動詞照實際類把關。"""
    decision = _tool_decision("Manual", "memory_tools", policy_action="schema_injection")
    assert decision.effect == "ALLOW"


# ── 未列名工具（MCP／runtime）＝default dangerous ────────────────────────────────

@pytest.mark.parametrize(
    ("level", "expected"),
    [("Manual", "CONFIRM"), ("Auto", "REVIEW"), ("YOLO", "ALLOW")],
)
def test_unlisted_tool_defaults_dangerous(level: str, expected: str):
    assert _tool_decision(level, "some_unknown_mcp_tool").effect == expected


# ── 優先權與邊界 ────────────────────────────────────────────────────────────────

def test_blacklist_beats_read_only_class():
    """黑名單命中凌駕 read_only 分類（Tier 1 恆先行）。"""
    for level in _LEVELS:
        assert _tool_decision(level, "evil_tool").effect == "DENY", level


def test_unknown_level_falls_back_to_confirm():
    assert _tool_decision("Wat", "write").effect == "CONFIRM"


@pytest.mark.parametrize("legacy", ["ReadOnly", "SafeAuto", "FullYOLO"])
def test_retired_level_names_fall_back_to_confirm(legacy: str):
    """舊名稱不再被決策表認得：一律 CONFIRM（＝Manual；FullYOLO、SafeAuto 比原本嚴）。"""
    assert _tool_decision(legacy, "write").effect == "CONFIRM"
    assert _terminal_decision(legacy, "ls -la").effect == "CONFIRM"


def test_action_param_lookup():
    """執行層據 action_param_for 抽出動詞（審計只記動詞、不記整包 args）。"""
    risk = _config().tool_risk
    assert risk.action_param_for("memory_tools") == "action"
    assert risk.action_param_for("read") is None
    assert risk.action_param_for("not_listed") is None
