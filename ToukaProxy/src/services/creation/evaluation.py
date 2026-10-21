"""ADR-0069 Phase B — EVAL 後置執行器（持久化 + 選用評分；SRP/DIP）。

引擎在 commit 之後呼叫 ``EvalRunner.run``：向 kind 取得評測任務、序列化為
``config/evals/<kind>/<id>.eval.md`` 回歸基線（經 ``resource_manager`` 寫檔），並可選擇性
以注入的 ``Judge`` 計分（預設關閉、不計分）。**整段非阻斷**：任何例外只記 warning、
回 ``ok=False`` 的報告，絕不向引擎拋出（已 commit 的產物不受影響）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from src.config import get_config_dir
from src.services.creation.eval_formatter import serialize_eval_md
from src.services.creation.models import EvalReport
from src.services.resource_manager import resource_manager

if TYPE_CHECKING:
    from pathlib import Path

    from src.models.identity import AgentIdentity
    from src.services.creation.kinds import CreationKind, CreationLaunchContext
    from src.services.creation.models import CommitResult, CreationSpec, EvalTask

logger = logging.getLogger(__name__)


class Judge(Protocol):
    """LLM-as-judge 介面（DIP）。Phase D 注入真實評分器；Phase B 不提供。"""

    async def score(
        self, tasks: list[EvalTask], artifact_md: str, identity: AgentIdentity | None = None
    ) -> float:
        """對評測任務計分（0.0–1.0）；identity＝建立者（記帳用），None＝系統帳。"""
        ...


class NoopJudge:
    """無評分器哨兵：注入它（或保持 ``None``）→ 產生基線但不計分（``score`` 留 None）。"""


def _is_real_judge(judge: Judge | None) -> bool:
    """是否為可實際計分的 judge（排除 ``None`` 與 ``NoopJudge`` 哨兵）。"""
    return judge is not None and not isinstance(judge, NoopJudge)


def _eval_path(kind: str, id_: str) -> Path:
    """Eval 基線檔的 SSOT 路徑：``config/evals/<kind>/<id>.eval.md``（衍生、可重生）。"""
    return get_config_dir() / "evals" / kind / f"{id_}.eval.md"


class EvalRunner:
    """後置 EVAL：產生回歸基線檔 ＋ 選用計分。引擎注入；預設不計分。"""

    def __init__(self, judge: Judge | None = None) -> None:
        """初始化評估執行器，可選擇性注入評分器。"""
        self._judge = judge

    def set_judge(self, judge: Judge | None) -> None:
        """注入／置換評分器（Phase D 啟動時呼叫）；預設仍為 None（不計分）。"""
        self._judge = judge

    async def run(
        self,
        kind: str,
        adapter: CreationKind,
        spec: CreationSpec,
        commit: CommitResult,
        *,
        scoring: bool = False,
        identity: AgentIdentity | None = None,
        context: CreationLaunchContext | None = None,
    ) -> EvalReport:
        """生成評測任務 → 寫 ``<id>.eval.md`` →（選用）計分。非阻斷、絕不拋例外。"""
        try:
            probes = await adapter.probe_eval_inputs(spec, context)
            tasks = adapter.generate_eval_tasks(spec, probes)
            report = EvalReport(
                ok=True,
                kind=kind,
                id=commit.id,
                tasks=tasks,
                detail=f"{len(tasks)} 題唯讀評測基線",
            )
            md = serialize_eval_md(report)
            path = _eval_path(kind, commit.id)
            resource_manager.write_file_text(path, md, make_parents=True)
            report.artifact = str(path)
            if scoring and _is_real_judge(self._judge):
                report.score = await self._judge.score(tasks, md, identity)
                report.scored = True
        except Exception as e:
            logger.warning("EVAL 後置階段失敗（不影響已 commit 的產物）：%s", e)
            return EvalReport(
                ok=False,
                kind=kind,
                id=getattr(commit, "id", ""),
                detail="評測基線產生失敗",
                warnings=[f"EVAL 失敗：{type(e).__name__}: {e}"],
            )
        else:
            return report


# 模組級單例（無 judge → 預設不計分，直到 Phase D 注入 ModelJudge）。
eval_runner = EvalRunner()
