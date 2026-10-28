"""設定範本防線（config-drift-audit 收尾，ADR-0095）。

防止「設定 .example 漂移」再發生——稽核曾發現多種漂移逃過守衛/測試：範本帶模型沒有的
過時鍵、種出連不上的 MCP server、範本與 schema 不同步。本測試把「設定範本品質」納入 CI：

  1. 所有 config/*.json.example 為合法 JSON。
  2. 有 pydantic 模型的設定，其 .example 能通過 model_validate（型別/必填正確）。
  3. 有模型的 .example 不得含「模型沒有的鍵」（meta 鍵 version/author/description 除外）——
     擋「範本帶過時/不存在設定」（如曾經的 autonomy `pulse_cooldown_seconds`）。
  4. server_config.json.example 的 MCP 啟動指令須指向真實存在的檔案/模組——
     擋「種出連不上的 server」（如曾經的 `touka_memory.server` / `research_server.py`）。
  5. ToukaProxy/.env.example 文件化的 env 必須是 Settings 欄位（或白名單）——
     擋「範本宣傳一個模型沒有、會被 extra='ignore' 靜默丟棄的 env」（如曾經的
     `RELOAD_MODELS_ON_REQUEST` / `FORCE_AGENT_ID` 類過時失效設定，防線 #2）。

已知限制（誠實記錄）：不檢查「模型有、範本卻省略的『有預設值』欄位」（如 summary_model）。
帶預設的欄位省略是合法常態，強制列舉會大量誤報；此類靠人工/審查補。env 平價僅檢
ToukaProxy/.env.example（唯一有 pydantic Settings 的 env）；ToukaMemory/根/前端 env 無對應模型。
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from src.config import Settings
from src.models.autonomy import AutonomyConfigModel
from src.models.hooks import HooksConfig
from src.models.identity import IdentityMapConfig
from src.models.permissions import PermissionsConfig
from src.models.safety import AuthPoliciesConfig
from src.models.sliding_window import SlidingWindowConfig
from src.routers.group_settings import GroupWhitelist

# 直接指向 repo 內真實設定目錄（不經 get_config_dir，避免測試沙箱重導向）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_DIR = _REPO_ROOT / "ToukaProxy" / "config"
_PROXY_ROOT = _REPO_ROOT / "ToukaProxy"
_META_KEYS = {"version", "author", "description"}

# 設定檔名（不含 .example）→ 對應 pydantic 模型
_MODELED = {
    "auth_policies.json": AuthPoliciesConfig,
    "autonomy.json": AutonomyConfigModel,
    "permissions.json": PermissionsConfig,
    "sliding_window.json": SlidingWindowConfig,
    "hooks.json": HooksConfig,  # ADR-0292
    # ADR-0147：靈魂→角色 SSOT，加入 ESSENTIAL_CONFIGS 自癒（缺檔全塌 Base）。
    # 由 .example 逐位元複製到 live，故 .example 須通過 IdentityMapConfig 驗證。
    "identity_map.json": IdentityMapConfig,
    # group_whitelist 由 ensure_configs 以 shutil.copy2 從 .example 逐位元複製到 live，
    # 且 GroupWhitelist 為 extra="forbid"。故 .example 不得含任何模型外的鍵（連 _comment
    # 說明鍵也不行，否則自癒生成的 live 檔會被執行期驗證擋下 → GET 回 CONFIG_LOAD_FAILED）。
    # test_modeled_example_validates 以原始資料 model_validate（不剝 meta 鍵），可精準擋下。
    "group_whitelist.json": GroupWhitelist,
}

# errors.json 無 pydantic 模型（是 {code: {id,status,msg,i18n}} 字典），故不入 _MODELED，
# 改以專屬結構檢查＋與生成鏡像對齊納入 Guard #34。error_codes.ts 由 scripts/sync_errors.py
# 從 live errors.json 生成；.example 須與其一致，杜絕「改了錯誤碼卻忘記同步 .example」的
# 漂移——正是本檔曾缺 .example、讓 fresh checkout 的 ErrorCode enum 退回單一 SYSTEM_ERROR、
# ~269 測試崩潰的根因。
_ERROR_CODES_TS = _REPO_ROOT / "ToukaChat" / "src" / "lib" / "error_codes.ts"
_TS_ENTRY_RE = re.compile(
    r"(?P<key>[A-Z0-9_]+):\s*\{\s*"
    r"id:\s*(?P<id>\d+),\s*"
    r"status:\s*(?P<status>\d+),\s*"
    r'msg:\s*"(?P<msg>[^"]*)",\s*'
    r'i18n:\s*"(?P<i18n>[^"]*)"\s*\}',
    re.DOTALL,
)


def _load_example(name: str) -> dict:
    return json.loads((_CONFIG_DIR / f"{name}.example").read_text(encoding="utf-8"))


def _parse_error_codes_ts() -> dict:
    """反解 error_codes.ts 的 ERRORS 物件為 {code: {id,status,msg,i18n}}。"""
    text = _ERROR_CODES_TS.read_text(encoding="utf-8")
    block = text[text.index("export const ERRORS = {") : text.index("} as const;")]
    return {
        m.group("key"): {
            "id": int(m.group("id")),
            "status": int(m.group("status")),
            "msg": m.group("msg"),
            "i18n": m.group("i18n"),
        }
        for m in _TS_ENTRY_RE.finditer(block)
    }


def test_all_config_examples_valid_json():
    examples = sorted(_CONFIG_DIR.glob("*.json.example"))
    assert examples, "找不到任何 config/*.json.example"
    for ex in examples:
        try:
            json.loads(ex.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            pytest.fail(f"{ex.name} 非合法 JSON：{e}")


@pytest.mark.parametrize("name,model", list(_MODELED.items()))
def test_modeled_example_validates(name, model):
    """.example 能通過對應 pydantic 模型。"""
    model.model_validate(_load_example(name))


@pytest.mark.parametrize("name,model", list(_MODELED.items()))
def test_modeled_example_no_stale_keys(name, model):
    """.example 不得帶模型未定義的鍵（meta 除外）——擋過時/不存在設定漂移。"""
    data = _load_example(name)
    allowed = set(model.model_fields) | _META_KEYS
    stale = set(data) - allowed
    assert not stale, f"{name}.example 含模型未定義的鍵（疑過時漂移）：{sorted(stale)}"


def test_errors_example_structure_valid():
    """errors.json.example 每碼須有 int id/status、非空 msg、str i18n，且 id 唯一。

    errors.json 是錯誤碼跨端 SSOT（禁令 44 / ADR-0014），無 pydantic 模型，故以此
    結構檢查納入 Guard #34。它是 gitignore 的 live 檔，靠此 .example 由 bootstrap
    （config 匯入時 ensure_system_bootstrap glob 還原）自癒，缺它 fresh checkout 會崩。
    """
    data = _load_example("errors.json")
    assert data, "errors.json.example 為空"
    ids = []
    for code, d in data.items():
        assert isinstance(d.get("id"), int), f"{code}.id 非整數"
        assert isinstance(d.get("status"), int), f"{code}.status 非整數"
        assert isinstance(d.get("msg"), str) and d["msg"], f"{code}.msg 須為非空字串"
        assert isinstance(d.get("i18n"), str), f"{code}.i18n 須為字串"
        ids.append(d["id"])
    assert len(ids) == len(set(ids)), "errors.json.example 有重複 id"


def test_errors_example_matches_generated_ts():
    """errors.json.example 須與 sync_errors.py 生成的 error_codes.ts 完全一致（防漂移）。

    error_codes.ts 由 live errors.json 生成（Guard #18）；.example 為 live 的種子，
    三者須同源。此測試確保新增/修改錯誤碼時 .example 同步更新，杜絕再次漂移。
    """
    example = _load_example("errors.json")
    generated = _parse_error_codes_ts()
    assert example == generated, (
        "errors.json.example 與 error_codes.ts 不一致——改了錯誤碼請同步更新 .example "
        "並重跑 scripts/sync_errors.py。"
        f"\n  僅在 .example：{sorted(set(example) - set(generated))}"
        f"\n  僅在 .ts：{sorted(set(generated) - set(example))}"
    )


def test_errors_example_contains_mcp_oauth_contract():
    """Fresh checkout 必須具備 ADR-0206 的完整、穩定 OAuth 錯誤合約。"""
    errors = _load_example("errors.json")
    expected = {
        "MCP_OAUTH_DISABLED": (5001, 503),
        "MCP_OAUTH_INTERACTIVE_REQUIRED": (5002, 403),
        "MCP_OAUTH_CALLBACK_METHOD": (5003, 405),
        "MCP_OAUTH_CALLBACK_CONTENT_TYPE": (5004, 415),
        "MCP_OAUTH_CALLBACK_TOO_LARGE": (5005, 413),
        "MCP_OAUTH_CALLBACK_INVALID": (5006, 400),
        "MCP_OAUTH_STATE_INVALID": (5007, 400),
        "MCP_OAUTH_ISSUER_MISMATCH": (5008, 400),
        "MCP_OAUTH_STALE_AUTHORIZATION": (5009, 409),
        "MCP_OAUTH_UPSTREAM_ERROR": (5010, 502),
        "MCP_OAUTH_RESTORE_CONFLICT": (5011, 409),
        "MCP_OAUTH_KEY_ROTATION_UNAVAILABLE": (5012, 409),
        "MCP_OAUTH_KEY_ROTATION_CONFLICT": (5013, 409),
        "MCP_OAUTH_CALLBACK_AMBIGUOUS": (5014, 400),
        "MCP_OAUTH_CALLBACK_ROUTE": (5015, 400),
        "MCP_OAUTH_CLOCK_ROLLBACK": (5016, 503),
    }
    actual = {code: (errors[code]["id"], errors[code]["status"]) for code in expected}
    assert actual == expected


def test_permissions_define_debugger_role():
    """permissions.json 與 .example 都須定義 Debugger 角色（繼承 Owner）。

    軌B 生產保真防線：identity_map 把 DebugAgent 映射到 Debugger，若該角色未定義會
    退回空權限 → ToolLayer 過濾掉全部工具 → 計分卡的模型一個工具都收不到（called: []）。
    擋「有人移除 Debugger 角色」回歸。
    """
    for name in ("permissions.json", "permissions.json.example"):
        data = json.loads((_CONFIG_DIR / name).read_text(encoding="utf-8"))
        roles = data.get("roles", {})
        assert "Debugger" in roles, f"{name} 缺 Debugger 角色（DebugAgent 會退回空權限）"
        assert "Owner" in roles["Debugger"].get("inherits", []), (
            f"{name} 的 Debugger 應 inherits Owner 以取得全工具面"
        )
        # Owner 須能用任意技能（否則 can_use_skill 全 False，技能/docgen 無法授權）。
        assert "*" in roles["Owner"].get("allowed_skills", []), (
            f"{name} 的 Owner 應有 allowed_skills=['*']（技能系統前提）"
        )


def test_server_config_example_commands_resolve():
    """server_config.json.example 的 MCP 啟動指令須指向真實檔案/模組（擋壞種子）。"""
    data = _load_example("server_config.json")
    for sid, conf in (data.get("mcpServers") or {}).items():
        if conf.get("command") != "python":
            continue  # 外部執行檔（uvx/npx 等）不在本檢查範圍
        args = conf.get("args") or []
        if len(args) >= 2 and args[0] == "-m":
            module = args[1]
            try:
                spec = importlib.util.find_spec(module)
            except ModuleNotFoundError:
                spec = None
            assert spec is not None, f"server '{sid}'：模組 '{module}' 不存在（壞種子）"
        elif args:
            assert (_PROXY_ROOT / args[0]).exists(), (
                f"server '{sid}'：腳本 '{args[0]}' 不存在（壞種子）"
            )


# ── 防線 #2：.env.example ↔ Settings 平價 ────────────────────────────────────

# 行首（可帶 # 註解）的大寫 env 鍵：擷取 `KEY=` 與 `# KEY=` 兩種形式。
_ENV_KEY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")

# ToukaProxy/.env.example 中合法存在、但不屬於 Settings 模型的 env（測試/整合期，
# 經 os.environ 直接讀取）。新增此類 env 到範本時，請一併加入此白名單（強制有意識的決定）。
_ENV_EXAMPLE_NON_SETTINGS = {
    "MEMORY_SYSTEM_SRC_DIR",  # 整合模式定位 ToukaMemory 源碼；bootstrap/memory 經 os.environ 讀取
    "TEST_PROXY_LOG_PATH",  # 測試腳本路徑（os.environ）
    "TEST_DB_PATH_MEMORY",
    "TEST_DB_PATH_PROXY",
}


def _parse_env_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for line in text.splitlines():
        m = _ENV_KEY_RE.match(line)
        if m:
            keys.add(m.group(1))
    return keys


def test_proxy_env_example_keys_in_settings():
    """ToukaProxy/.env.example 文件化的 env 必須是 Settings 欄位（或白名單）。

    擋「範本宣傳一個 Settings 沒有、會被 extra='ignore' 靜默丟棄的 env」——正是
    RELOAD_MODELS_ON_REQUEST / FORCE_AGENT_ID 那類過時/失效設定（防線 #2）。
    """
    text = (_PROXY_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = _parse_env_keys(text)
    allowed = set(Settings.model_fields) | _ENV_EXAMPLE_NON_SETTINGS
    stale = documented - allowed
    assert not stale, (
        f"ToukaProxy/.env.example 文件化了 Settings 沒有的 env（會被 extra='ignore' "
        f"靜默忽略，疑過時/失效）：{sorted(stale)}。"
        f"若為刻意的非-Settings env，請加入 _ENV_EXAMPLE_NON_SETTINGS 白名單。"
    )
