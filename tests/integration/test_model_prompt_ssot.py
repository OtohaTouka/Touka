"""Integration test for Model Prompt SSOT."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.models.identity import AgentIdentity
from src.services.soul_service import SoulService
from src.utils.brain_persistence import record_working_brain

from tests.helpers.claude_stream import delegate_stream_to_create


def test_system_prompt_injection_consistency(mock_identity, global_test_sandbox):
    """驗證 SoulService 產出的系統提示詞包含核心主權標籤且結構一致。"""
    agent_id = "DebugAgent"
    soul_dir = Path(global_test_sandbox) / "soul" / agent_id
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text(f"Identity: {agent_id}", encoding="utf-8")

    svc = SoulService()
    identity = mock_identity(agent_id=agent_id)
    prompt, _ = svc.get_soul_data(identity)

    assert prompt is not None
    assert agent_id in prompt


def test_soul_layer_middleware_injection(
    auth_client, mock_text_response, global_test_sandbox
):
    """驗證 SoulLayer 中介軟體確實攔截並注入了提示詞。"""
    # 1. 在 Sandbox 中建立靈魂檔案
    agent_id = "DebugAgent"
    soul_dir = Path(global_test_sandbox) / "soul" / agent_id
    soul_dir.mkdir(parents=True, exist_ok=True)
    (soul_dir / "soul.md").write_text(f"I am {agent_id}", encoding="utf-8")
    (soul_dir / "config.json").write_text(
        '{"is_toukasoul": true}', encoding="utf-8"
    )

    # 設置活躍大腦，防止 SoulLayer 降級到 Gemini (SSOT: Brain Routing)
    record_working_brain(
        AgentIdentity(agent_id=agent_id), "claude-3-5-sonnet"
    )

    # 2. 配置環境 (使用顯式注入)
    mock_create = AsyncMock()
    mock_create.return_value = mock_text_response("OK")

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = mock_create
    # ADR-0118：factory 有即時金鑰時會 with_options 換 client——回自身，讓原 mock 收到 create。
    mock_client.with_options.return_value = mock_client
    delegate_stream_to_create(mock_client)  # 3b-U1：主迴圈走 messages.stream()

    auth_client.app.state.context["anthropic_client"] = mock_client

    # 3. 發送請求 Payload
    payload = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Who are you?"}],
    }

    # 4. 執行請求
    headers = {"X-Touka-Soul-ID": agent_id}
    response = auth_client.post(
        "/v1/messages", json=payload, headers=headers
    )
    assert response.status_code == 200
    for _ in response.iter_lines():
        pass

    # 5. 檢查模型接收到的 system 參數
    assert mock_create.called, (
        "Mock not called -- request was likely routed to a non-Claude handler."
    )
    calls = mock_create.call_args_list
    system_param = None
    for call in calls:
        if "system" in call.kwargs:
            system_param = call.kwargs["system"]
            break

    assert system_param is not None, f"System prompt not found in calls: {calls}"
    assert agent_id in str(system_param)
