"""ADR-0203：MCP P5 API 的 auth、Pydantic 與 canonical 投影。"""

from __future__ import annotations

from typing import Any

import pytest
import src.main as main_mod
from fastapi.testclient import TestClient
from src.main import app
from src.models.identity import AgentIdentity
from src.protocols.mcp.canonical import (
    McpPromptArgument,
    McpPromptCatalog,
    McpPromptDescriptor,
    McpPromptMessage,
    McpPromptResult,
    McpResourceCatalog,
    McpResourceDescriptor,
    McpResourceReadResult,
    McpResourceTemplateCatalog,
    McpResourceTemplateDescriptor,
    McpTextPromptContent,
    McpTextResourceContent,
    McpToolCatalog,
    McpToolDescriptor,
)
from src.protocols.registry import ProtocolServerConfig
from src.services.identity_router import get_verified_identity
from src.services.permission_service import permission_service


class _Registry:
    def get_server(self, name: str) -> ProtocolServerConfig | None:
        if name != "srv":
            return None
        return ProtocolServerConfig(
            command="echo",
            enabled_tools=["*"],
            disabled_tools=[],
        )


class _P5Manager:
    registry = _Registry()

    def __init__(self) -> None:
        self.contexts: list[Any] = []

    def _record(self, context: Any) -> None:
        self.contexts.append(context)

    def error_message(self, _name: str, error: BaseException) -> str:
        return str(error)

    async def list_server_tool_descriptors(
        self,
        _name: str,
        *,
        request_context: Any,
    ) -> McpToolCatalog:
        self._record(request_context)
        return McpToolCatalog(
            source_protocol_version="2026-07-28",
            items=[
                McpToolDescriptor(
                    source_protocol_version="2026-07-28",
                    name="lookup",
                    title="Lookup",
                    description="Find a record",
                    input_schema={
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                    },
                    output_schema={
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "array",
                    },
                    annotations={"readOnlyHint": True},
                    icons=[{"src": "https://example.test/icon.png"}],
                    meta={"fixture": True},
                )
            ],
        )

    async def list_server_resources(
        self,
        _name: str,
        *,
        request_context: Any,
    ) -> McpResourceCatalog:
        self._record(request_context)
        return McpResourceCatalog(
            source_protocol_version="2026-07-28",
            items=[
                McpResourceDescriptor(
                    source_protocol_version="2026-07-28",
                    name="readme",
                    uri="touka-resource://fixture/readme",
                    mime_type="text/plain; charset=utf-8",
                )
            ],
        )

    async def list_server_resource_templates(
        self,
        _name: str,
        *,
        request_context: Any,
    ) -> McpResourceTemplateCatalog:
        self._record(request_context)
        return McpResourceTemplateCatalog(
            source_protocol_version="2026-07-28",
            items=[
                McpResourceTemplateDescriptor(
                    source_protocol_version="2026-07-28",
                    name="item",
                    uri_template="touka-resource://fixture/{id}",
                )
            ],
        )

    async def read_server_resource(
        self,
        _name: str,
        uri: str,
        *,
        request_context: Any,
    ) -> McpResourceReadResult:
        self._record(request_context)
        if uri.startswith("file:"):
            msg = "file URI blocked"
            raise ValueError(msg)
        return McpResourceReadResult(
            source_protocol_version="2026-07-28",
            contents=[
                McpTextResourceContent(
                    uri=uri,
                    mime_type="text/plain; charset=utf-8",
                    text="fixture",
                    encoded_size=7,
                )
            ],
        )

    async def list_server_prompts(
        self,
        _name: str,
        *,
        request_context: Any,
    ) -> McpPromptCatalog:
        self._record(request_context)
        return McpPromptCatalog(
            source_protocol_version="2026-07-28",
            items=[
                McpPromptDescriptor(
                    source_protocol_version="2026-07-28",
                    name="review",
                    arguments=[McpPromptArgument(name="topic", required=True)],
                )
            ],
        )

    async def get_server_prompt(
        self,
        _name: str,
        prompt_name: str,
        arguments: dict[str, str] | None,
        *,
        request_context: Any,
    ) -> McpPromptResult:
        self._record(request_context)
        return McpPromptResult(
            source_protocol_version="2026-07-28",
            description=f"{prompt_name}:{arguments}",
            messages=[
                McpPromptMessage(
                    role="user",
                    content=McpTextPromptContent(
                        text="external",
                        injection_flagged=True,
                    ),
                )
            ],
            injection_flagged=True,
        )


