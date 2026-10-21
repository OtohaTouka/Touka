
from src.core.security import AuthSecretManager
from src.models.identity import AgentIdentity
from src.services.identity_service import identity_service


def test_identity_service_secret_sync(monkeypatch):
    """測試 JWTAuthenticationProvider 在金鑰重置後能同步更新，避免快取 stall。"""
    # 1. 模擬金鑰為 secret1
    secret1 = "secret_one_32_bytes_long_minimum_strength"
    monkeypatch.setattr(AuthSecretManager, "get_secret", lambda: secret1)
    
    # 測試用 identity 
    identity = AgentIdentity.for_role(
        agent_id="test_agent", role="user", is_human=False, capabilities=[]
    )
    
    # 用此時的狀態簽發 token1
    token1 = identity_service.issue_token(identity)
    
    # 驗證 token1 應可通過
    verified1 = identity_service.verify_token(token1)
    assert verified1 is not None
    assert verified1.agent_id == "test_agent"
    
    # 2. 模擬金鑰更新為 secret2
    secret2 = "secret_two_32_bytes_long_minimum_strength"
    monkeypatch.setattr(AuthSecretManager, "get_secret", lambda: secret2)
    
    # 簽發 token2
    token2 = identity_service.issue_token(identity)
    
    # 預期結果（在修復後）：
    # - token1 在重置後，因為 identity_service 改用 secret2 驗證，
    #   用 secret2 驗證 secret1 的 token1 應該失敗，傳回 None。
    # - token2 用新金鑰簽發，應該能用新金鑰驗證成功，傳回 AgentIdentity。
    
    # 用舊的 token1 驗證，預期應失敗（None）
    assert identity_service.verify_token(token1) is None
    
    # 用新的 token2 驗證，預期應成功（AgentIdentity）
    verified2 = identity_service.verify_token(token2)
    assert verified2 is not None
    assert verified2.agent_id == "test_agent"
