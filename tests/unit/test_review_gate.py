"""Phase 3 步驟 3-4（互審設計文件 §4.3/§4.4）：Task 審查欄位＋落盤 gate——TDD.

gate 做成 ``_save_mission_unlocked``（唯一落盤閘口）的**狀態不變式**，而非某個
轉移方法上的檢查——因為 ``save_mission`` 直呼落盤、零 FSM 檢查（已核碼的後門）。
- review_required 的 task：``review_status ∈ {APPROVED, APPROVED_LOW_CONFIDENCE(僅
  normal 風險)}`` 才可落盤為 DONE；
- review 欄位只能經 ``record_review`` 寫入（can_review capability＋reviewer≠看板
  擁有者＝堵自簽）；
- **TOCTOU**：record APPROVED 後 producer 重寫內容（artifact/test_log）→ 複審失效
  （review_status 重設），防「先蓋 PASS 再改產出」；
- ``verification_cmd_locked``：init 帶 cmd 即上鎖，落盤時鎖定 cmd 被剝除即 raise。
"""

from __future__ import annotations

import pytest
from memory_server.models.mission import Task, TaskStatus
from memory_server.services.mission_manager import MissionManager
from src.models.identity import AgentIdentity

# --------------------------------------------------------------------------- #
# 輔助
# --------------------------------------------------------------------------- #


def _owner(agent_id: str) -> AgentIdentity:
    return AgentIdentity.for_role(agent_id=agent_id, role="Owner")


# record_review 的呼叫者＝review_loop（跑在主 agent 的 request context 內，Owner/
# Debugger 角色天然過 can_review）；自簽防線在 reviewer_id 標籤獨立性（≠看板擁有者）。


async def _mission_with_reviewed_task(
    identity: AgentIdentity, *, review_required: bool = True
) -> MissionManager:
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity,
        goal="互審 gate 測試",
        initial_tasks=[{"id": "t1", "description": "產出", "verification_cmd": "pytest -q"}],
    )
    if review_required:
        state = await manager.get_current_mission(identity)
        state.tasks[0].review_required = True
        await manager.save_mission(identity, state)
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DOING)
    return manager


# --------------------------------------------------------------------------- #
# 1. Task 審查欄位（§4.3 九欄位）
# --------------------------------------------------------------------------- #


def test_task_review_fields_defaults():
    task = Task(id="t", description="d")
    assert task.review_status is None
    assert task.critic_verdict is None
    assert task.reviewer_id is None
    assert task.review_attempts_total == 0
    assert task.evidence_digest is None
    assert task.review_required is False
    assert task.review_locked is False
    assert task.verification_cmd_locked is False
    assert task.review_risk == "normal"


# --------------------------------------------------------------------------- #
# 2. 落盤不變式（gate 在唯一落盤閘口）
# --------------------------------------------------------------------------- #


async def test_update_task_done_blocked_without_approval():
    identity = _owner("GateBlock")
    manager = await _mission_with_reviewed_task(identity)
    with pytest.raises(ValueError, match="[Rr]eview"):
        await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DONE)


async def test_save_mission_backdoor_also_blocked():
    """紅隊：繞過 update_task、手改 state 後直呼 save_mission → 不變式仍擋。"""
    identity = _owner("GateBackdoor")
    manager = await _mission_with_reviewed_task(identity)
    state = await manager.get_current_mission(identity)
    state.tasks[0].status = TaskStatus.DONE  # 後門直改
    with pytest.raises(ValueError, match="[Rr]eview"):
        await manager.save_mission(identity, state)


async def test_approved_task_can_be_done():
    identity = _owner("GateApproved")
    manager = await _mission_with_reviewed_task(identity)
    await manager.record_review(
        identity,
        "t1",
        review_status="APPROVED",
        reviewer_id="__reviewer_t1__@m-critic",
        critic_verdict="APPROVE: 證據齊備",
    )
    state = await manager.update_task(
        identity=identity, task_id="t1", status=TaskStatus.DONE
    )
    assert state.tasks[0].status == TaskStatus.DONE
    assert state.status == TaskStatus.DONE  # 聚合也放行


async def test_low_confidence_passes_only_normal_risk():
    identity = _owner("GateLowConf")
    manager = await _mission_with_reviewed_task(identity)
    await manager.record_review(
        identity,
        "t1",
        review_status="APPROVED_LOW_CONFIDENCE",
        reviewer_id="__reviewer_t1__@same-brain",
    )
    # normal 風險：放行
    await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DONE)

    # high 風險：同款低信心不放行（決策 3 分層）
    identity2 = _owner("GateLowConfHigh")
    manager2 = await _mission_with_reviewed_task(identity2)
    state = await manager2.get_current_mission(identity2)
    state.tasks[0].review_risk = "high"
    await manager2.save_mission(identity2, state)
    await manager2.record_review(
        identity2,
        "t1",
        review_status="APPROVED_LOW_CONFIDENCE",
        reviewer_id="__reviewer_t1__@same-brain",
    )
    with pytest.raises(ValueError, match="[Rr]eview"):
        await manager2.update_task(
            identity=identity2, task_id="t1", status=TaskStatus.DONE
        )


