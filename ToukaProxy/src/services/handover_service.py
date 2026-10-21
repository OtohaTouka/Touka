"""專案交接報告生成服務 (Project Handover Service).

負責在對話轉換或交班時，自動收集審計日誌與夥伴清單，並生成 .touka_handover.md 檔案以提供給下一位 Agent 閱讀。
"""

import logging
from pathlib import Path

from src.services.resource_manager import resource_manager

from ..utils.async_db import get_async_db
from ..utils.brain_persistence import _get_settings_path
from ..utils.time_utils import format_epoch_to_taipei, get_current_epoch

logger = logging.getLogger(__name__)

class HandoverService:
    """負責生成專案接班文件 (.touka_handover.md) (SOLID - SRP)。
    """
    async def generate_handover_file(self, project_id: str, new_agent_id: str, old_agent_id: str | None = None):
        """生成接班 Markdown 檔案到專案根目錄"""
        if not project_id:
            return

        try:
            # 1. 獲取專案資訊與路徑
            async with get_async_db() as db:
                async with db.execute("SELECT name, root_path FROM projects WHERE id = ?", (project_id,)) as cursor:
                    project = await cursor.fetchone()
                
                if not project:
                    return
                
                # 2. 獲取最近審計事件
                async with db.execute(
                    "SELECT agent_id, action, summary, timestamp FROM project_audit WHERE project_id = ? ORDER BY timestamp DESC LIMIT 10",
                    (project_id,)
                ) as cursor:
                    events = await cursor.fetchall()

            # 3. 獲取所有夥伴 (Agents)
            partners = self._get_available_partners()

            # 4. 格式化內容
            content = self._format_handover_md(
                project_name=project["name"],
                new_agent=new_agent_id,
                old_agent=old_agent_id,
                events=events,
                partners=partners
            )

            # 5. 寫入檔案
            handover_path = Path(project["root_path"]) / ".touka_handover.md"
            resource_manager.make_dir(Path(handover_path).parent)
            resource_manager.write_file_text(handover_path, content)
            
            logger.info(f"📄 Handover file updated: {handover_path}")

        except Exception as e:
            logger.error(f"Error generating handover file: {e}")

    def _get_available_partners(self) -> list[str]:
        """從設定中獲取所有已註冊的 Agent ID"""
        settings = resource_manager.read_json(_get_settings_path())
        if settings:
            return list(settings.keys())
        return ["Unknown"]

    def _format_handover_md(self, project_name: str, new_agent: str, old_agent: str | None, events: list[dict], partners: list[str]) -> str:
        now = format_epoch_to_taipei(get_current_epoch())[0]
        
        lines = [
            f"# 🤝 Project Handover: {project_name}",
            f"\n> **Generated At**: {now}",
            "\n## 👤 Active Agent Change",
            f"- **New Assignee**: `{new_agent}`",
            f"- **Previous Assignee**: `{old_agent or 'None'}`",
            "\n## 👥 Available Partners",
            ", ".join([f"`{p}`" for p in partners]),
            "\n## 📜 Recent Project Activity (Audit Log)",
            "| Time | Agent | Action | Summary |",
            "| :--- | :--- | :--- | :--- |"
        ]

        if not events:
            lines.append("| - | - | - | No recent events recorded |")
        else:
            for e in events:
                time_str = format_epoch_to_taipei(e["timestamp"])[0][5:16] # 取得 mm-dd HH:MM 區段
                lines.append(f"| {time_str} | {e['agent_id']} | {e['action']} | {e['summary']} |")

        lines.append("\n---\n*Note: This is an automated system-generated summary for the next agent.*")
        return "\n".join(lines)

# Global Instance
handover_service = HandoverService()
