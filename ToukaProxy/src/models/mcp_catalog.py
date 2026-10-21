"""MCP P5 對外 API 契約（ADR-0203）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from src.protocols.mcp.canonical import (
    McpCatalogIssue,
    McpPromptDescriptor,
    McpPromptResult,
    McpResourceDescriptor,
    McpResourceReadResult,
    McpResourceTemplateDescriptor,
    McpToolDescriptor,
)


class MCPServerToolItem(McpToolDescriptor):
    """完整 tool descriptor 加 Touka per-installation 啟用狀態。"""

    enabled: bool


class MCPServerToolsResponse(BaseModel):
    """單台伺服器的完整工具面、schema 與治理狀態。"""

    server: str
    source_protocol_version: str
    supported: bool = True
    tools: list[MCPServerToolItem] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = Field(default_factory=list)


class MCPServerResourcesResponse(BaseModel):
    """resources/list 的完整 canonical 回應。"""

    server: str
    source_protocol_version: str
    supported: bool = True
    items: list[McpResourceDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class MCPServerResourceTemplatesResponse(BaseModel):
    """resources/templates/list 的完整 canonical 回應。"""

    server: str
    source_protocol_version: str
    supported: bool = True
    items: list[McpResourceTemplateDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class MCPResourceReadRequest(BaseModel):
    """resources/read request；URI 的安全語意由 manager 唯一執法。"""

    model_config = ConfigDict(extra="forbid")
    uri: str


class MCPResourceReadResponse(BaseModel):
    """resources/read API response。"""

    server: str
    uri: str
    result: McpResourceReadResult


class MCPServerPromptsResponse(BaseModel):
    """prompts/list 的完整 canonical 回應。"""

    server: str
    source_protocol_version: str
    supported: bool = True
    items: list[McpPromptDescriptor] = Field(default_factory=list)
    issues: list[McpCatalogIssue] = Field(default_factory=list)


class MCPPromptGetRequest(BaseModel):
    """prompts/get request。"""

    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict[str, str] | None = None


class MCPPromptGetResponse(BaseModel):
    """prompts/get API response；result 內 trust 固定 untrusted。"""

    server: str
    name: str
    result: McpPromptResult
