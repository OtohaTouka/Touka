"""ADR-0069 Phase B — EVAL 基線檔（``<id>.eval.md``）序列化／解析。

純函式模組、零 IO（與 ``skill_parser`` 同設計：文字 ↔ 結構）。YAML frontmatter 為
機器可讀的單一事實（含完整 tasks，供無損 round-trip 回歸比對）；body 為人類可讀的
``## Task`` 區塊（僅呈現、不參與解析）。**不寫時間戳** → 內容決定性、可 diff。
"""
from __future__ import annotations

import logging

import yaml

from src.services import skill_parser
from src.services.creation.models import EvalReport, EvalTask

logger = logging.getLogger(__name__)

_FENCE = "---"


def serialize_eval_md(report: EvalReport) -> str:
    """將 ``EvalReport`` 組裝為 ``<id>.eval.md`` 文字（frontmatter ＋ 人類可讀 body）。"""
    header = {
        "id": report.id,
        "kind": report.kind,
        "ok": report.ok,
        "scored": report.scored,
        "score": report.score,
        "detail": report.detail,
        "task_count": len(report.tasks),
        "warnings": list(report.warnings),
        "tasks": [t.model_dump() for t in report.tasks],
    }
    yaml_text = yaml.safe_dump(header, allow_unicode=True, sort_keys=False)
    lines = [f"# Eval Baseline — {report.id or report.kind}", ""]
    for t in report.tasks:
        lines.append(f"## Task {t.id} — {t.title}")
        if t.probe:
            lines.append(f"- probe: {t.probe}")
        if t.expectation:
            lines.append(f"- expectation: {t.expectation}")
        lines.append("")
        if t.description:
            lines.append(t.description)
            lines.append("")
    body = "\n".join(lines)
    return f"{_FENCE}\n{yaml_text}{_FENCE}\n{body}"


def parse_eval_md(raw: str) -> EvalReport:
    """將 ``<id>.eval.md`` 文字解析回 ``EvalReport``（容錯：壞任務略過、不整份崩）。"""
    fm_text, _ = skill_parser.split_frontmatter(raw)
    fm = skill_parser.parse_frontmatter(fm_text)
    tasks: list[EvalTask] = []
    for item in fm.get("tasks", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            tasks.append(EvalTask(**item))
        except (TypeError, ValueError):
            logger.warning("Eval task 解析失敗，略過一筆。")
    return EvalReport(
        ok=bool(fm.get("ok", False)),
        kind=str(fm.get("kind", "")),
        id=str(fm.get("id", "")),
        tasks=tasks,
        scored=bool(fm.get("scored", False)),
        score=fm.get("score"),
        detail=str(fm.get("detail", "")),
        warnings=list(fm.get("warnings", []) or []),
    )
