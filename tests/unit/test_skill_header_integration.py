"""ADR-0059 structural tests: x-touka-skills header integration (TDD).

Validates that the entire frontend pipeline for sending active skills
is wired up correctly:
  connections.ts  → ChatOptions.active_skills + x-touka-skills header
  useChatStream.ts → reads session.active_skills and passes to options
  SkillSelector.tsx → UI component exists and uses the correct endpoint
"""

from pathlib import Path
import re

CONNECTIONS_PATH = Path("ToukaChat/src/lib/connections.ts")
CHAT_STREAM_PATH = Path("ToukaChat/src/hooks/useChatStream.ts")
SKILL_SELECTOR_PATH = Path("ToukaChat/src/components/chat/SkillSelector.tsx")
CHAT_AREA_PATH = Path("ToukaChat/src/components/chat/ChatArea.tsx")


def test_connections_chat_options_has_active_skills():
    """ChatOptions interface must declare active_skills?: string[]."""
    content = CONNECTIONS_PATH.read_text(encoding="utf-8")
    assert "active_skills" in content, (
        "connections.ts: ChatOptions must include active_skills field"
    )


def test_connections_sends_x_touka_skills_header():
    """connections.ts must set x-touka-skills header when active_skills present."""
    content = CONNECTIONS_PATH.read_text(encoding="utf-8")
    assert "x-touka-skills" in content, (
        "connections.ts must build and send the x-touka-skills header"
    )


def test_usechatstream_reads_active_skills_from_session():
    """useChatStream must read active_skills from session and pass to fetchChatStream."""
    content = CHAT_STREAM_PATH.read_text(encoding="utf-8")
    assert "active_skills" in content, (
        "useChatStream.ts must reference active_skills when calling fetchChatStream"
    )


def test_skill_selector_component_exists():
    """SkillSelector component file must be created."""
    assert SKILL_SELECTOR_PATH.exists(), (
        "ToukaChat/src/components/chat/SkillSelector.tsx must exist"
    )


def test_skill_selector_uses_skills_list_endpoint():
    """SkillSelector must fetch available skills from /v1/skills/list."""
    content = SKILL_SELECTOR_PATH.read_text(encoding="utf-8")
    assert "/v1/skills/list" in content, (
        "SkillSelector.tsx must call /v1/skills/list to get available skills"
    )


def test_skill_selector_uses_fetch_touka_not_raw_fetch():
    """SkillSelector must use fetchTouka() (FETCH_SSOT), never raw fetch()."""
    content = SKILL_SELECTOR_PATH.read_text(encoding="utf-8")
    assert "fetchTouka" in content, (
        "SkillSelector.tsx must use fetchTouka() (FETCH_SSOT guard)"
    )
    # Raw fetch() calls are banned outside connections.ts
    raw_fetch_calls = re.findall(r"(?<!\w)fetch\(", content)
    assert len(raw_fetch_calls) == 0, (
        f"SkillSelector.tsx must not call raw fetch() — found {len(raw_fetch_calls)} call(s). "
        "Use fetchTouka() instead (FETCH_SSOT)."
    )


def test_chat_area_imports_skill_selector():
    """ChatArea.tsx must import and render SkillSelector."""
    content = CHAT_AREA_PATH.read_text(encoding="utf-8")
    assert "SkillSelector" in content, (
        "ChatArea.tsx must import and use <SkillSelector /> component"
    )


def test_skill_selector_updates_session_active_skills():
    """SkillSelector must update session.active_skills (not a local-only state)."""
    content = SKILL_SELECTOR_PATH.read_text(encoding="utf-8")
    assert "active_skills" in content, (
        "SkillSelector.tsx must read/write session.active_skills as SSOT"
    )
    assert "syncSession" in content, (
        "SkillSelector.tsx must call syncSession to persist skill selection"
    )
