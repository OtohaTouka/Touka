from unittest.mock import patch

import pytest
from src.models.identity import AgentIdentity
from src.services.models.manager import ModelManager


@pytest.fixture
def manager():
    with patch("src.services.models.manager.config_utils") as mock_config:
        mock_config.read_config_json.return_value = {"models": [{"id": "test-model"}]}
        mock_config.config_exists.return_value = True
        return ModelManager()

def test_delete_model_triggers_agent_registration_removal(manager):
    """驗證刪除模型時，是否同步觸發了 AgentService 的註冊移除。"""

    # Patch where the name is used (manager module binding), not where defined.
    with patch("src.services.models.manager.agent_service") as mock_agent_service:
        mock_agent_service.delete_agent_registration.return_value = True

        result = manager.delete_model(AgentIdentity(agent_id="test-model"))

        assert result is True

        called_args = mock_agent_service.delete_agent_registration.call_args[0][0]
        assert called_args.agent_id == "test-model"

        assert not any(m["id"] == "test-model" for m in manager._models_cache)


def test_delete_model_returns_true_if_only_registration_deleted(manager):
    """驗證即使快取中沒有該模型，但註冊表中有，刪除操作仍應視為成功。"""

    with patch("src.services.models.manager.agent_service") as mock_agent_service:
        mock_agent_service.delete_agent_registration.return_value = True

        result = manager.delete_model(AgentIdentity(agent_id="unseen-agent"))

        assert result is True
        called_args = mock_agent_service.delete_agent_registration.call_args[0][0]
        assert called_args.agent_id == "unseen-agent"
