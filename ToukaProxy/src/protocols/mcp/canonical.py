"""MCP 的 SDK-neutral canonical boundary（2026 遷移 P1）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.models import identity as identity_models
from src.models import policy as policy_models


class McpRequestOrigin(StrEnum):
    """MCP 呼叫來源；不同來源的委派與互動規則不得混用。"""

    FOREGROUND = "foreground"
    HEARTBEAT = "heartbeat"
    DISPATCH = "dispatch"


class ExecutionKind(StrEnum):
    """Server-derived execution path; caller-declared origin cannot replace it。"""

    FOREGROUND = "foreground"
    HEARTBEAT = "heartbeat"
    DISPATCH = "dispatch"


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class McpInvocationBindingError(ValueError):
    """An authorized invocation does not match the actual JSON-RPC wire attempt。"""

    def __init__(self) -> None:
        super().__init__("MCP wire attempt does not match its authorization ticket.")


class McpToolNameCollisionError(ValueError):
    """A catalog contains ambiguous raw or Unicode/case-equivalent tool names。"""

    def __init__(self) -> None:
        super().__init__("MCP tool catalog contains an ambiguous tool name.")


def digest_mcp_arguments(arguments: Any) -> str:
    """Return the canonical JSON digest shared by authorization and MRTR bindings。"""
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def validate_mcp_tool_name_set(names: tuple[str, ...] | list[str]) -> None:
    """Reject exact duplicates and NFC＋casefold aliases before provider projection。"""
    raw_names: set[bytes] = set()
    normalized_names: set[str] = set()
    for name in names:
        raw_name = name.encode("utf-8")
        normalized_name = unicodedata.normalize("NFC", name).casefold()
        if not raw_name or raw_name in raw_names or normalized_name in normalized_names:
            raise McpToolNameCollisionError()
        raw_names.add(raw_name)
        normalized_names.add(normalized_name)


class McpInvocationAuthorization(BaseModel):
    """One short-lived, immutable authorization for one exact MCP wire call。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str = Field(min_length=1, max_length=256)
    authority_kind: Literal["workload", "delegation"]
    workload_principal_id: str | None = Field(default=None, min_length=1, max_length=256)
    owner_user_id: str = Field(min_length=1, max_length=256)
    human_principal_id: str | None = Field(default=None, min_length=1, max_length=256)
    delegation_digest: str | None = None
    delegation_generation: int | None = Field(default=None, ge=1)
    execution_agent_id: str = Field(min_length=1, max_length=256)
    execution_kind: ExecutionKind = ExecutionKind.HEARTBEAT
    origin: McpRequestOrigin
    installation_id: str = Field(min_length=1, max_length=256)
    installation_generation: int = Field(ge=1)
    auth_profile_id: str = Field(min_length=1, max_length=256)
    profile_generation: int = Field(ge=1)
    target_binding_digest: str
    credential_generation: int | None = Field(default=None, ge=1)
    operation_method: Literal["tools/call"] = "tools/call"
    raw_tool_name: str = Field(min_length=1, max_length=1024)
    arguments_digest: str
    required_scopes: frozenset[str] = Field(default_factory=frozenset)
    auth_level: str = Field(min_length=1, max_length=64)
    policy_digest: str
    request_id: str = Field(min_length=1, max_length=256)
    issued_at: float
    expires_at: float

    @field_validator(
        "arguments_digest",
        "policy_digest",
        "delegation_digest",
        "target_binding_digest",
    )
    @classmethod
    def digest_must_be_sha256(cls, value: str | None) -> str | None:
        """Require one lowercase canonical SHA-256 representation。"""
        if value is not None and not _SHA256_DIGEST_RE.fullmatch(value):
            msg = "MCP authorization digests must use canonical sha256 hex encoding."
            raise ValueError(msg)
        return value

    @field_validator("required_scopes")
    @classmethod
    def scopes_must_be_nonempty_strings(cls, value: frozenset[str]) -> frozenset[str]:
        """Reject scope aliases created by leading or trailing whitespace。"""
        if any(not scope or scope != scope.strip() for scope in value):
            msg = "MCP authorization scopes must be non-empty canonical strings."
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_authority_binding(self) -> McpInvocationAuthorization:
        """Keep workload and owner-delegation authority forms disjoint。"""
        if (
            self.origin != McpRequestOrigin.HEARTBEAT
            or self.execution_kind != ExecutionKind.HEARTBEAT
        ):
            msg = "P8-B workload authorization is heartbeat-only."
            raise ValueError(msg)
        if self.expires_at <= self.issued_at:
            msg = "MCP authorization expiry must be after issuance."
            raise ValueError(msg)
        if self.authority_kind == "workload":
            if (
                self.workload_principal_id is None
                or self.credential_generation is None
                or self.human_principal_id is not None
                or self.delegation_digest is not None
                or self.delegation_generation is not None
            ):
                msg = (
                    "Workload authority requires a credential generation and must not "
                    "contain delegation binding fields."
                )
                raise ValueError(msg)
        elif (
            self.workload_principal_id is not None
            or self.credential_generation is not None
            or self.human_principal_id is None
            or self.delegation_digest is None
            or self.delegation_generation is None
        ):
            msg = (
                "Delegation authority requires exact human, digest, and generation "
                "binding without a workload credential generation."
            )
            raise ValueError(msg)
        return self

    def verify_wire_attempt(
        self,
        *,
        method: str,
        tool_name: str,
        arguments: Any,
        is_batch: bool = False,
    ) -> None:
        """Fail closed unless the real JSON-RPC attempt is byte-exact to this ticket。"""
        method_matches = hmac.compare_digest(
            method.encode("utf-8"),
            self.operation_method.encode("utf-8"),
        )
        name_matches = hmac.compare_digest(
            tool_name.encode("utf-8"),
            self.raw_tool_name.encode("utf-8"),
        )
        arguments_match = hmac.compare_digest(
            digest_mcp_arguments(arguments),
            self.arguments_digest,
        )
        if is_batch or not method_matches or not name_matches or not arguments_match:
            raise McpInvocationBindingError()

    @property
    def handle_principal_key(self) -> str:
        """Hash every authority generation that may affect bearer reuse。"""
        parts = (
            self.authority_kind,
            self.workload_principal_id or "",
            self.human_principal_id or "",
            self.delegation_digest or "",
            str(self.delegation_generation or 0),
            self.execution_agent_id,
            self.execution_kind.value,
            self.origin.value,
            self.installation_id,
            str(self.installation_generation),
            self.auth_profile_id,
            str(self.profile_generation),
            self.target_binding_digest,
            str(self.credential_generation or 0),
            self.operation_method,
            self.raw_tool_name,
        )
        encoded = (part.encode("utf-8") for part in parts)
        canonical = b"".join(
            len(part).to_bytes(4, "big") + part for part in encoded
        )
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class McpTransportBootstrapAuthorization(BaseModel):
    """Distinct short-lived authority for authenticated transport bootstrap。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str = Field(min_length=1, max_length=256)
    parent_authorization: McpInvocationAuthorization
    allowed_methods: frozenset[
        Literal[
            "initialize",
            "notifications/initialized",
            "server/discover",
            "tools/list",
            "transport/get",
        ]
    ] = frozenset(
        {
            "initialize",
            "notifications/initialized",
            "server/discover",
            "tools/list",
            "transport/get",
        }
    )
    issued_at: float
    expires_at: float

    @model_validator(mode="after")
    def validate_bootstrap_binding(self) -> McpTransportBootstrapAuthorization:
        """Keep tool-call authority disjoint from transport bootstrap authority。"""
        if (
            self.expires_at <= self.issued_at
            or self.expires_at > self.parent_authorization.expires_at
            or not self.allowed_methods
            or "tools/call" in self.allowed_methods
        ):
            raise ValueError()
        return self

    @property
    def handle_principal_key(self) -> str:
        """Bootstrap and its parent invocation share exactly one handle partition。"""
        return self.parent_authorization.handle_principal_key


class McpWorkloadWireAttempt(BaseModel):
    """Immutable digest of the actual credential-bearing outbound request。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str = Field(min_length=1, max_length=256)
    egress_kind: Literal[
        "mcp_bearer",
        "transport_bootstrap",
        "token_exchange",
        "delegated_refresh",
    ]
    method: str = Field(min_length=1, max_length=256)
    payload_digest: str
    target_binding_digest: str
    raw_tool_name: str | None = Field(default=None, min_length=1, max_length=1024)
    arguments_digest: str | None = None

    @field_validator("payload_digest", "arguments_digest", "target_binding_digest")
    @classmethod
    def payload_digest_must_be_sha256(cls, value: str | None) -> str | None:
        """Accept only canonical digests produced at the final wire boundary。"""
        if value is not None and not _SHA256_DIGEST_RE.fullmatch(value):
            raise ValueError()
        return value

    @model_validator(mode="after")
    def validate_wire_shape(self) -> McpWorkloadWireAttempt:
        """Require exact tool fields only for the final MCP bearer send。"""
        if self.egress_kind == "mcp_bearer":
            if (
                self.method != "tools/call"
                or self.raw_tool_name is None
                or self.arguments_digest is None
            ):
                raise ValueError()
        elif self.raw_tool_name is not None or self.arguments_digest is not None:
            raise ValueError()
        return self


