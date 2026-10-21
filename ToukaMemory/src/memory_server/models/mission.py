# ruff: noqa: E501, TRY003, TRY301, TRY004, RUF100
"""任務看板模型 (Mission Board Models).

本模組定義了代理人自主工作的任務模型與狀態追蹤結構。
支援任務樹拆解、相依性管理以及 Artifacts (產出物) 的關聯，
是實現長生命週期任務 (Long-running Missions) 的核心資料模型。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskStatus(StrEnum):
    """任務狀態列舉."""

    TODO = "TODO"
    DOING = "DOING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


# --- Phase 3 互審（ADR-0137，設計文件 §4.2/§4.3）：審查子 FSM 狀態 SSOT ---
# 與 TaskStatus 正交：task.review_status 記審查子狀態，gate 讀它決定 DONE 能否落盤。
REVIEW_STATES = frozenset({
    "EVIDENCE_COLLECTING",
    "REVIEWING",
    "REVISING",
    "APPROVED",
    "APPROVED_LOW_CONFIDENCE",
    "BLOCKED_FOR_HUMAN",
})
REVIEW_PASSING_STATES = frozenset({"APPROVED", "APPROVED_LOW_CONFIDENCE"})


def review_passing(task: Task) -> bool:
    """審查 gate 判定：APPROVED 恆過；LOW_CONFIDENCE 僅 normal 風險過（決策 3 分層）.

    ``review_required=False``（預設）＝不設 gate、恆過（零回歸漸進上線）。
    """
    if not task.review_required:
        return True
    if task.review_status == "APPROVED":
        return True
    return task.review_status == "APPROVED_LOW_CONFIDENCE" and task.review_risk == "normal"


class Task(BaseModel):
    """單一任務項模型.

    Attributes:
        id (str): 任務唯一識別碼。
        description (str): 任務描述。
        status (TaskStatus): 目前執行狀態。
        dependencies (list[str]): 依賴的任務 ID 列表。
        artifact_ref (str | None): 指向產出的檔案路徑或 Artifact URI。
        created_at (float): 建立時間戳 (Unix Epoch)。
        updated_at (float): 最後更新時間戳 (Unix Epoch)。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str
    description: str
    status: TaskStatus = TaskStatus.TODO
    dependencies: list[str] = []
    artifact_ref: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    verification_cmd: str | None = None
    boundary_rules: list[str] = []
    test_log: str | None = None
    blocker_detail: str | None = None
    # --- Phase 3 互審欄位（ADR-0137，設計文件 §4.3；與 TaskStatus 正交）---
    review_required: bool = False  # per-task 開關，預設關＝零回歸；True 時 gate fail-closed
    review_status: str | None = None  # 審查子 FSM 態（REVIEW_STATES）；None＝未進審查流
    critic_verdict: str | None = None  # 最後一次 critique 摘要（人類/主 agent 可讀溯源）
    reviewer_id: str | None = None  # __reviewer_{task}__@{model} 或 human:{user}
    review_attempts_total: int = 0  # 跨生命週期累計往返（BLOCKED→DOING 重進不歸零）
    evidence_digest: str | None = None  # EvidenceBundle 摘要（客觀證據快照，非模型意見）
    review_locked: bool = False  # 達預算上限後永鎖：重進一律走 human-ack
    verification_cmd_locked: bool = False  # init 帶 cmd 即上鎖（producer 不可覆寫考卷）
    review_risk: str = "normal"  # "normal"|"high"；單調只升（高風險⇒異模型 critic 硬前提）
    created_at: float = Field(default_factory=lambda: 0.0)  # 由 Service 注入精確時間
    updated_at: float = Field(default_factory=lambda: 0.0)

    @field_validator("artifact_ref", mode="before")
    @classmethod
    def validate_artifact_ref(cls, v: Any) -> Any:
        """驗證單一產出物路徑，防範路徑穿越與越權存取 (SSOT / TDD)."""
        if v is None:
            return v
        if not isinstance(v, str):
            err_msg = "Artifact reference must be a string"
            raise ValueError(err_msg)
        cls._check_secure_path(v)
        return v

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def validate_artifact_refs(cls, v: Any) -> Any:
        """驗證多產出物列表，防範路徑穿越與越權存取 (SSOT / TDD)."""
        if v is None:
            return []
        if not isinstance(v, list):
            err_msg = "Artifact references must be a list"
            raise ValueError(err_msg)
        for item in v:
            if not isinstance(item, str):
                err_msg = "Artifact references must be strings"
                raise ValueError(err_msg)
            cls._check_secure_path(item)
        return v

    @classmethod
    def _check_secure_path(cls, val: str) -> None:
        """核心路徑安全性校驗。"""
        if not val:
            return
        # 1. 防止點點向上穿越
        if ".." in val:
            err_msg = "Path traversal detected: '..' is not allowed in artifact paths"
            raise ValueError(err_msg)
        # 2. 防止 Windows/Unix 絕對路徑及磁碟機盤符
        if val.startswith("/") or val.startswith("\\") or re.match(r"^[a-zA-Z]:", val):
            err_msg = "Absolute paths are not allowed in artifact paths"
            raise ValueError(err_msg)
        # 3. 確保其為相對路徑且無 escape
        try:
            p = Path(val)
            if p.is_absolute():
                err_msg = "Absolute paths are not allowed in artifact paths"
                raise ValueError(err_msg)
        except Exception as e:
            err_msg = f"Invalid path format: {e}"
            raise ValueError(err_msg) from e


class MissionState(BaseModel):
    """完整任務看板狀態模型.

    一個 Mission 包含多個 Task，代表一個宏觀目標的達成路徑。

    Attributes:
        mission_id (str): 任務總體識別碼。
        goal (str): 宏觀目標描述。
        agent_id (str): 負責執行的代理人 ID。
        status (TaskStatus): 總體進度狀態。
        tasks (list[Task]): 子任務列表。
        metadata (dict[str, Any]): 額外的元數據資訊。
        final_verdict (str | None): mission 級最終驗收結論（Phase 2 finalize 原語；
            全 task DONE 後由主 agent 對照原始 goal 記錄）。
        finalized_at (float | None): 驗收結論記錄時間戳。
        created_at (float): 建立時間戳。
        updated_at (float): 更新時間戳。
    """

    mission_id: str
    goal: str
    agent_id: str  # noqa: IDENTITY_SSOT
    status: TaskStatus = TaskStatus.TODO
    tasks: list[Task] = []
    metadata: dict[str, Any] = {}
    final_verdict: str | None = None
    finalized_at: float | None = None
    created_at: float = Field(default_factory=lambda: 0.0)
    updated_at: float = Field(default_factory=lambda: 0.0)
