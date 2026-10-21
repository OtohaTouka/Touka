"""ADR-0088 — Permissions Registry Guard（#32）自我測試。

以 importlib 載入守衛、餵 in-memory 資料給純函式；並驗證現況 repo 全綠
（確認 permissions.json／.example 的工具權限皆對應真實 tool_registry）。
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "scripts" / "guard_permissions_registry.py"


def _load():
    spec = importlib.util.spec_from_file_location("guard_perm_reg_under_test", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass 需能由 sys.modules 解析 __module__
    spec.loader.exec_module(mod)
    return mod


def test_extract_registry_tool_names():
    g = _load()
    src = (
        "other = 1\n"
        "tool_registry = {\n"
        "    'research': research,\n"
        "    'memory_tools': memory_tools,\n"
        "    'bash': lambda **k: None,\n"
        "}\n"
    )
    assert g.extract_registry_tool_names(src) == {"research", "memory_tools", "bash"}


def test_find_invalid_flags_unknown_tool():
    g = _load()
    perms = {
        "roles": {
            "Subagent": {
                "allowed_tools": ["research", "memory_manager"],
                "denied_tools": [],
            }
        }
    }
    bad = g.find_invalid_tool_refs(perms, {"research", "memory_tools"}, Path("x.json"))
    assert [v.name for v in bad] == ["memory_manager"]


def test_find_invalid_clean_with_wildcard_and_denied():
    g = _load()
    perms = {
        "roles": {
            "Owner": {"allowed_tools": ["*"], "denied_tools": []},
            "Subagent": {"allowed_tools": ["research"], "denied_tools": ["update_my_soul"]},
            "Base": {"allowed_tools": [], "denied_tools": ["*"]},
        }
    }
    valid = {"research", "update_my_soul"}
    assert g.find_invalid_tool_refs(perms, valid, Path("x.json")) == []


def test_global_denied_tools_are_checked_against_registry():
    g = _load()
    perms = {"roles": {}, "global_denied_tools": ["research", "ghost_tool"]}
    bad = g.find_invalid_tool_refs(perms, {"research"}, Path("x.json"))
    assert [(v.role, v.field, v.name) for v in bad] == [
        ("<global>", "global_denied_tools", "ghost_tool")
    ]


def test_tool_risk_flags_unknown_tool_and_bad_class():
    """tool_risk（H1）：鍵打錯字＋非法風險類（含聚合條目 actions）都要抓。"""
    g = _load()
    policies = {
        "tool_risk": {
            "default": "dangerous",
            "tools": {
                "research": "read_only",
                "reserch": "read_only",  # 打錯字
                "vault_tools": {"action_param": "action", "actions": {"read": "readonly"}},
            },
        }
    }
    bad = g.find_invalid_tool_risk_refs(policies, {"research", "vault_tools"}, Path("a.json"))
    names = [v.name for v in bad]
    assert "reserch" in names  # 未知工具名
    assert "vault_tools.read=readonly" in names  # 非法風險類（正確值為 read_only）


def test_tool_risk_parity_flags_unclassified_tool():
    """parity 棘輪：registry 有、分類表沒有 → 違規（新工具必須顯式分類）。"""
    g = _load()
    policies = {"tool_risk": {"default": "dangerous", "tools": {"research": "read_only"}}}
    bad = g.find_invalid_tool_risk_refs(policies, {"research", "brand_new_tool"}, Path("a.json"))
    assert [v.name for v in bad] == ["brand_new_tool"]
    assert "parity" in bad[0].field


def test_tool_risk_missing_block_is_violation():
    """整個 tool_risk 區塊缺席（舊 read_only_tools 形態）＝違規（防設定檔忘記遷移）。"""
    g = _load()
    policies = {"read_only_tools": ["research"]}
    bad = g.find_invalid_tool_risk_refs(policies, {"research"}, Path("a.json"))
    assert bad and bad[0].field == "tool_risk"


def test_real_repo_permissions_and_fallback_clean_with_mutation(monkeypatch):
    """設定檔與 fallback 皆乾淨；在 fallback 植入死字串時 Guard #32 必須變紅。"""
    g = _load()
    assert g.run_all() == []

    mutated = copy.deepcopy(g.DEFAULT_PERMISSIONS_CONFIG)
    mutated["roles"]["Subagent"]["allowed_tools"] = ["ghost_tool"]
    monkeypatch.setattr(g, "DEFAULT_PERMISSIONS_CONFIG", mutated)

    bad = g.run_all()
    assert any(v.file == g._FALLBACK_PERMISSIONS_SOURCE and v.name == "ghost_tool" for v in bad)


# ── MCP 條目形狀（ADR-0195 軸 B）─────────────────────────────────────────


def test_mcp_refs_accept_all_legal_shapes():
    """四種合法形態全部放行——太嚴會讓正常設定紅，比漏抓更早被繞過。"""
    g = _load()
    perms = {
        "roles": {
            "Owner": {"allowed_mcp_servers": ["*"]},
            "Subagent": {
                "allowed_mcp_servers": ["stripe", "github/list_issues", "zapier/*"],
                "denied_mcp_tools": ["*", "stripe/create_refund", "github/*"],
            },
        },
        "global_denied_mcp_tools": ["*", "stripe/create_refund"],
    }
    assert g.find_invalid_mcp_tool_refs(perms, Path("x.json")) == []


