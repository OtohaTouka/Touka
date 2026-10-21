"""Native public input contracts, separate from trusted runtime kwargs."""

from __future__ import annotations

from typing import Any, Literal

from memory_server.models.mission import Task
from memory_server.tools.contracts import (
    MEMORY_ACTION_CONTRACTS,
    PublicToolInput,
    action_parameters,
    public_parameters,
)
from pydantic import Field, ValidationError, field_validator
from src.services.tool_errors import VALIDATION_ERROR, build_tool_error
from src.tools.execution_context import EXECUTION_CONTEXT_KWARG_NAMES

MAX_SUBAGENT_GROUP = 8
SUBAGENT_GROUP_CONCURRENCY = 4


class MissionTaskInput(PublicToolInput):
    """Public input model for MissionTask."""

    task_id: str = Field(description="子任務代碼 (如 t1)")
    description: str = Field(description="子任務內容")
    dependency_task_ids: list[str] = Field(default_factory=list, description="前置子任務 ID 清單")
    verification_cmd: str | None = Field(None, description="任務測試驗證指令")
    boundary_rules: list[str] = Field(default_factory=list, description="開發禁令與規範")
    review_required: bool = Field(False, description="是否必須通過既有審核流程才能完成")
    review_risk: Literal["normal", "high"] = Field(
        "normal", description="審核風險；high 沿既有異模型 critic 要求"
    )
    test_log: str | None = Field(None, description="初始化已有的測試輸出，不等於通過審核")
    artifact_refs: list[str] = Field(
        default_factory=list, description="初始化產出物相對路徑；沿既有路徑安全驗證"
    )

    @field_validator("artifact_refs")
    @classmethod
    def validate_references(cls, value: list[str]) -> list[str]:
        """Apply the existing domain artifact-path validator."""
        return Task.validate_artifact_refs(value)


class MissionTargetInput(PublicToolInput):
    """Public input model for MissionTarget."""

    target_agent_id: str | None = Field(
        None, description="看板擁有者；跨身分操作仍需 Supervisor 權限"
    )


class MissionInitializeInput(MissionTargetInput):
    """Public input model for MissionInitialize."""

    action: Literal["initialize"]
    goal: str = Field(min_length=1, description="任務大目標")
    tasks: list[MissionTaskInput] = Field(default_factory=list, description="初始子任務清單")
    notify_on_complete: bool = Field(
        False,
        description=(
            "initialize 使用；只有 target_agent_id=DispatchAgent "
            "才產生 task callback_token，其他目標不通知"
        ),
    )


class MissionUpdateInput(MissionTargetInput):
    """Public input model for MissionUpdate."""

    action: Literal["update_status"]
    task_id: str = Field(description="目標任務 ID")
    status: Literal["TODO", "DOING", "DONE", "FAILED", "BLOCKED"] = Field(
        description="新狀態；BLOCKED 必須同時提供 blocker_detail，原 FSM 仍強制"
    )
    artifact_refs: list[str] | None = Field(None, description="產出物路徑清單（單一產出亦用此欄）")
    test_log: str | None = Field(None, description="測試執行輸出")
    blocker_detail: str | None = Field(None, description="BLOCKED 必填受阻原因")

    @field_validator("artifact_refs")
    @classmethod
    def validate_references(cls, value: list[str] | None) -> list[str] | None:
        """Apply the existing domain artifact-path validator."""
        return Task.validate_artifact_refs(value) if value is not None else None


class MissionBoardInput(MissionTargetInput):
    """Public input model for MissionBoard."""

    action: Literal["get_board"]


class MissionAwaitInput(PublicToolInput):
    """Public input model for MissionAwait."""

    action: Literal["await_dispatch_result"]
    callback_token: str = Field(description="DispatchAgent initialize 回傳的 task 專屬回呼憑證")
    timeout: float = Field(300.0, description="等待超時秒數")


class MissionFinalizeInput(MissionTargetInput):
    """Public input model for MissionFinalize."""

    action: Literal["finalize"]
    verdict: str | None = Field(
        None, description="省略＝驗收清單；填入＝全 DONE 後記錄最終驗收結論"
    )


class MissionReviewInput(MissionTargetInput):
    """Public input model for MissionReview."""

    action: Literal["request_review"]
    task_id: str = Field(description="被審任務 ID")
    producer_claims: str = Field("", description="被審產出宣稱，不作為已證明事實")
    critic_model: str | None = Field(None, description="critic 模型；沿現行高風險異模型與降級政策")


