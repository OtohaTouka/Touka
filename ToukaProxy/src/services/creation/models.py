"""ADR-0069 創建引擎資料模型（CreationSpec / 各階段報告）。

純 Pydantic 模型、零 IO。``SkillSpec`` / ``McpSpec`` 為兩種產物的輸入規格；
``ValidationReport`` / ``SmokeReport`` / ``EvalReport`` / ``CommitResult`` /
``CreationResult`` 為流水線各階段輸出；``McpScaffoldSpec`` / ``ScaffoldResult``
為 Phase C scaffold 的輸入／結果。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreationSpec(BaseModel):
    """創建規格基底（供 CreationKind 協定統一型別）。"""


class SkillSpec(CreationSpec):
    """Skill 創建規格（對映 ``SkillService.update_skill`` 的 skill_data）。"""

    id: str
    name: str = ""
    description: str = ""
    category: str = "General"
    author: str = ""
    is_system: bool = False
    system_prompt: str = ""
    required_tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)


class McpSpec(CreationSpec):
    """MCP server 創建規格。"""

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    sdk_major: Literal[2] = 2


class ParamSpec(BaseModel):
    """要生成的工具參數（``name`` 須為合法 Python 識別字；``type`` 限白名單）。"""

    name: str
    type: str = "str"  # str|int|float|bool；未知 → 降級 str（scaffold 層處理）


class ToolSpec(BaseModel):
    """要生成的單一 ``@mcp.tool()`` stub 宣告。"""

    name: str
    description: str = ""
    params: list[ParamSpec] = Field(default_factory=list)


class McpScaffoldSpec(CreationSpec):
    """從零生成 MCPServer 的高階規格（PRE-engine 輸入；ADR-0069 Phase C）。"""

    name: str
    description: str = ""
    tools: list[ToolSpec] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """VALIDATE 階段輸出；``ok=False`` 時引擎不進入 COMMIT。"""

    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SmokeReport(BaseModel):
    """SMOKE 階段輸出；``ok=False`` 時引擎不進入 COMMIT（``warnings`` 不擋）。"""

    ok: bool
    detail: str = ""
    tool_count: int | None = None
    warnings: list[str] = Field(default_factory=list)


class CommitResult(BaseModel):
    """COMMIT 階段輸出。"""

    id: str
    status: str = "created"
    artifact: str | None = None


class EvalTask(BaseModel):
    """單一可驗證的「唯讀」評測任務（回歸基線的最小單位）。"""

    id: str
    title: str
    description: str = ""
    expectation: str = ""
    probe: str = ""  # MCP→工具名；skill→工具/觸發鍵


class EvalReport(BaseModel):
    """EVAL 階段輸出（後置、非阻斷）：產物的回歸基線 ＋ 選用評分。

    ``ok=False`` 僅代表評測基線產生失敗（記 warning），**不**影響已完成的 commit。
    ``score``/``scored`` 為選用 LLM-as-judge 結果；預設關閉（``score=None``）。
    """

    ok: bool
    kind: str = ""
    id: str = ""
    tasks: list[EvalTask] = Field(default_factory=list)
    scored: bool = False
    score: float | None = None
    detail: str = ""
    warnings: list[str] = Field(default_factory=list)
    artifact: str | None = None  # 已存的 <id>.eval.md 路徑


class CreationResult(BaseModel):
    """``CreationEngine.create`` 的整體結果。"""

    validation: ValidationReport
    smoke: SmokeReport | None = None
    commit: CommitResult | None = None
    committed: bool = False
    evaluation: EvalReport | None = None


class ScaffoldResult(BaseModel):
    """``McpScaffolder.scaffold`` 結果；``ok=True`` 時 ``spec`` 可直接餵引擎。"""

    ok: bool
    server_path: str | None = None
    spec: McpSpec | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
