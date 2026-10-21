"""群聊別名解析與點名路由服務 (Group Chat Router).

負責解析對話中的點名標記 (如 @別名 或 -@別名)，並依據白名單 (group_whitelist.json) 的
ToukaSoul 區映射至 agents_registry 裡的靈魂。群聊成員只限靈魂（ADR-0245）。
"""

import logging
import re
from typing import Any

from ..config import get_config_dir
from .resource_manager import resource_manager
from .soul_service import soul_service

logger = logging.getLogger(__name__)

# ADR-0245：白名單只認這一區；舊 Native／Custom 等其他區讀到就略過並記警告，不改檔。
SOUL_SECTION = "ToukaSoul"


def drop_legacy_sections(data: dict) -> dict:
    """只留 ToukaSoul 區；其他有內容的區記警告（檔案不動）."""
    ignored = sorted(k for k, v in data.items() if k != SOUL_SECTION and v)
    if ignored:
        logger.warning(
            "⚠️ 群聊只限靈魂（ADR-0245）：group_whitelist.json 的 %s 區已略過，檔案未修改。",
            ", ".join(ignored),
        )
    return {SOUL_SECTION: data[SOUL_SECTION]} if SOUL_SECTION in data else {}


def load_whitelist() -> dict[str, str]:
    """載入群聊白名單的靈魂別名（別名 → 靈魂 ID）."""
    data = resource_manager.read_json(str(get_config_dir() / "group_whitelist.json"))
    section = drop_legacy_sections(data).get(SOUL_SECTION) if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def resolve_soul(target: str, registry: dict) -> str | None:
    """以 ID 或註冊別名（不分大小寫）在 agents_registry 找靈魂；找不到或不是靈魂回 None."""
    t = target.lower()
    for soul_id, aliases in registry.items():
        matched = t == soul_id.lower() or t in [a.lower() for a in aliases]
        if matched and soul_service.is_toukasoul(soul_id):
            return soul_id
    return None


def parse_group_roster(messages: list[Any], registry: dict) -> Any:
    """解析對話歷史，動態決定本次發言的靈魂名單 (Roster)。

    回傳格式：List[dict] -> [{"name": str, "id": str}, ...]，id 一律是 agents_registry 的鍵。
    """
    roster = []
    whitelist_map = {str(k).lower(): str(v) for k, v in load_whitelist().items()}

    # 修改正則：移除回溯，直接捕捉 @ 或 -@ 開頭的點名
    mention_pattern = re.compile(r"(-?@)([a-zA-Z0-9_\-\u4e00-\u9fa5]+)", re.IGNORECASE)

    latest_turn_mentions = []
    roles = [m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "") for m in messages]
    # 只對最新一則 user 訊息嚴格驗證；更早歷史裡解析不到的點名（如已退役的別名）略過。
    last_user = max((i for i, role in enumerate(roles) if role == "user"), default=-1)

    for i, m in enumerate(messages):
        if roles[i] == "user":
            content = (
                getattr(m, "content", "")
                if not isinstance(m, dict)
                else m.get("content", "")
            )

            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                )
            elif isinstance(content, dict):
                text = content.get("text", "")

            mentions = mention_pattern.findall(text)

            is_latest = i == len(messages) - 1

            for prefix, name in mentions:
                name_lower = name.lower()

                # 白名單（ToukaSoul 區）值必須是靈魂（ID 或註冊別名）；原始模型 ID／regex 不展開，
                # 也不退回拿別名本身比對 registry（ADR-0245）
                target = whitelist_map.get(name_lower)
                soul_id = resolve_soul(target, registry) if target is not None else None
                if soul_id is None:
                    if i != last_user:
                        logger.debug("群聊歷史點名『@%s』解析不到，略過。", name)
                        continue
                    if target is None:
                        return {
                            "error": f"找不到靈魂『@{name}』，請確認是否打錯或尚未加入群聊白名單；"
                            "群聊成員只限靈魂。"
                        }
                    return {"error": f"『@{name}』指向的『{target}』不是靈魂，群聊成員只限靈魂。"}

                entry = {"name": name, "id": soul_id}

                if prefix == "-@":
                    # 移除該名稱的所有對應
                    roster = [r for r in roster if r["name"].lower() != name_lower]
                else:  # prefix == "@"
                    # 允許重複點名（例如點名兩次同一個名稱，要求其回覆兩次）
                    roster.append(entry)
                    if is_latest:
                        latest_turn_mentions.append(entry)

    # 規則 3: 如果最後一句有明確點名，這回合只讓被點名的人講話
    if latest_turn_mentions:
        return latest_turn_mentions

    # 無明確點名的話拋錯
    if not roster:
        return {"error": "群組對話中未指定任何代理人，請使用 @名稱 標記要對話的對象。"}

    return roster
