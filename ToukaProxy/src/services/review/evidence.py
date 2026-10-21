"""EvidenceCollector——純 Python 蒐證（互審設計文件 §4.5 層1，ADR-0137）.

verdict 的權威來源：critic 無法以幻覺竄改的**客觀證據**——
跑 per-task ``verification_cmd`` 取 exit code、核對 ``artifact_refs`` 實際
存在、節錄 ``test_log``，組成 ``EvidenceBundle`` 供 GroundedReviewer 比對。

反橡皮圖章三件套（設計 §6.2-6：cmd 由 producer 自寫不可自證）：
- **拒 no-op 前綴**：``true``／``echo …``／``exit 0``／``:``——假考卷判硬失敗；
- **pytest 零收集**：exit 0 但輸出含「collected 0 items／no tests ran」＝
  表面成功的橡皮圖章，判硬失敗；
- **test_log 斷言數 >0**（``test_log_passes``）——「N passed」且 N>0 才算數。

安全邊界：``shlex.split`` 後以 ``shell=False`` 執行（無 shell 注入面）、
``cwd=artifact_root``（agent 沙盒）、硬 timeout；蒐證對檔案**唯讀**。

指令由 agent 撰寫、可能不經 HITL 執行，故與 ``execute_command`` 同等對待：
先過 ``command_gate``（終端指令政策）、子程序只拿淨化環境（無 Touka 密鑰）、
以新行程群組啟動，逾時或取消時連坐殺整棵行程樹。spawn 前才經隔離 runner 改寫啟動參數
（iso-c2：和終端同一個代理人指令 profile；專案根由呼叫端從 session 取得）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import TYPE_CHECKING, Protocol

from src.services.evolution_security import EvolutionPathGuard
from src.services.execution_broker import spawn_exec
from src.services.isolation.agent_command import command_level, command_scope, prepare_command
from src.services.isolation.runners import IsolationUnavailableError, executor_max_seconds
from src.services.permission_service import permission_service
from src.services.process_registry import (
    reap_cancelled_process,
    reap_process_tree,
    terminate_process_tree,
)
from src.services.review.reviewer import CmdResult, EvidenceBundle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from src.models.identity import AgentIdentity
    from src.services.auto_reviewer import ReviewOutcome as ReviewerOutcome
    from src.services.isolation.consent import ExecutionGrant

    # 回傳拒絕理由；None＝放行。
    CommandGate = Callable[[str], Awaitable[str | None]]
    # Auto 審查員（U2c-3）；回判決（"allow"／"escalate"／"deny"）＋理由。
    CommandReviewer = Callable[[str], Awaitable[ReviewerOutcome]]
    # 取回這條指令經卡片核准的 grant（只能一次）；None＝沒有同意。
    ConsentTaker = Callable[[str], ExecutionGrant | None]


class CommandApprover(Protocol):
    """為這一條指令問人類；回審批結果（approval_service 的 "approved"／"rejected"／"timeout"…）.

    卡上帶 Auto 審查員轉人的理由（``review_reason``）與底線的「只有人能核准」（``human_only``）。
    """

    def __call__(
        self, cmd: str, *, review_reason: str | None = None, human_only: bool = False
    ) -> Awaitable[str]:
        """問人類並等決定。"""

    def take_consent(self, cmd: str) -> ExecutionGrant | None:
        """取回這條指令經卡片核准的 grant（只能一次）。"""

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60
_OUTPUT_SNIPPET_LIMIT = 400
_TEST_LOG_EXCERPT_LIMIT = 400

# 驗證指令卡在人類這一關的固定理由（B1）：review_loop 見到就不送審、不計次數、不上鎖。
AWAITING_HUMAN_APPROVAL = "requires human approval, but no human is present to approve it"
NO_HUMAN_DECISION = "requires human approval, but no human decision arrived"  # 逾時、佇列滿
REJECTED_BY_HUMAN = "rejected by the human"
DENIED_BY_REVIEWER = "denied by the Auto reviewer"
NO_ISOLATED_ENVIRONMENT = "requires an isolated execution environment, but none is available"

# no-op 假考卷：整條等於／或以這些前綴開頭（正規化小寫後比對）。
_NOOP_EXACT = frozenset({"true", ":", "exit 0"})
_NOOP_PREFIXES = ("echo", "true ", "exit 0 ", ": ")

# pytest（等）零收集＝表面 exit 0 的橡皮圖章。
_ZERO_COLLECTION_RE = re.compile(r"collected 0 items|no tests ran", re.IGNORECASE)
_PASSED_RE = re.compile(r"(\d+)\s+passed")


def has_passing_assertions(test_log: str | None) -> bool:
    """test_log 斷言數 >0：內容須含「N passed」且 N>0（機械判定、寧嚴勿鬆）。"""
    if not test_log:
        return False
    match = _PASSED_RE.search(test_log)
    return bool(match) and int(match.group(1)) > 0


def _noop_reason(cmd: str) -> str | None:
    normalized = cmd.strip().lower()
    if normalized in _NOOP_EXACT:
        return f"no-op command rejected: '{cmd.strip()}'"
    if any(normalized.startswith(p) for p in _NOOP_PREFIXES):
        return f"no-op command rejected: '{cmd.strip()}'"
    return None


def policy_command_gate(
    identity: AgentIdentity,
    *,
    auth_level: str,
    approver: CommandApprover | None = None,
    reviewer: CommandReviewer | None = None,
    project_root: Path | None = None,
) -> CommandGate:
    """verification_cmd 的政策閘：與 ``execute_command`` 相同的終端指令政策.

    ALLOW → 放行；CONFIRM → 有 ``approver``（有人在場）就為**這一條指令**問人類，核准才
    放行，被拒回 ``REJECTED_BY_HUMAN``、沒等到決定回 ``NO_HUMAN_DECISION``；沒有 approver＝
    無人在場，回 ``AWAITING_HUMAN_APPROVAL``（B2：不接受 request_review 卡的預核准；B1）；
    DENY 與未知決策一律拒絕、不問人。REVIEW（Auto）由 ``reviewer``（Auto 審查員）決定：allow
    放行、deny 回 ``DENIED_BY_REVIEWER``、其餘照 CONFIRM 問人（沒有 reviewer 也照 CONFIRM）。
    問人時卡上帶審查員轉人的理由；HUMAN_ONLY 的卡標「只有人能核准」。分類用這條指令有看
    專案的隔離層級（``project_root`` 只來自 session），不用全域預設。
    """

    async def _gate(cmd: str) -> str | None:
        isolation = await command_level(identity, project_root)
        strategy = permission_service.classify_command(
            cmd, identity, auth_level, isolation=isolation
        )
        if strategy == "ALLOW":
            return None
        review_reason: str | None = None
        if strategy == "REVIEW" and reviewer is not None:
            outcome = await reviewer(cmd)
            if outcome.verdict == "allow":
                return None
            if outcome.verdict == "deny":
                return DENIED_BY_REVIEWER
            review_reason = outcome.reason
        # HUMAN_ONLY（硬地板，ADR-0246）同樣逐條問人；approver 只能是人類審批卡，
        # 審查員核准（U2c-3）不得滿足 HUMAN_ONLY（HUMAN_ONLY 根本不送審）。
        if strategy in ("CONFIRM", "HUMAN_ONLY", "REVIEW"):
            if approver is None:
                return AWAITING_HUMAN_APPROVAL
            verdict = await approver(
                cmd, review_reason=review_reason, human_only=strategy == "HUMAN_ONLY"
            )
            if verdict == "approved":
                return None
            return REJECTED_BY_HUMAN if verdict == "rejected" else NO_HUMAN_DECISION
        return f"denied by command policy ({strategy or 'unknown'})"

    return _gate


async def _run_cmd(
    cmd: str,
    *,
    artifact_root: Path,
    timeout_seconds: float,
    command_gate: CommandGate,
    identity: AgentIdentity | None = None,
    project_root: Path | None = None,
    consent: ConsentTaker | None = None,
) -> CmdResult:
    """執行一條 verification_cmd（與 terminal 同一套執行邊界，經 ExecutionBroker）.

    解析 → 政策閘（CONFIRM 可能逐條問人）→ shell=False → 淨化環境 → 新行程群組 →
    沙盒 cwd → 硬 timeout；逾時或被取消都連坐殺行程樹。蒐證對檔案唯讀。
    """
    noop = _noop_reason(cmd)
    if noop:
        return CmdResult(cmd=cmd, rejected_reason=noop)

    try:
        args = shlex.split(cmd)
    except ValueError as exc:
        return CmdResult(cmd=cmd, rejected_reason=f"unparseable command: {exc}")
    if not args:
        return CmdResult(cmd=cmd, rejected_reason="empty command")

    # 解析得出才過政策閘：不為跑不起來的指令跳審批卡。
    denied = await command_gate(cmd)
    if denied:
        return CmdResult(cmd=cmd, rejected_reason=denied)

    try:
        grant = consent(cmd) if consent is not None else None
        scope = None if identity is None else await command_scope(identity, project_root, grant)
        clean_env = EvolutionPathGuard.get_clean_env()
        prepared = await prepare_command(
            scope, args, clean_env, artifact_root, max_seconds=executor_max_seconds(timeout_seconds)
        )
    except IsolationUnavailableError as exc:
        logger.warning("Verification command not run: isolation unavailable (%s)", exc.reason)
        return CmdResult(cmd=cmd, rejected_reason=NO_ISOLATED_ENVIRONMENT)
    plan = prepared.plan
    try:
        proc = await spawn_exec(
            *plan.argv,
            new_process_group=True,
            cwd=str(plan.cwd),
            env=plan.env,
            pass_fds=plan.pass_fds,
            stdin=asyncio.subprocess.DEVNULL,  # 不繼承 proxy 的 stdin（TTY 時可 TIOCSTI）
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        prepared.report()  # 沒啟動：狀態 fd 的讀端照樣關掉
        return CmdResult(cmd=cmd, rejected_reason=f"command not runnable: {exc}")
    finally:
        plan.release()  # spawn 成功或失敗都關掉 runner 為這次開的 fd

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await reap_process_tree(proc, terminate_process_tree)  # 執行緒裡殺＋有界回收
        return CmdResult(cmd=cmd, rejected_reason=f"timeout after {timeout_seconds}s")
    except asyncio.CancelledError:
        await reap_cancelled_process(proc, terminate_process_tree)
        raise
    finally:
        prepared.report()  # 跑完（含逾時、取消）讀狀態 fd：只認啟動器寫的代碼

    output = (stdout or b"").decode("utf-8", errors="replace").strip()
    snippet = output[:_OUTPUT_SNIPPET_LIMIT]
    returncode = proc.returncode

    if returncode == 0 and _ZERO_COLLECTION_RE.search(output):
        # 表面成功、實際零測試＝橡皮圖章（pytest exit 5 已非 0，此擋 exit 0 變體）。
        return CmdResult(
            cmd=cmd,
            exit_code=returncode,
            output_snippet=snippet,
            rejected_reason="zero tests collected (rubber-stamp verification)",
        )

    return CmdResult(cmd=cmd, exit_code=returncode, output_snippet=snippet)


async def collect_evidence(
    *,
    artifact_refs: list[str],
    verification_cmd: str | None,
    test_log: str | None,
    artifact_root: Path,
    command_gate: CommandGate,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    identity: AgentIdentity | None = None,
    project_root: Path | None = None,
    consent: ConsentTaker | None = None,
) -> EvidenceBundle:
    """對一個 task 做全部客觀蒐證，回 EvidenceBundle（非阻塞、確定性）.

    ``command_gate`` 必填：呼叫端必須明示 verification_cmd 的執行政策。沒有 ``identity``
    時隔離層級當 none、沒有同意（真的 runner broker 一律拒絕執行）。
    """
    cmd_results: list[CmdResult] = []
    if verification_cmd and verification_cmd.strip():
        cmd_results.append(
            await _run_cmd(
                verification_cmd,
                artifact_root=artifact_root,
                timeout_seconds=timeout_seconds,
                command_gate=command_gate,
                identity=identity,
                project_root=project_root,
                consent=consent,
            )
        )

    artifacts_present = {
        ref: (artifact_root / ref).is_file() for ref in (artifact_refs or [])
    }

    excerpt = (test_log or "").strip()[:_TEST_LOG_EXCERPT_LIMIT] or None
    return EvidenceBundle(
        cmd_results=cmd_results,
        artifacts_present=artifacts_present,
        test_log_excerpt=excerpt,
    )


def bundle_digest(bundle: EvidenceBundle) -> str:
    """證據包的緊湊摘要（落盤到 task.evidence_digest 的可審計快照）。"""
    parts: list[str] = []
    for r in bundle.cmd_results:
        if r.rejected_reason:
            parts.append(f"cmd `{r.cmd}` REJECTED（{r.rejected_reason}）")
        else:
            parts.append(f"cmd `{r.cmd}` exit {r.exit_code}")
    for ref, present in bundle.artifacts_present.items():
        parts.append(f"{ref}{'✓' if present else '✗'}")
    if bundle.test_log_excerpt:
        parts.append(f"test_log:{'✓' if has_passing_assertions(bundle.test_log_excerpt) else '✗'}")
    return "; ".join(parts)[:600] or "(no evidence)"