class LegacyArtifactInput(PublicToolInput):
    """Validated compatibility input; never included in public tool schemas."""

    artifact_ref: str | None = None

    @field_validator("artifact_ref")
    @classmethod
    def validate_reference(cls, value: str | None) -> str | None:
        """Apply the existing domain singular artifact-path validator."""
        return Task.validate_artifact_ref(value)


MISSION_ACTION_CONTRACTS = {
    "initialize": MissionInitializeInput,
    "update_status": MissionUpdateInput,
    "get_board": MissionBoardInput,
    "await_dispatch_result": MissionAwaitInput,
    "finalize": MissionFinalizeInput,
    "request_review": MissionReviewInput,
}


class EvolutionVerifyInput(PublicToolInput):
    """Public input model for EvolutionVerify."""

    skill_id: str = Field(description="evolution_define 使用的 ID")
    code: str = Field(description="MCP Python 實作")
    test_tool_name: str = Field(description="驗證要調用的工具名")
    test_arguments: dict[str, Any] = Field(description="驗證工具參數")
    dependencies: list[str] | None = Field(None, description="PIP 依賴清單")
    submit_for_deploy: bool = Field(False, description="驗證成功後送既有人類審核；預設不部署")
    # Keep raw values until the existing verifier clamps ONCE. int(0.5)==0
    # must not be clamped a second time back up to the system ceiling.
    timeout_override: Any = Field(
        None,
        description=(
            "只收緊 MCP 驗證 sandbox 秒數，不含建立 venv/install/deploy；"
            "無效或不小於系統上限則沿既有上限"
        ),
        json_schema_extra={"type": "number"},
    )
    max_storage_mb: Any = Field(
        None,
        description="只收緊 MCP 驗證 sandbox MiB；無效或不小於系統上限則沿既有上限",
        json_schema_extra={"type": "number"},
    )


class CreateSkillInput(PublicToolInput):
    """Public input model for CreateSkill."""

    id: str = Field(min_length=1, description="skill 唯一 ID（kebab-case）")
    description: str = Field(description="用途描述")
    name: str = Field("", description="顯示名稱")
    category: str = Field("General", description="分類")
    author: str = Field("", description="作者 metadata；未填或空值使用当前 agent ID，不改權限身分")
    system_prompt: str = Field("", description="skill 系統提示")
    required_tools: list[str] | None = Field(None, description="依賴的工具 ID")
    mcp_servers: list[str] | None = Field(None, description="依賴的 MCP server ID")


class IdeDiffInput(PublicToolInput):
    """Public input model for IdeDiff."""

    path: str = Field(description="檔案路徑")
    new_content: str = Field(description="建議的新內容；session 由系統自動注入")


class SoulTargetInput(PublicToolInput):
    """Public input model for SoulTarget."""

    type: Literal["soul", "config"] | None = Field(
        None, description="soul=soul.md（最少 50 字）；config=config.json；兩者皆禁止 delete"
    )
    filename: str | None = Field(None, description="或指定靈魂目錄內其他檔案；既有保護仍強制")


class SoulWriteInput(SoulTargetInput):
    """Public input model for SoulWrite."""

    action: Literal["write"]
    content: str = Field(description="write 完整內容；soul.md 最少 50 字")


class SoulReplaceInput(SoulTargetInput):
    """Public input model for SoulReplace."""

    action: Literal["str_replace"]
    old_str: str = Field(description="替換的舊內容")
    new_str: str = Field(description="替換的新內容")


class SoulInsertInput(SoulTargetInput):
    """Public input model for SoulInsert."""

    action: Literal["insert"]
    insert_line: int = Field(
        description="0-based 插入位置；沿既有邊界夾取，不同於檔案 insert 的 1-based"
    )
    new_str: str = Field(description="插入的新內容")


class SoulUndoInput(SoulTargetInput):
    """Public input model for SoulUndo."""

    action: Literal["undo_edit"]


class SoulDeleteInput(SoulTargetInput):
    """Public input model for SoulDelete."""

    action: Literal["delete"]


SOUL_ACTION_CONTRACTS = {
    "write": SoulWriteInput,
    "str_replace": SoulReplaceInput,
    "insert": SoulInsertInput,
    "undo_edit": SoulUndoInput,
    "delete": SoulDeleteInput,
}


