"""ADR-0112：設定快照（Config Snapshot）匯入／匯出服務。

可攜、**零機密、零使用者資料**的「系統設定知識」快照。與既有兩者明確區隔：
  - 系統備份（ADR-0108/9）＝全加密災難復原（含 .env＋資料庫＋靈魂）。
  - GDPR 匯出（ADR-0110）＝單一使用者自己的對話／記憶。
本服務只處理 admin 可調的 ``config/*.json`` 旋鈕，用於跨機器克隆／版本化／還原已知良好基線。

安全核心（縱深防禦）：
  - **EXCLUDE 硬規則**：never serialize ``.env`` 機密、``users.json``、``identity_map``、
    ``permissions.agent_mappings``（綁定本機 agent ID）、``heartbeat_whitelist``、靈魂／個資／DB。
  - **機密遮罩**：MCP ``env`` 值與連線 ``apiKey`` 於匯出時遮罩；匯入時遇遮罩佔位值一律**跳過不
    覆寫**（絕不用 ``••••`` 假值蓋掉目標的真金鑰）。
  - **完整性**：manifest 內 ``sha256`` 對正規化 sections 取雜湊；匯入時重算比對。
  - **原生判準／合併策略**由單一 ``SECTIONS`` 註冊表（SSOT）宣告，杜絕各區散落硬編。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.services.mcp_isolation_store import IsolationRecord

from src.core.exceptions import ErrorCode, ToukaException
from src.services.agent_service import agent_service
from src.services.autonomy import autonomy_service
from src.services.backup_crypto import encrypt_backup_zip
from src.services.mcp_isolation_store import (
    McpIsolationStore,
    delete,
    env_changed,
    reconcile_saved_entries,
)
from src.services.models import model_manager
from src.services.permission_service import permission_service
from src.services.resource_manager import resource_manager
from src.services.sliding_window_service import sliding_window_service
from src.utils import config_utils
from src.utils.config_utils import read_config_json, save_config_json
from src.utils.secrets import is_masked, mask_value
from src.utils.time_utils import format_epoch_to_taipei, get_current_epoch

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
EXPORT_KIND = "settings_snapshot"
_SNAPSHOT_ARCNAME = "touka-settings.json"

# 體積上限（鏡像 GDPR _MAX_* 風格）：設定極小，過大者拒絕＝可疑/損毀。
_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024  # 2 MiB
_MAX_SECTION_BYTES = 512 * 1024  # 512 KiB


# ── Section 註冊表（SSOT：包含/合併策略/轉換/熱載入皆宣告於此） ───────────────────


@dataclass(frozen=True)
class SectionSpec:
    """單一設定區的可攜性宣告。"""

    key: str
    """envelope 內的 section 名。"""
    filename: str
    """``config/`` 下的來源檔名。"""
    strategy: str
    """合併策略：``dict``（深合併）／``list``（依 id upsert）／``replace``（整檔取代）。"""

    # --- list 策略 ---
    wrapper: str | None = None
    """list 包裹鍵（如 models.json 的 ``models``）。"""
    id_field: str = "id"
    only_existing: bool = False
    """True＝只更新目標已存在的同 id 項，不新增（如原生模型設定）。"""
    item_fields: tuple[str, ...] | None = None
    """限定可複製的欄位（None＝全部）。"""

    # --- 轉換 ---
    export_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """匯出轉換：raw 設定檔 → 匯出資料（脫敏／遮罩／剔除執行期欄位）。"""
    import_clean_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    """匯入清洗：(incoming, current) → 清洗後 incoming（跳過遮罩機密等）。"""

    # --- 生命週期 ---
    reload_key: str | None = None
    """套用後呼叫的熱載入器鍵（見 ``_RELOADERS``）；None＝讀取時即時生效或需重啟。"""
    requires_restart: bool = False
    """True＝模組啟動時載入、無熱載入路徑，套用後須重啟才完全生效。"""
    after_save: Callable[[], Any] | None = None
    """寫入後立刻同步呼叫（不是 best-effort 的熱載入，失敗不吞）。"""
    before_write: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
    """寫入前呼叫 ``(current, merged)``；失敗就不寫這一區（MCP 隔離設定檔先清新名稱的舊紀錄）。"""
    after_write: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
    """寫入後呼叫 ``(current, merged)``（MCP 隔離設定檔清掉被移除名稱的紀錄）。"""


# ── 匯出轉換（脫敏／遮罩／剔除執行期欄位） ──────────────────────────────────────

_MODEL_EXPORT_FIELDS = ("id", "enabled", "visibility", "temperature", "top_p", "top_k")
# 匯入時只把設定欄位套到既有同 id 模型（id 僅用於配對、不複製）。顯式定義避免切片耦合脆弱。
_MODEL_IMPORT_FIELDS = _MODEL_EXPORT_FIELDS[1:]


def _export_models(raw: dict[str, Any]) -> dict[str, Any]:
    """只帶 id＋設定旋鈕；剔除 last_seen/status 等執行期狀態。"""
    models = []
    for m in raw.get("models", []):
        if not isinstance(m, dict) or "id" not in m:
            continue
        models.append({k: m[k] for k in _MODEL_EXPORT_FIELDS if k in m})
    return {"models": models}


def _export_permissions(raw: dict[str, Any]) -> dict[str, Any]:
    """剔除 ``agent_mappings``（綁定本機 agent ID，跨機器是鬼參照）。"""
    return {k: v for k, v in raw.items() if k != "agent_mappings"}


def _strip_url_credentials(url: str) -> str:
    """移除 URL 內嵌帳密（``https://user:pass@host`` → ``https://host``），避免匯出洩漏。"""
    return re.sub(r"(://)[^/@\s]+@", r"\1", url)


