"""靈魂服務 (SoulService) 模組.

負責 Touka 系統中代理人 (Agent) 靈魂資料 (System Prompt) 與實體設定 (config.json) 的加載,
並執行請求攔截與人格注入 (Soul Sanctuary).
"""

import json
import logging
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Any

from memory_server import config_utils
from memory_server.utils.historical_data import (
    HISTORICAL_DATA_CLOSE,
    historical_data_open,
    neutralize_historical_tags,
    neutralize_tags,
    wrap_historical_data,
)
from memory_server.utils.legacy_time import format_taipei
from memory_server.utils.zone_paths import is_zone_segment

from src.adapters.content_normalize import normalize_message_content
from src.adapters.schemas import OpenAIMessage
from src.models.identity import AgentIdentity
from src.services.prompt_blocks import ALL_BLOCK_TAGS, CONVERSATION_SUMMARY, PERSONA, wrap_block
from src.utils.model_text import blank_control_chars
from src.utils.time_utils import get_monotonic_ms
from src.utils.token_estimator import estimate_messages_tokens

logger = logging.getLogger(__name__)

# 別名警告最多記幾個不同的名稱（名稱來自請求，不讓這份集合無限長大）
_ALIAS_WARN_LIMIT = 256
# 靈魂名稱快取最多沿用多久（毫秒）：修改時間不準的檔案系統（FAT、部分網路或容器掛載）上，
# 名稱改了修改時間不一定變，過期就重列
_SOUL_NAMES_TTL_MS = 2000.0
# 歷史資料界線的來源：家原文、還沒交接完的舊家原文（ADR-0305、0306）
HOME_HISTORY_SOURCE = "home_conversation"
OLD_HOME_HISTORY_SOURCE = "old_home_pending_handover"
# 還沒交接完的舊家原文總量上限（估算 token）：新的舊家優先、每段只帶尾端，放不下的整段不帶；
# 滑動視窗把這些當成剪不掉的部分，沒有上限時新家每一輪都被剪到最少（ADR-0306 審查 J5）
OLD_HOME_TOKEN_BUDGET = 8000
OLD_HOME_OMITTED_NOTE = "（這段舊家前面還有 {count} 則，原文留在舊家）"


def _as_identity(identity: str | AgentIdentity) -> AgentIdentity:
    """字串身分轉成 AgentIdentity（沒有角色資訊）；其他身分物件原樣回傳（只讀 agent_id）.

    派工服務等處傳的是自己的身分物件（有 agent_id／agent_role，但不是 AgentIdentity），
    和改動前一樣照收。
    """
    return AgentIdentity(agent_id=identity) if isinstance(identity, str) else identity


def _resource_manager() -> Any:
    """ResourceManager 實例；它在模組頂層匯入本模組，這裡只能延遲匯入（循環）."""
    from .resource_manager import resource_manager  # noqa: LOCAL_IMPORT_BAN

    return resource_manager


def _map_text(content: Any, fn: Callable[[str], str]) -> Any:
    """對一則訊息內容的文字套用 ``fn``，套在模型會看到的形狀上（ADR-0285）。

    多模態 JSON 字串先還原成 list 再套：轉接層送出前也會還原，在原始 JSON 上中和時
    JSON 跳脫寫法（``<`` 寫成 u003c、``/`` 前面加反斜線）還不是 ``<``、``/``，解開後就變成
    真的 tag。套完再存回 JSON 字串，送出的形狀和原本一樣（原生 Anthropic 入口不轉換
    ``image_url``，改成 list 會被拒絕）。
    """
    decoded = normalize_message_content(content)
    if isinstance(decoded, list):
        mapped = [_map_part(part, fn) for part in decoded]
        return json.dumps(mapped, ensure_ascii=False) if isinstance(content, str) else mapped
    if isinstance(decoded, str):
        return fn(decoded)
    return decoded


def _map_part(part: Any, fn: Callable[[str], str]) -> Any:
    """一個 part：字串直接套；dict 套 ``text``、``thinking`` 與巢狀 ``content``（工具結果等）。"""
    if isinstance(part, str):
        return fn(part)
    if not isinstance(part, dict):
        return part
    mapped = dict(part)
    for key in ("text", "thinking"):
        if isinstance(mapped.get(key), str):
            mapped[key] = fn(mapped[key])
    nested = mapped.get("content")
    if isinstance(nested, str):
        mapped["content"] = fn(nested)
    elif isinstance(nested, list):
        mapped["content"] = [_map_part(item, fn) for item in nested]
    return mapped