def test_mcp_refs_reject_dead_string_shapes():
    """形狀就不可能匹配的死字串——寫下去等於權限「看似有、實則無」。"""
    g = _load()
    perms = {
        "roles": {
            "Subagent": {
                "allowed_mcp_servers": ["/tool", "a//b", " stripe", "srv/", "a/b/c", ""],
                "denied_mcp_tools": [],
            }
        }
    }
    bad = g.find_invalid_mcp_tool_refs(perms, Path("x.json"))
    assert {v.name for v in bad} == {"/tool", "a//b", " stripe", "srv/", "a/b/c", ""}
    assert all(v.reason for v in bad), "形狀違規必須帶可讀原因"


def test_mcp_refs_accept_bare_wildcard_on_deny_side():
    """P2：角色 deny self-only，裸 `*` 是局部 deny-all，不再沿繼承鏈擴散。"""
    g = _load()
    perms = {"roles": {"Owner": {"allowed_mcp_servers": ["*"], "denied_mcp_tools": ["*"]}}}
    assert g.find_invalid_mcp_tool_refs(perms, Path("x.json")) == []


def test_global_denied_mcp_tools_are_shape_checked():
    g = _load()
    perms = {"roles": {}, "global_denied_mcp_tools": ["*", "/tool"]}
    bad = g.find_invalid_mcp_tool_refs(perms, Path("x.json"))
    assert [(v.role, v.field, v.name) for v in bad] == [
        ("<global>", "global_denied_mcp_tools", "/tool")
    ]


def test_mcp_refs_reject_non_string_entries():
    """設定檔可能寫成數字或 null——非字串一律違規，不得 crash 也不得靜默放行。"""
    g = _load()
    perms = {"roles": {"Subagent": {"allowed_mcp_servers": [1, None]}}}
    bad = g.find_invalid_mcp_tool_refs(perms, Path("x.json"))
    assert len(bad) == 2


def test_run_all_actually_calls_the_mcp_shape_check(tmp_path, monkeypatch):
    """接線測試：`run_all` 必須真的呼叫形狀檢查。

    純函式全綠但沒被接進 `run_all`＝守衛存在卻從不執法，而現況 repo 設定乾淨，
    `test_real_repo_permissions_clean` 對這個變異零判別力。
    """
    g = _load()
    bad_cfg = tmp_path / "permissions.json"
    bad_cfg.write_text(
        '{"roles": {"Subagent": {"allowed_tools": [], "denied_tools": [],'
        ' "denied_mcp_tools": ["/tool"]}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(g, "_PERMISSION_FILES", [bad_cfg])
    monkeypatch.setattr(g, "_AUTH_POLICY_FILES", [])

    names = [v.name for v in g.run_all()]
    assert "/tool" in names


def test_mcp_refs_allow_misspelled_tool_name():
    """誠實邊界：存在性驗不了——拼錯的工具名形狀合法，本守衛必須放行。

    這條把「守衛能做什麼」也寫成合約：日後有人以為它擋得住拼字錯誤而少寫測試時，
    這裡是唯一的書面反證。
    """
    g = _load()
    perms = {"roles": {"Owner": {"denied_mcp_tools": ["stripe/creat_refund"]}}}
    assert g.find_invalid_mcp_tool_refs(perms, Path("x.json")) == []


def test_duplicate_resolution_must_remain_dangerous():
    guard = _load()
    policy = {"tool_risk": {"tools": {"memory_tools": {"actions": {"manage_dups": "read_only"}}}}}
    assert guard.find_invalid_tool_risk_refs(policy, {"memory_tools"}, Path("example.json"))


def test_public_sources_never_probe_private_settings(tmp_path, monkeypatch):
    guard = _load()
    permission_paths = [tmp_path / p.name for p in guard._PERMISSION_FILES]
    policy_paths = [tmp_path / p.name for p in guard._AUTH_POLICY_FILES]
    (tmp_path / "permissions.json.example").write_text('{"roles":{}}', encoding="utf-8")
    (tmp_path / "auth_policies.json.example").write_text(
        '{"tool_risk":{"tools":{"example":"safe"}}}', encoding="utf-8"
    )
    builtin = tmp_path / "builtin.py"
    builtin.write_text("tool_registry = {'example': example}", encoding="utf-8")
    monkeypatch.setattr(guard, "_PERMISSION_FILES", permission_paths)
    monkeypatch.setattr(guard, "_AUTH_POLICY_FILES", policy_paths)
    monkeypatch.setattr(guard, "_BUILTIN_INIT", builtin)
    monkeypatch.setattr(guard, "DEFAULT_PERMISSIONS_CONFIG", {"roles": {}})
    original_exists, original_read = Path.exists, Path.read_text

    def exists(path):
        assert path.name not in {"permissions.json", "auth_policies.json"}, "private probe"
        return original_exists(path)

    def read(path, *args, **kwargs):
        assert path.name not in {"permissions.json", "auth_policies.json"}, "private read"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read)
    assert guard.run_all() == []


@pytest.mark.parametrize("field", ["_PERMISSION_FILES", "_AUTH_POLICY_FILES"])
def test_public_sources_required_example_missing_fails(tmp_path, monkeypatch, field):
    guard = _load()
    missing = tmp_path / "required.json.example"
    monkeypatch.setattr(guard, "_PERMISSION_FILES", [])
    monkeypatch.setattr(guard, "_AUTH_POLICY_FILES", [])
    monkeypatch.setattr(guard, field, [missing])
    assert any(v.file == missing for v in guard.run_all())
