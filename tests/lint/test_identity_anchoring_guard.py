import ast
import pytest
import importlib.util
import sys
from pathlib import Path

# 動態載入 scripts/lint_ssot.py
root = Path(__file__).parent.parent.parent
spec = importlib.util.spec_from_file_location("lint_ssot", root / "scripts" / "lint_ssot.py")
lint_ssot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint_ssot)
IdentitySovereigntyRule = lint_ssot.IdentitySovereigntyRule

def test_identity_anchoring_violation():
    code = """
def update_soul(soul_id: str, data: dict):
    pass

async def delete_agent(agent_id: str):
    pass
    """
    tree = ast.parse(code)
    # Simulate being in a service file
    rule = IdentitySovereigntyRule("src/services/test_service.py")
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rule.check_FunctionDef(node)
            
    assert len(rule.violations) == 2
    assert "soul_id: str" in rule.violations[0]["msg"]
    assert "agent_id: str" in rule.violations[1]["msg"]

def test_identity_anchoring_success():
    code = """
from src.models.identity import AgentIdentity

def update_soul(identity: AgentIdentity, data: dict):
    pass

async def delete_agent(identity: AgentIdentity):
    pass
    """
    tree = ast.parse(code)
    rule = IdentitySovereigntyRule("src/services/test_service.py")
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rule.check_FunctionDef(node)
            
    assert len(rule.violations) == 0

def test_identity_anchoring_noqa():
    code = """
def update_soul(soul_id: str, data: dict): # noqa: IDENTITY_SSOT
    pass
    """
    # Simulate noqa registry
    ignored_lines = {2: {"IDENTITY_SSOT"}}
    tree = ast.parse(code)
    rule = IdentitySovereigntyRule("src/services/test_service.py", ignored_lines=ignored_lines)
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rule.check_FunctionDef(node)
            
    assert len(rule.violations) == 0