def _export_connections(raw: dict[str, Any]) -> dict[str, Any]:
    """``apiKey`` 一律 null（機密只在 .env）；``url`` 去除內嵌帳密（縱深防禦）。"""
    out = copy.deepcopy(raw)
    for conn in out.get("connections", []):
        if not isinstance(conn, dict):
            continue
        if "apiKey" in conn:
            conn["apiKey"] = None
        if isinstance(conn.get("url"), str):
            conn["url"] = _strip_url_credentials(conn["url"])
    return out


def _export_mcp_servers(raw: dict[str, Any]) -> dict[str, Any]:
    """遮罩每台 MCP 伺服器的 ``env`` 機密值（保留鍵名，讓匯入端知道要填哪些）。"""
    out = copy.deepcopy(raw)
    for srv in out.get("mcpServers", {}).values():
        if isinstance(srv, dict) and isinstance(srv.get("env"), dict):
            srv["env"] = {k: mask_value(k, str(v)) for k, v in srv["env"].items()}
    return out


def _export_backup_schedule(raw: dict[str, Any]) -> dict[str, Any]:
    """剔除 ``last_run_epoch``（排程器執行期狀態）。"""
    return {k: v for k, v in raw.items() if k != "last_run_epoch"}


# ── 匯入清洗（遮罩機密一律從目標既有值「回填」：merge 與 replace 皆不以假值覆寫真金鑰） ──


