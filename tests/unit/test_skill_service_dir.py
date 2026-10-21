"""SkillService 目錄載入 + 契約測試 (ADR-0066 Phase 1).

驗證 SkillService 從 config/skills/<id>/ 目錄格式載入技能，並維持
get_skill 的 9 key 契約、.mcp.json 推導、寫入/刪除、sync_dispatcher 改寫。

策略：注入 base_dir=tmp_path + 真實 resource_manager 寫實檔，最貼近行為。
檔案 IO 一律透過 resource_manager (IO_PRISON 守則)。
conftest 的 autouse reset_for_test 會在每個測試後還原單例至沙盒。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from src.core.exceptions import ToukaException
from src.services.resource_manager import resource_manager
from src.services.skill_service import skill_service


def _write_skill(skills_dir, sid, fm_lines, body, mcp_json=None):
    """在 tmp skills 目錄寫一個 SKILL.md (+ 選填 .mcp.json)。"""
    skill_dir = skills_dir / sid
    resource_manager.make_dir(skill_dir)
    content = "---\n" + "\n".join(fm_lines) + "\n---\n" + body
    resource_manager.write_file_text(skill_dir / "SKILL.md", content)
    if mcp_json is not None:
        resource_manager.write_json(skill_dir / ".mcp.json", mcp_json, indent=2)
    return skill_dir


@pytest.fixture
def svc(tmp_path):
    """指向 tmp_path 的 SkillService 單例 (繞過沙盒 config)。"""
    skills_dir = tmp_path / "skills"
    resource_manager.make_dir(skills_dir)
    skill_service._skills_dir = skills_dir
    skill_service.config_path = str(skills_dir)
    skill_service._metadata = {}
    skill_service.load_config()
    return skill_service


# --- 載入與掃描 ---

def test_missing_dir_returns_empty(tmp_path):
    """缺 skills 目錄 → list_skills()==[] 不拋。"""
    skill_service._skills_dir = tmp_path / "nonexistent"
    skill_service.config_path = str(skill_service._skills_dir)
    skill_service._metadata = {}
    skill_service.load_config()
    assert skill_service.list_skills() == []


def test_scan_only_direct_subdirs(svc, tmp_path):
    """只認含 SKILL.md 的直接子目錄；巢狀/雜訊忽略。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "real-skill", ["name: real-skill"], "body")
    # 雜訊 1：巢狀 references/SKILL.md (不應被當成獨立 skill)
    nested = skills_dir / "real-skill" / "references"
    resource_manager.make_dir(nested)
    resource_manager.write_file_text(nested / "SKILL.md", "---\nname: nested\n---\nx")
    # 雜訊 2：散落的非 SKILL.md 檔
    resource_manager.write_file_text(skills_dir / "stray.txt", "noise")
    svc.load_config()
    ids = [s["id"] for s in svc.list_skills()]
    assert ids == ["real-skill"]


# --- get_skill 契約 ---

def test_get_skill_nine_keys(svc, tmp_path):
    """get_skill 回含全部 9 個契約 key。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "demo", ["name: demo", "display-name: 示範"], "prompt body")
    svc.load_config()
    skill = svc.get_skill("demo")
    for key in (
        "id", "name", "description", "system_prompt",
        "required_tools", "mcp_servers", "is_system", "author", "category",
    ):
        assert key in skill


def test_get_skill_system_prompt_equals_body(svc, tmp_path):
    """system_prompt == SKILL.md body。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "demo", ["name: demo"], "第一行\n第二行")
    svc.load_config()
    assert svc.get_skill("demo")["system_prompt"] == "第一行\n第二行"


def test_mcp_json_derives_servers(svc, tmp_path):
    """.mcp.json={"touka-dispatch":{...}} → mcp_servers==["touka-dispatch"]。"""
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir, "coord", ["name: coord"], "body",
        mcp_json={"touka-dispatch": {"type": "sse", "url": "http://x/sse"}},
    )
    svc.load_config()
    assert svc.get_skill("coord")["mcp_servers"] == ["touka-dispatch"]


