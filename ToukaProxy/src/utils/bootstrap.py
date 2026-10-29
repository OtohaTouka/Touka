"""系統引導工具 (Bootstrap Utility)。

負責在系統啟動時檢查並恢復缺失的核心設定檔。
從 .example 檔案恢復或建立預設的 JSON 結構。
"""

import json
import logging
import shutil
from pathlib import Path

from src.config import CONFIG_DIR, get_config_dir
from src.models.autonomy import AutonomyConfigModel
from src.models.hooks import HooksConfig
from src.models.identity import IdentityMapConfig
from src.models.permissions import PermissionsConfig
from src.models.safety import AuthPoliciesConfig
from src.models.sliding_window import SlidingWindowConfig

logger = logging.getLogger(__name__)

# 有 pydantic 模型的 live 設定檔 → 對應模型（啟動 schema 驗證用）。
# 與 test_config_examples.py（Guard #34，守「範本 .example」）互補：此處守「live 真檔」
# ——live 檔 gitignore、CI 上不存在無法靜態守衛，故改在每次啟動時驗證。
_LIVE_MODELED: dict[str, type] = {
    "autonomy.json": AutonomyConfigModel,
    "permissions.json": PermissionsConfig,
    "identity_map.json": IdentityMapConfig,
    "auth_policies.json": AuthPoliciesConfig,
    "sliding_window.json": SlidingWindowConfig,
    # ADR-0292：壞掉的 hook 設定在啟動時就擋下（執行中壞掉則沿用上一份好的）。
    "hooks.json": HooksConfig,
}


class ConfigValidationError(RuntimeError):
    """live 設定檔啟動驗證失敗（malformed JSON 或不符 schema）。"""


def _has_template(cfg: Path, name: str) -> bool | None:
    """同資料夾有沒有 ``name`` 的 .example；讀不到（權限一類）回 None。"""
    try:
        return (cfg / f"{name}.example").is_file()
    except OSError:
        return None


def _delete_hint(cfg: Path, broken: list[str]) -> str:
    """刪掉壞檔會不會重建：看同資料夾有沒有 .example。

    有範本的檔，下次啟動會照範本補回（``hydrate_config_examples``）；沒有範本的刪掉就是少了
    那份設定。看不出有沒有範本時不猜，請人先確認（這裡是錯誤訊息，不能自己再丟例外）。
    """
    templates = {name: _has_template(cfg, name) for name in broken}
    rebuilt = [name for name in broken if templates[name] is True]
    kept = [name for name in broken if templates[name] is False]
    unknown = [name for name in broken if templates[name] is None]
    hint = ""
    if rebuilt:
        hint += f"{'、'.join(rebuilt)} 也可以刪除後重啟，系統會從 .example 範本重建"
        hooks_off = "（hooks.json 的範本是關閉的：所有 hook 都不執行）。"
        hint += hooks_off if "hooks.json" in rebuilt else "。"
    if kept:
        hint += f"{'、'.join(kept)} 沒有範本，刪除後不會重建，等於沒有那份設定。"
    if unknown:
        hint += f"{'、'.join(unknown)} 看不出有沒有 .example 範本，刪除前先確認同資料夾有沒有範本。"
    return hint

# --- SSOT: 定義必需的設定檔清單 ---
# 這些檔案若遺失，系統將嘗試從 .example 恢復或建立預設值
ESSENTIAL_CONFIGS = [
    "agents_registry.json",
    "models.json",
    "models_blacklist.json",
    "model_context_windows.json",
    "model_pricing.json",
    "server_config.json",
    "skill_catalog.json",
    "mcp_hub.json",
    "bots.json",
    "group_whitelist.json",
    "connections.json",
    "auth_policies.json",
    "autonomy.json",
    "permissions.json",
    # ADR-0147：靈魂→角色 SSOT。缺檔時退 DEFAULT_IDENTITY_MAP(default_role=Base)
    # → 所有靈魂＋DebugAgent 全塌 Base（「Sumi 掉 Owner」＋除錯/計分卡失權）。
    # 由 .example 自癒（tag_rules is_toukasoul→Owner、DebugAgent→Debugger）。
    "identity_map.json",
    # ADR-0275：每台 MCP 的來源與額外允許的資料夾；存在卻讀不到時不覆寫。
    "mcp_server_isolation.json",
]