class McpRequestContext(BaseModel):
    """一次 MCP 操作的不可變安全上下文。"""

    model_config = ConfigDict(frozen=True)

    request_id: str
    session_id: str | None = None
    identity: identity_models.AgentIdentity
    human_principal_id: str | None = None
    installation_id: str
    auth_level: str
    execution_kind: ExecutionKind = ExecutionKind.FOREGROUND
    origin: McpRequestOrigin
    policy_context: policy_models.PolicyContext | None = None
    cancellation_id: str | None = None
    trace_context: dict[str, str] = Field(default_factory=dict)
    # 人類在 Touka 審批卡上核准過這次呼叫（只由 ToolExecutor 依可信 _hitl_preapproved 設定；
    # 審查員與 YOLO 放行為 False）。ToukaIDE 據此不跳第二次確認（ADR-0246 U2c-7）。
    human_approved: bool = False

    @model_validator(mode="after")
    def validated_runtime_types(self) -> McpRequestContext:
        """Pydantic 必須把兩個安全物件解析為正式 runtime types。"""
        if not isinstance(self.identity, identity_models.AgentIdentity):
            msg = "MCP request identity must be an AgentIdentity."
            raise TypeError(msg)
        if self.policy_context is not None and not isinstance(
            self.policy_context,
            policy_models.PolicyContext,
        ):
            msg = "MCP policy context must be a PolicyContext."
            raise TypeError(msg)
        return self

    @property
    def principal_key(self) -> str:
        """handle/cache 隔離鍵：人類 principal 優先，否則使用凍結的執行身分。"""
        return self.human_principal_id or self.identity.agent_id

    @property
    def oauth_principal_key(self) -> str:
        """Composite human＋effective-soul key for interactive OAuth handles。"""
        if self.human_principal_id is None:
            msg = "Interactive OAuth requires a verified human principal."
            raise ValueError(msg)
        parts = (self.human_principal_id, self.identity.agent_id)
        canonical = b"".join(
            len(encoded).to_bytes(4, "big") + encoded
            for encoded in (part.encode("utf-8") for part in parts)
        )
        return hashlib.sha256(canonical).hexdigest()