async def test_non_review_task_unaffected():
    """review_required=False（預設）＝零回歸：照舊 DONE。"""
    identity = _owner("GateOff")
    manager = await _mission_with_reviewed_task(identity, review_required=False)
    state = await manager.update_task(
        identity=identity, task_id="t1", status=TaskStatus.DONE
    )
    assert state.tasks[0].status == TaskStatus.DONE


# --------------------------------------------------------------------------- #
# 3. record_review：隔離寫路徑（堵自簽）
# --------------------------------------------------------------------------- #


async def test_record_review_requires_capability():
    identity = _owner("RecCap")
    manager = await _mission_with_reviewed_task(identity)
    nobody = AgentIdentity.for_role(agent_id="RecCap-nobody", role="Base")
    with pytest.raises((ValueError, PermissionError), match="can_review"):
        await manager.record_review(
            nobody,
            "t1",
            review_status="APPROVED",
            reviewer_id="__reviewer_t1__@m",
        )


async def test_record_review_rejects_self_signing():
    """reviewer_id ＝ 看板擁有者 → 自簽假通過，拒絕。"""
    identity = _owner("RecSelf")
    manager = await _mission_with_reviewed_task(identity)
    with pytest.raises(ValueError, match="self"):
        await manager.record_review(
            identity,
            "t1",
            review_status="APPROVED",
            reviewer_id="RecSelf",  # == 看板 agent_id
        )


async def test_record_review_rejects_unknown_status():
    identity = _owner("RecBadStatus")
    manager = await _mission_with_reviewed_task(identity)
    with pytest.raises(ValueError, match="review_status"):
        await manager.record_review(
            identity,
            "t1",
            review_status="TOTALLY_FINE",
            reviewer_id="__reviewer_t1__@m",
        )


async def test_record_review_persists_fields_and_attempts():
    identity = _owner("RecPersist")
    manager = await _mission_with_reviewed_task(identity)
    await manager.record_review(
        identity,
        "t1",
        review_status="REVISING",
        reviewer_id="__reviewer_t1__@m-critic",
        critic_verdict="REJECT: 缺結論",
        evidence_digest="cmd exit 1",
        attempts_increment=1,
    )
    state = await manager.get_current_mission(identity)
    task = state.tasks[0]
    assert task.review_status == "REVISING"
    assert task.critic_verdict == "REJECT: 缺結論"
    assert task.reviewer_id == "__reviewer_t1__@m-critic"
    assert task.evidence_digest == "cmd exit 1"
    assert task.review_attempts_total == 1


# --------------------------------------------------------------------------- #
# 4. TOCTOU：先蓋 PASS 再改產出 → 複審失效
# --------------------------------------------------------------------------- #


async def test_content_rewrite_after_approval_resets_review():
    identity = _owner("Toctou")
    manager = await _mission_with_reviewed_task(identity)
    await manager.record_review(
        identity,
        "t1",
        review_status="APPROVED",
        reviewer_id="__reviewer_t1__@m",
    )
    # producer 改產出（內容變更）→ 複審失效
    await manager.update_task(
        identity=identity,
        task_id="t1",
        status=TaskStatus.DOING,
        artifact_refs=["rewritten.md"],
    )
    state = await manager.get_current_mission(identity)
    assert state.tasks[0].review_status is None  # stale 化
    # 之後想 DONE 必須重審
    with pytest.raises(ValueError, match="[Rr]eview"):
        await manager.update_task(identity=identity, task_id="t1", status=TaskStatus.DONE)


async def test_status_only_transition_keeps_review():
    """純狀態轉移（無內容參數）不重設複審（DOING→DONE 是正常收案路徑）。"""
    identity = _owner("ToctouKeep")
    manager = await _mission_with_reviewed_task(identity)
    await manager.record_review(
        identity,
        "t1",
        review_status="APPROVED",
        reviewer_id="__reviewer_t1__@m",
    )
    state = await manager.update_task(
        identity=identity, task_id="t1", status=TaskStatus.DONE
    )
    assert state.tasks[0].review_status == "APPROVED"


# --------------------------------------------------------------------------- #
# 5. verification_cmd 上鎖
# --------------------------------------------------------------------------- #


async def test_verification_cmd_locked_at_init_and_strip_rejected():
    identity = _owner("CmdLock")
    manager = await _mission_with_reviewed_task(identity, review_required=False)
    state = await manager.get_current_mission(identity)
    assert state.tasks[0].verification_cmd_locked is True  # init 帶 cmd 即上鎖

    state.tasks[0].verification_cmd = None  # 紅隊：剝除考卷
    with pytest.raises(ValueError, match="locked"):
        await manager.save_mission(identity, state)


async def test_task_without_cmd_not_locked():
    identity = _owner("CmdNoLock")
    manager = MissionManager()
    await manager.initialize_mission(
        identity=identity, goal="g", initial_tasks=[{"id": "t1", "description": "d"}]
    )
    state = await manager.get_current_mission(identity)
    assert state.tasks[0].verification_cmd_locked is False
