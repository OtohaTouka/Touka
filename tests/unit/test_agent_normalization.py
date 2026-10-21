from unittest.mock import patch

from src.services.agent_detector import detect_target_agent

# 中性測試用 registry（不含真實 Agent 名稱）
_TEST_REGISTRY = {
    "Alice": ["Ember", "Star"],
    "Bob": ["Wave"],
    "Charlie": ["Charlie"],
}


def test_agent_detection_case_insensitivity():
    """測試偵測器是否能正確將小寫輸入校準為 PascalCase。"""
    with patch("src.services.agent_detector.config_utils.read_config_json", return_value=_TEST_REGISTRY):
        # 測試 toukasoul 前綴 (Lowercase input -> PascalCase output)
        assert detect_target_agent([], model_name="toukasoulalice") == "Alice"
        assert detect_target_agent([], model_name="toukasoulbob") == "Bob"
        # 測試全大寫
        assert detect_target_agent([], model_name="TOUKASOULALICE") == "Alice"


def test_agent_detection_from_registry():
    """測試從註冊表中精確匹配時的校準。"""
    with patch("src.services.agent_detector.config_utils.read_config_json", return_value=_TEST_REGISTRY):
        assert detect_target_agent([], model_name="alice") == "Alice"
        assert detect_target_agent([], model_name="Alice") == "Alice"


def test_agent_detection_from_context():
    """測試從對話內容中偵測關鍵字。"""
    with patch("src.services.agent_detector.config_utils.read_config_json", return_value=_TEST_REGISTRY):
        # "Ember" 是 Alice 的別名
        messages = [{"role": "user", "content": "Ember, how are you?"}]
        assert detect_target_agent(messages) == "Alice"

        # "alice" 是 Alice 的 ID
        messages_lower = [{"role": "user", "content": "alice, help me"}]
        assert detect_target_agent(messages_lower) == "Alice"
