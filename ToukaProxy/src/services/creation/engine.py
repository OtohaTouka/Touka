"""ADR-0069 CreationEngine — 創建流水線編排（VALIDATE → SMOKE → COMMIT →（後置）EVAL）。

引擎只負責編排與關卡控制（SRP）；產物類型行為由注入的 ``CreationKind`` 提供
（DIP / OCP）。新增產物類型只需新增 adapter 並註冊，引擎不變。EVAL 為 commit 後的
非阻斷階段（產生回歸基線，委派 ``eval_runner``），不影響 ``committed``。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.core.exceptions import ErrorCode, ToukaException
from src.services.creation.evaluation import eval_runner
from src.services.creation.mcp_kind import McpCreationKind
from src.services.creation.models import CreationResult
from src.services.creation.skill_kind import SkillCreationKind

if TYPE_CHECKING:
    from src.models.identity import AgentIdentity
    from src.services.creation.kinds import CreationKind, CreationLaunchContext
    from src.services.creation.models import CreationSpec

logger = logging.getLogger(__name__)


class CreationEngine:
    """以固定流水線創建產物；各類型行為委派 ``CreationKind`` 策略。"""

    def __init__(self, kinds: dict[str, CreationKind]) -> None:
        """初始化創建引擎，注入各種類型的創建策略。"""
        self._kinds = kinds

    async def create(
        self,
        kind: str,
        spec: CreationSpec,
        *,
        run_smoke: bool = True,
        run_eval: bool = True,
        eval_scoring: bool = False,
        identity: AgentIdentity | None = None,
        context: CreationLaunchContext | None = None,
    ) -> CreationResult:
        """執行 VALIDATE →（SMOKE，gate）→ COMMIT →（後置 EVAL，非阻斷）。

        任一前置關卡失敗即停、不寫入。EVAL 於 commit 後執行（``run_eval`` 控制），
        只產生回歸基線（＋選用計分，``eval_scoring`` 預設關閉），**不影響** ``committed``。
        ``identity``＝建立者，計分時記帳用（None＝系統帳）。
        """
        adapter = self._kinds.get(kind)
        if adapter is None:
            logger.warning("Unknown creation kind: %s", kind)
            raise ToukaException(ErrorCode.VALIDATION_ERROR)

        report = adapter.validate(spec)
        if not report.ok:
            return CreationResult(validation=report, committed=False)

        smoke_report = None
        if run_smoke:
            smoke_report = await adapter.smoke(spec, context)
            if not smoke_report.ok:
                return CreationResult(
                    validation=report, smoke=smoke_report, committed=False
                )

        result = adapter.commit(spec, context)
        logger.info("Created %s artifact: %s", kind, result.id)

        eval_report = None
        if run_eval:
            eval_report = await eval_runner.run(
                kind, adapter, spec, result, scoring=eval_scoring, identity=identity,
                context=context,
            )

        return CreationResult(
            validation=report,
            smoke=smoke_report,
            commit=result,
            committed=True,
            evaluation=eval_report,
        )


# 模組級單例（與 skill_service / 其他服務一致）。
creation_engine = CreationEngine(
    {
        "skill": SkillCreationKind(),
        "mcp": McpCreationKind(),
    }
)
