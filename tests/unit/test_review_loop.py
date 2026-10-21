"""Phase 3 步驟 7（互審設計文件 §4.2/§4.5/§4.6）：review_loop 單輪審查——TDD.

驅動一輪「蒐證→隔離檢查→審→verdict 分流→落板」：
- **證據優先**：critic 意見 APPROVE 但證據硬檢查不過 → 仍 REVISING（verdict 不能推翻證據）；
- **隔離**：高風險＋同模型 → 不開審直接 BLOCKED_FOR_HUMAN（fail-closed，省成本）；
  同模型＋normal → APPROVE 降級 APPROVED_LOW_CONFIDENCE（顯式、不背書）；
- **軟任務**（無任何可執行證據）→ BLOCKED_FOR_HUMAN（不靜默放行——設計反轉 2）；
- **收斂三重保險**：attempts 達 cap → BLOCKED＋review_locked；停滯（連續兩輪成功
  解析的 fixes 集合相同）→ 早停 BLOCKED；APPROVED* 終態；
- **不落盤臨時身分**：全路徑不呼 record_working_brain。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from memory_server.models.mission import TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity
from src.services.autonomy import autonomy_service
from src.services.completion import CompletionError, CompletionResult
from src.services.review.budget import _reset_review_ledger
from src.services.review.review_loop import (
    ReviewOutcome,
    build_critic_completer,
    run_review_round,
)
from src.services.review.reviewer import EvidenceBundle, GroundedReviewer
from src.tools import mission_tools as mt

_COMPLETE = "src.services.review.review_loop.complete_once"

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_ledger():
    _reset_review_ledger()
    yield
    _reset_review_ledger()


def _completer_returning(verdict_json: str):
    calls: list[tuple[str, str]] = []

    async def _completer(system: str, user: str) -> str:
        calls.append((system, user))
        return verdict_json

    return _completer, calls


def _approve_json() -> str:
    return (
        '```json\n{"verdict": "APPROVE", "reasoning": "證據齊備",'
        ' "required_fixes": [], "confidence": 0.9}\n```'
    )


def _reject_json(issue: str = "缺結論") -> str:
    return (
        '```json\n{"verdict": "REJECT", "reasoning": "不過",'
        f' "required_fixes": [{{"issue": "{issue}", "evidence_anchor": "out.md",'
        ' "severity": "blocking"}], "confidence": 0.9}\n```'
    )


async def _make_reviewed_mission(identity: AgentIdentity, *, risk: str = "normal"):
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity,
        goal="產出摘要",
        initial_tasks=[
            {
                "id": "t1",
                "description": "寫 out.md",
                "review_required": True,
                "review_risk": risk,
            }
        ],
    )
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DOING)
    return manager


async def _run(
    manager: MissionManager,
    identity: AgentIdentity,
    tmp_path,
    *,
    completer,
    critic_model: str | None = "critic-brain",
    producer_model: str | None = "producer-brain",
    claims: str = "已完成 out.md",
) -> ReviewOutcome:
    state = await manager.get_current_mission(identity)
    task = state.tasks[0]
    return await run_review_round(
        task=task,
        goal=state.goal,
        producer_claims=claims,
        artifact_root=tmp_path,
        parent_identity=identity,
        mission_id=state.mission_id,
        manager=manager,
        completer=completer,
        critic_model=critic_model,
        producer_model=producer_model,
    )


def _with_artifact(manager, identity, tmp_path):
    (tmp_path / "out.md").write_text("摘要", encoding="utf-8")


async def _set_artifacts(manager, identity):
    await manager.update_task(
        identity=identity,
        task_id="t1",
        status=TaskStatus.DOING,
        artifact_refs=["out.md"],
    )


# --------------------------------------------------------------------------- #
# 1. 放行路徑：異模型 APPROVED／同模型降級
# --------------------------------------------------------------------------- #


async def test_approve_with_isolated_critic_records_approved(tmp_path, monkeypatch):
    identity = AgentIdentity.for_role(agent_id="LoopApprove", role="Owner")
    manager = await _make_reviewed_mission(identity)
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    brain_spy = MagicMock()
    monkeypatch.setattr("src.utils.brain_persistence.record_working_brain", brain_spy)

    completer, _calls = _completer_returning(_approve_json())
    outcome = await _run(manager, identity, tmp_path, completer=completer)

    assert outcome.review_status == "APPROVED"
    state = await manager.get_current_mission(identity)
    task = state.tasks[0]
    assert task.review_status == "APPROVED"
    assert task.reviewer_id.startswith("__reviewer_t1__@")
    assert task.evidence_digest
    brain_spy.assert_not_called()  # 不落盤臨時身分

    # gate 放行收案
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DONE)


async def test_same_brain_normal_risk_downgrades_low_confidence(tmp_path):
    identity = AgentIdentity.for_role(agent_id="LoopSameBrain", role="Owner")
    manager = await _make_reviewed_mission(identity)
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    completer, _ = _completer_returning(_approve_json())
    outcome = await _run(
        manager, identity, tmp_path,
        completer=completer, critic_model="same-brain", producer_model="same-brain",
    )
    assert outcome.review_status == "APPROVED_LOW_CONFIDENCE"


async def test_high_risk_same_brain_blocks_without_calling_critic(tmp_path):
    """高風險＋同模型＝隔離硬前提不滿足 → 不開審直接 BLOCKED（fail-closed＋省成本）。"""
    identity = AgentIdentity.for_role(agent_id="LoopHighRisk", role="Owner")
    manager = await _make_reviewed_mission(identity, risk="high")
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    completer, calls = _completer_returning(_approve_json())
    outcome = await _run(
        manager, identity, tmp_path,
        completer=completer, critic_model="same-brain", producer_model="same-brain",
    )
    assert outcome.review_status == "BLOCKED_FOR_HUMAN"
    assert calls == []  # critic 未被呼叫


# --------------------------------------------------------------------------- #
# 2. 證據優先：意見 APPROVE 推翻不了硬檢查
# --------------------------------------------------------------------------- #


async def test_approve_verdict_cannot_override_failed_evidence(tmp_path):
    identity = AgentIdentity.for_role(agent_id="LoopEvidence", role="Owner")
    manager = await _make_reviewed_mission(identity)
    await _set_artifacts(manager, identity)  # 宣稱 out.md 但檔案不存在

    completer, _ = _completer_returning(_approve_json())
    outcome = await _run(manager, identity, tmp_path, completer=completer)
    assert outcome.review_status == "REVISING"  # 證據層否決意見層

    state = await manager.get_current_mission(identity)
    assert state.tasks[0].review_attempts_total == 1


# --------------------------------------------------------------------------- #
# 3. 軟任務／REJECT 往返／收斂保險
# --------------------------------------------------------------------------- #


async def test_soft_task_without_evidence_blocks_for_human(tmp_path):
    identity = AgentIdentity.for_role(agent_id="LoopSoft", role="Owner")
    manager = await _make_reviewed_mission(identity)
    # 無 artifact、無 verification_cmd（初始未帶）＝無可執行證據

    completer, calls = _completer_returning(_approve_json())
    outcome = await _run(manager, identity, tmp_path, completer=completer)
    assert outcome.review_status == "BLOCKED_FOR_HUMAN"
    assert calls == []  # 軟任務不開審（human-ack 路徑）


async def test_reject_goes_revising_then_stagnation_blocks(tmp_path):
    identity = AgentIdentity.for_role(agent_id="LoopStagnate", role="Owner")
    manager = await _make_reviewed_mission(identity)
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    completer, _ = _completer_returning(_reject_json("缺結論"))
    first = await _run(manager, identity, tmp_path, completer=completer)
    assert first.review_status == "REVISING"

    # 第二輪同樣的 fixes 集合 → 停滯早停
    second = await _run(manager, identity, tmp_path, completer=completer)
    assert second.review_status == "BLOCKED_FOR_HUMAN"
    assert "停滯" in second.note or "stagnation" in second.note.lower()


async def test_attempts_cap_blocks_and_locks(tmp_path):
    identity = AgentIdentity.for_role(agent_id="LoopCap", role="Owner")
    manager = await _make_reviewed_mission(identity)
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    # 每輪不同 issue（避開停滯偵測），直到 CAP=3 觸頂
    outcomes = []
    for i in range(3):
        completer, _ = _completer_returning(_reject_json(f"問題{i}"))
        outcomes.append(await _run(manager, identity, tmp_path, completer=completer))

    assert outcomes[0].review_status == "REVISING"
    assert outcomes[1].review_status == "REVISING"
    assert outcomes[2].review_status == "BLOCKED_FOR_HUMAN"  # 第 3 次達 CAP

    state = await manager.get_current_mission(identity)
    assert state.tasks[0].review_locked is True

    # 永鎖後再審 → 直接 BLOCKED，不開審
    completer, calls = _completer_returning(_reject_json("再來"))
    again = await _run(manager, identity, tmp_path, completer=completer)
    assert again.review_status == "BLOCKED_FOR_HUMAN"
    assert calls == []


async def test_no_completer_fail_closed_revising(tmp_path):
    """審查傳輸不可用＝fail-closed：按 REJECT 分流（不放行）。"""
    identity = AgentIdentity.for_role(agent_id="LoopNoLLM", role="Owner")
    manager = await _make_reviewed_mission(identity)
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    outcome = await _run(manager, identity, tmp_path, completer=None)
    assert outcome.review_status in ("REVISING", "BLOCKED_FOR_HUMAN")
    assert outcome.review_status != "APPROVED"


# --------------------------------------------------------------------------- #
# 4. mission_tools(action="request_review") 接線（monkeypatch run_review_round）
# --------------------------------------------------------------------------- #


async def test_mission_tool_request_review_wires_and_formats(monkeypatch):
    identity = AgentIdentity.for_role(agent_id="ToolReview", role="Owner")
    await _make_reviewed_mission(identity)  # 建板；工具自建 manager 讀同一看板

    seen: dict = {}

    async def _fake_round(**kwargs):
        seen.update(kwargs)
        return ReviewOutcome(
            review_status="APPROVED",
            critique=None,
            evidence_digest="out.md✓; exit 0",
            note="審查通過",
        )

    monkeypatch.setattr(
        "src.services.review.review_loop.run_review_round", _fake_round
    )

    out = str(
        await mt.mission_tools(
            action="request_review",
            task_id="t1",
            producer_claims="已完成 out.md",
            identity=identity,
            model="producer-brain",
            _ctx=None,
        )
    )
    assert "APPROVED" in out
    assert "out.md" in out
    assert seen["producer_claims"] == "已完成 out.md"
    assert seen["task"].id == "t1"
    # critic 走 complete_once（ADR-0244）：completer 綁定本工具的已驗證身分與 critic 模型
    calls: list[tuple] = []

    async def _fake_complete(*args):
        calls.append(args)
        return CompletionResult(text="審查", usage={}, cost_usd=0.0, stop_reason="end",
                                error=None)

    monkeypatch.setattr(_COMPLETE, _fake_complete)
    assert await seen["completer"]("系統", "內容") == "審查"
    assert calls == [(identity, "critic", "producer-brain", "系統", "內容", 1024, 120.0)]


async def test_mission_tool_request_review_missing_task_errors():
    identity = AgentIdentity.for_role(agent_id="ToolReviewMiss", role="Owner")
    await _make_reviewed_mission(identity)  # 建板但查不到目標 task
    out = str(
        await mt.mission_tools(
            action="request_review", task_id="nope", identity=identity, _ctx=None
        )
    )
    assert out.startswith("Error")


async def test_mission_tool_request_review_enforces_cost_ceiling(monkeypatch):
    """E76（ADR-0175）：工具端真接線——autonomy cap 有數字＋累計花費超標 → 不
    monkeypatch run_review_round，request_review 必 BLOCKED（互審成本閘不可關，
    無視 cap.enabled=False）。

    成本閘先於蒐證（review_loop 順序 3），故載重斷言是 note「成本天花板」：漏傳
    config 時本板無任何證據＝落軟任務分支（status 同為 BLOCKED 但 note 不同）＝仍紅。
    """
    identity = AgentIdentity.for_role(agent_id="ToolCostCap", role="Owner")
    await _make_reviewed_mission(identity)

    monkeypatch.setitem(
        autonomy_service.config, "cap", {"enabled": False, "max_total_cost_usd": 0.01}
    )
    monkeypatch.setattr(autonomy_service, "get_total_cost_usd", lambda _identity: 0.02)

    out = str(
        await mt.mission_tools(
            action="request_review",
            task_id="t1",
            producer_claims="已完成 out.md",
            identity=identity,
            _ctx=None,
        )
    )
    assert "BLOCKED_FOR_HUMAN" in out
    assert "成本天花板" in out  # 載重斷言：確為成本閘（非軟任務）擋下


# --------------------------------------------------------------------------- #
# 5. critic 走 complete_once（ADR-0244）：任何非成功都照原本的傳輸失敗＝REJECT
# --------------------------------------------------------------------------- #


def _failed_with_approve_text(reason: str):
    """非成功結果，但刻意夾帶 APPROVE 文字：critic 不得採用。"""

    async def _fake(*_args):
        return CompletionResult(
            text=_approve_json(), usage={}, cost_usd=0.0, stop_reason="error",
            error=CompletionError("upstream_error", reason, "x"),
        )

    return _fake


@pytest.mark.parametrize(
    "reason",
    ["over_budget", "model_unavailable", "missing_credentials", "timeout", "upstream",
     "max_tokens", "refusal", "safety", "unknown_stop", "internal"],
)
async def test_unsuccessful_critic_completion_is_reject(monkeypatch, reason):
    monkeypatch.setattr(_COMPLETE, _failed_with_approve_text(reason))
    identity = AgentIdentity.for_role(agent_id="CriticFail", role="Owner")
    reviewer = GroundedReviewer(
        build_critic_completer(identity, "critic-brain"),
        critic_model="critic-brain",
        reviewer_id="r",
    )
    report = await reviewer.review(
        goal="g", boundary_rules=[], producer_claims="",
        evidence=EvidenceBundle(artifacts_present={"out.md": True}),
    )
    assert report.verdict == "REJECT"
    assert report.reasoning == "fail-closed: completer error: CriticCallError"


async def test_unsuccessful_critic_round_is_revising_not_approved(tmp_path, monkeypatch):
    """整輪：critic 超預算＝按 REJECT 分流（REVISING），證據齊備也不放行。"""
    monkeypatch.setattr(_COMPLETE, _failed_with_approve_text("over_budget"))
    identity = AgentIdentity.for_role(agent_id="LoopCriticFail", role="Owner")
    manager = await _make_reviewed_mission(identity)
    _with_artifact(manager, identity, tmp_path)
    await _set_artifacts(manager, identity)

    outcome = await _run(
        manager, identity, tmp_path,
        completer=build_critic_completer(identity, "critic-brain"),
    )
    assert outcome.review_status == "REVISING"
    assert outcome.critique.reasoning == "fail-closed: completer error: CriticCallError"
