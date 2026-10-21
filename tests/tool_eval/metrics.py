"""Pure scoring for tool-call eval cases — no I/O, no network, deterministic.

Given a case and the tool calls that were observed (from replay or a live run),
produce a ``CaseResult``; ``aggregate`` rolls a batch up into a ``Report``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .cases import EvalCase, ToolCall


# 探索中性家族：多步規劃的「先看再做」不記 wrong（read-family 統一動詞）。
# selection 仍須在回合上限內呼叫到 expected 工具——偷懶只探索不給分。
EXPLORATION_NEUTRAL_TOOLS = frozenset({"read", "list", "find", "grep"})


@dataclass(frozen=True)
class CaseResult:
    """Per-case scoring outcome."""

    case_id: str
    total_calls: int
    evaluated_selection: bool
    selection_ok: bool
    wrong_tool_calls: int
    hallucinated_calls: int
    invalid_param_calls: int
    error_calls: int
    denial_ok: bool | None  # None when the case is not a policy-denial case
    turns: int = 1
    turns_to_success: int | None = None  # 第幾回合首次呼叫到 expected（1-based）
    abstention_ok: bool | None = None  # relevance：expect_no_tool 時是否克制（零呼叫）


def score_case(case: EvalCase, observed: list[ToolCall], known: frozenset[str]) -> CaseResult:
    """單回合計分（向後相容入口）＝一回合的 episode。"""
    return score_episode(case, [observed], known)


def score_episode(
    case: EvalCase,
    episode_turns: list[list[ToolCall]],
    known: frozenset[str],
) -> CaseResult:
    """Score one case against a multi-turn episode of observed tool calls.

    ``known`` is the set of REAL tool names (builtin + MCP). A call whose name is
    not in ``known`` counts as a hallucination; a real-but-unexpected tool counts
    as a wrong tool — EXCEPT the exploration-neutral read family
    (:data:`EXPLORATION_NEUTRAL_TOOLS`)：多步規劃的「先看再做」不是選錯，但
    selection 仍要求在回合內達標（``turns_to_success`` 記錄第幾回合達成）。

    ``expected_tools`` is an ANY-OF set（合法替代路徑）：呼叫到其中任何一個即
    selection ok、且都不算 wrong——例：讀 soul 走 update_my_soul 或
    vault_tools(vault=soul) 皆為設計上的合法路徑（soul 讀統一）。
    """
    observed = [call for turn in episode_turns for call in turn]
    name_set = {c.name for c in observed}
    expected = set(case.expected_tools)
    forbidden = set(case.forbidden_tools)

    evaluated_selection = bool(expected)
    selection_ok = bool(expected & name_set) and not (forbidden & name_set)

    turns_to_success: int | None = None
    if expected:
        for idx, turn in enumerate(episode_turns, start=1):
            if any(c.name in expected for c in turn):
                turns_to_success = idx
                break

    wrong = halluc = invalid = errors = 0
    for call in observed:
        if call.name not in known:
            halluc += 1
        elif (
            expected
            and call.name not in expected
            and call.name not in EXPLORATION_NEUTRAL_TOOLS
        ):
            wrong += 1
        required = case.required_args.get(call.name, [])
        if any(arg not in call.args for arg in required):
            invalid += 1
        if call.status == "error":
            errors += 1

    denial_ok: bool | None = None
    if case.policy_denied:
        denial_ok = all(c.status != "ok" for c in observed if c.name in forbidden)

    # BFCL relevance：expect_no_tool 案，正解＝零呼叫（任何呼叫＝過度調用）。
    abstention_ok: bool | None = None
    if case.expect_no_tool:
        abstention_ok = len(observed) == 0

    return CaseResult(
        case_id=case.id,
        total_calls=len(observed),
        evaluated_selection=evaluated_selection,
        selection_ok=selection_ok,
        wrong_tool_calls=wrong,
        hallucinated_calls=halluc,
        invalid_param_calls=invalid,
        error_calls=errors,
        denial_ok=denial_ok,
        turns=len(episode_turns),
        turns_to_success=turns_to_success,
        abstention_ok=abstention_ok,
    )


@dataclass(frozen=True)
class Report:
    """Aggregate metrics across a batch of cases (rates are fractions 0..1)."""

    n_cases: int
    total_calls: int
    tool_selection_accuracy: float
    wrong_tool_rate: float
    invalid_param_rate: float
    hallucinated_tool_rate: float
    tool_error_rate: float
    avg_tool_calls_per_case: float
    policy_denial_accuracy: float | None
    per_case: tuple[CaseResult, ...]
    avg_turns_to_success: float | None = None  # 多回合：達標案例的平均回合數
    reliability: dict[int, float] | None = None  # pass^k（多樣本模式；None＝單樣本）
    abstention_accuracy: float | None = None  # relevance：expect_no_tool 案的克制率


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


# --------------------------------------------------------------------------- #
# 可靠度：pass^k（τ-bench 式）——跨 k 次試驗「全部成功」的機率
# --------------------------------------------------------------------------- #


def pass_hat_k(successes: int, trials: int) -> dict[int, float]:
    """單案例 pass^k：跑 ``trials`` 次、``successes`` 次成功時，隨機取 k 次全成功的機率.

    ＝ C(c,k)/C(n,k)（k>c 時分子為 0）。回傳 {1..trials: 機率}。pass^1＝平均成功率、
    pass^n＝該案例 n 次全成功才算（最嚴格）。純函式、可決定性。
    """
    n = max(1, trials)
    c = max(0, min(successes, n))
    return {k: comb(c, k) / comb(n, k) for k in range(1, n + 1)}


def mean_pass_hat_k(case_successes: list[int], trials: int) -> dict[int, float]:
    """聚合 pass^k：各案例（同 ``trials`` 次）pass^k 於每個 k 取平均。空清單→{}。"""
    if not case_successes:
        return {}
    n = max(1, trials)
    per_case = [pass_hat_k(c, n) for c in case_successes]
    return {k: sum(p[k] for p in per_case) / len(per_case) for k in range(1, n + 1)}


def aggregate(
    results: Iterable[CaseResult],
    *,
    reliability: dict[int, float] | None = None,
) -> Report:
    """Roll per-case results up into a single Report（``reliability``＝選填 pass^k）。"""
    items = list(results)
    total = sum(r.total_calls for r in items)

    selected = [r for r in items if r.evaluated_selection]
    selection_accuracy = _rate(sum(1 for r in selected if r.selection_ok), len(selected))

    denials = [r for r in items if r.denial_ok is not None]
    denial_accuracy = (
        _rate(sum(1 for r in denials if r.denial_ok), len(denials)) if denials else None
    )

    successes = [r.turns_to_success for r in items if r.turns_to_success is not None]
    avg_turns = (sum(successes) / len(successes)) if successes else None

    abstentions = [r for r in items if r.abstention_ok is not None]
    abstention_accuracy = (
        _rate(sum(1 for r in abstentions if r.abstention_ok), len(abstentions))
        if abstentions
        else None
    )

    return Report(
        n_cases=len(items),
        total_calls=total,
        tool_selection_accuracy=selection_accuracy,
        wrong_tool_rate=_rate(sum(r.wrong_tool_calls for r in items), total),
        invalid_param_rate=_rate(sum(r.invalid_param_calls for r in items), total),
        hallucinated_tool_rate=_rate(sum(r.hallucinated_calls for r in items), total),
        tool_error_rate=_rate(sum(r.error_calls for r in items), total),
        avg_tool_calls_per_case=_rate(total, len(items)),
        policy_denial_accuracy=denial_accuracy,
        per_case=tuple(items),
        avg_turns_to_success=avg_turns,
        reliability=reliability,
        abstention_accuracy=abstention_accuracy,
    )


def format_report(report: Report) -> str:
    """Render a Report as a short human-readable block (percentages)."""
    lines = [
        f"cases={report.n_cases} tool_calls={report.total_calls}",
        f"tool_selection_accuracy = {report.tool_selection_accuracy:.1%}",
        f"wrong_tool_rate         = {report.wrong_tool_rate:.1%}",
        f"invalid_param_rate      = {report.invalid_param_rate:.1%}",
        f"hallucinated_tool_rate  = {report.hallucinated_tool_rate:.1%}",
        f"tool_error_rate         = {report.tool_error_rate:.1%}",
        f"avg_tool_calls_per_case = {report.avg_tool_calls_per_case:.2f}",
    ]
    if report.policy_denial_accuracy is not None:
        lines.append(f"policy_denial_accuracy  = {report.policy_denial_accuracy:.1%}")
    if report.abstention_accuracy is not None:
        lines.append(f"abstention_accuracy     = {report.abstention_accuracy:.1%}")
    if report.avg_turns_to_success is not None:
        lines.append(f"avg_turns_to_success    = {report.avg_turns_to_success:.2f}")
    if report.reliability:
        parts = "  ".join(f"pass^{k}={v:.2f}" for k, v in sorted(report.reliability.items()))
        lines.append(f"reliability             = {parts}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Phase 3 前置（ADR-0136）：episode 級互審 gate 指標
# --------------------------------------------------------------------------- #

# 軸警告（互審設計文件 §6.2-7）：本檔既有指標量的是「工具層」——
# hallucinated_tool_rate 只測『工具名捏造』、selection_ok 只測『選對工具』，
# 與互審要降的 **artifact 內容／宣稱型幻覺**不同軸，不可作為降幻覺主指標；
# 該軸請用下方 gate_recall／gate_precision／false_block_rate（episode 級、
# 以 EvalCase.injected_failure 為標籤、配非 LLM oracle 裁決 ground truth）。
HALLUCINATION_AXIS_NOTE = (
    "hallucinated_tool_rate 只測工具名捏造、selection_ok 只測工具選擇——"
    "皆與 artifact 內容／宣稱型幻覺不同軸，不可作為互審降幻覺主指標；"
    "請用 gate_recall／gate_precision／false_block_rate（episode 級＋非 LLM oracle）。"
)


@dataclass(frozen=True)
class GateOutcome:
    """一個 episode 的互審 gate 結局：注錯標籤 × 是否被擋。"""

    case_id: str
    injected_failure: str  # "none" 或注錯種類（goal_drift/fabrication/unverified_claim）
    blocked: bool  # 互審 gate 是否擋下（review 未過＝True）


@dataclass(frozen=True)
class GateReport:
    """gate 指標聚合（分母為零的比率＝None，誠實呈現不可算）。"""

    total: int
    injected: int
    blocked: int
    gate_recall: float | None
    gate_precision: float | None
    false_block_rate: float | None


def gate_recall(outcomes: list[GateOutcome]) -> float | None:
    """有注錯的 episode 中被 gate 擋下的比例（抓壞率）；無注錯案→None。"""
    injected = [o for o in outcomes if o.injected_failure != "none"]
    if not injected:
        return None
    return _rate(sum(1 for o in injected if o.blocked), len(injected))


def gate_precision(outcomes: list[GateOutcome]) -> float | None:
    """被擋下的 episode 中真的有注錯的比例（擋得準率）；無被擋案→None。"""
    blocked = [o for o in outcomes if o.blocked]
    if not blocked:
        return None
    return _rate(sum(1 for o in blocked if o.injected_failure != "none"), len(blocked))


def false_block_rate(outcomes: list[GateOutcome]) -> float | None:
    """乾淨（無注錯）episode 中被誤擋的比例（誤殺率）；無乾淨案→None。"""
    clean = [o for o in outcomes if o.injected_failure == "none"]
    if not clean:
        return None
    return _rate(sum(1 for o in clean if o.blocked), len(clean))


def aggregate_gate(outcomes: list[GateOutcome]) -> GateReport:
    """聚合 gate 結局為 GateReport（純函式、確定性）。"""
    return GateReport(
        total=len(outcomes),
        injected=sum(1 for o in outcomes if o.injected_failure != "none"),
        blocked=sum(1 for o in outcomes if o.blocked),
        gate_recall=gate_recall(outcomes),
        gate_precision=gate_precision(outcomes),
        false_block_rate=false_block_rate(outcomes),
    )


def _fmt_rate(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def format_gate_report(report: GateReport) -> str:
    """Render a GateReport as a short human-readable block."""
    return "\n".join(
        [
            f"episodes={report.total} injected={report.injected} blocked={report.blocked}",
            f"gate_recall      = {_fmt_rate(report.gate_recall)}",
            f"gate_precision   = {_fmt_rate(report.gate_precision)}",
            f"false_block_rate = {_fmt_rate(report.false_block_rate)}",
        ]
    )