@pytest.fixture
def p5_manager(monkeypatch: pytest.MonkeyPatch) -> _P5Manager:
    manager = _P5Manager()
    monkeypatch.setattr(main_mod, "protocol_manager", manager)
    app.state.context["protocol_manager"] = manager
    return manager


@pytest.fixture
def unprivileged_client():
    identity = AgentIdentity.for_role("BaseP5", "Base")
    app.dependency_overrides[get_verified_identity] = lambda: identity
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_tools_endpoint_preserves_full_descriptor_and_policy(
    auth_client: TestClient,
    p5_manager: _P5Manager,
):
    response = auth_client.get("/v1/mcp/srv/tools")
    assert response.status_code == 200
    body = response.json()
    tool = body["tools"][0]
    assert tool["title"] == "Lookup"
    assert tool["input_schema"]["$schema"].endswith("2020-12/schema")
    assert tool["output_schema"]["type"] == "array"
    assert tool["annotations"] == {"readOnlyHint": True}
    assert tool["icons"][0]["src"].endswith("icon.png")
    assert tool["meta"] == {"fixture": True}
    assert tool["enabled"] is True
    assert p5_manager.contexts[0].identity.agent_id


@pytest.mark.parametrize(
    ("path", "item_key"),
    [
        ("/v1/mcp/srv/resources", "uri"),
        ("/v1/mcp/srv/resource-templates", "uri_template"),
        ("/v1/mcp/srv/prompts", "name"),
    ],
)
def test_catalog_endpoints_have_typed_responses(
    auth_client: TestClient,
    p5_manager: _P5Manager,
    path: str,
    item_key: str,
):
    response = auth_client.get(path)
    assert response.status_code == 200
    assert response.json()["items"][0][item_key]


def test_resource_read_and_prompt_get_are_post_and_untrusted(
    auth_client: TestClient,
    p5_manager: _P5Manager,
):
    resource = auth_client.post(
        "/v1/mcp/srv/resources/read",
        json={"uri": "touka-resource://fixture/readme"},
    )
    prompt = auth_client.post(
        "/v1/mcp/srv/prompts/get",
        json={"name": "review", "arguments": {"topic": "security"}},
    )

    assert resource.status_code == 200
    assert resource.json()["result"]["contents"][0]["text"] == "fixture"
    assert prompt.status_code == 200
    assert prompt.json()["result"]["trust"] == "untrusted"
    assert prompt.json()["result"]["injection_flagged"] is True


def test_malicious_resource_uri_returns_validation_error(
    auth_client: TestClient,
    p5_manager: _P5Manager,
):
    response = auth_client.post(
        "/v1/mcp/srv/resources/read",
        json={"uri": "file:///etc/passwd"},
    )
    assert response.status_code == 422


def test_p5_catalogs_reject_unprivileged_identity(
    unprivileged_client: TestClient,
    p5_manager: _P5Manager,
):
    assert unprivileged_client.get("/v1/mcp/srv/resources").status_code == 403
    assert unprivileged_client.get("/v1/mcp/srv/prompts").status_code == 403


def test_p5_data_plane_obeys_mcp_server_permission(
    auth_client: TestClient,
    p5_manager: _P5Manager,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        permission_service,
        "is_mcp_server_allowed",
        lambda _server, _identity: False,
    )
    assert auth_client.get("/v1/mcp/srv/resources").status_code == 403
    assert auth_client.get("/v1/mcp/srv/prompts").status_code == 403
