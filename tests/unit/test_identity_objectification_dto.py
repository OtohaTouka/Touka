import pytest
from pydantic import BaseModel
from src.models.identity import AgentIdentity

class MockDTO(BaseModel):
    agent_id: AgentIdentity

class MockOptionalDTO(BaseModel):
    agent_id: AgentIdentity | None = None

def test_identity_from_str_conversion():
    """驗證 DTO 能將字串自動轉化為 AgentIdentity 物件。"""
    # 1. 直接初始化 AgentIdentity
    ident = AgentIdentity.validate_from_str("Alice")
    assert isinstance(ident, dict)
    obj = AgentIdentity(**ident)
    assert obj.agent_id == "Alice"
    assert obj.display_role == "Base"

    # 2. 透過 DTO 初始化
    dto = MockDTO(agent_id="Bob")
    assert isinstance(dto.agent_id, AgentIdentity)
    assert dto.agent_id.agent_id == "Bob"

def test_identity_optional_conversion():
    """驗證 Optional 欄位的轉化。"""
    dto = MockOptionalDTO(agent_id="Charlie")
    assert dto.agent_id.agent_id == "Charlie"
    
    dto_none = MockOptionalDTO(agent_id=None)
    assert dto_none.agent_id is None

def test_identity_object_preservation():
    """驗證如果已經是物件，則保持不變。"""
    existing = AgentIdentity.for_role(agent_id="Alice", role="Owner")
    dto = MockDTO(agent_id=existing)
    assert dto.agent_id.display_role == "Owner"
    assert dto.agent_id.agent_id == "Alice"
