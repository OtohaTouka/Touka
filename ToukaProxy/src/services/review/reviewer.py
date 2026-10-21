"""Reviewer 介面與實作（互審設計文件 §4.1，ADR-0137）.

泛化 ``creation/evaluation.Judge`` 的 DIP 精神（``Judge``／``NoopJudge`` 原地
不動、繼續供 creation EVAL）：reviewer 回**結構化 critique**（verdict＋
required_fixes＋grounded＋confidence），且方向與 ModelJudge 相反——

**fail-closed**：這是 gate 不是計分。解析失敗／verdict 缺失／APPROVE 卻附
blocking fixes（矛盾回報）／APPROVE 但低 confidence／completer 例外——
一律映射 REJECT：審查器壞掉時寧可擋住不放行。

``GroundedReviewer`` 的 LLM 傳輸以 ``completer``（async ``(system, user) -> str``）
注入——單元測試零網路；生產接線由 review_loop 提供（一次呼叫、非子 ReAct loop）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# APPROVE 的最低自評把握度：低於此值的放行主張不可信，fail-closed 轉 REJECT。
_MIN_APPROVE_CONFIDENCE = 0.5
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)
_EVIDENCE_SNIPPET_LIMIT = 2000

_SYSTEM_PROMPT = (
    "你是對抗式紅隊審查員（critic）。你的職責是**挑錯，不是附和**：預設產出有問題，"
    "盡力找出 producer 沒滿足 goal、違反 boundary、或宣稱做了但證據不足之處。\n"
    "裁決鐵則：以 evidence（客觀蒐證：cmd exit code／artifact 存在性／test_log）為準，"
    "**忽略 producer 的自我宣稱**——宣稱不是事實。\n"
    "回覆最後必須附一個 ```json 塊：\n"
    "```json\n"
    '{"verdict": "APPROVE|REJECT|ABSTAIN", "reasoning": "...", '
    '"required_fixes": [{"issue": "...", "evidence_anchor": "對應的證據（檔案/輸出）", '
    '"severity": "blocking|advisory"}], "confidence": 0.0}\n'
    "```\n"
    "找不到問題才可 APPROVE；無法判斷用 ABSTAIN；blocking fix 必須附 evidence_anchor。"
)


# --------------------------------------------------------------------------- #
# 資料模型
# --------------------------------------------------------------------------- #


class RequiredFix(BaseModel):
    """一條需修正項：問題＋證據錨（blocking 必附）＋嚴重度。"""

    model_config = ConfigDict(frozen=True)

    issue: str
    evidence_anchor: str | None = None
    severity: Literal["blocking", "advisory"] = "blocking"


class CmdResult(BaseModel):
    """一條 verification_cmd 的客觀執行結果（EvidenceCollector 產）。"""

    model_config = ConfigDict(frozen=True)

    cmd: str
    exit_code: int | None = None
    output_snippet: str = ""
    rejected_reason: str | None = None  # no-op／不可執行等硬拒理由

    @property
    def ok(self) -> bool:
        """硬檢查單項：真的跑了且 exit 0。"""
        return self.rejected_reason is None and self.exit_code == 0


class EvidenceBundle(BaseModel):
    """客觀證據包（純資料；critic 無法以幻覺竄改）。"""

    model_config = ConfigDict(frozen=True)

    cmd_results: list[CmdResult] = []
    artifacts_present: dict[str, bool] = {}
    test_log_excerpt: str | None = None

    @property
    def hard_checks_pass(self) -> bool:
        """證據層硬檢查：全部 cmd exit 0 且全部宣稱 artifact 存在（空＝真空真）。

        E75（ADR-0137 反橡皮圖章第 3 件接入 gate）：test_log 非空時另須含可數的
        通過斷言（「N passed」且 N>0）——producer 貼任意文字充當測試證據不再過硬檢。
        test_log 缺席仍走原語意（軟任務由 has_evidence 分流，真空真不變）。
        """
        if self.test_log_excerpt:
            # evidence 模組頂層 import 本類（CmdResult/EvidenceBundle）——函式內延遲載入破循環
            from src.services.review import evidence as _evidence  # noqa: LOCAL_IMPORT_BAN

            if not _evidence.has_passing_assertions(self.test_log_excerpt):
                return False
        return all(r.ok for r in self.cmd_results) and all(
            self.artifacts_present.values()
        )

    @property
    def has_evidence(self) -> bool:
        """是否存在任何可執行證據（軟任務分流依據：無證據→human-ack 路徑）。"""
        return bool(self.cmd_results) or bool(self.artifacts_present)


class CritiqueReport(BaseModel):
    """審查結論（結構化、可落盤、可程式化裁決）。"""

    model_config = ConfigDict(frozen=True)

    verdict: Literal["APPROVE", "REJECT", "ABSTAIN"]
    reasoning: str = ""
    required_fixes: list[RequiredFix] = []
    confidence: float = 0.0
    grounded: bool = False  # 所有 blocking fix 皆有 evidence_anchor
    reviewer_id: str = ""
    critic_model: str = ""


class Reviewer(Protocol):
    """互審審查者協定（DIP；GroundedReviewer／NoopReviewer 皆實作）。"""

    async def review(
        self,
        *,
        goal: str,
        boundary_rules: list[str],
        producer_claims: str,
        evidence: EvidenceBundle,
    ) -> CritiqueReport:
        """審一次產出，回結構化 CritiqueReport。"""
        ...


# --------------------------------------------------------------------------- #
# 解析（fail-closed 核心）
# --------------------------------------------------------------------------- #


def _fail_closed(reason: str, *, reviewer_id: str, critic_model: str) -> CritiqueReport:
    return CritiqueReport(
        verdict="REJECT",
        reasoning=f"fail-closed: {reason}",
        reviewer_id=reviewer_id,
        critic_model=critic_model,
    )


def _coerce_fixes(raw: Any) -> list[RequiredFix]:
    fixes: list[RequiredFix] = []
    if not isinstance(raw, list):
        return fixes
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("issue", "")).strip():
            continue
        severity = item.get("severity")
        anchor = item.get("evidence_anchor")
        fixes.append(
            RequiredFix(
                issue=str(item["issue"]),
                evidence_anchor=str(anchor) if isinstance(anchor, str) and anchor else None,
                severity=severity if severity in ("blocking", "advisory") else "blocking",
            )
        )
    return fixes


def parse_critique(text: str, *, reviewer_id: str, critic_model: str) -> CritiqueReport:
    """Critic 輸出 → CritiqueReport；任何不可信形態一律 fail-closed REJECT."""
    matches = list(_JSON_BLOCK_RE.finditer(text or ""))
    if not matches:
        return _fail_closed(
            "critic output has no json block", reviewer_id=reviewer_id, critic_model=critic_model
        )
    try:
        data = json.loads(matches[-1].group(1))
    except (json.JSONDecodeError, ValueError):
        return _fail_closed(
            "critic json unparseable", reviewer_id=reviewer_id, critic_model=critic_model
        )
    if not isinstance(data, dict):
        return _fail_closed(
            "critic json is not an object", reviewer_id=reviewer_id, critic_model=critic_model
        )

    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in ("APPROVE", "REJECT", "ABSTAIN"):
        return _fail_closed(
            "verdict missing or invalid", reviewer_id=reviewer_id, critic_model=critic_model
        )

    fixes = _coerce_fixes(data.get("required_fixes"))
    blocking = [f for f in fixes if f.severity == "blocking"]
    grounded = all(f.evidence_anchor for f in blocking)

    raw_conf = data.get("confidence")
    confidence = (
        min(1.0, max(0.0, float(raw_conf)))
        if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool)
        else 0.0
    )

    reasoning = str(data.get("reasoning", ""))

    if verdict == "APPROVE" and blocking:
        # 矛盾回報：說放行卻列了 blocking 問題 → 不可信，fail-closed。
        return CritiqueReport(
            verdict="REJECT",
            reasoning=f"fail-closed: APPROVE with blocking fixes (contradiction). {reasoning}",
            required_fixes=fixes,
            confidence=confidence,
            grounded=grounded,
            reviewer_id=reviewer_id,
            critic_model=critic_model,
        )
    if verdict == "APPROVE" and confidence < _MIN_APPROVE_CONFIDENCE:
        return CritiqueReport(
            verdict="REJECT",
            reasoning=(
                f"fail-closed: APPROVE below confidence floor "
                f"({confidence:.2f} < {_MIN_APPROVE_CONFIDENCE}). {reasoning}"
            ),
            required_fixes=fixes,
            confidence=confidence,
            grounded=grounded,
            reviewer_id=reviewer_id,
            critic_model=critic_model,
        )

    return CritiqueReport(
        verdict=verdict,  # type: ignore[arg-type]
        reasoning=reasoning,
        required_fixes=fixes,
        confidence=confidence,
        grounded=grounded,
        reviewer_id=reviewer_id,
        critic_model=critic_model,
    )


# --------------------------------------------------------------------------- #
# 實作
# --------------------------------------------------------------------------- #


class NoopReviewer:
    """哨兵：``review_required=False`` 時注入＝不審直接放行（保留漸進開關）。"""

    async def review(
        self,
        *,
        goal: str,  # noqa: ARG002 — Protocol 介面對稱
        boundary_rules: list[str],  # noqa: ARG002
        producer_claims: str,  # noqa: ARG002
        evidence: EvidenceBundle,  # noqa: ARG002
    ) -> CritiqueReport:
        """回無條件 APPROVE（等同不設 gate；審計欄位標記 noop）。"""
        return CritiqueReport(
            verdict="APPROVE",
            reasoning="review not required (noop sentinel)",
            confidence=1.0,
            grounded=True,
            reviewer_id="noop",
            critic_model="",
        )


def _render_evidence(evidence: EvidenceBundle) -> str:
    lines = ["## Evidence（客觀蒐證，非 producer 自述）"]
    for r in evidence.cmd_results:
        if r.rejected_reason:
            lines.append(f"- cmd `{r.cmd}` → REJECTED（{r.rejected_reason}）")
        else:
            snippet = r.output_snippet[:300]
            lines.append(f"- cmd `{r.cmd}` → exit {r.exit_code}；輸出節錄：{snippet}")
    for ref, present in evidence.artifacts_present.items():
        lines.append(f"- artifact `{ref}` → {'存在' if present else '不存在'}")
    if evidence.test_log_excerpt:
        lines.append(f"- test_log 節錄：{evidence.test_log_excerpt[:300]}")
    if len(lines) == 1:
        lines.append("- （無可執行證據）")
    return "\n".join(lines)


class GroundedReviewer:
    """證據接地紅隊審查者：一次 completer 呼叫＋fail-closed 解析（非子 ReAct loop）。"""

    def __init__(
        self,
        completer: Callable[[str, str], Awaitable[str]],
        *,
        critic_model: str,
        reviewer_id: str,
    ) -> None:
        """注入 LLM 傳輸（completer）、critic 模型標記與審查者身分標記。"""
        self._completer = completer
        self._critic_model = critic_model
        self._reviewer_id = reviewer_id

    async def review(
        self,
        *,
        goal: str,
        boundary_rules: list[str],
        producer_claims: str,
        evidence: EvidenceBundle,
    ) -> CritiqueReport:
        """組 context 隔離的審查提示（不含 producer CoT）→ 一次呼叫 → 解析."""
        boundary_lines = "\n".join(f"- {rule}" for rule in boundary_rules) or "-（無）"
        user_prompt = (
            f"# 任務目標（goal）\n{goal}\n\n"
            f"# 邊界規則（boundary_rules）\n{boundary_lines}\n\n"
            f"# Producer 宣稱（claims——僅供對照，不可當事實）\n"
            f"{producer_claims[:_EVIDENCE_SNIPPET_LIMIT] or '（無）'}\n\n"
            f"{_render_evidence(evidence)}\n\n"
            "請對抗式審查：宣稱是否被證據支持？goal 是否真的達成？boundary 是否被違反？"
        )
        try:
            text = await self._completer(_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:  # noqa: BLE001 — gate 語義：審查傳輸失敗必須擋住而非放行
            logger.exception("GroundedReviewer completer failed (fail-closed REJECT)")
            return _fail_closed(
                f"completer error: {type(exc).__name__}",
                reviewer_id=self._reviewer_id,
                critic_model=self._critic_model,
            )
        return parse_critique(
            text, reviewer_id=self._reviewer_id, critic_model=self._critic_model
        )
