"""Phase 3 步驟 5（互審設計文件 §4.5 層1）：EvidenceCollector——TDD.

verdict 的權威來源：**純 Python 蒐證**（非 LLM）——跑 verification_cmd 取 exit
code、核對 artifact 存在、節錄 test_log。反橡皮圖章三件套（設計 §6.2-6）：
- 拒 no-op 前綴（true／echo…／exit 0／:）＝假考卷判硬失敗；
- pytest 零收集（exit 0 但「collected 0 items／no tests ran」）＝假通過判硬失敗；
- test_log 斷言數 >0（``test_log_passes``）。

ADR-0293：``artifact_root`` 是個人空間的 ``ZonePath``（``zone_drive``）。證據檔不跟隨連結、不能
跳出個人空間；cwd 交出到 spawn 回來；行程帶 ``agent_id`` 登記，重置、刪除期間不啟動。
"""

from __future__ import annotations

import sys

import pytest
from memory_server.utils.no_follow import LinkRefusedError
from memory_server.utils.zone_paths import HandoffKind, is_handed_off
from src.models.identity import AgentIdentity
from src.services import destructive_ops
from src.services.process_registry import process_registry
from src.services.resource_manager import resource_manager
from src.services.review import evidence
from src.services.review.evidence import (
    AGENT_BUSY,
    bundle_digest,
    collect_evidence,
    has_passing_assertions,
)

from tests.support.links import make_dir_link

_PY = sys.executable


async def _allow_all(_cmd: str) -> str | None:
    """本檔測蒐證語意；指令政策閘另由 test_subprocess_hardening_security 覆蓋。"""
    return None


async def test_artifact_existence_checked(zone_drive):
    resource_manager.zone_write_text(zone_drive.child("out.md"), "內容")
    bundle = await collect_evidence(
        artifact_refs=["out.md", "ghost.md"],
        verification_cmd=None,
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    assert bundle.artifacts_present == {"out.md": True, "ghost.md": False}
    assert bundle.hard_checks_pass is False  # ghost 不存在


async def test_cmd_exit_codes_captured(zone_drive):
    ok = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "print(\'3 passed\')"',
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    assert ok.cmd_results[0].exit_code == 0
    assert "3 passed" in ok.cmd_results[0].output_snippet
    assert ok.hard_checks_pass is True

    fail = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "import sys; sys.exit(1)"',
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    assert fail.cmd_results[0].exit_code == 1
    assert fail.hard_checks_pass is False


async def test_noop_commands_hard_rejected(zone_drive):
    for cmd in ("true", "echo ok", "exit 0", ":"):
        bundle = await collect_evidence(
            artifact_refs=[],
            verification_cmd=cmd,
            test_log=None,
            artifact_root=zone_drive,
            command_gate=_allow_all,
        )
        result = bundle.cmd_results[0]
        assert result.rejected_reason is not None, cmd
        assert result.ok is False, cmd
        assert bundle.hard_checks_pass is False, cmd


async def test_pytest_zero_collection_rejected(zone_drive):
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "print(\'collected 0 items\')"',
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    result = bundle.cmd_results[0]
    assert result.exit_code == 0  # 表面成功
    assert result.rejected_reason is not None  # 但零收集＝橡皮圖章，硬拒
    assert bundle.hard_checks_pass is False


async def test_missing_command_rejected(zone_drive):
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd="definitely_not_a_real_command_xyz --flag",
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    assert bundle.cmd_results[0].rejected_reason is not None


async def test_timeout_rejected(zone_drive):
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "import time; time.sleep(5)"',
        test_log=None,
        artifact_root=zone_drive,
        command_gate=_allow_all,
        timeout_seconds=1,
    )
    assert bundle.cmd_results[0].rejected_reason is not None
    assert "timeout" in bundle.cmd_results[0].rejected_reason.lower()


def test_test_log_assertion_gate():
    assert has_passing_assertions("== 3 passed in 0.2s ==") is True
    assert has_passing_assertions("0 passed, 2 failed") is False
    assert has_passing_assertions("looks good to me") is False
    assert has_passing_assertions("") is False
    assert has_passing_assertions(None) is False


async def test_bundle_digest_compact(zone_drive):
    resource_manager.zone_write_text(zone_drive.child("out.md"), "x")
    bundle = await collect_evidence(
        artifact_refs=["out.md"],
        verification_cmd=f'"{_PY}" -c "print(\'2 passed\')"',
        test_log="2 passed",
        artifact_root=zone_drive,
        command_gate=_allow_all,
    )
    digest = bundle_digest(bundle)
    assert "exit 0" in digest
    assert "out.md" in digest
    assert len(digest) < 600


# ── ADR-0293：證據檔與 cwd 在個人空間裡，不跟隨連結 ──────────────────
ALICE = AgentIdentity.for_role(agent_id="Alice", role="Owner")


