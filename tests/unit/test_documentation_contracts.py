"""Current-facing documentation ratchet for ADR-0116 and the guardrail range."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CURRENT_GUIDANCE_PATHS = (
    "ToukaHeartbeat/README.md",
    "ToukaMemory/README.md",
    "ToukaProxy/README.md",
    "docs/index.md",
    "docs/architecture/CHAT_INTERFACE.md",
    "docs/architecture/IDE_INTERFACE.md",
    "docs/architecture/MEMORY_INTERFACE.md",
    "docs/architecture/PROXY_INTERFACE.md",
    "docs/guides/testing_guide.md",
    "docs/memory/agent_instructions.md",
    "docs/memory/memory_payload_example.md",
    "docs/proxy/API_endpoints.md",
    "docs/proxy/api_reference/factual_store.md",
    "docs/proxy/api_reference/legacy_store.md",
    "docs/proxy/api_reference/memory_manager.md",
    "docs/proxy/api_reference/resource_manager.md",
    "docs/proxy/api_reference/vector_store.md",
    "docs/proxy/user_manual.md",
    "tests/README.md",
)

_RETIRED_CLAIMS = (
    "支援**兩種運作模式**",
    "ToukaMemory 以獨立 MCP 伺服器運行",
    "MCP 模式降級至 `_FallbackResourceProxy`",
    "修復後兩種模式均正常",
    "整合與 MCP 獨立兩種模式",
    "整合與 MCP 模式下統一",
    "自動資源管理降級",
    "自動降級保證系統韌性",
    "自動適應 I/O 模式",
    "自動適應運作模式",
    "模式自動切換",
    "兩種環境下完全相同",
)

_REQUIRED_CONTRACT_ANCHORS = {
    "ToukaHeartbeat/README.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "ToukaMemory/README.md": ("ADR-0116", "整合模式單一部署", "fail-loud"),
    "ToukaProxy/README.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/index.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/architecture/CHAT_INTERFACE.md": (
        "ADR-0116",
        "同行程函式庫",
        "不再以獨立 MCP server",
    ),
    "docs/architecture/IDE_INTERFACE.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/architecture/MEMORY_INTERFACE.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/architecture/PROXY_INTERFACE.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/guides/testing_guide.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/memory/agent_instructions.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/memory/memory_payload_example.md": ("ADR-0116", "整合模式單一部署"),
    "docs/proxy/API_endpoints.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/proxy/api_reference/factual_store.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/proxy/api_reference/legacy_store.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/proxy/api_reference/memory_manager.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/proxy/api_reference/resource_manager.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/proxy/api_reference/vector_store.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "docs/proxy/user_manual.md": (
        "ADR-0116",
        "整合模式單一部署",
        "ResourceManager",
        "fail-loud",
    ),
    "tests/README.md": ("ADR-0116", "ResourceManager", "fail-loud"),
}

def _read_guidance(relative_path: str) -> str:
    return (_REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_current_guidance_does_not_advertise_retired_memory_modes() -> None:
    violations: list[str] = []

    for relative_path in _CURRENT_GUIDANCE_PATHS:
        content = _read_guidance(relative_path)
        for retired_claim in _RETIRED_CLAIMS:
            if retired_claim in content:
                violations.append(f"{relative_path}: {retired_claim}")

    assert violations == [], "Retired ToukaMemory claims remain:\n" + "\n".join(violations)


def test_current_guidance_carries_the_integration_only_contract() -> None:
    violations: list[str] = []

    for relative_path, required_anchors in _REQUIRED_CONTRACT_ANCHORS.items():
        content = _read_guidance(relative_path)
        missing = [anchor for anchor in required_anchors if anchor not in content]
        if missing:
            violations.append(f"{relative_path}: missing {', '.join(missing)}")

    assert violations == [], "ADR-0116 anchors are incomplete:\n" + "\n".join(violations)


def test_pre_task_protocol_names_the_complete_guardrail_range() -> None:
    agents_content = _read_guidance("AGENTS.md")
    declared_match = re.search(
        r"§18 開發禁令.*?禁令 1[–-](?P<maximum>\d+)",
        agents_content,
    )
    assert declared_match is not None, "AGENTS.md pre-task guardrail range is missing"

    guardrail_numbers = [
        int(match.group("number"))
        for match in re.finditer(
            r"^\*\s+\*\*禁令 (?P<number>\d+)(?=\*\*|\s*[（(])",
            agents_content,
            flags=re.MULTILINE,
        )
    ]
    assert guardrail_numbers, "AGENTS.md guardrail inventory is empty"
    assert guardrail_numbers == list(range(1, guardrail_numbers[-1] + 1))
    assert int(declared_match.group("maximum")) == guardrail_numbers[-1]