class McpToolDescriptor(BaseModel):
    """不依附 Python SDK 欄位命名的工具描述。"""

    source_protocol_version: str
    name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    icons: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] | None = None


class McpCatalogIssue(BaseModel):
    """單一 catalog item 被隔離時的安全、可顯示原因。"""

    item_name: str
    code: str
    message: str


class McpToolCatalog(BaseModel):
    """單台 server 的完整、SDK-neutral 工具目錄。"""

    source_protocol_version: str
    supported: bool = True
    items: list[McpToolDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class McpResourceDescriptor(BaseModel):
    """MCP resource descriptor；URI 只是不可信識別字串，不是本機路徑。"""

    source_protocol_version: str
    name: str
    title: str | None = None
    uri: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None
    icons: list[dict[str, Any]] = Field(default_factory=list)
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class McpResourceTemplateDescriptor(BaseModel):
    """MCP resource template descriptor。"""

    source_protocol_version: str
    name: str
    title: str | None = None
    uri_template: str
    description: str = ""
    mime_type: str | None = None
    icons: list[dict[str, Any]] = Field(default_factory=list)
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class McpResourceCatalog(BaseModel):
    """單台 server 的 resource catalog。"""

    source_protocol_version: str
    supported: bool = True
    items: list[McpResourceDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class McpResourceTemplateCatalog(BaseModel):
    """單台 server 的 resource-template catalog。"""

    source_protocol_version: str
    supported: bool = True
    items: list[McpResourceTemplateDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class McpTextResourceContent(BaseModel):
    """經 MIME／UTF-8／DLP 邊界驗證的文字 resource。"""

    type: Literal["text"] = "text"
    uri: str
    mime_type: str
    text: str
    encoded_size: int
    injection_flagged: bool = False
    dlp_redacted: bool = False
    meta: dict[str, Any] | None = None


class McpBlobResourceContent(BaseModel):
    """經嚴格 base64 與 decoded-size 邊界驗證的二進位 resource。"""

    type: Literal["blob"] = "blob"
    uri: str
    mime_type: str
    blob: str
    decoded_size: int
    meta: dict[str, Any] | None = None


McpResourceContent = Annotated[
    McpTextResourceContent | McpBlobResourceContent,
    Field(discriminator="type"),
]


class McpResourceReadResult(BaseModel):
    """resources/read complete result；不保存或暴露上游 continuation state。"""

    source_protocol_version: str
    contents: list[McpResourceContent] = Field(default_factory=list)
    injection_flagged: bool = False
    dlp_redacted: bool = False
    meta: dict[str, Any] | None = None


class McpPromptArgument(BaseModel):
    """Prompt argument descriptor。"""

    name: str
    title: str | None = None
    description: str = ""
    required: bool = False


class McpPromptDescriptor(BaseModel):
    """MCP prompt descriptor。"""

    source_protocol_version: str
    name: str
    title: str | None = None
    description: str = ""
    arguments: list[McpPromptArgument] = Field(default_factory=list)
    icons: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] | None = None


class McpPromptCatalog(BaseModel):
    """單台 server 的 prompt catalog。"""

    source_protocol_version: str
    supported: bool = True
    items: list[McpPromptDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class McpTextPromptContent(BaseModel):
    """外部 prompt 文字；只可作 untrusted context。"""

    type: Literal["text"] = "text"
    text: str
    injection_flagged: bool = False
    dlp_redacted: bool = False
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class McpImagePromptContent(BaseModel):
    """外部 prompt 圖像內容。"""

    type: Literal["image"] = "image"
    data: str
    mime_type: str
    decoded_size: int
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class McpAudioPromptContent(BaseModel):
    """外部 prompt 音訊內容。"""

    type: Literal["audio"] = "audio"
    data: str
    mime_type: str
    decoded_size: int
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class McpResourceLinkPromptContent(BaseModel):
    """外部 prompt 的 resource link；不得被解析為本機路徑。"""

    type: Literal["resource_link"] = "resource_link"
    name: str
    title: str | None = None
    uri: str
    description: str = ""
    mime_type: str | None = None
    size: int | None = None
    icons: list[dict[str, Any]] = Field(default_factory=list)
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class McpEmbeddedResourcePromptContent(BaseModel):
    """外部 prompt 內嵌 resource。"""

    type: Literal["resource"] = "resource"
    resource: McpResourceContent
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


McpPromptContent = Annotated[
    McpTextPromptContent
    | McpImagePromptContent
    | McpAudioPromptContent
    | McpResourceLinkPromptContent
    | McpEmbeddedResourcePromptContent,
    Field(discriminator="type"),
]


class McpPromptMessage(BaseModel):
    """強型別 prompt message；MCP 只允許 user/assistant。"""

    role: Literal["user", "assistant"]
    content: McpPromptContent


class McpPromptResult(BaseModel):
    """prompts/get complete result；外部內容永遠標記為 untrusted。"""

    source_protocol_version: str
    trust: Literal["untrusted"] = "untrusted"
    description: str = ""
    messages: list[McpPromptMessage] = Field(default_factory=list)
    injection_flagged: bool = False
    dlp_redacted: bool = False
    meta: dict[str, Any] | None = None


class McpCompleteOutcome(BaseModel):
    """完整工具結果；structured content 保留任意 JSON 與顯式 null。"""

    result_type: Literal["complete"] = "complete"
    source_protocol_version: str
    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: Any = None
    structured_content_present: bool = False
    is_error: bool = False
    meta: dict[str, Any] | None = None


class McpInputRequiredOutcome(BaseModel):
    """MRTR canonical 形狀；P1 不宣稱 legacy wire 可產生此結果。"""

    result_type: Literal["input_required"] = "input_required"
    source_protocol_version: str
    input_requests: list[dict[str, Any]] = Field(default_factory=list)
    request_state: str
    meta: dict[str, Any] | None = None


class McpTaskOutcome(BaseModel):
    """Tasks canonical 形狀；只供後續 extension 階段接線。"""

    result_type: Literal["task"] = "task"
    source_protocol_version: str
    task_id: str
    status: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    input_requests: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] | None = None


McpOperationOutcome = Annotated[
    McpCompleteOutcome | McpInputRequiredOutcome | McpTaskOutcome,
    Field(discriminator="result_type"),
]


class McpErrorKind(StrEnum):
    """MCP 失敗面：JSON-RPC、transport 與合法 tool result 必須分流。"""

    PROTOCOL = "protocol"
    TRANSPORT = "transport"
    TOOL_RESULT = "tool_result"


class McpCanonicalError(BaseModel):
    """保留協定資訊、同時指向 Touka ErrorCode SSOT 的 canonical error。"""

    kind: McpErrorKind
    message: str
    source_protocol_version: str
    touka_error_code: str
    tool_error_class: str
    protocol_code: int | None = None
    protocol_data: Any | None = None


class McpOperationException(Exception):
    """JSON-RPC／transport 失敗的 typed exception；tool-result error 不走此類。"""

    def __init__(self, error: McpCanonicalError) -> None:
        self.error = error
        super().__init__(error.message)
