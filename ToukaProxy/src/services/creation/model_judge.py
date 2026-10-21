"""ADR-0069 Phase D — ModelJudge：把 Phase B 的 Judge 接縫接上真實 LLM（預設關閉）。

實作 ``evaluation.Judge`` 協定：一次 ``complete_once``（purpose=``judge``，ADR-0244）請 LLM 對
「評測任務 vs 產物」打 0–1 分；記帳到建立者身分（拿不到時記到 System 帳）並受成本閘約束。
**fail-safe**（非成功／解析失敗／例外回 0.0；EVAL 本就非阻斷，計分失敗不擋 commit）。
僅在 ``TOUKA_EVAL_JUDGE_ENABLED`` 開啟**且** ``TOUKA_EVAL_JUDGE_MODEL`` 明確指定時於 main.py
注入，且呼叫端 ``eval_scoring=True`` 才會實際呼叫（雙閘、預設零成本）；沒有寫死的評分模型
預設，啟用卻沒設模型時不啟用。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from src.models.identity import AgentIdentity

if TYPE_CHECKING:
    from src.services.completion import Completer
    from src.services.creation.models import EvalTask

logger = logging.getLogger(__name__)

# 解析 0.0–1.0 的浮點（拒負號；只認 0、0.x、1、1.0…）。
_SCORE_RE = re.compile(r"(?<![-0-9.])(0(?:\.\d+)?|1(?:\.0+)?)(?![0-9])")
_MAX_TOKENS = 16
_TIMEOUT_S = 30.0
_SYSTEM_ACCOUNT = AgentIdentity(agent_id="System")  # 拿不到建立者時的記帳身分（無角色）
_ENABLED_VALUES = frozenset({"1", "true", "yes"})
_ARTIFACT_LIMIT = 4000
_SYSTEM = (
    "You are a strict evaluation judge. Given read-only evaluation tasks and a generated "
    "artifact, reply with ONLY a single float in [0.0, 1.0] scoring how well the artifact "
    "satisfies the tasks. No words, no explanation — just the number."
)


def resolve_judge_model(enabled: str | None, model: str | None) -> tuple[bool, str | None]:
    """純函式：由兩個環境值決定評分設定，回傳（是否要求啟用, 可用的評分模型）。

    只有啟用旗標為真且明確指定模型時才回模型名；空白或未設定的模型視為沒設（回 None）。
    環境變數的讀取留在 main.py（禁令 42），本函式不讀 env、不記日誌。
    """
    requested = (enabled or "").lower() in _ENABLED_VALUES
    chosen = (model or "").strip()
    return requested, (chosen if requested and chosen else None)


class ModelJudge:
    """以單次 LLM 呼叫對創建產物的評測任務計分（實作 Judge 協定）。"""

    def __init__(self, completer: Completer, model: str) -> None:
        """初始化模型評分器，注入一次性呼叫（生產為 complete_once）與使用的模型。"""
        self._completer = completer
        self._model = model

    async def score(
        self, tasks: list[EvalTask], artifact_md: str, identity: AgentIdentity | None = None
    ) -> float:
        """回傳 0.0–1.0 分數；任何失敗 fail-safe 回 0.0（不阻斷 commit）。"""
        try:
            task_lines = "\n".join(f"- {t.title}: {t.expectation}" for t in tasks)
            prompt = (
                f"# Tasks\n{task_lines}\n\n"
                f"# Artifact\n{artifact_md[:_ARTIFACT_LIMIT]}\n\nScore (0.0-1.0):"
            )
            result = await self._completer(
                identity or _SYSTEM_ACCOUNT, "judge", self._model, _SYSTEM, prompt,
                _MAX_TOKENS, _TIMEOUT_S,
            )
            if not result.ok:
                logger.warning("ModelJudge 呼叫未成功（%s），回 0.0", result.error.reason)
                return 0.0
            match = _SCORE_RE.search(result.text)
            if not match:
                logger.warning("ModelJudge：無法解析分數，回 0.0（原文：%r）", result.text)
                return 0.0
            return max(0.0, min(1.0, float(match.group(1))))
        except Exception as e:
            logger.warning("ModelJudge 計分失敗，fail-safe 回 0.0：%s", e)
            return 0.0