async def _collect(drive, *, refs=(), cmd=None, identity=None, timeout=60):
    return await collect_evidence(
        artifact_refs=list(refs),
        verification_cmd=cmd,
        test_log=None,
        artifact_root=drive,
        command_gate=_allow_all,
        identity=identity,
        timeout_seconds=timeout,
    )


def _recording_spawn(monkeypatch, *, after=None) -> list[dict]:
    """包住真的 spawn：記下 cwd 當下是否交出中；``after`` 在 spawn 回來後呼叫。"""
    real = evidence.spawn_exec
    calls: list[dict] = []

    async def spawn(*argv, **kwargs):
        cwd = kwargs["cwd"]
        calls.append({"cwd": cwd, "handed": is_handed_off(cwd, HandoffKind.SUBPROCESS_CWD)})
        proc = await real(*argv, **kwargs)
        if after is not None:
            after()
        return proc

    monkeypatch.setattr(evidence, "spawn_exec", spawn)
    return calls


async def test_refs_outside_the_drive_or_not_a_file_are_not_present(zone_drive, zone_data_root):
    """跳出個人空間、絕對路徑或資料夾都不算證據（以前 ``../x.md`` 會找到外面的檔案）。"""
    (zone_data_root / "personal_drive" / "x.md").write_text("外面", encoding="utf-8")
    resource_manager.zone_make_dir(zone_drive.child("sub"))
    refs = ["../x.md", "a/../../x.md", "/etc/passwd", "sub"]
    bundle = await _collect(zone_drive, refs=refs)
    assert bundle.artifacts_present == dict.fromkeys(refs, False)


@pytest.mark.parametrize("ref", ["out/report.md", "out"])
async def test_a_linked_evidence_path_is_refused(zone_drive, zone_data_root, tmp_path, ref):
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    (fake / "report.md").write_text("假的", encoding="utf-8")
    make_dir_link(zone_data_root / "personal_drive" / "Alice" / "out", fake)
    with pytest.raises(LinkRefusedError):
        await _collect(zone_drive, refs=[ref])


async def test_a_linked_drive_does_not_start_the_command(
    zone_drive, zone_data_root, tmp_path, monkeypatch
):
    folder = zone_data_root / "personal_drive" / "Alice"
    folder.rmdir()
    fake = tmp_path.resolve() / "fake"
    fake.mkdir()
    make_dir_link(folder, fake)
    calls = _recording_spawn(monkeypatch)
    with pytest.raises(LinkRefusedError):
        await _collect(zone_drive, cmd=f'"{_PY}" -c "print(1)"', identity=ALICE)
    assert calls == []


async def test_the_command_runs_in_the_handed_off_drive_and_is_registered(
    zone_drive, zone_data_root, monkeypatch
):
    """cwd 交出到 spawn 回來；行程帶 agent_id 登記，跑完取消登記與交出。"""
    calls = _recording_spawn(monkeypatch)
    registered: list[tuple] = []
    real_register = process_registry.register

    def register(session_id, proc, *, agent_id=None):
        registered.append((session_id, agent_id))
        real_register(session_id, proc, agent_id=agent_id)

    monkeypatch.setattr(process_registry, "register", register)
    cmd = f'"{_PY}" -c "import os; print(os.getcwd())"'
    bundle = await _collect(zone_drive, cmd=cmd, identity=ALICE)
    result = bundle.cmd_results[0]
    assert result.exit_code == 0 and "Alice" in result.output_snippet
    assert [call["handed"] for call in calls] == [True]
    assert not is_handed_off(calls[0]["cwd"], HandoffKind.SUBPROCESS_CWD)
    assert registered == [(None, "Alice")]
    assert process_registry.terminate_agent("Alice") == 0  # 跑完已取消登記


async def test_a_held_agent_does_not_start_the_command(zone_drive, monkeypatch):
    calls = _recording_spawn(monkeypatch)
    destructive_ops.hold(ALICE)
    try:
        bundle = await _collect(zone_drive, cmd=f'"{_PY}" -c "print(1)"', identity=ALICE)
    finally:
        destructive_ops.release(ALICE)
    assert bundle.cmd_results[0].rejected_reason == AGENT_BUSY
    assert calls == []


async def test_a_reset_that_starts_during_spawn_stops_the_command(zone_drive, monkeypatch):
    """檢查之後、登記之前才開始的重置：登記後再看一次，停掉這個行程。"""
    _recording_spawn(monkeypatch, after=lambda: destructive_ops.hold(ALICE))
    cmd = f'"{_PY}" -c "import time; time.sleep(30)"'
    try:
        bundle = await _collect(zone_drive, cmd=cmd, identity=ALICE)
    finally:
        destructive_ops.release(ALICE)
    assert bundle.cmd_results[0].rejected_reason == AGENT_BUSY
    assert process_registry.terminate_agent("Alice") == 0
