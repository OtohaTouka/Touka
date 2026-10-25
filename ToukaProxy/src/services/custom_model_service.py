"""自定義模型服務 (CustomModelService) 模組.

負責管理系統中「Level 2 自定義模型」的物理生命週期，包含模型資料夾的
建立、讀取、更新、刪除 (CRUD) 以及 System Prompt 與設定檔的持久化管理.
"""

import json
import logging
from typing import Any

from src.models.identity import AgentIdentity
from src.utils import custom_io

logger = logging.getLogger(__name__)


class CustomModelService:
    """自定義模型實體管理服務.

    負責 data/custom/ 目羅下所有模型實體檔案的維護，
    確保自定義模型的 System Prompt 與物理配置符合 SSOT 標準.
    """

    def __init__(self) -> None:
        """初始化自定義模型服務."""
        pass

    def get_model_data(self, identity: AgentIdentity) -> tuple[str | None, dict[str, Any]]:
        """從物理路徑載入指定自定義模型的 System Prompt 與設定檔 (Identity SSOT)."""
        system_prompt = custom_io.read_custom_file(identity, "system_prompt.md")
        config_raw = custom_io.read_custom_file(identity, "config.json")
        
        config = {}
        if config_raw:
            try:
                config = json.loads(config_raw)
            except Exception:
                logger.exception(f"Error parsing config.json for {identity.agent_id}")

        return system_prompt, config

    def save_model(
        self,
        identity: AgentIdentity,
        name: str,
        description: str,
        system_prompt: str,
        config: dict[str, Any],
    ) -> bool:
        """保存自定義模型到物理目錄 (Identity SSOT)."""
        # 1. 保存 System Prompt
        if not custom_io.write_custom_file(identity, "system_prompt.md", system_prompt):
            return False

        # 2. 保存 Config
        full_config = {
            "id": identity.agent_id,
            "name": name,
            "description": description,
            **config,
        }
        
        try:
            content = json.dumps(full_config, indent=4, ensure_ascii=False)
            return custom_io.write_custom_file(identity, "config.json", content)
        except Exception:
            logger.exception(f"Error preparing config.json for {identity.agent_id}")
            return False

    def delete_model(self, identity: AgentIdentity) -> bool:
        """刪除自定義模型目錄 (Identity SSOT)."""
        return custom_io.delete_custom_dir(identity)

    def list_models(self) -> list[dict[str, Any]]:
        """掃描目錄並回傳所有自定義模型的概要資訊."""
        models = []
        model_ids = custom_io.list_custom_ids()
        
        for mid in model_ids:
            temp_identity = AgentIdentity(agent_id=mid, agent_role="Base")
            config_raw = custom_io.read_custom_file(temp_identity, "config.json")
            
            if config_raw:
                try:
                    data = json.loads(config_raw)
                    models.append({
                        "id": mid,
                        "name": data.get("name", mid),
                        "description": data.get("description", ""),
                        "owned_by": "custom",
                        "type": "custom",
                        "base_model": data.get("base_model"),
                    })
                except Exception:
                    models.append({
                        "id": mid,
                        "name": mid,
                        "owned_by": "custom",
                        "type": "custom",
                    })
        return models

    def rename_model(self, old_identity: AgentIdentity, new_id: str) -> None:
        """重命名自定義模型的 ID (Identity SSOT).

        搬資料夾失敗時拋例外、config.json 不改：來源不見 FileNotFoundError、新 ID 已被占用
        FileExistsError、其他（沙箱檢查不過、搬移出錯）OSError。呼叫端據此回錯誤、不搬擁有權。
        新位置的 config.json 改不了 id 時把資料夾搬回、拋 OSError（舊審查報告丙-2）。
        """
        if old_identity.agent_id == new_id:
            return

        if not custom_io.move_custom_dir(old_identity.agent_id, new_id):
            if not custom_io.custom_path_exists(old_identity.agent_id):
                raise FileNotFoundError(old_identity.agent_id)
            if custom_io.custom_path_exists(new_id):
                raise FileExistsError(new_id)
            raise OSError(new_id)

        if not self._write_config_id(AgentIdentity(agent_id=new_id, agent_role="Base")):
            if not self._move_back(new_id, old_identity.agent_id):
                logger.critical(
                    "AUDIT: custom model rename rollback FAILED old=%s new=%s",
                    old_identity.agent_id,
                    new_id,
                )
            raise OSError(new_id)

    def undo_rename(self, old_id: str, new_id: str) -> bool:
        """改名之後的步驟失敗時搬回：資料夾搬回舊 ID、config.json 的 id 改回舊 ID。

        回傳是否完整搬回，出錯也算沒搬回、不往外丟；呼叫端在失敗時寫 AUDIT。
        """
        if not self._move_back(new_id, old_id):
            return False
        return self._write_config_id(AgentIdentity(agent_id=old_id, agent_role="Base"))

    @staticmethod
    def _move_back(from_id: str, to_id: str) -> bool:
        """回滾用的搬移：出錯一律當沒搬成，不往外丟。"""
        try:
            return custom_io.move_custom_dir(from_id, to_id)
        except Exception:
            logger.exception("Failed to move custom model %s back to %s", from_id, to_id)
            return False

    def _write_config_id(self, identity: AgentIdentity) -> bool:
        """把這個模型 config.json 的 id 改成資料夾的 ID。

        內容不是 JSON 物件時不用改（之後存檔會整份重寫）；不見了、讀不到、寫不進去或出錯
        回 False，不往外丟（路由只對有 config.json 的模型改名）。
        """
        try:
            return self._try_write_config_id(identity)
        except Exception:
            logger.exception("Failed to update config.json for %s", identity.agent_id)
            return False

    @staticmethod
    def _try_write_config_id(identity: AgentIdentity) -> bool:
        config_raw = custom_io.read_custom_file(identity, "config.json")
        if config_raw is None:
            return False
        try:
            config_data = json.loads(config_raw)
            config_data["id"] = identity.agent_id
        except (ValueError, TypeError):
            logger.warning(
                "config.json for %s is not a JSON object; id left as is", identity.agent_id
            )
            return True
        content = json.dumps(config_data, indent=4, ensure_ascii=False)
        return custom_io.write_custom_file(identity, "config.json", content)


# 全系統單例
custom_model_service = CustomModelService()
