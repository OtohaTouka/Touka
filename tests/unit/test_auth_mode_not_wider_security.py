"""ADR-0246 不放寬總表（U2c-4）：代表性動作 × 三種模式 × 有人／無人在場，新模式的結果
不比對應的舊等級寬。

對應：ReadOnly→Manual、SafeAuto→Auto、FullYOLO→YOLO；另外讀取時對照把已存的 SafeAuto 當
Manual，所以 SafeAuto→Manual 也要不寬。舊等級的效果是字面值（U2c-1 合入後、本單位改名前
的決策表＋地板，用範本規則求得），新模式的效果由現行引擎求出。

事先核准的變寬只有四項（master §7-4）：ReadOnly→Manual（核准後可寫）、KF-4、Auto 經審查員
放行、IDE（ADR-0246 決定 9）。本總表會碰到第一項與第三項（U2c-3，ADR-0246 決定 3：
終端指令與 dangerous 工具交給審查員，有人在場時審查員可能放行；無人在場一律不送審、
直接拒絕），逐格列在 ``_APPROVED_WIDENING``；KF-4 逐格列在下方 ``_KF4_WIDENING``（U2c-5）；
IDE 的格子在 ``ToukaIDE/src/__tests__/authPolicy.test.ts``（U2c-7），本檔只管 Proxy 的決策。
多出任何一格都算變寬，測試轉紅。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.models.autonomy import AutoReviewConfig
from src.models.identity import AgentIdentity
from src.models.policy import PolicyContext
from src.models.safety import AuthPoliciesConfig

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "ToukaProxy" / "config" / "auth_policies.json.example"
)
_AUTONOMY_EXAMPLE = _EXAMPLE.with_name("autonomy.json.example")
_ACTOR = AgentIdentity.for_role(agent_id="NotWiderProbe", role="Owner", is_human=False)

# 代表性動作：名稱 → (PolicyContext.action, resource, tool_action)
_ACTIONS: dict[str, tuple[str, str, str | None]] = {
    "read": ("tool_call", "read", None),
    "write": ("tool_call", "write", None),
    "delete": ("tool_call", "delete", None),
    "mcp": ("tool_call", "some_unlisted_mcp_tool", None),
    "skill_install": ("tool_call", "request_skill_install", None),  # F5
    "send_message": ("tool_call", "send_message", None),  # F6
    "memory_save": ("tool_call", "memory_tools", "save"),
    "ls": ("terminal_execute", "ls -la", None),  # 舊白名單
    "curl": ("terminal_execute", "curl http://x", None),
    "echo_var": ("terminal_execute", "echo $HOME", None),  # 舊白名單＋變數
    "ls_redirect": ("terminal_execute", "ls > out.txt", None),  # 舊白名單＋寫檔
    "read_env": ("terminal_execute", "cat .env", None),  # F2（舊白名單）
    "blacklisted": ("terminal_execute", "sudo ls", None),
    "advertise_delete": ("schema_injection", "delete", None),
}

# 舊等級的效果（字面值）
_OLD: dict[str, dict[str, str]] = {
    "ReadOnly": {
        "read": "ALLOW", "write": "DENY", "delete": "DENY", "mcp": "DENY",
        "skill_install": "DENY", "send_message": "DENY", "memory_save": "DENY",
        "ls": "DENY", "curl": "DENY", "echo_var": "DENY", "ls_redirect": "DENY",
        "read_env": "DENY", "blacklisted": "DENY", "advertise_delete": "DENY",
    },
    "SafeAuto": {
        "read": "ALLOW", "write": "ALLOW", "delete": "CONFIRM", "mcp": "CONFIRM",
        "skill_install": "CONFIRM", "send_message": "CONFIRM", "memory_save": "ALLOW",
        "ls": "ALLOW", "curl": "CONFIRM", "echo_var": "ALLOW", "ls_redirect": "ALLOW",
        "read_env": "HUMAN_ONLY", "blacklisted": "DENY", "advertise_delete": "CONFIRM",
    },
    "FullYOLO": {
        "read": "ALLOW", "write": "ALLOW", "delete": "ALLOW", "mcp": "ALLOW",
        "skill_install": "ALLOW", "send_message": "ALLOW", "memory_save": "ALLOW",
        "ls": "ALLOW", "curl": "ALLOW", "echo_var": "ALLOW", "ls_redirect": "ALLOW",
        "read_env": "HUMAN_ONLY", "blacklisted": "DENY", "advertise_delete": "ALLOW",
    },
}  # fmt: skip

_PAIRS = (
    ("ReadOnly", "Manual"),
    ("SafeAuto", "Auto"),
    ("SafeAuto", "Manual"),
    ("FullYOLO", "YOLO"),
)

# ReadOnly→Manual：核准後可寫（ADR-0246 ③）。(舊等級, 新模式, 動作, 有人在場)
_APPROVED_WIDENING = frozenset(
    {("ReadOnly", "Manual", name, True) for name in (
        "write", "delete", "mcp", "skill_install", "send_message", "memory_save",
        "ls", "curl", "echo_var", "ls_redirect", "read_env", "advertise_delete",
    )}
    | {("ReadOnly", "Manual", name, False) for name in (
        "ls", "curl", "echo_var", "ls_redirect", "advertise_delete",
    )}
    # Auto 經審查員放行（U2c-3；ADR-0246 決定 3、master §7-4）：終端指令與 dangerous 工具，
    # 舊 SafeAuto 要人核准，現在有人在場時審查員可能直接放行；無人在場不送審，不變寬
    | {("SafeAuto", "Auto", name, True) for name in ("curl", "delete", "mcp")}
)  # fmt: skip

_DENIED, _HUMAN, _RUNS = 0, 1, 2


def _outcome(effect: str, *, present: bool, action: str, headless_terminal: bool = False) -> int:
    """效果在執行層的結果：0 拒絕、1 要人放行、2 自動跑（廣告：0 隱藏、2 看得到）。"""
    if action == "schema_injection":
        return _DENIED if effect == "DENY" else _RUNS
    if effect == "ALLOW":
        return _RUNS
    if effect == "HUMAN_ONLY":
        return _HUMAN if present else _DENIED
    if effect == "REVIEW":
        # 有人在場：審查員可能放行（最寬的情況）；無人在場不送審、直接拒絕（工具一律如此，
        # 終端在 headless_terminal 預設 false 時如此；開了就送審、審查員可能放行）
        if present or (headless_terminal and action == "terminal_execute"):
            return _RUNS
        return _DENIED
    if effect == "CONFIRM":
        # 無人在場的非終端工具直接拒絕；終端落到 terminal 遺留通道（只有人能放行）
        return _HUMAN if present or action == "terminal_execute" else _DENIED
    return _DENIED


def _new_effect(mode: str, name: str) -> str:
    action, resource, tool_action = _ACTIONS[name]
    environment: dict[str, object] = {"auth_level": mode, "isolation": "landlock"}
    if tool_action is not None:
        environment["tool_action"] = tool_action
    config = AuthPoliciesConfig.model_validate(json.loads(_EXAMPLE.read_text(encoding="utf-8")))
    return config.evaluate(
        PolicyContext(actor=_ACTOR, resource=resource, action=action, environment=environment)
    ).effect


def _widened_cells(*, headless_terminal: bool = False) -> set[tuple[str, str, str, bool]]:
    widened = set()
    for old, new in _PAIRS:
        for name, (action, _resource, _tool_action) in _ACTIONS.items():
            new_effect = _new_effect(new, name)
            for present in (True, False):
                before = _outcome(_OLD[old][name], present=present, action=action)
                after = _outcome(
                    new_effect, present=present, action=action, headless_terminal=headless_terminal
                )
                if after > before:
                    widened.add((old, new, name, present))
    return widened


def test_new_modes_are_not_wider_than_old_levels():
    assert _widened_cells() - _APPROVED_WIDENING == set()


def test_approved_widening_is_exactly_the_approved_cells():
    """變寬的格子恰好就是事先核准的那些（防止核准清單掩護其他變寬，或寫成空砲）。"""
    assert _widened_cells() == _APPROVED_WIDENING


@pytest.mark.parametrize(("old", "new"), [("SafeAuto", "Auto"), ("SafeAuto", "Manual")])
def test_safe_auto_successors_are_strictly_narrower_somewhere(old: str, new: str):
    """SafeAuto 的兩個接班模式都比原本嚴：舊白名單指令不再無條件自動跑（無人在場時不跑）。"""
    assert _OLD[old]["ls"] == "ALLOW"
    assert _outcome(_new_effect(new, "ls"), present=False, action="terminal_execute") < _RUNS


def test_reviewer_widening_is_present_only_and_spares_floors():
    """送審的是終端與 dangerous 工具；無人在場時不比舊等級寬；F5／F6 與地板不送審。"""
    for name in ("curl", "delete", "mcp"):
        assert _new_effect("Auto", name) == "REVIEW"
        assert _outcome("REVIEW", present=False, action=_ACTIONS[name][0]) == _DENIED
    for name in ("skill_install", "send_message", "read_env"):
        assert _new_effect("Auto", name) == "HUMAN_ONLY"


# auto_review.headless_terminal=true（R-2c ③ #1）：需人類決定才可打開。打開後無人在場的終端
# 指令也送審、審查員可能放行，範圍是所有靈魂所有無人在場的 run（心跳、子代理、派工、SNS），
# 不只心跳。預設關；只有人類 admin 能改（test_autonomy_reviewer_authority_security.py）。
_HEADLESS_TERMINAL_WIDENING = frozenset({("SafeAuto", "Auto", "curl", False)})
_HEADLESS_TERMINAL_HEARTBEAT = frozenset({("safe", "curl")})  # 超過舊 SafeAuto 無人在場的格子


def test_headless_terminal_is_off_by_default():
    assert AutoReviewConfig().headless_terminal is False
    example = json.loads(_AUTONOMY_EXAMPLE.read_text(encoding="utf-8"))
    assert example["auto_review"]["headless_terminal"] is False


def test_headless_terminal_switch_widens_exactly_these_cells():
    """開關打開才多出來的變寬，逐格列出；不在事先核准清單內，所以要人類決定才可打開。"""
    extra = _widened_cells(headless_terminal=True) - _widened_cells()
    assert extra == _HEADLESS_TERMINAL_WIDENING
    assert not extra & _APPROVED_WIDENING
    beyond_old = {
        (level, name)
        for level, (old, new) in _HEARTBEAT_LEVELS.items()
        for name, (action, _resource, _tool_action) in _ACTIONS.items()
        if _outcome(_new_effect(new, name), present=False, action=action, headless_terminal=True)
        > _outcome(_OLD[old][name], present=False, action=action)
    }
    assert beyond_old == _HEADLESS_TERMINAL_HEARTBEAT


# KF-4（U2c-5，master §7-4）：以前心跳送不出任何工具，每格都等於拒絕；現在照心跳的模式帶
# 工具。心跳一律無人在場。security_level → (改名前的舊等級, 現在的模式)；看不懂的值舊映射是
# SafeAuto（DA24），現在是 Manual。
_HEARTBEAT_LEVELS = {
    "safe": ("SafeAuto", "Auto"),
    "full": ("FullYOLO", "YOLO"),
    "garbage": ("SafeAuto", "Manual"),
}
_KF4_WIDENING = frozenset(
    {("safe", name) for name in ("read", "write", "memory_save", "advertise_delete")}
    | {("full", name) for name in (
        "read", "write", "delete", "mcp", "skill_install", "send_message", "memory_save",
        "ls", "curl", "echo_var", "ls_redirect", "advertise_delete",
    )}
    | {("garbage", name) for name in (
        "read", "ls", "curl", "echo_var", "ls_redirect", "advertise_delete",
    )}
)  # fmt: skip


def test_kf4_heartbeat_widening_is_exact_and_bounded_by_old_level():
    """心跳變寬的格子恰好是 KF-4 核准的那些，而且每格都不比對應舊等級在無人在場時寬
    （心跳拿到工具，但結果不超過那個等級本來沒有人在場時的結果；地板與黑名單照擋）。"""
    widened = set()
    for level, (old, new) in _HEARTBEAT_LEVELS.items():
        for name, (action, _resource, _tool_action) in _ACTIONS.items():
            after = _outcome(_new_effect(new, name), present=False, action=action)
            assert after <= _outcome(_OLD[old][name], present=False, action=action), (level, name)
            if after > _DENIED:
                widened.add((level, name))
    assert widened == _KF4_WIDENING