def _clean_mcp_servers(incoming: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """把 incoming 中遮罩／非法的 env 機密「回填」為目標既有真值；無既有則移除該鍵。

    回填（非單純丟棄）使 **merge 與 replace 兩種模式皆不會以遮罩假值或 null 覆寫真金鑰**：
    merge 時回填值＝目標值（深合併 no-op）；replace 時整檔取代但機密已是目標真值。
    """
    cleaned = copy.deepcopy(incoming)
    cur_servers = current.get("mcpServers") if isinstance(current.get("mcpServers"), dict) else {}
    for name, srv in cleaned.get("mcpServers", {}).items():
        if not isinstance(srv, dict):
            continue
        # 來源與額外允許的資料夾不在 server_config.json（ADR-0275）：舊快照或手造資料帶進來
        # 的這兩個鍵一律拿掉，免得寫進去讓舊版設定頁讀不懂。
        srv.pop("origin", None)
        srv.pop("sandbox_paths", None)
        cur = cur_servers.get(name) if isinstance(cur_servers.get(name), dict) else {}
        # vault-jail 政策（per-agent 隔離）不可被外來/舊快照清掉：cleaned 缺則自目標既有還原
        # （複審 Medium：replace 模式匯入舊快照會 fail open）。
        if not srv.get("vault_jail_arg") and cur.get("vault_jail_arg"):
            srv["vault_jail_arg"] = cur["vault_jail_arg"]
        # 同理，per-installation 工具治理（ADR-0195 軸 A）不可被外來/舊快照清掉——
        # 匯入一份 ADR-0195 之前的快照不得把「已關閉 create_refund」靜默打開。
        if srv.get("enabled_tools") is None and cur.get("enabled_tools") is not None:
            srv["enabled_tools"] = copy.deepcopy(cur["enabled_tools"])
        if not srv.get("disabled_tools") and cur.get("disabled_tools"):
            srv["disabled_tools"] = copy.deepcopy(cur["disabled_tools"])
        cur_env = cur.get("env") if isinstance(cur.get("env"), dict) else {}
        env = srv.get("env")
        if not isinstance(env, dict):
            # 非 dict（如 null）→ 還原為目標既有 env，避免 clobber；無則移除整個 env 鍵。
            if cur_env:
                srv["env"] = copy.deepcopy(cur_env)
            else:
                srv.pop("env", None)
            continue
        new_env: dict[str, Any] = {}
        for k, v in env.items():
            if is_masked(str(v)):
                if k in cur_env:
                    new_env[k] = cur_env[k]  # 還原目標既有真值（不寫遮罩假值）
                # else：無既有真值可回填 → 丟棄該鍵（新伺服器待 admin 手動補填）
            else:
                new_env[k] = v
        srv["env"] = new_env
    return cleaned


def _mcp_names(document: dict[str, Any]) -> set[str]:
    servers = document.get("mcpServers")
    return set(servers) if isinstance(servers, dict) else set()


def _refuse_invalid_mcp_entries(
    incoming: dict[str, Any], current: dict[str, Any], warnings: list[str],
) -> dict[str, Any]:
    """逐筆拒絕非物件；replace 也保留該名稱原有的有效設定，不以壞值覆蓋。"""
    cleaned = copy.deepcopy(incoming)
    servers = cleaned.get("mcpServers", {})
    existing = current.get("mcpServers", {})
    for name, entry in list(servers.items()):
        if isinstance(entry, dict):
            continue
        warnings.append(f"mcp:{name} 不是設定物件，已拒絕匯入該條目")
        prior = existing.get(name) if isinstance(existing, dict) else None
        if isinstance(prior, dict):
            servers[name] = copy.deepcopy(prior)
        else:
            servers.pop(name)
    return cleaned


def _forget_new_mcp_records(current: dict[str, Any], merged: dict[str, Any]) -> None:
    """匯入內容沒有本機來源授權；env 變動不可繼承第三方來源與人給的資料夾。"""
    added = _mcp_names(merged) - _mcp_names(current)
    existing, incoming = current.get("mcpServers", {}), merged.get("mcpServers", {})
    changed = [name for name in _mcp_names(merged) & _mcp_names(current)
               if isinstance(incoming[name], dict)
               if env_changed(existing[name], incoming[name])]
    if added or changed:
        def apply(records: dict[str, IsolationRecord]) -> None:
            delete(added)(records)
            reconcile_saved_entries(existing, incoming, changed, human=False)(records)

        McpIsolationStore(config_utils.CONFIG_DIR).mutate(apply)


def _forget_removed_mcp_records(current: dict[str, Any], merged: dict[str, Any]) -> None:
    """取代模式移除的名稱：寫完設定才清紀錄；清不掉只記錄（孤兒紀錄會被忽略）。"""
    if removed := _mcp_names(current) - _mcp_names(merged):
        try:
            McpIsolationStore(config_utils.CONFIG_DIR).mutate(delete(removed))
        except ToukaException:
            logger.warning("settings_snapshot: MCP 隔離紀錄沒清掉（%d 台）", len(removed))


def _clean_connections(incoming: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """把 incoming 連線的 apiKey（遮罩／null）「回填」為目標同 id 既有金鑰；無則移除。"""
    cleaned = copy.deepcopy(incoming)
    cur_by_id = {
        c.get("id"): c for c in current.get("connections", []) if isinstance(c, dict)
    }
    for conn in cleaned.get("connections", []):
        if not isinstance(conn, dict):
            continue
        val = conn.get("apiKey")
        if val is None or is_masked(str(val)):
            cur = cur_by_id.get(conn.get("id"))
            if cur and cur.get("apiKey"):
                conn["apiKey"] = cur["apiKey"]  # 還原目標既有金鑰
            else:
                conn.pop("apiKey", None)  # merge 保留目標；replace 為新連線、無金鑰待補
    return cleaned


# ── SECTIONS：包含哪些、如何合併、如何熱載入（單一 SSOT） ─────────────────────────

SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec("autonomy", "autonomy.json", "dict", reload_key="autonomy"),
    SectionSpec("permissions", "permissions.json", "dict",
                export_fn=_export_permissions, reload_key="permissions"),
    SectionSpec("auth_policies", "auth_policies.json", "dict"),
    SectionSpec("sliding_window", "sliding_window.json", "dict", reload_key="sliding_window"),
    SectionSpec("group_whitelist", "group_whitelist.json", "dict"),
    SectionSpec("instruction_templates", "instruction_templates.json", "dict"),
    # 定價／上下文視窗：pricing.py／context_window_registry.py 每次呼叫惰性讀檔 → 改檔即生效、
    # 零停機（不需重啟、不需熱載入器）。
    SectionSpec("model_pricing", "model_pricing.json", "dict"),
    SectionSpec("model_context_windows", "model_context_windows.json", "dict"),
    SectionSpec("models_blacklist", "models_blacklist.json", "replace"),
    SectionSpec("models", "models.json", "list", wrapper="models", id_field="id",
                only_existing=True, item_fields=_MODEL_IMPORT_FIELDS,
                export_fn=_export_models, reload_key="models"),
    # connections：模型路由快取於啟動／手動 rescan 時自連線發現，匯入後需重啟才重新探測。
    # 寫入後立刻撤下舊路由（和連線設定存檔同一套），隔離的封鎖埠馬上看得到匯入的新上游。
    SectionSpec("connections", "connections.json", "list", wrapper="connections",
                id_field="id", export_fn=_export_connections, import_clean_fn=_clean_connections,
                requires_restart=True, after_save=model_manager.reload_connections),
    SectionSpec("mcp_servers", "server_config.json", "dict", export_fn=_export_mcp_servers,
                import_clean_fn=_clean_mcp_servers, requires_restart=True,
                before_write=_forget_new_mcp_records, after_write=_forget_removed_mcp_records),
    SectionSpec("skills_custom", "skill_catalog.json", "list", wrapper="catalog", id_field="id"),
    SectionSpec("backup_schedule", "backup_schedule.json", "dict",
                export_fn=_export_backup_schedule, import_clean_fn=lambda inc, _cur: {
                    k: v for k, v in inc.items() if k != "last_run_epoch"
                }),
)

_BY_KEY: dict[str, SectionSpec] = {s.key: s for s in SECTIONS}


def _reload_permissions() -> None:
    permission_service.reload_all()
    agent_service.sync_with_permissions()


# 套用後熱載入器（best-effort；失敗只記錄不阻斷——設定已寫入磁碟）。
_RELOADERS: dict[str, Callable[[], Any]] = {
    "autonomy": autonomy_service.reload_all,
    "models": model_manager.load_initial_config,
    "sliding_window": sliding_window_service.load_config,
    "permissions": _reload_permissions,
}


# ── 雜湊／合併引擎 ──────────────────────────────────────────────────────────────


def _canonical_sha256(sections: dict[str, Any]) -> str:
    """對 sections 區塊正規化（排序鍵、緊湊）取 sha256，作為完整性指紋。"""
    canonical = json.dumps(sections, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deep_merge(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """深合併 incoming 覆蓋 current；巢狀 dict 遞迴，其餘（含 list）以 incoming 取代。"""
    out = copy.deepcopy(current)
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _upsert_list(
    spec: SectionSpec, current: dict[str, Any], incoming: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """依 id upsert list 包裹；回傳 (合併結果, 略過的 id 警告)。"""
    warnings: list[str] = []
    out = copy.deepcopy(current)
    # 縱深防禦：目標既有設定若損毀（包裹鍵非 list）→ 重設為空 list，避免 append 崩潰。
    items = out.get(spec.wrapper)
    if not isinstance(items, list):
        items = []
        out[spec.wrapper] = items
    by_id = {it.get(spec.id_field): it for it in items if isinstance(it, dict)}
    for inc in incoming.get(spec.wrapper, []):
        if not isinstance(inc, dict) or spec.id_field not in inc:
            continue
        iid = inc[spec.id_field]
        target = by_id.get(iid)
        if target is None:
            if spec.only_existing:
                warnings.append(f"{spec.key}:{iid} 不存在於本機，已略過")
                continue
            new_item = inc if spec.item_fields is None else {
                spec.id_field: iid, **{k: inc[k] for k in spec.item_fields if k in inc}
            }
            items.append(copy.deepcopy(new_item))
            by_id[iid] = items[-1]
        else:
            fields = inc if spec.item_fields is None else {
                k: inc[k] for k in spec.item_fields if k in inc
            }
            for k, v in fields.items():
                if k != spec.id_field:
                    target[k] = copy.deepcopy(v)
    return out, warnings


# ── 公開 API ───────────────────────────────────────────────────────────────────


def build_snapshot(*, exported_by_user_id: str | None = None) -> dict[str, Any]:
    """收集 allowlist 設定 → 組裝 envelope（含 manifest＋sha256 完整性指紋）。"""
    sections: dict[str, Any] = {}
    for spec in SECTIONS:
        raw = read_config_json(spec.filename)
        if not raw:
            continue
        data = spec.export_fn(raw) if spec.export_fn else raw
        sections[spec.key] = {"data": data}

    now = get_current_epoch()
    manifest = {
        "exported_at_epoch": now,
        "exported_at_taipei": format_epoch_to_taipei(now),
        "exported_by_user_id": exported_by_user_id,
        "sections": sorted(sections.keys()),
        "excluded": [
            "secrets (api keys, auth/dispatch/heartbeat tokens)",
            "users / identity_map / permissions.agent_mappings",
            "souls / personal data / databases / runtime state",
            "mcp env values & connection api keys (masked)",
        ],
        "sha256": _canonical_sha256(sections),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "export_kind": EXPORT_KIND,
        "manifest": manifest,
        "sections": sections,
    }


def package_encrypted_snapshot(snapshot: dict[str, Any], passphrase: str) -> Path:
    """把快照 JSON 封進明文 zip 後以 AES-256 加密（ADR-0108 crypto）；回傳加密 zip 路徑。

    明文 zip 為暫存、加密後即刪（永不出網）；呼叫端負責清理回傳的加密檔。
    """
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(suffix=".zip", prefix="settings_snap_", delete=False) as tmp:
        plain = Path(tmp.name)
    try:
        with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_SNAPSHOT_ARCNAME, payload)
        return encrypt_backup_zip(plain, passphrase)
    finally:
        resource_manager.remove_file(plain)


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    """結構／版本／完整性／體積驗證；回傳已驗證的 sections（key→data）。

    失敗一律 raise（呼叫端轉 422）；本函式不寫入任何狀態。
    """
    if not isinstance(snapshot, dict):
        _reject("快照格式錯誤（非物件）")
    if snapshot.get("export_kind") != EXPORT_KIND:
        _reject("這不是設定快照檔（export_kind 不符）")
    ver = snapshot.get("schema_version")
    if not isinstance(ver, int) or ver > SCHEMA_VERSION:
        _reject(f"快照 schema 版本不相容（檔案 {ver}，本系統最高支援 {SCHEMA_VERSION}）")

    raw_sections = snapshot.get("sections")
    if not isinstance(raw_sections, dict) or not raw_sections:
        _reject("快照不含任何設定區")

    envelope_size = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
    if envelope_size > _MAX_ENVELOPE_BYTES:
        _reject("快照過大（疑似損毀或非設定快照）")

    manifest = snapshot.get("manifest") or {}
    expected_sha = manifest.get("sha256")
    if expected_sha and expected_sha != _canonical_sha256(raw_sections):
        _reject("快照完整性校驗失敗（內容遭竄改或傳輸損毀）")

    validated: dict[str, Any] = {}
    for key, wrapped in raw_sections.items():
        if key not in _BY_KEY:
            continue  # 未知區（可能來自更新版本）→ 忽略，不阻斷
        if not isinstance(wrapped, dict) or "data" not in wrapped:
            _reject(f"設定區 {key} 結構錯誤")
        data = wrapped["data"]
        if not isinstance(data, dict):
            _reject(f"設定區 {key} 內容錯誤")
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > _MAX_SECTION_BYTES:
            _reject(f"設定區 {key} 過大")
        # list 策略區的包裹鍵須為 list（否則 replace 會寫入損毀結構、merge 會逐字元無聲略過）。
        spec = _BY_KEY[key]
        if key == "mcp_servers" and not isinstance(data.get("mcpServers", {}), dict):
            _reject("設定區 mcp_servers 的「mcpServers」必須是物件")
        if spec.strategy == "list" and spec.wrapper in data \
                and not isinstance(data[spec.wrapper], list):
            _reject(f"設定區 {key} 的「{spec.wrapper}」必須是清單")
        validated[key] = data
    if not validated:
        _reject("快照不含任何可辨識的設定區")
    return validated


def apply_snapshot(
    snapshot: Any,
    *,
    mode: str = "merge",
    sections: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """驗證 → （per-section）合併或取代 → 寫入 → 熱載入。回傳套用摘要。

    ``mode``：``merge``（預設，安全加性）或 ``replace``（整區覆蓋）。
    ``sections``：限定只套用的區（None＝快照中所有可辨識區）。
    ``dry_run``：只計算與驗證，不寫入、不熱載入（供前端預覽）。
    """
    if mode not in ("merge", "replace"):
        _reject(f"未知匯入模式：{mode}")
    validated = validate_snapshot(snapshot)
    subset = set(sections) if sections else None

    applied: list[str] = []
    warnings: list[str] = []
    skipped_secrets: list[str] = []
    requires_restart: list[str] = []
    reload_keys: set[str] = set()

    for key, incoming in validated.items():
        if subset is not None and key not in subset:
            continue
        spec = _BY_KEY[key]
        current = read_config_json(spec.filename) or {}

        if key == "mcp_servers":
            incoming = _refuse_invalid_mcp_entries(incoming, current, warnings)
        cleaned = spec.import_clean_fn(incoming, current) if spec.import_clean_fn else incoming
        _track_masked_secrets(spec, incoming, current, skipped_secrets)

        if mode == "replace":
            merged = copy.deepcopy(cleaned)
        elif spec.strategy == "list":
            merged, warns = _upsert_list(spec, current, cleaned)
            warnings.extend(warns)
        elif spec.strategy == "replace":
            merged = copy.deepcopy(cleaned)
        else:  # dict
            merged = _deep_merge(current, cleaned)

        if not dry_run:
            if spec.before_write:
                spec.before_write(current, merged)
            save_config_json(spec.filename, merged)
            if spec.after_write:
                spec.after_write(current, merged)
            if spec.after_save:
                spec.after_save()
            if spec.reload_key:
                reload_keys.add(spec.reload_key)
        applied.append(key)
        if spec.requires_restart:
            requires_restart.append(key)

    if not dry_run and reload_keys:
        _run_reloaders(reload_keys)

    return {
        "dry_run": dry_run,
        "mode": mode,
        "applied": applied,
        "warnings": warnings,
        "skipped_secrets": skipped_secrets,
        "requires_restart": requires_restart,
    }


# ── 內部輔助 ───────────────────────────────────────────────────────────────────


def _reject(detail: str) -> None:
    """驗證失敗：raise 422（沿用既有 VALIDATION_ERROR，不新增錯誤碼）。"""
    raise ToukaException(ErrorCode.VALIDATION_ERROR, detail_override=detail)


def _track_masked_secrets(
    spec: SectionSpec, incoming: dict[str, Any], current: dict[str, Any], skipped: list[str],
) -> None:
    """記錄「遮罩且目標無既有值可回填」的 MCP env 機密——即真正**待 admin 手動補填**者。

    遮罩但目標已有真值者已被 `import_clean_fn` 回填，不需補填、不列入。連線 apiKey 一律 null
    （機密在 .env／ProviderKeys，非 per-connection），故不在此追蹤以免假陽性雜訊。
    """
    if spec.key != "mcp_servers":
        return
    cur_servers = current.get("mcpServers") if isinstance(current.get("mcpServers"), dict) else {}
    for name, srv in incoming.get("mcpServers", {}).items():
        env = srv.get("env") if isinstance(srv, dict) else None
        if not isinstance(env, dict):
            continue
        cur = cur_servers.get(name) if isinstance(cur_servers.get(name), dict) else {}
        cur_env = cur.get("env") if isinstance(cur.get("env"), dict) else {}
        for k, v in env.items():
            if is_masked(str(v)) and k not in cur_env:
                skipped.append(f"mcp:{name}:{k}")


def _run_reloaders(reload_keys: set[str]) -> None:
    """套用後熱載入（best-effort；個別失敗只記錄，不回滾——設定已落盤）。"""
    for rk in reload_keys:
        fn = _RELOADERS.get(rk)
        if not fn:
            continue
        try:
            fn()
        except Exception:
            logger.exception("settings_snapshot: 熱載入 %s 失敗（設定已寫入，建議重啟）", rk)