class SubagentTaskInput(PublicToolInput):
    """Public input model for SubagentTask."""

    goal: str = Field(description="自包含的子任務目標與完整指示")
    task_id: str | None = Field(None, description="mission board task ID 結果標記")
    sub_model: str | None = Field(None, description="子任務模型；省略沿用父模型")


class SubagentGroupInput(PublicToolInput):
    """Public input model for SubagentGroup."""

    subtasks: list[SubagentTaskInput] = Field(
        max_length=MAX_SUBAGENT_GROUP,
        description=(
            f"每批最多 {MAX_SUBAGENT_GROUP} 個任務，同時最多 {SUBAGENT_GROUP_CONCURRENCY} 個子 loop"
        ),
    )


PUBLIC_TOOL_CONTRACTS = {
    "memory_tools": MEMORY_ACTION_CONTRACTS,
    "mission_tools": MISSION_ACTION_CONTRACTS,
    "evolution_implement_and_verify": EvolutionVerifyInput,
    "create_skill": CreateSkillInput,
    "ide_open_diff": IdeDiffInput,
    "edit_soul": SOUL_ACTION_CONTRACTS,
    "dispatch_subagent_group": SubagentGroupInput,
}


def native_parameters(name: str) -> dict[str, Any]:
    """Canonical parameters from the selected public contract."""
    contract = PUBLIC_TOOL_CONTRACTS[name]
    return (
        action_parameters(contract) if isinstance(contract, dict) else public_parameters(contract)
    )


def _normalize_aliases(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: value
        for key, value in arguments.items()
        if key not in EXECUTION_CONTEXT_KWARG_NAMES
        and key not in {"identity", "manager", "resource_proxy"}
    }
    if name == "memory_tools" and "importance_min" in public:
        minimum = public.pop("importance_min")
        public["importance"] = public.get("importance") or minimum
    if name == "mission_tools" and isinstance(public.get("tasks"), list):
        tasks = []
        for task in public["tasks"]:
            if not isinstance(task, dict):
                tasks.append(task)
                continue
            item = dict(task)
            if "id" in item:
                item["task_id"] = item.pop("id")
            if "dependencies" in item:
                item["dependency_task_ids"] = item.pop("dependencies")
            tasks.append(item)
        public["tasks"] = tasks
    return public


def validate_native_arguments(
    name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Validate only selected native public calls; never echo untrusted values."""
    contract = PUBLIC_TOOL_CONTRACTS.get(name)
    if contract is None:
        return arguments, None
    public = _normalize_aliases(name, arguments)
    if isinstance(contract, dict):
        action = public.get("action")
        model = contract.get(action) if isinstance(action, str) else None
        if model is None:
            return {}, build_tool_error(
                VALIDATION_ERROR,
                "Unknown public action.",
                suggestion="Use an action from the advertised tool schema.",
                param="action",
            )
    else:
        model = contract
    try:
        legacy: dict[str, Any] = {}
        task_legacy: dict[int, dict[str, Any]] = {}
        if name == "mission_tools":
            if "artifact_ref" in public:
                legacy = LegacyArtifactInput.model_validate(
                    {"artifact_ref": public.pop("artifact_ref")}
                ).model_dump(exclude_unset=True)
            tasks = public.get("tasks")
            for index, task in enumerate(tasks if isinstance(tasks, list) else []):
                if isinstance(task, dict) and "artifact_ref" in task:
                    task_legacy[index] = LegacyArtifactInput.model_validate(
                        {"artifact_ref": task.pop("artifact_ref")}
                    ).model_dump(exclude_unset=True)
        validated = model.model_validate(public)
    except ValidationError as exc:
        # Error locations can contain unknown user-controlled keys. Only publish
        # field names found in this contract, and never Pydantic input/context.
        issues = [
            {
                "field": str(error["loc"][0])
                if error["loc"] and error["loc"][0] in model.model_fields
                else "arguments",
                "reason": error["type"],
            }
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        return {}, build_tool_error(
            VALIDATION_ERROR,
            "Invalid public tool arguments.",
            suggestion="Check the advertised fields, types and action requirements.",
            issues=issues,
        )
    result = validated.model_dump(exclude_unset=True)
    result.update(legacy)
    for index, fields in task_legacy.items():
        result["tasks"][index].update(fields)
    return result, None