def ensure_configs() -> list[str]:
    """確保所有必需的設定檔皆存在。

    若遺失，則嘗試從 .example 恢復，或建立預設的 JSON 結構。
    遵循 SSOT 原則且絕對不覆蓋已存在的資料。
    """
    # 確保 config 目錄存在
    config_path = Path(CONFIG_DIR)
    if not config_path.exists():
        config_path.mkdir(parents=True, exist_ok=True)
        logger.info("📁 已建立設定目錄: %s", CONFIG_DIR)

    restored_files = []

    for filename in ESSENTIAL_CONFIGS:
        target_path = config_path / filename
        example_path = target_path.with_name(target_path.name + ".example")

        # 1. 檢查檔案是否已存在 (保護現有資料)
        if target_path.exists():
            continue

        # 2. 嘗試從 .example 恢復
        if example_path.exists():
            try:
                shutil.copy2(example_path, target_path)
                logger.info("✨ 已從範本恢復設定檔: %s", filename)
                restored_files.append(filename)
            except Exception:
                logger.exception("❌ 恢復 %s 失敗", filename)
        else:
            # 3. 若無範本，建立預設的合法 JSON 結構 (空字典)
            try:
                with target_path.open("w", encoding="utf-8") as f:
                    json.dump({}, f, indent=4, ensure_ascii=False)
                logger.info("🆕 已建立預設設定檔 (空白): %s", filename)
                restored_files.append(filename)
            except Exception:
                logger.exception("❌ 建立預設 %s 失敗", filename)

    if restored_files:
        logger.info(
            "✅ 自動引導完成，共修復 %d 個檔案: %s",
            len(restored_files),
            ", ".join(restored_files),
        )

    return restored_files


def validate_live_configs(config_dir: Path | str | None = None) -> list[str]:
    """啟動自我健檢：驗證實際運作中的 live 設定檔（ADR-0096）。

    與靜態範本守衛（Guard #34，``test_config_examples``）互補——那裡守 ``.example``、
    這裡守 gitignore 的 live 真檔（CI 全新 checkout 不存在 live 檔、無法靜態守衛，故改在
    每次啟動時驗證真檔）。應在 :func:`ensure_configs` 之後呼叫（缺檔已被還原）。

    檢查（僅針對「已存在」的檔案；缺檔交由 ``ensure_configs`` 還原，不在此報錯）：
      1. 所有 ``config/*.json`` 為合法 JSON——直接 ``json.loads``，**不經會把壞 JSON
         當空字典靜默吞掉的** ``resource_manager.read_json``。
      2. 有 pydantic 模型者（autonomy/permissions/identity_map/auth_policies/
         sliding_window）通過 ``model_validate``（型別/必填正確）。

    任一檔案有問題即 **fail-fast** 拋 :class:`ConfigValidationError`（附逐檔原因＋修復
    指引）——勝過「靜默退回預設」（正是自主成本上限 cap/quota 那類設定失效的根因）。

    Args:
        config_dir: 設定目錄；預設於呼叫時解析 :func:`get_config_dir`（測試可注入 tmp）。

    Returns:
        list[str]: 通過驗證的設定檔名（供啟動日誌摘要）。

    Raises:
        ConfigValidationError: 任一 live 設定檔為壞 JSON 或不符其模型 schema。
    """
    cfg = Path(config_dir) if config_dir is not None else get_config_dir()
    if not cfg.exists():
        return []

    problems: list[str] = []
    broken: list[str] = []
    checked: list[str] = []

    # glob 在資料夾列不出來時靜默回空，健檢會空過；先列一次，列不出來就中止
    try:
        next(cfg.iterdir(), None)
    except OSError as e:
        problems.append(f"  • {cfg.name}/：無法列出設定資料夾（{type(e).__name__}: {e}）")

    for path in sorted(cfg.glob("*.json")):
        name = path.name
        # 1. JSON 合法性（直接解析；read_json 會吞掉壞 JSON 故不可用於偵測）
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append(f"  • {name}：JSON 格式錯誤（{e}）")
            broken.append(name)
            continue
        except Exception as e:  # 讀檔層失敗（權限/編碼等）
            problems.append(f"  • {name}：無法讀取（{type(e).__name__}: {e}）")
            broken.append(name)
            continue

        # 2. schema 驗證（僅有模型者）
        model = _LIVE_MODELED.get(name)
        if model is not None:
            try:
                model.model_validate(data)
            except Exception as e:
                problems.append(f"  • {name}：不符 schema（{type(e).__name__}）— {e}")
                broken.append(name)
                continue

        checked.append(name)

    if problems:
        detail = "\n".join(problems)
        msg = (
            "啟動中止：偵測到 live 設定檔錯誤——\n"
            f"{detail}\n"
            f"請修正上列檔案內容。{_delete_hint(cfg, broken)}"
        )
        logger.critical("🛑 %s", msg)
        raise ConfigValidationError(msg)

    logger.info("🛡️ live 設定檔啟動健檢通過（%d 檔，含 schema 驗證）。", len(checked))
    return checked
