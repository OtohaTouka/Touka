"""計分卡序列化：markdown（人讀）＋ json（機器讀／前端）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.services.scorecard.models import Scorecard


def to_json(card: Scorecard) -> dict[str, Any]:
    """序列化為 json（前端／機器讀）。"""
    return {
        "model": card.model,
        "budget_usd": card.budget_usd,
        "spent_usd": card.spent_usd,
        "passed": card.passed,
        "total": card.total,
        "skipped": card.skipped,
        "total_tokens": card.total_tokens,
        "results": [
            {
                "scenario": r.scenario_id,
                "surface": r.surface,
                "pass": r.structural_pass,
                "skipped": r.skipped,
                "detail": r.detail,
                "tokens": r.tokens,
                "cost_usd": r.cost_usd,
                "latency_ms": r.latency_ms,
                "estimated": r.estimated,
                "tool_trace": [dict(t) for t in r.tool_trace],
                "react_turns": r.react_turns,
                "finish_reason": r.finish_reason,
                "early_stop": r.early_stop,
                "artifact_valid": r.artifact_valid,
                "artifact_detail": r.artifact_detail,
            }
            for r in card.results
        ],
    }


def to_markdown(card: Scorecard) -> str:
    """序列化為 markdown（人讀）。"""
    skip_note = f"（略過 {card.skipped}）" if card.skipped else ""
    lines = [
        f"# 模型計分卡：{card.model}",
        "",
        f"- 結構通過：**{card.passed}/{card.total}**{skip_note}",
        f"- 總 token：{card.total_tokens}",
        f"- 花費：**${card.spent_usd:.6f}** / 預算 ${card.budget_usd:.4f}",
        "",
        "| 情境 | surface | 結構 | tokens | 成本(USD) | 延遲(ms) | 說明 |",
        "|---|---|---|---|---|---|---|",
    ]
    any_estimated = False
    for r in card.results:
        status = "略過" if r.skipped else ("✅" if r.structural_pass else "❌")
        mark = "~" if r.estimated else ""
        any_estimated = any_estimated or r.estimated
        lines.append(
            f"| {r.scenario_id} | {r.surface} | {status} | {mark}{r.tokens} | "
            f"{mark}{r.cost_usd:.6f} | {r.latency_ms} | {r.detail} |"
        )
    if any_estimated:
        lines.append("")
        lines.append("> ～ 為估算值（供應商未回報 usage，依文字長度估算）。")
    return "\n".join(lines)
