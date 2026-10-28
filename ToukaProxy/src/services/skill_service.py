"""技能服務 (SkillService).

負責管理 Agent 的動態技能 (System Prompt 注入) 與 MCP 伺服器掛載。

技能以 Anthropic 標準 SKILL.md 目錄格式儲存 (ADR-0066)：每個技能一個目錄，
含 SKILL.md (YAML frontmatter + Markdown body) 與選填的 .mcp.json。
採三層 Progressive Disclosure：
  第 1 層 metadata：常駐 (frontmatter)，list/schema 用
  第 2 層 body：觸發時讀取 (get_skill / apply_skills_to_request)
  第 3 層 resources：按需讀取 (get_skill_resource)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from xml.sax.saxutils import escape, quoteattr

import httpx
from pydantic import ValidationError

from src.config import get_config_dir
from src.core.exceptions import ErrorCode, ToukaException
from src.services import skill_parser
from src.services.resource_manager import resource_manager
from src.services.skill_catalog_service import (
    COMMIT_SHA_RE,
    SkillCatalogEntry,
    SkillCatalogService,
)
from src.utils.time_utils import get_current_epoch

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"
_MCP_FILE = ".mcp.json"
# catalog 安裝來源 provenance（版本追蹤）：記下釘的 commit，供「最新／可更新」判定。
_SOURCE_FILE = ".touka_source.json"

# Skill 安裝來源 allowlist（防 SSRF）
ALLOWED_SKILL_DOMAINS = frozenset({
    "raw.githubusercontent.com",
    "github.com",
    "smithery.ai",
})
MAX_SKILL_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB
# ADR-0074 真技能目錄：逐檔釘 commit raw 抓取上限（runtime 只用 raw、零 api.github.com）。
MAX_CATALOG_FILES = 50
MAX_CATALOG_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB（鏡像 archive 解壓上限）
RAW_GITHUB_BASE = "https://raw.githubusercontent.com"


def _xml_server_entry(server_id: str, command: str) -> str:
    """Render one ``<server>`` registry entry with XML-escaped fields (#12 fix).

    Skill/MCP metadata is attacker-influenced (a crafted skill could embed
    ``</description>...`` to break out of the element and inject directives into the
    resident dispatcher system prompt). ``quoteattr``/``escape`` neutralise the
    structural breakout. Note metadata is reachable not only via out-of-band admin
    edits but via agent tool-calls (create/install skill) and remote catalog/market
    sources, so this only closes the *structural* XML breakout — residual plain-text
    prompt-injection in skill descriptions remains and must be weighed at install time.
    """
    return (
        f"  <server id={quoteattr(server_id)}>\n    <type>MCP Server</type>\n    "
        f"<description>已被配置於系統中的外部工具集 "
        f"({escape(str(command))})</description>\n  </server>"
    )


def _xml_skill_entry(skill_id: str, display: str, description: str) -> str:
    """Render one ``<skill>`` registry entry with XML-escaped fields (#12 fix).

    See :func:`_xml_server_entry` for the threat model.
    """
    return (
        f"  <skill id={quoteattr(skill_id)}>\n    <name>{escape(str(display))}</name>\n    "
        f"<description>{escape(str(description))}</description>\n  </skill>"
    )


def _content_version(body: str) -> str:
    """以 body 內容雜湊作為穩定版本：內容沒變→版本不變→git 不被弄髒。

    dispatcher SKILL.md 每次 sync 重生；改用內容雜湊取代 epoch 時間戳，使
    registry 未變時重生產出位元組相同的檔，消除每次 import/啟動的無意義 git diff。
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _write_if_changed(path: Path, content: str) -> bool:
    """內容和現有檔相同就不寫，回傳有沒有寫（ADR-0290）。

    調度員檔每次啟動都重算；整份重寫的檔會被 Windows 的 git 誤判成改過。讀檔用通用
    換行，只差 CRLF 的舊檔也算相同；讀不到（權限、不是 UTF-8）就照寫，壞檔才修得回來。
    """
    if resource_manager.exists(path):
        try:
            if resource_manager.read_file_text(path) == content:
                return False
        except (OSError, UnicodeDecodeError):
            logger.warning("Dispatcher file %s is unreadable; rewriting it.", path)
    resource_manager.write_file_text(path, content, make_parents=True)
    return True


def _render_dispatcher_body(skill_list_xml: str) -> str:
    """技能調度員 system prompt 本體（SSOT；靜態 sync 與 per-request 過濾共用）。"""
    return f"""你具備「依需求動態啟用技能」的能力。
為了節省 Context 資源，系統僅提供技能清單，未預先載入每個技能的完整指令。

可用技能清單：
<skill_registry>
{skill_list_xml}
</skill_registry>

當你判斷某個技能有助於當前任務時，調用 `activate_skill(skill_id="...")` 來啟用它。
啟用後，該技能的完整指令將在你的「下一輪對話」注入並生效。
請僅依上述清單啟用，勿臆測不存在的技能。"""


class SkillService:
    """管理動態技能與 MCP 調度 (SOLID - SRP)。

    技能 SSOT 為 ``config/skills/`` 目錄。對外完全相容 Anthropic Agent Skills
    標準；Touka 原生需求 (is_system / author / category / display-name) 以擴充
    frontmatter 欄位實現，標準工具忽略未知欄位，不破壞相容。
    """

    _instance = None

    def __new__(cls) -> SkillService:
        """實作單例模式。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False  # noqa: SLF001
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        # 技能目錄 SSOT。config_path 保留為契約字串屬性，供 context_snapshot 標記來源。
        self._skills_dir = get_config_dir() / "skills"
        self.config_path = str(self._skills_dir)
        self._metadata: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, dict[str, Any]] = {}
        self.load_config()
        self._initialized = True

    def reset_for_test(self) -> None:
        """重置測試狀態，重新解析目錄並載入。"""
        # 測試沙盒可能切換了 config 目錄，需重新解析路徑 (SSOT)。
        self._skills_dir = get_config_dir() / "skills"
        self.config_path = str(self._skills_dir)
        self._metadata = {}
        self._sources = {}
        self.load_config()
        logger.info("🛡️ SkillService test state reset.")

    # --- 第 1 層 Metadata (常駐) ---

    def load_config(self) -> None:
        """掃描技能目錄，載入第 1 層 metadata (frontmatter)。

        缺目錄或空目錄回空 metadata，不拋例外 (與沙盒/首次啟動相容)。
        """
        self._metadata = {}
        self._sources = {}
        if not resource_manager.exists(self._skills_dir):
            logger.info("Skill directory not found at %s. Starting empty.", self._skills_dir)
            return

        for sid in self._scan_skill_ids():
            md_path = self._skills_dir / sid / _SKILL_FILE
            try:
                raw = resource_manager.read_file_text(md_path)
            except OSError:
                logger.warning("Failed to read %s, skipping.", md_path)
                continue
            fm, _body = skill_parser.parse_skill_md(raw)
            self._metadata[sid] = fm
            src = self._read_skill_source(sid)
            if src is not None:
                self._sources[sid] = src

        logger.info("Loaded %d skills from %s", len(self._metadata), self._skills_dir)

    def _scan_skill_ids(self) -> list[str]:
        """掃描技能目錄，回傳含 SKILL.md 的直接子目錄 id 清單。

        glob_search 採 rglob，故過濾僅保留 ``<id>/SKILL.md`` (兩段路徑)，
        排除 ``<id>/references/SKILL.md`` 等巢狀雜訊。
        """
        ids = []
        for rel in resource_manager.glob_search(self._skills_dir, _SKILL_FILE):
            parts = rel.split("/")
            if len(parts) == 2 and parts[1] == _SKILL_FILE:
                ids.append(parts[0])
        return ids

    def _read_skill_source(self, skill_id: str) -> dict[str, Any] | None:
        """讀取技能安裝來源 provenance（catalog 安裝寫入的 ``.touka_source.json``）。

        非 catalog 安裝（自建 / URL）無此檔，回 None（不誤報來源）。
        """
        path = self._skills_dir / skill_id / _SOURCE_FILE
        if not resource_manager.exists(path):
            return None
        data = resource_manager.read_json(path)
        return data if isinstance(data, dict) else None

    def list_skills(self) -> list[dict[str, Any]]:
        """列出所有技能的 metadata (第 1 層，不含 system_prompt body)。

        含 catalog 安裝來源（``source_catalog_id`` / ``source_commit``）供前端
        判定「最新／可更新」（版本追蹤；無來源者為 None）。
        """
        return [
            {
                "id": sid,
                "name": skill_parser.read_touka_field(fm, "display-name") or fm.get("name", sid),
                "description": fm.get("description", ""),
                "category": skill_parser.read_touka_field(fm, "category", "General"),
                "is_system": bool(skill_parser.read_touka_field(fm, "is_system", False)),
                "source_catalog_id": (self._sources.get(sid) or {}).get("catalog_id"),
                "source_commit": (self._sources.get(sid) or {}).get("commit"),
            }
            for sid, fm in self._metadata.items()
        ]

    def get_all_skills_schema(self) -> list[dict[str, Any]]:
        """獲取所有技能的精簡 Schema，用於 UI 展示與工具選擇 (第 1 層)。"""
        return [
            {
                "id": sid,
                "name": skill_parser.read_touka_field(fm, "display-name") or fm.get("name", sid),
                "description": fm.get("description", ""),
                "category": skill_parser.read_touka_field(fm, "category", "General"),
                "is_system": bool(skill_parser.read_touka_field(fm, "is_system", False)),
                "capability": skill_parser.read_touka_field(fm, "capability"),
            }
            for sid, fm in self._metadata.items()
        ]

    # --- 第 2 層 Body (觸發時) ---

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        """讀取單一技能完整內容 (第 2 層)，回傳契約 9 key dict。

        不存在時回 None，呼叫端 (router) 據此回 404。
        """
        md_path = self._skills_dir / skill_id / _SKILL_FILE
        if not resource_manager.exists(md_path):
            return None
        try:
            raw = resource_manager.read_file_text(md_path)
        except OSError:
            logger.warning("Failed to read skill %s", skill_id)
            return None
        fm, body = skill_parser.parse_skill_md(raw)
        mcp_servers = self._derive_mcp_servers(skill_id)
        return skill_parser.map_to_contract(fm, body, mcp_servers)

    def _derive_mcp_servers(self, skill_id: str) -> list[str]:
        """從同目錄 .mcp.json 的 top-level keys 推導 mcp_servers 清單。

        先檢查檔案存在再讀取，避免對無 .mcp.json 的技能 (如系統 dispatcher)
        觸發非必要的 read_json。只取 keys、不碰 value，故 value 內的
        ``${DISPATCH_URL}`` 等插值佔位不影響 JSON 解析 (推導與插值解耦)。
        """
        mcp_path = self._skills_dir / skill_id / _MCP_FILE
        if not resource_manager.exists(mcp_path):
            return []
        data = resource_manager.read_json(mcp_path)
        if isinstance(data, dict):
            return list(data.keys())
        return []

    # --- 第 3 層 Resources (按需) ---

    def get_skill_resource(self, skill_id: str, rel_path: str) -> str | None:
        """讀取技能目錄下的 bundled resource (第 3 層，按需)。

        rel_path 正規化後必須仍在 ``<skill_id>/`` 目錄內，防止 ``../`` 目錄逃逸。
        逃逸或檔案不存在回 None。
        """
        skill_root = (self._skills_dir / skill_id).resolve()
        target = (skill_root / rel_path).resolve()
        if target != skill_root and skill_root not in target.parents:
            logger.warning("Skill resource path escape blocked: %s / %s", skill_id, rel_path)
            return None
        if not resource_manager.exists(target):
            return None
        try:
            return resource_manager.read_file_text(target)
        except OSError:
            return None

    def list_skill_resources(self, skill_id: str) -> list[str]:
        """列舉技能目錄下 scripts/ references/ assets/ 的檔案 (第 3 層)。"""
        skill_dir = self._skills_dir / skill_id
        resources = []
        for sub in ("scripts", "references", "assets"):
            sub_dir = skill_dir / sub
            if resource_manager.exists(sub_dir):
                resources.extend(
                    f"{sub}/{rel}" for rel in resource_manager.glob_search(sub_dir, "*")
                )
        return resources

    # --- 寫入 (SSOT = 目錄) ---

    def update_skill(self, skill_id: str, skill_data: dict[str, Any]) -> None:
        """新增或更新技能，寫入 SKILL.md (+ 選填 .mcp.json)。"""
        display_name = (
            skill_data.get("display-name") or skill_data.get("name") or skill_id
        )
        frontmatter = skill_parser.build_frontmatter(
            name=skill_id,
            description=skill_data.get("description", ""),
            display_name=display_name,
            category=skill_data.get("category", "General"),
            author=skill_data.get("author", ""),
            is_system=bool(skill_data.get("is_system", False)),
            allowed_tools=skill_data.get("required_tools") or None,
        )

        body = skill_data.get("system_prompt", "")
        content = skill_parser.serialize_skill_md(frontmatter, body)
        md_path = self._skills_dir / skill_id / _SKILL_FILE
        resource_manager.write_file_text(md_path, content, make_parents=True)

        # 選填：從 mcp_servers 清單反向產生最小 .mcp.json 設定。
        mcp_servers = skill_data.get("mcp_servers") or []
        if mcp_servers:
            mcp_config = {name: {} for name in mcp_servers}
            mcp_path = self._skills_dir / skill_id / _MCP_FILE
            resource_manager.write_json(mcp_path, mcp_config, indent=2)

        # reload 第 1 層 metadata (與 load 解析結果等價，round-trip 已驗證)。
        self._metadata[skill_id] = frontmatter

        # 技能清單變動 → 重新同步技能調度員 (保持清單最新)。
        if skill_id != "system-skill-dispatcher":
            self.sync_skill_dispatcher()

    def delete_skill(self, skill_id: str) -> None:
        """刪除技能目錄。系統技能 (is_system) 受保護不可刪除。"""
        fm = self._metadata.get(skill_id, {})
        # 掃描缺口 E35（ADR-0175）：is_system 須走 read_touka_field——生產寫入
        # （build_frontmatter）把擴充欄位收進 metadata 子欄位，直讀頂層鍵只命中
        # legacy 檔＝新制系統技能曾可被刪。
        if skill_parser.read_touka_field(fm, "is_system"):
            logger.warning("Refused to delete system skill '%s'.", skill_id)
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)
        skill_dir = self._skills_dir / skill_id
        resource_manager.remove_dir(skill_dir, missing_ok=True)
        self._metadata.pop(skill_id, None)

        # 技能清單變動 → 重新同步技能調度員。
        self.sync_skill_dispatcher()

    # --- 注入 (邏輯不變，僅資料來源改為目錄) ---

    def render_skill_registry(self, skill_ids: list[str]) -> str:
        """生成技能調度員 system prompt，``<skill_registry>`` 只列給定的 skill_ids.

        ADR-0148（技能廣告 RBAC 過濾）：SkillLayer 依 ``can_use_skill`` 算出該身分
        「用得到」的技能集合後呼叫本方法——**廣告面（看得到）自此與授權面（用得到）
        同判準**，杜絕「看得到用不了」（無權技能仍被廣告→模型 activate→批准後撞
        RBAC，審批被白白浪費）。無任何可列項 → 回空字串（呼叫端據此不注入 dispatcher，
        連帶不廣告 activate_skill）。系統技能不列。
        """
        entries = []
        for sid in skill_ids:
            fm = self._metadata.get(sid)
            if not fm or skill_parser.read_touka_field(fm, "is_system"):
                continue
            display = skill_parser.read_touka_field(fm, "display-name") or fm.get("name", sid)
            entries.append(_xml_skill_entry(sid, display, fm.get("description", "")))
        if not entries:
            return ""
        return _render_dispatcher_body("\n".join(entries))

    def append_system_prompt(self, request: Any, text: str) -> None:
        """把一段文字附加到請求的 system prompt（Anthropic system / OpenAI messages）。"""
        if not text:
            return
        if hasattr(request, "system"):
            # Anthropic 格式
            request.system = (request.system or "") + text
        elif hasattr(request, "messages"):
            # OpenAI 格式，找第一個 system 訊息
            for msg in request.messages:
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
                if role == "system":
                    content = (
                        msg.get("content")
                        if isinstance(msg, dict)
                        else getattr(msg, "content", "")
                    )
                    new_content = (content or "") + text
                    if isinstance(msg, dict):
                        msg["content"] = new_content
                    else:
                        msg.content = new_content
                    break
            else:
                # 若沒找到 system，新增一個
                if isinstance(request.messages, list):
                    request.messages.insert(0, {"role": "system", "content": text})

    def apply_skills_to_request(
        self, request: Any, skill_ids: list[str], dispatcher_body: str | None = None
    ) -> None:
        """將選定的技能注入到請求的 System Prompt 中。

        ADR-0148：``dispatcher_body``（SkillLayer 依 can_use_skill 過濾後的
        skill_registry 提示）以 ``<skill name="system-skill-dispatcher">`` 併入
        **同一個** ``<capabilities>`` 區塊——與舊制（dispatcher 走 apply_skills 被包成
        一個 skill）結構一致，維持 context_breakdown 的 tag-SSOT 歸因（不落 system_other）。
        """
        formatted_prompts = []
        for sid in skill_ids:
            skill = self.get_skill(sid)
            if skill and skill.get("system_prompt"):
                name = skill.get("name", sid)
                prompt = skill["system_prompt"]
                # #12 fix: the skill name is attacker-influenced (crafted frontmatter)
                # and enters an XML attribute injected into request.system on EVERY
                # request — quoteattr neutralises attribute breakout. The {prompt} body
                # is the skill's own natural-language instruction text, left as-is.
                formatted_prompts.append(
                    f"<skill name={quoteattr(str(name))}>\n{prompt}\n</skill>"
                )

        if dispatcher_body:
            formatted_prompts.append(
                f'<skill name={quoteattr("system-skill-dispatcher")}>\n'
                f"{dispatcher_body}\n</skill>"
            )

        if not formatted_prompts:
            return

        combined_skill_prompt = (
            "\n\n<capabilities>\n" + "\n".join(formatted_prompts) + "\n</capabilities>"
        )
        self.append_system_prompt(request, combined_skill_prompt)
        logger.info(f"Injected {len(formatted_prompts)} skills into request.")

    # --- 系統調度員同步 (SSOT Sync) ---

    def sync_system_dispatcher(self) -> None:
        """根據 MCP 設定動態產出調度員技能，寫入 SKILL.md (SSOT Sync)。"""
        try:
            config_path = get_config_dir() / "server_config.json"
            data = resource_manager.read_json(config_path)
            if not data:
                logger.warning(
                    "MCP config not found at %s, skipping dispatcher sync.", config_path
                )
                return

            mcp_servers = data.get("mcpServers", {})

            mcp_items = []
            for name, cfg in mcp_servers.items():
                if not cfg.get("enabled", True):
                    continue
                cmd = cfg.get("command", "unknown")
                mcp_items.append(_xml_server_entry(name, cmd))

            mcp_list_xml = "\n".join(mcp_items) if mcp_items else "  (目前無可用伺服器)"

            system_prompt = f"""你擁有「動態過渡式」調用外部工具的能力。
為了節省 Context 資源，系統並未載入所有工具。
    `system-mcp-dispatcher` 是一個特殊的系統技能，負責管理 MCP 伺服器的發現與動態掛載。

    1. **發現能力**：如果您在清單中找不到工具，請調用 `search_skill_market` 搜尋。
    2. **發現掛載**：
    <mcp_registry>
    {mcp_list_xml}
    </mcp_registry>

    3. **執行掛載**：若確認需要某個伺服器，請調用 `connect_mcp_server(server_id="...")`。
    4. **生效機制**：掛載操作將在您的「下一輪對話」生效，請通知使用者並等待。

請勿嘗試猜測路徑，僅基於上述註冊表進行連線。"""

            frontmatter = skill_parser.build_frontmatter(
                name="system-mcp-dispatcher",
                description="系統調度器。可隨時『召喚』外部 MCP 伺服器工具。",
                display_name="🛡️ MCP Dynamic Dispatcher",
                category="System",
                author="Touka System",
                is_system=True,
                version=_content_version(system_prompt),
            )
            content = skill_parser.serialize_skill_md(frontmatter, system_prompt)
            md_path = self._skills_dir / "system-mcp-dispatcher" / _SKILL_FILE
            written = _write_if_changed(md_path, content)
            self._metadata["system-mcp-dispatcher"] = frontmatter
            if written:
                logger.info("✅ System MCP Dispatcher skill synchronized with SSOT.")
        except Exception:
            logger.exception("Failed to sync system dispatcher")

    def sync_skill_dispatcher(self) -> None:
        """根據已載入技能動態產出技能調度員，列出可啟用技能清單 (SSOT Sync)。

        仿 sync_system_dispatcher：讓 Agent 看到可用 skill 清單 (Progressive
        Disclosure 第 1 層)，再透過 activate_skill 工具按需展開 (Pull 模式)。
        只列非系統技能，故排除自己與 system-mcp-dispatcher，不遞迴自我列舉。
        """
        try:
            skill_items = []
            for sid, fm in self._metadata.items():
                if skill_parser.read_touka_field(fm, "is_system"):
                    continue
                display = skill_parser.read_touka_field(fm, "display-name") or fm.get("name", sid)
                desc = fm.get("description", "")
                skill_items.append(_xml_skill_entry(sid, display, desc))

            skill_list_xml = "\n".join(skill_items) if skill_items else "  (目前無可啟用的技能)"

            system_prompt = _render_dispatcher_body(skill_list_xml)

            frontmatter = skill_parser.build_frontmatter(
                name="system-skill-dispatcher",
                description="技能調度器。列出可用技能，供 Agent 依需求動態啟用。",
                display_name="🧩 Skill Dynamic Dispatcher",
                category="System",
                author="Touka System",
                is_system=True,
                version=_content_version(system_prompt),
            )
            content = skill_parser.serialize_skill_md(frontmatter, system_prompt)
            md_path = self._skills_dir / "system-skill-dispatcher" / _SKILL_FILE
            written = _write_if_changed(md_path, content)
            self._metadata["system-skill-dispatcher"] = frontmatter
            if written:
                logger.info("✅ System Skill Dispatcher synchronized with SSOT.")
        except Exception:
            logger.exception("Failed to sync skill dispatcher")

    # --- 打包 / 安裝 (Phase 4) ---

    def package_skill(self, skill_id: str) -> Path:
        """打包 config/skills/<id>/ 為 .skill (zip)，回傳暫存檔路徑。

        zip 內含 ``<id>/`` 前綴 (相容 Anthropic)。不存在 → RESOURCE_NOT_FOUND。
        暫存於 temp/skill_exports (非 protected)，由呼叫端負責清理。
        """
        skill_dir = self._skills_dir / skill_id
        if not resource_manager.exists(skill_dir):
            raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND, resource_type="Skill")
        export_dir = Path(resource_manager.get_temp_path("skill_exports"))
        resource_manager.make_dir(export_dir)
        out = export_dir / f"{skill_id}.skill"
        return resource_manager.create_archive(skill_dir, out, arc_base=self._skills_dir)

    async def install_skill_from_url(self, url: str) -> str:
        """從受信任 URL 下載 .skill 安裝，回傳 skill_id。

        安全：domain allowlist (SSRF) + follow_redirects=False + 下載大小上限
        + extract_archive (zip bomb/path traversal)；解壓到臨時、驗證通過才上線。
        覆寫既有 id 用 .bak 原子保證；finally 清臨時 (回滾)。
        """
        if urlparse(url).netloc not in ALLOWED_SKILL_DOMAINS:
            logger.warning("Skill install blocked: domain not in allowlist (%s).", url)
            raise ToukaException(ErrorCode.AUTH_PERMISSION_DENIED)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content

        if len(content) > MAX_SKILL_DOWNLOAD_BYTES:
            raise ToukaException(ErrorCode.REQUEST_TOO_LARGE)

        work = Path(resource_manager.get_temp_path("skill_import")) / uuid4().hex
        tmp_zip = work / "pkg.skill"
        staging = work / "staging"
        try:
            resource_manager.write_file_binary(tmp_zip, content)
            resource_manager.extract_archive(tmp_zip, staging)

            roots = {
                rel.split("/")[0]
                for rel in resource_manager.glob_search(staging, _SKILL_FILE)
                if len(rel.split("/")) == 2
            }
            if len(roots) != 1:
                raise ToukaException(ErrorCode.VALIDATION_ERROR)
            skill_id = roots.pop()
            self._validate_skill_dir(staging / skill_id)
            return self._atomic_install_from_staging(skill_id, staging / skill_id)
        finally:
            resource_manager.remove_dir(work, missing_ok=True)

    def _validate_skill_dir(self, skill_dir: Path) -> str:
        """寬鬆驗證 (Touka 自家)：SKILL.md 存在 + frontmatter 可 parse + 有 name。

        刻意不套 Anthropic 嚴格 quick_validate，以允許 Touka 擴充欄位 (metadata)。
        """
        md = skill_dir / _SKILL_FILE
        if not resource_manager.exists(md):
            raise ToukaException(ErrorCode.VALIDATION_ERROR)
        fm, _body = skill_parser.parse_skill_md(resource_manager.read_file_text(md))
        name = fm.get("name")
        if not name or not isinstance(name, str):
            raise ToukaException(ErrorCode.VALIDATION_ERROR)
        return name

    def _atomic_install_from_staging(self, skill_id: str, staged_skill_dir: Path) -> str:
        """把已驗證的 staging skill 目錄原子上線至 config/skills/<id>/。

        覆寫既有用 ``.bak`` 備份保證 + 失敗回滾；兩個安裝器（URL / catalog）共用（DRY）。
        """
        dest = self._skills_dir / skill_id
        backup = self._skills_dir / f"{skill_id}.bak"
        if resource_manager.exists(dest):
            resource_manager.remove_dir(backup, missing_ok=True)
            resource_manager.move_file(dest, backup)
        try:
            resource_manager.move_file(staged_skill_dir, dest)
        except Exception:
            if resource_manager.exists(backup):
                resource_manager.move_file(backup, dest)
            raise
        resource_manager.remove_dir(backup, missing_ok=True)
        self.load_config()
        self.sync_skill_dispatcher()
        return skill_id

    async def install_skill_from_catalog(self, catalog_id: str) -> str:
        """從策展目錄安裝真技能：逐檔釘 commit 從 raw.githubusercontent.com 抓取（ADR-0074）。

        安全：只接受 catalog 內已審 id（使用者不自填 URL）；釘 40-hex commit（不可變）；
        runtime 只 raw（零 api.github.com）；no-redirect + 單檔/總量上限 + 逐檔路徑穿越；
        驗證通過才以 .bak 原子上線；finally 清臨時（回滾）。
        """
        catalog = SkillCatalogService(catalog_path=str(get_config_dir() / "skill_catalog.json"))
        raw_entry = next((e for e in catalog.get_catalog() if e.get("id") == catalog_id), None)
        if raw_entry is None:
            raise ToukaException(ErrorCode.RESOURCE_NOT_FOUND, resource_type="Skill")
        try:
            entry = SkillCatalogEntry.model_validate(raw_entry)
        except ValidationError as exc:
            raise ToukaException(ErrorCode.VALIDATION_ERROR) from exc
        if not COMMIT_SHA_RE.fullmatch(entry.commit):
            raise ToukaException(ErrorCode.VALIDATION_ERROR)
        if len(entry.files) > MAX_CATALOG_FILES:
            raise ToukaException(ErrorCode.REQUEST_TOO_LARGE)

        work = Path(resource_manager.get_temp_path("skill_import")) / uuid4().hex
        skill_root = (work / catalog_id).resolve()
        try:
            total = 0
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                for f in entry.files:
                    url = (
                        f"{RAW_GITHUB_BASE}/{entry.repo}/{entry.commit}"
                        f"/{entry.base_path}/{f.path}"
                    )
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.content
                    total += len(data)
                    if len(data) > MAX_SKILL_DOWNLOAD_BYTES or total > MAX_CATALOG_TOTAL_BYTES:
                        raise ToukaException(ErrorCode.REQUEST_TOO_LARGE)
                    target = (skill_root / f.path).resolve()
                    if target != skill_root and skill_root not in target.parents:
                        raise ToukaException(ErrorCode.VALIDATION_ERROR)
                    # 內容完整性：逐檔 sha256 比對（防竄改/MITM/損毀）
                    if hashlib.sha256(data).hexdigest() != f.sha256:
                        logger.warning(
                            "Skill catalog sha256 mismatch: %s / %s", catalog_id, f.path
                        )
                        raise ToukaException(ErrorCode.VALIDATION_ERROR)
                    resource_manager.write_file_binary(target, data)

            # 版本追蹤：寫入安裝來源 provenance（隨原子上線；不污染上游 SKILL.md）。
            resource_manager.write_json(
                skill_root / _SOURCE_FILE,
                {
                    "catalog_id": entry.id,
                    "commit": entry.commit,
                    "repo": entry.repo,
                    "installed_at": int(get_current_epoch()),
                },
                indent=2,
            )

            self._validate_skill_dir(skill_root)
            return self._atomic_install_from_staging(catalog_id, skill_root)
        finally:
            resource_manager.remove_dir(work, missing_ok=True)


skill_service = SkillService()
# 兩個調度員檔由 main.py 的 lifespan 產生（設定自癒與 MCP 名稱遷移之後），載入時不寫（ADR-0290）
