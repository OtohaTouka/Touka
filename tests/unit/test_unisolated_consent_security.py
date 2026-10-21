"""同意帳本（iso-c2 U1c，禁令 20 安全測試）.

人核准「未隔離／隔離降級」的審批卡後，同意只綁那一次呼叫的正規化 subject：
同 key 不同 subject 兩筆作廢、只能用一次、10 分鐘過期、只有政策層級 ``none`` 才能記同意；
同 key 重新記錄一律清掉舊的同意。
"""

from __future__ import annotations

import pytest
from src.models.identity import AgentIdentity
from src.services.isolation.consent import (
    CONSENT_TTL_SECONDS,
    ConsentKey,
    ConsentLedger,
    consent_subject,
)

KEY = ConsentKey("Alice", "s1", "call-1")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def ledger(clock: _Clock) -> ConsentLedger:
    return ConsentLedger(clock=clock)


# --- subject 正規化 -------------------------------------------------------------------


def test_terminal_subject_ignores_missing_env_tool_alias_and_background():
    base = consent_subject("execute_command", {"command": "pytest -q"})
    assert consent_subject("execute_command", {"command": "pytest -q", "env": "powershell"}) == base
    assert consent_subject("bash", {"command": "pytest -q"}) == base
    assert consent_subject("execute_command", {"command": "pytest -q", "background": True}) == base


def test_terminal_subject_ignores_trusted_context_kwargs():
    base = consent_subject("execute_command", {"command": "ls"})
    noisy = {
        "command": "ls",
        "_call_id": "other",
        "session_id": "s9",
        "project_id": "p9",
        "isolation": "executor",
        "human_present": True,
        "_hitl_preapproved": True,
        "identity": object(),
    }
    assert consent_subject("execute_command", noisy) == base


def test_terminal_subject_changes_with_command():
    assert consent_subject("execute_command", {"command": "ls"}) != consent_subject(
        "execute_command", {"command": "ls -a"}
    )


def test_verify_subject_is_distinct_from_terminal():
    assert consent_subject("verify", {"command": "ls"}) != consent_subject(
        "execute_command", {"command": "ls"}
    )


def test_evolution_subject_normalizes_defaults_and_order():
    args = {
        "skill_id": "s",
        "code": "print(1)",
        "test_tool_name": "t",
        "test_arguments": {"a": 1, "b": 2},
        "dependencies": ["b", "a"],
    }
    swapped = {**args, "test_arguments": {"b": 2, "a": 1}, "dependencies": ["a", "b"]}
    assert consent_subject("evolution_implement_and_verify", args) == consent_subject(
        "evolution_implement_and_verify", {**swapped, "submit_for_deploy": True}
    )
    bare = {"skill_id": "s", "code": "print(1)", "test_tool_name": "t"}
    assert consent_subject("evolution_implement_and_verify", bare) == consent_subject(
        "evolution_implement_and_verify", {**bare, "dependencies": None, "test_arguments": {}}
    )


def test_evolution_subject_changes_with_code():
    a = {"skill_id": "s", "code": "print(1)", "test_tool_name": "t"}
    assert consent_subject("evolution_implement_and_verify", a) != consent_subject(
        "evolution_implement_and_verify", {**a, "code": "print(2)"}
    )
    assert consent_subject("write_mcp_implementation", a) != consent_subject(
        "write_mcp_implementation", {**a, "code": "print(2)"}
    )


def test_create_mcp_subject_without_args_equals_empty_defaults():
    bare = {"name": "srv", "command": "npx"}
    assert consent_subject("create_mcp", bare) == consent_subject(
        "create_mcp", {**bare, "args": [], "env": {}}
    )
    assert consent_subject("create_mcp", bare) != consent_subject(
        "create_mcp", {**bare, "args": ["-y", "pkg"]}
    )


def test_scaffold_subject_binds_tool_spec():
    bare = {"name": "srv", "tools": [{"name": "a"}]}
    assert consent_subject("scaffold_mcp", bare) != consent_subject(
        "scaffold_mcp", {**bare, "tools": [{"name": "b"}]}
    )


def test_verify_mcp_locally_subject_binds_arguments():
    a = {"skill_id": "s", "tool_name": "t", "arguments": {"x": 1}}
    assert consent_subject("verify_mcp_locally", a) != consent_subject(
        "verify_mcp_locally", {**a, "arguments": {"x": 2}}
    )