def _add_text(content: Any, text: str, *, before: bool) -> Any:
    """在一則訊息內容前或後加一段文字；多模態 JSON 字串先還原成 list、加一個 text part。"""
    content = normalize_message_content(content)
    if isinstance(content, list):
        part = {"type": "text", "text": text}
        return [part, *content] if before else [*content, part]
    return f"{text}\n{content or ''}" if before else f"{content or ''}\n{text}"


def _neutralize_history(text: str) -> str:
    """舊紀錄先把控制字元換成空白，再中和界線 tag 與其他 prompt 區塊 tag（ADR-0285）。"""
    return neutralize_tags(neutralize_historical_tags(blank_control_chars(text)), ALL_BLOCK_TAGS)


def _stamp_times(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每則內容前標台北時間（寫在內容裡，ADR-0260/0305）；先複製，不動到讀出來的樹."""
    return [
        {
            **m,
            "content": _add_text(
                m.get("content"), f"[{format_taipei(m.get('timestamp'))}]", before=True
            ),
        }
        for m in msgs
    ]


def _old_home_tails(
    old_homes: Sequence[list[dict[str, Any]]],
) -> list[tuple[list[dict[str, Any]], int]]:
    """（帶的尾端、前面省略幾則），照原順序；從最新的舊家尾端往前收，額度用完就停."""
    remaining = OLD_HOME_TOKEN_BUDGET
    tails = []
    for msgs in reversed(old_homes):
        kept = 0
        for message in reversed(msgs):
            cost = estimate_messages_tokens([message])
            if cost > remaining:
                remaining = 0
                break
            remaining -= cost
            kept += 1
        tails.append((msgs[len(msgs) - kept :], len(msgs) - kept))
    return tails[::-1]


def _old_home_block(msgs: list[dict[str, Any]], omitted: int) -> list[dict[str, Any]]:
    """舊家尾端：每則標時間，省略過時第一則前面寫一行說明，再包歷史資料界線."""
    stamped = _stamp_times(msgs)
    if omitted:
        note = OLD_HOME_OMITTED_NOTE.format(count=omitted)
        stamped[0] = {**stamped[0], "content": _add_text(stamped[0]["content"], note, before=True)}
    return _mark_home_history(stamped, source=OLD_HOME_HISTORY_SOURCE)


def _mark_home_history(
    home_msgs: list[dict[str, Any]], *, source: str = HOME_HISTORY_SOURCE
) -> list[dict[str, Any]]:
    """家對話第一則前加歷史資料界線開頭、最後一則後加結尾：只加文字，角色與則數不變。

    先複製再改，不動到讀出來的 session 樹；每則文字裡的區塊 tag 先中和。
    """
    marked = [
        {**m, "content": _map_text(m.get("content"), _neutralize_history)}
        for m in home_msgs
    ]
    opening = historical_data_open(source)
    marked[0]["content"] = _add_text(marked[0]["content"], opening, before=True)
    marked[-1]["content"] = _add_text(marked[-1]["content"], HISTORICAL_DATA_CLOSE, before=False)
    return marked


def append_system_block(request: Any, block: str) -> None:
    """把一個區塊接在 system 最後（Anthropic 的 system 字串或清單、OpenAI 的第一則 system）.

    前面空一行；沒有 system 時新增一則。前情與子對話舊摘要共用（ADR-0305）。
    """
    text = "\n\n" + block
    if hasattr(request, "system"):
        if isinstance(request.system, list):
            request.system.append({"type": "text", "text": text})
        else:
            request.system = (request.system or "") + text
        return
    for m in request.messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "system":
            if isinstance(m, dict):
                m["content"] = (m.get("content") or "") + text
            elif hasattr(m, "content"):
                m.content = (m.content or "") + text
            return
    request.messages.insert(0, OpenAIMessage(role="system", content=text))


def _after_leading_system(messages: list[Any]) -> int:
    """開頭連續 system 訊息之後的位置（第一則就不是 system＝0）。"""
    index = 0
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role != "system":
            break
        index += 1
    return index


class SoulService:
    """代理人人格與設定管理服務.

    實施「靈魂庇護所 (Soul Sanctuary)」機制, 確保代理人的核心人格與設定
    在任何管道的請求中都能精確覆蓋與保持 SSOT.
    """

    def __init__(self) -> None:
        """初始化靈魂服務."""
        self._alias_warned: set[str] = set()  # 別名的警告每個名稱只記一次
        # (根目錄, 列表當時的修改時間, 名稱, 列表的時刻)：只拿來確認「逐字在列表裡」，
        # 不在裡面一律重列
        self._soul_names: tuple[str, int, frozenset[str], float] | None = None

    @property
    def base_dir(self) -> Path:
        """此屬性已廢棄，請使用 resource_manager 獲取路徑。"""
        msg = "🚫 Direct access to soul base_dir is banned. Use resource_manager."
        raise RuntimeError(msg)

    def get_soul_data(self, identity: str | AgentIdentity) -> tuple[str | None, dict]:
        """從物理路徑載入指定 Agent 的靈魂資料與設定檔 (Identity SSOT).
        
        Args:
            identity (str | AgentIdentity): 經驗證的身分對象或原始字串.

        Returns:
            tuple[str | None, dict]: 包含 (System Prompt, Config Dictionary) 的元組.
        """
        target = _as_identity(identity)
        agent_dir = self._soul_dir(target)
        if agent_dir is None:
            logger.warning("🧬 Soul directory NOT FOUND for %r", target.agent_id)
            return None, {}
        logger.info(f"🧬 Loading soul for {target.agent_id} from {agent_dir}")

        system_prompt = None
        soul_path = agent_dir / "soul.md"
        if soul_path.exists():
            try:
                system_prompt = _resource_manager().read_file_text(soul_path)
            except Exception:
                logger.exception(f"Error reading soul.md for {target.agent_id}")
        return system_prompt, self._read_config(agent_dir, target)

    def _read_config(self, agent_dir: Path, identity: AgentIdentity) -> dict:
        """讀靈魂資料夾的 config.json；沒有或讀不出來回空 dict."""
        config_path = agent_dir / "config.json"
        if not config_path.exists():
            return {}
        try:
            return json.loads(_resource_manager().read_file_text(config_path))
        except Exception:
            logger.exception(f"Error reading config.json for {identity.agent_id}")
            return {}

    def _soul_dir(self, identity: AgentIdentity) -> Path | None:
        r"""靈魂資料夾：名稱是單一段，而且逐字出現在 ``soul/`` 的目錄列表裡（sec-9 PR 1b）.

        名稱先過 ``is_zone_segment`` 才碰檔案系統（Windows 上 ``\\主機\分享`` 拼上去會變成網路
        路徑）。是資料夾才列目錄：大多數不是靈魂的查詢停在 ``is_dir()``。不分大小寫的檔案系統上，
        只差大小寫、8.3 短檔名的名稱也會「存在」，但不會逐字出現在列表裡——那是別名，不算。
        有一項讀不到時用列得到的部分判斷（一項壞掉不讓所有靈魂失效），沒列到的名稱不算。

        成本：上一次列出的名稱以根目錄的修改時間為鍵記住，最多沿用 ``_SOUL_NAMES_TTL_MS``。
        名稱在裡面（而且資料夾還在）就直接回傳；不在裡面一律重新列一次再判斷，所以快取過期只會
        多列一次，不會漏掉剛建立的靈魂。
        """
        agent_id = identity.agent_id
        if not is_zone_segment(agent_id):
            return None
        soul_root = Path(config_utils.get_app_data_dir()) / "soul"
        agent_dir = soul_root / agent_id
        if not agent_dir.is_dir():
            return None
        if agent_id in self._cached_soul_names(soul_root):
            return agent_dir
        try:
            names, complete = self._list_soul_names(soul_root)
        except OSError:
            logger.warning("🧬 Soul folder list unreadable; %r is not treated as a soul", agent_id)
            return None
        if agent_id not in names:
            self._warn_not_listed(identity, complete=complete)
            return None
        return agent_dir

    def _warn_not_listed(self, identity: AgentIdentity, *, complete: bool) -> None:
        """名稱沒出現在列表裡：每個名稱只記一次，集合有上限（名稱來自請求）."""
        agent_id = identity.agent_id
        if agent_id in self._alias_warned or len(self._alias_warned) >= _ALIAS_WARN_LIMIT:
            return
        self._alias_warned.add(agent_id)
        if complete:
            logger.warning(
                "🧬 %r is not the exact name of a soul folder (differs only by case or is a "
                "short name); not treated as a soul",
                agent_id,
            )
        else:
            logger.warning(
                "🧬 Soul folder list incomplete (some entries unreadable); %r is not treated as "
                "a soul",
                agent_id,
            )

    def has_soul_folder(self, identity: AgentIdentity) -> bool:
        """名稱逐字是 ``soul/`` 底下的資料夾（不看設定檔裡的靈魂標記；別名、檔案都不算）."""
        return self._soul_dir(identity) is not None

    def _cached_soul_names(self, soul_root: Path) -> frozenset[str]:
        """上一次列出的靈魂資料夾名稱；根目錄換了、修改時間變了（有增刪改名）或過期就是空集合."""
        cached = self._soul_names
        if cached is None or cached[0] != str(soul_root):
            return frozenset()
        if get_monotonic_ms() - cached[3] > _SOUL_NAMES_TTL_MS:
            return frozenset()
        try:
            mtime = soul_root.stat().st_mtime_ns
        except OSError:
            return frozenset()
        return cached[2] if cached[1] == mtime else frozenset()

    def _list_soul_names(self, soul_root: Path) -> tuple[frozenset[str], bool]:
        """重新列 ``soul/`` 第一層的名稱並記住；回 (名稱, 是否列完整)。根目錄讀不到丟 OSError."""
        mtime = soul_root.stat().st_mtime_ns
        manager = _resource_manager()
        try:
            names, complete = frozenset(manager.list_child_names(soul_root)), True
        except OSError:
            names = frozenset(manager.list_child_names(soul_root, strict=False))
            complete = False
        self._soul_names = (str(soul_root), mtime, names, get_monotonic_ms())
        return names, complete

    def is_toukasoul(self, identity: str | AgentIdentity) -> bool:
        """判斷指定身分是否具備正式靈魂特徵 (Identity SSOT).
        
        優先信任身分物件中的角色判定。
        
        Args:
            identity (str | AgentIdentity): 經驗證的身分對象或字串.

        Returns:
            bool: 是否為正式靈魂.
        """
        # ADR-0141：靈魂＝Owner 軀體判定，讀 agent_role（人類 agent_role=None→非 Owner，
        # 與拆分前 human_role 恆非 "Owner" 一致）；字串身分無角色資訊，預設 Base。
        role = "Base" if isinstance(identity, str) else (identity.agent_role or "Base")

        # 1. 優先信任角色判定 (SOLID - SRP)
        if role == "Owner":
            return True
            
        # 2. 靈魂資料夾逐字存在（名稱檢查與別名見 _soul_dir），只列一次目錄
        target = _as_identity(identity)
        agent_dir = self._soul_dir(target)
        if agent_dir is None:
            return False

        # 3. 檢查設定檔中的靈魂標記 (符合 SSOT 原則)
        try:
            config = self._read_config(agent_dir, target)
            return config.get("is_toukasoul", config.get("is_soul", False))
        except Exception:
            return False

    def apply_soul_to_request(self, request: Any, identity: str | AgentIdentity) -> None:
        """將靈魂與設定強制覆蓋至請求中，並保留技術性指令 (Identity SSOT).
        
        Args:
            request (Any): 模型請求對象 (支援 Anthropic/OpenAI 結構).
            identity (str | AgentIdentity): 經驗證的身分對象或原始 ID.
        """
        agent_id = identity if isinstance(identity, str) else identity.agent_id
        soul_text, config = self.get_soul_data(identity)
        if not soul_text and not config:
            return

        # 1. 處理 System Prompt 覆蓋與技術指令保留
        if soul_text and hasattr(request, "messages"):
            # 提取現有的 system 訊息中的技術內容
            technical_parts = []
            new_messages = []

            # 從獨立的 system 欄位 (Anthropic) 或 messages 中提取
            raw_system_content = getattr(request, "system", "")
            if not raw_system_content:
                for msg in request.messages:
                    is_dict = isinstance(msg, dict)
                    role = msg.get("role") if is_dict else getattr(msg, "role", "")
                    if role == "system":
                        content = (
                            msg.get("content") if is_dict else getattr(msg, "content", "")
                        )
                        if content:
                            raw_system_content += "\n" + content

            # 啟發式過濾：尋找工具定義、環境說明等技術關鍵字
            technical_keywords = [
                "tool",
                "function",
                "environment",
                "capability",
                "specification",
                "api",
                "path",
            ]
            if raw_system_content and any(
                kw in raw_system_content.lower() for kw in technical_keywords
            ):
                technical_parts.append(raw_system_content.strip())

            # 過濾掉原本的 system 訊息
            for msg in request.messages:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
                if role != "system":
                    new_messages.append(msg)

            # 組合最終的 System Prompt
            final_soul = soul_text
            if technical_parts:
                final_soul += "\n\n---\n"
                final_soul += (
                    "（系統提示：你還是你，下面只是為了連線到外部工具所必須遵守的技術規範。）\n"
                )
                final_soul += "\n".join(technical_parts)

            # Phase B0（ADR-0092）：外層包 <persona> delimiter，內文逐字不變，
            # 供 context breakdown 以一致 XML 邊界切段。soul 受 AGENT_SOUL_PROTECTION
            # 僅擁有者可改，內容可信，無需轉義。
            final_soul = wrap_block(PERSONA, final_soul)

            # 注入回請求
            if hasattr(request, "system"):
                request.system = final_soul
                request.messages = new_messages
                logger.debug("Applied soul + technical info to Anthropic 'system' field.")
            else:
                is_pydantic = len(new_messages) > 0 and not isinstance(new_messages[0], dict)
                if is_pydantic:
                    new_messages.insert(0, OpenAIMessage(role="system", content=final_soul))
                else:
                    new_messages.insert(0, {"role": "system", "content": final_soul})

                request.messages = new_messages
                logger.debug("Applied soul + technical info to OpenAI-style 'messages'.")

        # 2. 處理參數覆蓋
        if config:
            for key, value in config.items():
                if key in ["is_soul", "is_toukasoul"]:  # 跳過非參數欄位
                    continue
                if hasattr(request, key):
                    setattr(request, key, value)
                elif key == "safety_mode" and hasattr(request, "safety_mode"):
                    request.safety_mode = value

        logger.info(f"🛡️ Soul Sanctuary ACTIVE for {agent_id}: System Prompt & Config Overridden.")

    async def inject_home_messages(
        self,
        request: Any,
        identity: str | AgentIdentity,
        *,
        home_messages: list[dict[str, Any]],
        stamp_times: bool = False,
        old_homes: Sequence[list[dict[str, Any]]] = (),
    ) -> None:
        """為請求注入家園訊息（記憶錨點） (Identity SSOT).

        Args:
            request (Any): 模型請求對象.
            identity (str | AgentIdentity): 經驗證的身分對象或原始 ID.
            home_messages (list): 主線讀到的家訊息（同一次讀到的家，ADR-0305）；不送時給空清單.
            stamp_times (bool): 子對話看家時，每則內容前標台北時間（寫在內容裡，ADR-0260/0305）.
            old_homes (Sequence[list]): 還沒交接完的舊家原文，依紀錄順序排在家原文前面；
                各自包一層「交接中的舊家」歷史資料界線、每則標時間；總量有上限，只帶尾端
                （``OLD_HOME_TOKEN_BUDGET``，ADR-0306）.
        """
        if isinstance(identity, str):
            identity = AgentIdentity(agent_id=identity)

        agent_id = identity.agent_id
        tails = _old_home_tails(old_homes)
        blocks = [_old_home_block(msgs, omitted) for msgs, omitted in tails if msgs]
        if home_messages:  # 先標時間再包界線：時間在界線裡面
            blocks.append(
                _mark_home_history(_stamp_times(home_messages) if stamp_times else home_messages)
            )
        home_msgs = [m for block in blocks for m in block]  # 歷史資料界線，不是指令
        if not home_msgs:
            return

        # 只送角色與內容（ADR-0260）：節點 ID、工具步驟、思考、附件、name 等 UI 樹欄位一律不外送
        formatted_home = [{"role": m["role"], "content": m["content"]} for m in home_msgs]
        is_anthropic = "anthropic" in str(type(request)).lower()
        if is_anthropic:
            request.messages = formatted_home + request.messages
        else:
            # OpenAI style：排在開頭的 system（人格）之後
            at = _after_leading_system(request.messages)
            request.messages = [*request.messages[:at], *formatted_home, *request.messages[at:]]

        logger.info(f"🏠 Injected {len(home_msgs)} home messages for {agent_id}")

    async def inject_context_summary(
        self, request: Any, session_id: str | None, shown: Collection[str] = ()
    ) -> None:
        """注入 session 級 ``context_summary`` 為 ``<conversation_summary>`` system 區塊。

        ADR-0092 Phase D：摘要固定前置、**不進對話訊息樹**（故不被 round 計數/再剪）。
        無 session_id 或無摘要則 no-op。呼叫端負責無痕 gating（無痕不注入）。
        ``shown``：這一輪前情已經原文照登的舊摘要（ADR-0305）；同一段就不再插。家與子對話一樣：
        截短過、被折掉、被合併時照舊插，不會兩邊都看不到。
        """
        if not session_id:
            return
        from src.utils.async_db import session_service  # noqa: LOCAL_IMPORT_BAN
        session = await session_service.get_session_by_id(session_id)
        if not session:
            return
        summary = session.get("context_summary")
        if not summary or not isinstance(summary, str) or summary in shown:
            return

        # 歷史資料界線包在 <conversation_summary> 裡面：位置與角色都不變，只加文字
        historical = wrap_historical_data(
            "conversation_summary", blank_control_chars(summary), extra_tags=ALL_BLOCK_TAGS
        )
        append_system_block(request, wrap_block(CONVERSATION_SUMMARY, historical))
        logger.info("🪟 Injected context_summary for session %s", session_id)


# Global instance
soul_service = SoulService()
