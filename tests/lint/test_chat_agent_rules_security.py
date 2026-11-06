"""`next dev` must not write instructions for AI coding agents into ToukaChat.

Since Next.js 16.4, `next dev` writes ToukaChat/AGENTS.md when it detects an AI coding agent.
The generated block tells the agent to commit that file together with its work, which
conflicts with the project's own rules for agents. `agentRules: false` in next.config turns
this off, and makes `next dev` remove a block an earlier run left behind.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NEXT_CONFIG = ROOT / "ToukaChat" / "next.config.ts"


def _config_body() -> str:
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    body = re.search(r"(?ms)^const nextConfig: NextConfig = \{\n(.*?)^\};$", text)
    assert body is not None, "next.config.ts no longer declares `const nextConfig: NextConfig`"
    return body.group(1)


def test_next_config_turns_off_agent_rules() -> None:
    # Two-space indentation is a top-level key of nextConfig; a commented-out line does not match.
    assert re.search(r"(?m)^  agentRules: false,$", _config_body())


def test_next_config_sets_agent_rules_once_and_exports_that_object() -> None:
    text = NEXT_CONFIG.read_text(encoding="utf-8")

    # Nothing else in the file may set it again, and the object Next reads is the one checked.
    assert len(re.findall(r"\bagentRules\b", text)) == 1
    assert re.findall(r"(?m)^export default .*$", text) == ["export default nextConfig;"]