# --- 帳本 -----------------------------------------------------------------------------


def test_consent_round_trip_single_use(ledger: ConsentLedger):
    subject = consent_subject("execute_command", {"command": "ls"})
    ledger.record_policy(KEY, level="none", subject=subject)
    assert ledger.grant(KEY, fallback="landlock") is True
    grant = ledger.take(KEY, subject=subject)
    assert grant is not None
    assert grant.policy_level == "none"
    assert grant.consent is not None and grant.consent.fallback == "landlock"
    assert ledger.take(KEY, subject=subject) is None


def test_policy_record_without_consent_has_no_consent(ledger: ConsentLedger):
    ledger.record_policy(KEY, level="landlock", subject="s")
    grant = ledger.take(KEY, subject="s")
    assert grant is not None and grant.policy_level == "landlock" and grant.consent is None


def test_grant_only_when_policy_level_none(ledger: ConsentLedger):
    ledger.record_policy(KEY, level="landlock", subject="s")
    assert ledger.grant(KEY, fallback="direct") is False
    assert ledger.grant(ConsentKey("Alice", "s1", "missing"), fallback="direct") is False


def test_same_key_different_subject_voids_both(ledger: ConsentLedger):
    ledger.record_policy(KEY, level="none", subject="first")
    ledger.record_policy(KEY, level="none", subject="second")
    assert ledger.grant(KEY, fallback="direct") is False
    assert ledger.take(KEY, subject="first") is None
    assert ledger.take(KEY, subject="second") is None


def test_re_recording_the_same_call_drops_the_old_consent(ledger: ConsentLedger):
    """同一個呼叫 id、同樣內容再經一次政策：舊的同意不能沿用，要重新經人核准。"""
    ledger.record_policy(KEY, level="none", subject="s")
    assert ledger.grant(KEY, fallback="direct") is True
    ledger.record_policy(KEY, level="none", subject="s")
    grant = ledger.take(KEY, subject="s")
    assert grant is not None and grant.policy_level == "none" and grant.consent is None


def test_take_with_other_subject_is_rejected_and_voids(ledger: ConsentLedger):
    ledger.record_policy(KEY, level="none", subject="approved")
    ledger.grant(KEY, fallback="direct")
    assert ledger.take(KEY, subject="tampered") is None
    assert ledger.take(KEY, subject="approved") is None


def test_consent_expires_after_ten_minutes(ledger: ConsentLedger, clock: _Clock):
    assert CONSENT_TTL_SECONDS == 600
    ledger.record_policy(KEY, level="none", subject="s")
    ledger.grant(KEY, fallback="direct")
    clock.now += CONSENT_TTL_SECONDS + 1
    assert ledger.take(KEY, subject="s") is None


def test_consent_valid_just_before_expiry(ledger: ConsentLedger, clock: _Clock):
    ledger.record_policy(KEY, level="none", subject="s")
    clock.now += CONSENT_TTL_SECONDS - 5
    ledger.grant(KEY, fallback="direct")
    clock.now += CONSENT_TTL_SECONDS - 5
    assert ledger.take(KEY, subject="s") is not None


def test_keys_are_isolated_per_agent_and_call(ledger: ConsentLedger):
    ledger.record_policy(KEY, level="none", subject="s")
    ledger.grant(KEY, fallback="direct")
    assert ledger.take(ConsentKey("Bob", "s1", "call-1"), subject="s") is None
    assert ledger.take(ConsentKey("Alice", "s1", "call-2"), subject="s") is None
    assert ledger.take(KEY, subject="s") is not None


def test_key_is_built_from_verified_identity():
    alice = AgentIdentity.for_role("Alice", "Owner", capabilities=[])
    assert ConsentKey.of(alice, "s1", "call-1") == KEY
    assert ConsentKey.of(alice, None, None) == ConsentKey("Alice", "", "")


def test_ledger_is_bounded(clock: _Clock):
    ledger = ConsentLedger(clock=clock, max_entries=3)
    for index in range(5):
        ledger.record_policy(ConsentKey("A", "s", str(index)), level="none", subject="x")
    assert len(ledger) == 3
    assert ledger.take(ConsentKey("A", "s", "0"), subject="x") is None
    assert ledger.take(ConsentKey("A", "s", "4"), subject="x") is not None
