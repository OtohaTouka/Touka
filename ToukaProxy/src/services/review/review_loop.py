"""review_loop——單輪審查回合的 orchestrator 薄層（互審設計文件 §4.2，ADR-0137）.

驅動「蒐證 → 隔離/預算檢查 → 審 → verdict 分流 → 落板」一輪；producer 的
修正重跑由主 agent 決定（REVISING 回到主 agent＝FSM 的 REVISING→再蒐證由下一輪
``run_review_round`` 承接）。狀態**每輪落回看板**（``record_review``）＝跨重啟可續。

裁決鐵則（順序即優先級）：
1. **永鎖／深度／成本／等人核准／軟任務／隔離**先擋（不開審＝不花 critic 成本）；
2. **證據優先**：critic 意見 APPROVE 推翻不了硬檢查失敗（意見層不能否決證據層）；
3. 同模型＋normal 風險：APPROVE 降級 ``APPROVED_LOW_CONFIDENCE``（顯式、不背書）；
   高風險＋同模型：**不開審直接 BLOCKED**（異模型硬前提，決策 3/6）；
4. REJECT：停滯偵測（連續兩輪成功解析的 blocking fixes 集合相同→早停）→
   attempts 達 cap → ``BLOCKED_FOR_HUMAN``＋``review_locked``；否則 ``REVISING``；
5. reviewer 傳輸不可用＝fail-closed 合成 REJECT（絕不因審不了而放行）。

臨時審查身分不落盤：本模組**不**呼叫 ``record_working_brain``／soul 寫入；
critic 是一次 completer 呼叫（無工具面＝能力面根絕遞迴）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.services.completion import complete_once
from src.services.review.budget import (
    cost_ceiling_exceeded,
    depth_allows_spawn,
    effective_review_cap,
    get_review_ledger,
)
from src.services.review.evidence import (
    AWAITING_HUMAN_APPROVAL,
    NO_HUMAN_DECISION,
    NO_ISOLATED_ENVIRONMENT,
    REJECTED_BY_HUMAN,
    bundle_digest,
    collect_evidence,
    policy_command_gate,
)
from src.services.review.reviewer import (
    CritiqueReport,
    GroundedReviewer,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from src.models.identity import AgentIdentity
    from src.services.review.evidence import CommandApprover, CommandReviewer

logger = logging.getLogger(__name__)

_CRITIC_VERDICT_LIMIT = 300
_BLOCK = "BLOCKED_FOR_HUMAN"
_CRITIC_MAX_TOKENS = 1024
_CRITIC_TIMEOUT_S = 120.0
# 驗證指令卡在人類這一關（B1）：本輪不送審、不計次數、不上鎖；值＝回給代理人的 reason
_HUMAN_GATE = {
    AWAITING_HUMAN_APPROVAL: "human_approval_required",
    NO_HUMAN_DECISION: "human_approval_required",
    REJECTED_BY_HUMAN: "human_rejected_command",
    NO_ISOLATED_ENVIRONMENT: "isolation_unavailable",
}


class CriticCallError(Exception):
    """critic 的 complete_once 回非成功；GroundedReviewer 把它當傳輸失敗（fail-closed REJECT）。"""


def build_critic_completer(
    identity: AgentIdentity, model: str
) -> Callable[[str, str], Awaitable[str]]:
    """組 critic 的一次性 completer：走 complete_once（purpose=critic，ADR-0244）.

    單次呼叫、非子 ReAct loop；記帳到呼叫者身分並受成本閘約束，路由依 critic 模型
    （不跨廠商）。超預算、逾時、拒答、截斷、廠商錯誤一律拋 CriticCallError，即使
    結果帶著文字也不採用＝沿用原本「傳輸失敗＝REJECT」。
    """

    async def _completer(system: str, user: str) -> str:
        result = await complete_once(
            identity, "critic", model, system, user, _CRITIC_MAX_TOKENS, _CRITIC_TIMEOUT_S
        )
        if not result.ok:
            raise CriticCallError(result.error.reason)
        return result.text

    return _completer


@dataclass(frozen=True)
class ReviewOutcome:
    """一輪審查的裁決結果（review_status 已同步落板）。"""

    review_status: str
    critique: CritiqueReport | None
    evidence_digest: str
    note: str
    # B1：驗證指令卡在人類這一關時的 reason（note 為英文閘門理由）；None＝一般審查／永鎖
    human_gate: str | None = None


def _stash_key(task_id: str) -> str:
    return f"review_fixes:{task_id}"


def _synthetic_reject(reviewer_id: str, critic_model: str) -> CritiqueReport:
    return CritiqueReport(
        verdict="REJECT",
        reasoning="fail-closed: reviewer transport unavailable",
        reviewer_id=reviewer_id,
        critic_model=critic_model,
    )


async def run_review_round(
    *,
    task: Any,
    goal: str,
    producer_claims: str,
    artifact_root: Path,
    parent_identity: AgentIdentity,
    mission_id: str,
    manager: Any,
    completer: Callable[[str, str], Awaitable[str]] | None,
    critic_model: str | None,
    producer_model: str | None,
    config: dict[str, Any] | None = None,
    review_depth: int = 0,
    auth_level: str = "Manual",
    approver: CommandApprover | None = None,
    reviewer: CommandReviewer | None = None,
    project_root: Path | None = None,
) -> ReviewOutcome:
    """跑一輪審查並把結論落板；回 ReviewOutcome（供主 agent／工具層格式化）.

    ``auth_level`` 決定 verification_cmd 走哪一級終端指令政策；CONFIRM 級指令只在有
    ``approver``（有人在場）且人類對該條核准時執行。未帶＝Manual；沒被核准（無人在場、
    逾時、被拒）＝不執行、本輪不送審（B1）。Auto 下 REVIEW 級指令先問 ``reviewer``（審查員）。
    """
    # --- 以看板新鮮狀態為準（attempts/locked/risk 可能已被前輪更新）---
    state = await manager.get_current_mission(parent_identity)
    fresh = next((t for t in state.tasks if t.id == task.id), None) if state else None
    if fresh is None:
        return ReviewOutcome(_BLOCK, None, "", f"task '{task.id}' not found on board")
    prev_fixes = (state.metadata or {}).get(_stash_key(task.id))

    gate_reviewer_id = f"__reviewer_{task.id}__@gate"

    async def _record(
        status: str,
        *,
        reviewer_id: str,
        critique: CritiqueReport | None = None,
        digest: str = "",
        attempts_increment: int = 0,
        lock: bool | None = None,
        stash: list[str] | None = None,
    ) -> None:
        verdict_text = None
        if critique is not None:
            verdict_text = f"{critique.verdict}: {critique.reasoning}"[:_CRITIC_VERDICT_LIMIT]
        await manager.record_review(
            parent_identity,
            task.id,
            review_status=status,
            reviewer_id=reviewer_id,
            critic_verdict=verdict_text,
            evidence_digest=digest or None,
            attempts_increment=attempts_increment,
            review_locked=lock,
            stash_fixes=stash,
        )

    # --- 1. 永鎖：不再開審（human-ack 路徑）---
    if fresh.review_locked:
        return ReviewOutcome(
            _BLOCK, None, fresh.evidence_digest or "",
            "review_locked：已達預算上限永鎖，僅能 human-ack",
        )

    # --- 2/3. 深度與成本硬界（開任何子呼叫前）---
    if not depth_allows_spawn(review_depth):
        await _record(_BLOCK, reviewer_id=gate_reviewer_id)
        return ReviewOutcome(_BLOCK, None, "", "review_depth 超出兩層承諾，拒開審")
    if cost_ceiling_exceeded(parent_identity, config=config):
        await _record(_BLOCK, reviewer_id=gate_reviewer_id)
        return ReviewOutcome(_BLOCK, None, "", "成本天花板已達，拒開審（互審成本閘不可關）")

    # --- 4. 蒐證（非阻塞；verification_cmd 先過終端指令政策）---
    evidence = await collect_evidence(
        artifact_refs=list(fresh.artifact_refs or []),
        verification_cmd=fresh.verification_cmd,
        test_log=fresh.test_log,
        artifact_root=artifact_root,
        command_gate=policy_command_gate(
            parent_identity,
            auth_level=auth_level,
            approver=approver,
            reviewer=reviewer,
            project_root=project_root,
        ),
        identity=parent_identity,
        project_root=project_root,
        consent=approver.take_consent if approver is not None else None,
    )
    digest = bundle_digest(evidence)

    # --- 4b. 驗證指令卡在人類這一關（B1：無人在場／沒等到決定／被拒）：不送審、不計次數、不上鎖 ---
    gated = [r.rejected_reason for r in evidence.cmd_results if r.rejected_reason in _HUMAN_GATE]
    if gated:
        await _record(_BLOCK, reviewer_id=gate_reviewer_id, digest=digest)
        return ReviewOutcome(_BLOCK, None, digest, gated[0], human_gate=_HUMAN_GATE[gated[0]])

    # --- 5. 軟任務：無可執行證據 → human-ack（不靜默放行，設計反轉 2）---
    if not evidence.has_evidence:
        await _record(_BLOCK, reviewer_id=gate_reviewer_id, digest=digest)
        return ReviewOutcome(
            _BLOCK, None, digest, "軟任務（無可執行證據）：需 human-ack 才能收案"
        )

    # --- 6. 隔離：高風險＋同模型＝硬前提不滿足（不開審，fail-closed）---
    same_model = (not critic_model) or (
        bool(producer_model) and critic_model == producer_model
    )
    if fresh.review_risk == "high" and same_model:
        await _record(_BLOCK, reviewer_id=gate_reviewer_id, digest=digest)
        return ReviewOutcome(
            _BLOCK, None, digest,
            "高風險 task 需異模型 critic（硬前提）；未設定＝fail-closed 轉人",
        )

    # --- 7. spawn 記帳（mission 級 ledger）---
    ledger = get_review_ledger()
    if not await ledger.register_spawn(mission_id):
        await _record(_BLOCK, reviewer_id=gate_reviewer_id, digest=digest, lock=True)
        return ReviewOutcome(_BLOCK, None, digest, "mission 子 loop 開啟數達上限，永鎖")

    # --- 8. 審（一次 completer 呼叫；不可用＝fail-closed 合成 REJECT）---
    reviewer_id = f"__reviewer_{task.id}__@{critic_model or 'same-brain'}"
    if completer is None:
        critique = _synthetic_reject(reviewer_id, critic_model or "")
    else:
        reviewer = GroundedReviewer(
            completer, critic_model=critic_model or "", reviewer_id=reviewer_id
        )
        critique = await reviewer.review(
            goal=f"{goal}\n【子任務】{fresh.description}",
            boundary_rules=list(fresh.boundary_rules or []),
            producer_claims=producer_claims,
            evidence=evidence,
        )

    cap = effective_review_cap(((config or {}).get("review") or {}).get("max_attempts"))

    # --- 9. 分流 ---
    if critique.verdict == "APPROVE" and evidence.hard_checks_pass:
        status = (
            "APPROVED_LOW_CONFIDENCE"
            if same_model and fresh.review_risk == "normal"
            else "APPROVED"
        )
        await _record(status, reviewer_id=reviewer_id, critique=critique, digest=digest)
        note = (
            "同模型 critic：放行但不背書（LOW_CONFIDENCE）"
            if status == "APPROVED_LOW_CONFIDENCE"
            else "審查通過（異模型＋證據硬檢查全過）"
        )
        return ReviewOutcome(status, critique, digest, note)

    if critique.verdict == "ABSTAIN":
        await _record(_BLOCK, reviewer_id=reviewer_id, critique=critique, digest=digest)
        return ReviewOutcome(_BLOCK, critique, digest, "critic 無法判斷（ABSTAIN）→ 轉人")

    # REJECT（含「意見 APPROVE 但證據不過」的證據優先降流）
    evidence_override = critique.verdict == "APPROVE" and not evidence.hard_checks_pass

    fixes_norm = sorted({f.issue.strip() for f in critique.required_fixes if f.issue.strip()})
    if fixes_norm and prev_fixes == fixes_norm:
        # 停滯：連續兩輪成功解析、同一組 blocking 問題 → 早停（不再空轉）
        await _record(
            _BLOCK, reviewer_id=reviewer_id, critique=critique, digest=digest, lock=True
        )
        return ReviewOutcome(_BLOCK, critique, digest, "停滯偵測：兩輪同組問題未收斂，早停轉人")

    attempts_next = fresh.review_attempts_total + 1
    if attempts_next >= cap:
        await _record(
            _BLOCK,
            reviewer_id=reviewer_id,
            critique=critique,
            digest=digest,
            attempts_increment=1,
            lock=True,
        )
        return ReviewOutcome(
            _BLOCK, critique, digest, f"往返達上限（{cap}）：永鎖，僅能 human-ack"
        )

    await _record(
        "REVISING",
        reviewer_id=reviewer_id,
        critique=critique,
        digest=digest,
        attempts_increment=1,
        stash=fixes_norm or None,
    )
    note = (
        "證據硬檢查未過（意見層不能推翻證據層）：退回修正"
        if evidence_override
        else "critic 挑出問題：退回修正（見 required_fixes）"
    )
    return ReviewOutcome("REVISING", critique, digest, note)
