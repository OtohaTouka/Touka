"""Test handler identity sovereignty: verify AgentIdentity is resolved without crashes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.handlers.gemini_handler import handle_gemini_request  # noqa: E402
from src.handlers.openai_handler import handle_openai_request  # noqa: E402
from src.models.identity import AgentIdentity
from src.services.permission_service import permission_service  # noqa: E402


@pytest.fixture
def mock_context():
    """Build a minimal handler context with only agent_id (no identity)."""
    return {
        "agent_id": "DebugAgent",
        # identity deliberately omitted to test fallback
        "request_id": "test-req-001",
        "t0": 1700000000.0,
        "is_toukasoul": False,
        "enable_autonomy": False,
        "protocol_manager": MagicMock(),
        "tool_registry": {},
        "memory_tool_registry": {},
        "plugin_registry": {},
    }


class TestOpenAIHandlerSovereignty:
    """Verify openai_handler resolves identity from agent_id without NameError."""

    @pytest.mark.asyncio
    async def test_identity_fallback_no_crash(self, mock_context):
        """When context has agent_id but no identity, handler must not crash."""

        mock_context["api_key"] = "test-key"
        mock_context["url"] = "https://api.example.com/v1/chat/completions"
        mock_context["provider"] = "TestProvider"

        mock_request = MagicMock()
        mock_request.model = "test-model"
        mock_request.messages = []
        mock_request.model_dump.return_value = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        # E1：傳輸歸 adapter。_State.client 的 default_factory 在類別定義時就綁定 AsyncClient，
        # patch 模組屬性攔不到（曾真的連到 api.example.com），所以 patch 類別的 post。
        with patch(
            "src.handlers.adapters.openai_adapter.httpx.AsyncClient.post",
            AsyncMock(return_value=mock_resp),
        ) as mock_post:
            chunks = []
            async for chunk in handle_openai_request(mock_request, mock_context):
                chunks.append(chunk)

            # Should have produced SSE chunks without crashing
            assert len(chunks) > 0
            assert mock_post.await_count == 1
            print(f"OpenAI handler produced {len(chunks)} chunks without crash.")


class TestGeminiHandlerSovereignty:
    """Verify gemini_handler resolves identity from agent_id without NameError."""

    @pytest.mark.asyncio
    async def test_identity_fallback_no_crash(self, mock_context):
        """When context has agent_id but no identity, handler must not crash."""

        mock_context["gemini_api_key"] = "test-key"

        mock_request = MagicMock()
        mock_request.model = "gemini-2.5-flash"
        mock_request.messages = []

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "hello from gemini"}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
            },
        }

        with (
            patch("src.handlers.adapters.gemini_adapter.httpx.AsyncClient") as mock_client_cls,
            patch("src.handlers.adapters.gemini_adapter.convert_openai_to_gemini") as mock_convert,
        ):
            mock_convert.return_value = {
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            }

            # E2：傳輸歸 adapter——client 為直接持有的實例（非 async-with context manager）。
            mock_client_cls.return_value.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.aclose = AsyncMock()

            chunks = []
            async for chunk in handle_gemini_request(mock_request, mock_context):
                chunks.append(chunk)

            assert len(chunks) > 0
            print(f"Gemini handler produced {len(chunks)} chunks without crash.")


class TestIdentityResolution:
    """Verify the identity fallback correctly creates an AgentIdentity object."""

    def test_identity_is_created_from_agent_id(self):
        """The fallback logic must produce a valid AgentIdentity."""

        agent_id = "DebugAgent"
        identity = AgentIdentity(agent_id=agent_id)
        role = permission_service.get_role_key(identity)
        result = AgentIdentity.for_role(agent_id=agent_id, role=role)

        assert isinstance(result, AgentIdentity)
        assert result.agent_id == "DebugAgent"
        assert result.display_role is not None
        print(f"Identity resolved: {result}")