def test_no_mcp_json_empty_servers(svc, tmp_path):
    """無 .mcp.json → mcp_servers==[]。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "plain", ["name: plain"], "body")
    svc.load_config()
    assert svc.get_skill("plain")["mcp_servers"] == []


def test_mcp_json_with_env_var_no_error(svc, tmp_path):
    """.mcp.json value 含 ${DISPATCH_URL} → 推導不報錯 (插值解耦)。"""
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir, "coord", ["name: coord"], "body",
        mcp_json={"touka-dispatch": {"type": "sse", "url": "${DISPATCH_URL}/sse"}},
    )
    svc.load_config()
    assert svc.get_skill("coord")["mcp_servers"] == ["touka-dispatch"]


def test_nonexistent_id_returns_none(svc):
    """不存在 id → None (router 靠此回 404)。"""
    assert svc.get_skill("ghost") is None


def test_allowed_tools_to_required_tools(svc, tmp_path):
    """allowed-tools → get_skill()["required_tools"]。"""
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir, "tooled",
        ["name: tooled", "allowed-tools: [Read, Bash]"], "body",
    )
    svc.load_config()
    assert svc.get_skill("tooled")["required_tools"] == ["Read", "Bash"]


def test_get_all_skills_schema_fields(svc, tmp_path):
    """get_all_skills_schema 欄位與現狀一致 (含 capability 容缺)。"""
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "demo", ["name: demo", "display-name: 示範"], "body")
    svc.load_config()
    schema = svc.get_all_skills_schema()
    assert len(schema) == 1
    entry = schema[0]
    for key in ("id", "name", "description", "category", "is_system", "capability"):
        assert key in entry
    assert entry["id"] == "demo"
    assert entry["name"] == "示範"


# --- 寫入 / 刪除 ---

def test_update_skill_writes_file(svc, tmp_path):
    """update_skill 後磁碟出現 <id>/SKILL.md，get_skill 可回讀且 system_prompt 一致。"""
    svc.update_skill("new-skill", {
        "id": "new-skill", "name": "新技能",
        "description": "desc", "system_prompt": "注入內容",
        "required_tools": [], "mcp_servers": [],
    })
    assert resource_manager.exists(tmp_path / "skills" / "new-skill" / "SKILL.md")
    skill = svc.get_skill("new-skill")
    assert skill["system_prompt"] == "注入內容"
    assert skill["name"] == "新技能"


def test_update_skill_with_mcp_servers(svc, tmp_path):
    """update_skill 帶 mcp_servers → 產生 .mcp.json，推導回 ["x"]。"""
    svc.update_skill("mcp-skill", {
        "id": "mcp-skill", "name": "MCP技能",
        "description": "", "system_prompt": "body",
        "required_tools": [], "mcp_servers": ["x"],
    })
    assert resource_manager.exists(tmp_path / "skills" / "mcp-skill" / ".mcp.json")
    assert svc.get_skill("mcp-skill")["mcp_servers"] == ["x"]


def test_update_skill_fallback_display_name(svc, tmp_path):
    """update_skill 無 display-name → frontmatter display-name fallback 用 name。"""
    svc.update_skill("fb-skill", {
        "id": "fb-skill", "name": "顯示名",
        "description": "", "system_prompt": "body",
        "required_tools": [], "mcp_servers": [],
    })
    raw = resource_manager.read_file_text(tmp_path / "skills" / "fb-skill" / "SKILL.md")
    assert "display-name: 顯示名" in raw
    assert svc.get_skill("fb-skill")["name"] == "顯示名"


def test_delete_skill_removes_dir(svc, tmp_path):
    """delete_skill 後 get_skill 回 None、目錄消失。"""
    _write_skill(tmp_path / "skills", "temp", ["name: temp"], "body")
    svc.load_config()
    svc.delete_skill("temp")
    assert svc.get_skill("temp") is None
    assert not resource_manager.exists(tmp_path / "skills" / "temp")


def test_delete_system_skill_protected(svc, tmp_path):
    """delete_skill 對 is_system=True 拋 ToukaException，目錄保留。"""
    _write_skill(
        tmp_path / "skills", "sys-skill",
        ["name: sys-skill", "is_system: true"], "body",
    )
    svc.load_config()
    with pytest.raises(ToukaException):
        svc.delete_skill("sys-skill")
    assert resource_manager.exists(tmp_path / "skills" / "sys-skill")


def test_delete_system_skill_protected_nested_metadata(svc, tmp_path):
    """新制巢狀 metadata.is_system=true 同樣受刪除保護（E35/ADR-0175）。

    生產寫入（build_frontmatter）把 is_system 收進 metadata 子欄位；上一個測試
    只涵蓋 legacy 頂層格式＝假綠——直讀頂層鍵時新制系統技能曾可被刪。
    """
    _write_skill(
        tmp_path / "skills", "sys-nested",
        ["name: sys-nested", "metadata:", "  is_system: true"], "body",
    )
    svc.load_config()
    with pytest.raises(ToukaException):
        svc.delete_skill("sys-nested")
    assert resource_manager.exists(tmp_path / "skills" / "sys-nested")


def test_delete_system_skill_protected_after_update_roundtrip(svc):
    """生產寫入路徑 round-trip：update_skill(is_system=True) → 刪除必拒（E35/ADR-0175）。"""
    svc.update_skill("sys-rt", {
        "id": "sys-rt", "name": "系統技能", "description": "",
        "system_prompt": "body", "is_system": True,
    })
    with pytest.raises(ToukaException):
        svc.delete_skill("sys-rt")
    assert svc.get_skill("sys-rt") is not None


def test_config_path_ends_with_skills(svc):
    """config_path 以 skills 結尾 (context_snapshot 契約)。"""
    assert svc.config_path.endswith("skills")


def test_load_legacy_top_level_skill(svc, tmp_path):
    """舊格式 (top-level 擴充欄位) SKILL.md 仍正確載入 (向後相容 fallback)。"""
    _write_skill(
        tmp_path / "skills", "legacy",
        ["name: legacy", "display-name: 舊技能", "is_system: true",
         "author: Old", "category: Sys"],
        "body",
    )
    svc.load_config()
    skill = svc.get_skill("legacy")
    assert skill["name"] == "舊技能"
    assert skill["is_system"] is True
    assert skill["author"] == "Old"
    entry = next(s for s in svc.list_skills() if s["id"] == "legacy")
    assert entry["name"] == "舊技能"
    assert entry["is_system"] is True


# --- apply_skills_to_request (注入邏輯不變) ---

def test_apply_skills_anthropic_system(svc, tmp_path):
    """注入 Anthropic system (含 <capabilities> 區塊)。"""
    _write_skill(tmp_path / "skills", "inj", ["name: inj"], "技能指令")
    svc.load_config()

    class _Req:
        system = "原始系統提示"

    req = _Req()
    svc.apply_skills_to_request(req, ["inj"])
    assert "<capabilities>" in req.system
    assert "技能指令" in req.system
    assert "原始系統提示" in req.system


def test_apply_skills_openai_messages(svc, tmp_path):
    """注入 OpenAI messages system role。"""
    _write_skill(tmp_path / "skills", "inj", ["name: inj"], "技能指令")
    svc.load_config()

    class _Req:
        messages = [{"role": "system", "content": "原始"}]

    req = _Req()
    svc.apply_skills_to_request(req, ["inj"])
    assert "技能指令" in req.messages[0]["content"]


def test_reset_for_test_reflects_disk(svc, tmp_path):
    """reset_for_test 改磁碟後反映新狀態 (單例韌性)。"""
    _write_skill(tmp_path / "skills", "later", ["name: later"], "body")
    skill_service._skills_dir = tmp_path / "skills"
    skill_service.config_path = str(tmp_path / "skills")
    skill_service._metadata = {}
    skill_service.load_config()
    assert "later" in [s["id"] for s in skill_service.list_skills()]


# --- sync_system_dispatcher ---

def test_sync_dispatcher_writes_skill_md(svc, tmp_path):
    """mock server_config 有 2 servers → 寫 SKILL.md，body 含 <server id=...>。"""
    cfg = {"mcpServers": {"fetch": {"command": "uvx"}, "grok": {"command": "python"}}}
    with patch.object(resource_manager, "read_json", return_value=cfg):
        svc.sync_system_dispatcher()
    md = resource_manager.read_file_text(
        tmp_path / "skills" / "system-mcp-dispatcher" / "SKILL.md"
    )
    assert '<server id="fetch">' in md
    assert '<server id="grok">' in md


def test_sync_dispatcher_none_config_noop(svc, tmp_path):
    """read_json 回 None → 不寫、log warning。"""
    with patch.object(resource_manager, "read_json", return_value=None):
        svc.sync_system_dispatcher()
    assert not resource_manager.exists(tmp_path / "skills" / "system-mcp-dispatcher")


def test_sync_dispatcher_idempotent(svc, tmp_path):
    """連兩次 idempotent (覆寫不殘留舊內容)。"""
    cfg1 = {"mcpServers": {"fetch": {"command": "uvx"}}}
    cfg2 = {"mcpServers": {"grok": {"command": "python"}}}
    with patch.object(resource_manager, "read_json", return_value=cfg1):
        svc.sync_system_dispatcher()
    with patch.object(resource_manager, "read_json", return_value=cfg2):
        svc.sync_system_dispatcher()
    md = resource_manager.read_file_text(
        tmp_path / "skills" / "system-mcp-dispatcher" / "SKILL.md"
    )
    assert '<server id="grok">' in md
    assert '<server id="fetch">' not in md


def test_sync_dispatcher_version_is_content_hash(svc, tmp_path):
    """version 為內容雜湊（非 epoch）：同 registry 重生→位元組相同（消除 git 噪音）；
    registry 變→version 與內容變。"""
    cfg1 = {"mcpServers": {"fetch": {"command": "uvx"}}}
    cfg2 = {"mcpServers": {"grok": {"command": "python"}}}
    path = tmp_path / "skills" / "system-mcp-dispatcher" / "SKILL.md"
    with patch.object(resource_manager, "read_json", return_value=cfg1):
        svc.sync_system_dispatcher()
    md1 = resource_manager.read_file_text(path)
    with patch.object(resource_manager, "read_json", return_value=cfg1):
        svc.sync_system_dispatcher()
    md2 = resource_manager.read_file_text(path)
    with patch.object(resource_manager, "read_json", return_value=cfg2):
        svc.sync_system_dispatcher()
    md3 = resource_manager.read_file_text(path)
    assert md1 == md2  # 同 registry 重生 → 位元組相同
    assert md1 != md3  # registry 變 → version 雜湊與內容變
    assert "version:" in md1
