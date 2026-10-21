"""Phase 3 步驟 5（互審設計文件 §4.5 層1）：EvidenceCollector——TDD.

verdict 的權威來源：**純 Python 蒐證**（非 LLM）——跑 verification_cmd 取 exit
code、核對 artifact 存在、節錄 test_log。反橡皮圖章三件套（設計 §6.2-6）：
- 拒 no-op 前綴（true／echo…／exit 0／:）＝假考卷判硬失敗；
- pytest 零收集（exit 0 但「collected 0 items／no tests ran」）＝假通過判硬失敗；
- test_log 斷言數 >0（``test_log_passes``）。
"""

from __future__ import annotations

import sys

from src.services.review.evidence import (
    bundle_digest,
    collect_evidence,
    has_passing_assertions,
)

_PY = sys.executable


async def _allow_all(_cmd: str) -> str | None:
    """本檔測蒐證語意；指令政策閘另由 test_subprocess_hardening_security 覆蓋。"""
    return None


async def test_artifact_existence_checked(tmp_path):
    (tmp_path / "out.md").write_text("內容", encoding="utf-8")
    bundle = await collect_evidence(
        artifact_refs=["out.md", "ghost.md"],
        verification_cmd=None,
        test_log=None,
        artifact_root=tmp_path,
        command_gate=_allow_all,
    )
    assert bundle.artifacts_present == {"out.md": True, "ghost.md": False}
    assert bundle.hard_checks_pass is False  # ghost 不存在


async def test_cmd_exit_codes_captured(tmp_path):
    ok = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "print(\'3 passed\')"',
        test_log=None,
        artifact_root=tmp_path,
        command_gate=_allow_all,
    )
    assert ok.cmd_results[0].exit_code == 0
    assert "3 passed" in ok.cmd_results[0].output_snippet
    assert ok.hard_checks_pass is True

    fail = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "import sys; sys.exit(1)"',
        test_log=None,
        artifact_root=tmp_path,
        command_gate=_allow_all,
    )
    assert fail.cmd_results[0].exit_code == 1
    assert fail.hard_checks_pass is False


async def test_noop_commands_hard_rejected(tmp_path):
    for cmd in ("true", "echo ok", "exit 0", ":"):
        bundle = await collect_evidence(
            artifact_refs=[],
            verification_cmd=cmd,
            test_log=None,
            artifact_root=tmp_path,
            command_gate=_allow_all,
        )
        result = bundle.cmd_results[0]
        assert result.rejected_reason is not None, cmd
        assert result.ok is False, cmd
        assert bundle.hard_checks_pass is False, cmd


async def test_pytest_zero_collection_rejected(tmp_path):
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "print(\'collected 0 items\')"',
        test_log=None,
        artifact_root=tmp_path,
        command_gate=_allow_all,
    )
    result = bundle.cmd_results[0]
    assert result.exit_code == 0  # 表面成功
    assert result.rejected_reason is not None  # 但零收集＝橡皮圖章，硬拒
    assert bundle.hard_checks_pass is False


async def test_missing_command_rejected(tmp_path):
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd="definitely_not_a_real_command_xyz --flag",
        test_log=None,
        artifact_root=tmp_path,
        command_gate=_allow_all,
    )
    assert bundle.cmd_results[0].rejected_reason is not None


async def test_timeout_rejected(tmp_path):
    bundle = await collect_evidence(
        artifact_refs=[],
        verification_cmd=f'"{_PY}" -c "import time; time.sleep(5)"',
        test_log=None,
        artifact_root=tmp_path,
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


async def test_bundle_digest_compact(tmp_path):
    (tmp_path / "out.md").write_text("x", encoding="utf-8")
    bundle = await collect_evidence(
        artifact_refs=["out.md"],
        verification_cmd=f'"{_PY}" -c "print(\'2 passed\')"',
        test_log="2 passed",
        artifact_root=tmp_path,
        command_gate=_allow_all,
    )
    digest = bundle_digest(bundle)
    assert "exit 0" in digest
    assert "out.md" in digest
    assert len(digest) < 600
